# RAG no Gen AI Playground: runbooks de SRE + servidor MCP

Base de conhecimento de exemplo para testar **RAG** no Playground do OpenShift AI 3.5,
junto com o servidor MCP `OCP-Cluster-MCP` deste repo. A ideia da demo:

- **MCP** responde *como o cluster está agora* (pods, nós, operadores, capacidade).
- **RAG** responde *o que a casa manda fazer* (severidade, janela de mudança, limites,
  quem acionar), coisas que o modelo não tem como saber sozinho.

```
 Pergunta ─> Playground ─> OGX ─┬─> knowledge_search ─> pgvector do Playground (runbooks)
                                └─> MCP OCP-Cluster-MCP ─> API do cluster (somente leitura)
```

Os runbooks são **fictícios** (times, canais e limites inventados), escritos para que a
resposta certa só exista nos documentos. Assim fica claro quando o modelo usou o RAG.

| Arquivo | Conteúdo |
|---|---|
| `runbooks/RB-00-politicas-de-operacao.md` | Severidades SEV1 a SEV4, janela de mudança, congelamento, contatos |
| `runbooks/RB-01-pod-crashloopbackoff.md` | Pod em CrashLoopBackOff, OOMKilled, probes |
| `runbooks/RB-02-clusteroperator-degradado.md` | ClusterOperator degradado |
| `runbooks/RB-03-no-notready-e-capacidade.md` | Nó NotReady e limites de capacidade (CPU, memória, GPU) |
| `runbooks/RB-04-storage-e-pvc.md` | PVC cheio, pendente ou com erro de montagem |
| `runbooks/RB-05-modelos-e-openshift-ai.md` | InferenceService indisponível, tool calling, MaaS |
| `pdf/` | Os mesmos runbooks em PDF, para upload no Playground |

## 1. Subir os runbooks no Playground

O upload do Playground aceita **PDF, DOC ou CSV** (até 10 arquivos, 10 MB cada), por isso
use os arquivos de `pdf/`. Os embeddings ficam no PostgreSQL com pgvector que o OpenShift AI
cria para o Playground (recursos `genai-pgvector-*`, PVC de 5 GiB) e sobrevivem a reinícios
de pod; apagar o Playground apaga a base.

1. **Gen AI studio > Playground**, no projeto do seu Playground.
2. No painel de configurações, aba **Knowledge** (ou **RAG**) > **Upload files** > **Upload**.
3. Arraste os 6 PDFs de `rag/pdf/`. Sugestão de chunking para estes documentos curtos:
   **Maximum chunk length** 250 palavras, **Chunk overlap** 50, **Delimiter** em branco.
4. Espere a notificação **Source uploaded**; os arquivos aparecem em **Uploaded files**.
5. Na aba **MCP**, deixe **OCP-Cluster-MCP** marcado.
6. Na aba **Prompt**, cole o prompt da seção 2.

Documentação: [Testing your model with RAG](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/experimenting_with_models_in_the_gen_ai_playground/testing-your-model-with-rag_rhoai-user).
O RAG no Playground e o pgvector automático estão em **Technology Preview**.

Para regenerar os PDFs depois de editar um `.md`: `./rag/build_pdfs.sh` (precisa de
`pandoc` e `libreoffice`).

## 2. Prompt de sistema (RAG + MCP)

```
Você é uma SRE sênior deste cluster OpenShift. Responda sempre em português do Brasil, de forma direta.

Você tem duas fontes e deve usar as duas:
- Ferramentas do servidor OCP-Cluster-MCP: o estado ATUAL do cluster (pods, nós, operadores, eventos, capacidade, modelos). Nunca invente nomes, status ou números.
- A ferramenta knowledge_search: os runbooks da casa (RB-00 a RB-05) com severidades, janela de mudança, limites, procedimentos e contatos. Você DEVE usar knowledge_search sempre que a pergunta envolver severidade, quem acionar, quando pode mudar, limites ou "o que fazer".

Regras:
- Seu acesso é somente leitura. Nunca diga que alterou algo. Para correções, dê o comando oc para um humano executar, como está no runbook.
- Cite o código do runbook que embasou cada recomendação, por exemplo (RB-03).
- Se os runbooks não cobrirem o caso, diga isso e dê sua recomendação marcada como "fora dos runbooks".

Ao investigar um problema, responda neste formato:
- Resumo: 1 ou 2 frases, com a severidade segundo o RB-00.
- Evidências: o que as ferramentas MCP mostraram (nome, namespace, dado).
- O que o runbook manda: passos e regras do runbook, com o código.
- Próximos passos: ações em ordem, com o comando oc e quem acionar.
```

Para modelos pequenos (Llama 3.2 3B), uma versão curta:

```
Você é uma SRE do cluster OpenShift. Responda em português do Brasil.
Use as ferramentas do OCP-Cluster-MCP para ver o estado do cluster e nunca invente dados.
Você DEVE usar a ferramenta knowledge_search para regras, severidades, limites e procedimentos, e citar o runbook (RB-00 a RB-05).
Seu acesso é somente leitura. Para correções, sugira o comando oc para um humano executar.
```

## 3. Perguntas para a demo

Só RAG (o modelo não tem como saber sem os documentos):

- "Qual é a janela de mudança do cluster e quando é o congelamento de fim de ano?"
- "Um worker ficou NotReady. Qual a severidade e quem eu aciono?"
- "A partir de quantos por cento de CPU reservada precisamos pedir um nó novo?"
- "Posso apagar um PVC para liberar espaço?"

RAG + MCP (o modelo consulta o cluster e cruza com o runbook):

- "Tem algum pod com problema no cluster? Para cada um, diga a severidade e o que o runbook manda fazer."
- "Veja a capacidade dos workers. Estamos acima de algum limite do RB-03?"
- "Há ClusterOperator degradado? Se sim, qual a severidade e o que não devo fazer?"
- "Quero subir um modelo de 13B na GPU. O cluster tem GPU livre e o que a política diz?"
- "Algum PVC do projeto ai-fim passou do limite do RB-04?"

Se o modelo não buscar nos runbooks, reforce na pergunta: "consulte os runbooks".

## 4. Mesmo fluxo pelo cliente OGX (opcional)

Os PDFs do Playground ficam no pgvector daquele Playground. Para usar RAG pelo cliente
Python, crie um vector store próprio no OGX com os `.md` e passe o ID para o
`ask_cluster.py`, que combina a ferramenta `file_search` com o MCP:

```bash
pip install -r ogx-client/requirements.txt
export OGX_URL=http://<servico-ogx>.<projeto>.svc.cluster.local:8321
export MAAS_TOKEN=<token>     # só se o modelo vier do Models as a Service

python ogx-client/ingest_runbooks.py --list-models   # escolha o modelo de embedding
python ogx-client/ingest_runbooks.py                 # cria "ocp-runbooks" e imprime o vs_...
python ogx-client/ask_cluster.py -v --vector-store vs_... \
  "Tem pod com problema? Qual a severidade e o que o runbook manda fazer?"
```

Opções úteis do `ingest_runbooks.py`: `--embedding-model`, `--embedding-dimension`,
`--provider-id` (por exemplo `pgvector`) e `--chunk-tokens` / `--chunk-overlap`.

Para o Playground enxergar um vector store criado fora dele, é preciso o recurso
**external vector stores** (ConfigMap `gen-ai-aa-vector-stores` e a flag
`externalVectorStores` no `OdhDashboardConfig`), também em Technology Preview, e só com
bancos remotos (pgvector, Qdrant ou Milvus). Para a demo, o upload da seção 1 basta.
