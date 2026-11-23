# ocp-ai-lab

App de laboratório para **Red Hat OpenShift AI 3.5** com **OGX** (o antigo Llama Stack):
um servidor **MCP** com ferramentas **somente leitura** sobre o cluster OpenShift em
execução, pronto para ser testado no **Gen AI Playground** e por um cliente OGX em Python.

```
 Playground do OpenShift AI ─┐
                             ├──> OGX (Responses API) ──> modelo (vLLM, com tool calling)
 ogx-client/ask_cluster.py ──┘            │
                                          └──> MCP ocp-cluster (este repo) ──> API do cluster (get/list)
```

## O que tem aqui

| Pasta | Conteúdo |
|---|---|
| `mcp-server/` | Servidor MCP em Python (Streamable HTTP em `:8000/mcp`, health em `/healthz`), Containerfile e testes |
| `deploy/mcp-server/` | Manifestos Kustomize: Namespace, ServiceAccount, RBAC somente leitura, BuildConfig, ImageStream, Deployment e Service |
| `deploy/playground/` | ConfigMap `gen-ai-aa-mcp-servers` que registra o servidor no Playground |
| `deploy/ogx/` | Exemplo de `OGXServer` (só se você ainda não tiver um) |
| `ogx-client/` | Cliente Python que pergunta ao OGX e deixa o modelo usar as ferramentas MCP |

## Ferramentas MCP (14, todas somente leitura)

| Ferramenta | O que responde |
|---|---|
| `get_cluster_version` | Versão do OpenShift, canal, atualizações disponíveis |
| `check_cluster_updates` | Atualizações do OpenShift: versões disponíveis, a mais nova do mesmo minor, atualizações com riscos, bloqueio de upgrade minor e atualização em andamento |
| `check_operator_updates` | Operadores (OLM) com atualização pendente e se esperam aprovação manual |
| `list_cluster_operators` | Estado dos ClusterOperators (opção: só os com problema) |
| `list_nodes` | Nós, papéis, prontidão, CPU, memória, GPU, tipo e família da instância |
| `get_node_resources` | Capacidade de cada nó (CPU, memória, família, zona), quanto está reservado pelos pods e quanto está em uso; totais por família de instância |
| `list_namespaces` | Projetos, incluindo se são projetos de ciência de dados |
| `get_project_capacity` | Consumo por projeto: uso real de CPU e memória, requests e limits, ResourceQuotas e disco (PVCs: tamanho e uso real) |
| `list_pods` | Pods, prontidão, reinícios e motivos de falha (opção: só os com problema) |
| `get_pod_logs` | Últimas linhas de log de um pod (máximo 500) |
| `list_deployments` | Réplicas desejadas e prontas, imagens |
| `list_events` | Eventos recentes (por padrão só `Warning`) |
| `list_routes` | URLs externas e serviço de destino |
| `list_inference_services` | Modelos servidos pelo OpenShift AI (KServe) |

**Por que é seguro:** o ServiceAccount só tem `get/list/watch` (veja
`deploy/mcp-server/clusterrole.yaml`) e **não tem acesso a Secrets**. O servidor não
tem autenticação, por isso não criamos Route: ele só é acessível de dentro do cluster.
Atenção: logs de pods podem conter dados sensíveis; remova `pods/log` da ClusterRole se
não quiser expor isso.

**De onde vêm os números de capacidade:**
- Uso de CPU e memória: API `metrics.k8s.io` (o mesmo de `oc adm top`).
- Uso real de disco dos PVCs: Prometheus do cluster (`kubelet_volume_stats_*`), via
  Thanos Querier. Para isso o ServiceAccount recebe o papel `cluster-monitoring-view`
  (somente leitura, em `clusterrolebinding-monitoring.yaml`). Sem ele, a ferramenta
  mostra só o tamanho solicitado dos PVCs.

## Pré-requisitos

- OpenShift 4.19+ com OpenShift AI 3.5, `oc` logado como **cluster-admin** (para a ClusterRole e o ConfigMap do Playground).
- Gen AI Studio ativo (`spec.dashboardConfig.genAiStudio: true`) e operador OGX `Managed` no DataScienceCluster.
- Um modelo servido com **suporte a tool calling** (no vLLM: `--enable-auto-tool-choice` e `--tool-call-parser=...`). Sem isso o modelo não chama as ferramentas.

## 1. Subir o servidor MCP

```bash
oc apply -k deploy/mcp-server/
oc start-build ocp-cluster-mcp -n ocp-ai-lab --follow   # o BuildConfig também dispara sozinho na criação
oc rollout status deploy/ocp-cluster-mcp -n ocp-ai-lab
```

Para usar outro namespace, troque `ocp-ai-lab` em `deploy/mcp-server/kustomization.yaml`,
`namespace.yaml`, `clusterrolebinding.yaml` e `clusterrolebinding-monitoring.yaml`, e na URL do passo 2.

Para atualizar um servidor já implantado depois de mudanças no código:

```bash
oc apply -k deploy/mcp-server/                      # aplica permissões novas
oc start-build ocp-cluster-mcp -n ocp-ai-lab --follow   # nova imagem; o Deployment atualiza sozinho
```

Teste rápido de dentro do cluster:

```bash
oc run mcp-test -n ocp-ai-lab --rm -it --restart=Never --image=registry.access.redhat.com/ubi9/ubi-minimal -- \
  curl -s http://ocp-cluster-mcp.ocp-ai-lab.svc.cluster.local:8000/healthz
```

## 2. Registrar no Gen AI Playground

```bash
oc get configmap gen-ai-aa-mcp-servers -n redhat-ods-applications   # já existe?
```

- **Não existe:** `oc apply -f deploy/playground/gen-ai-aa-mcp-servers.yaml`
- **Já existe:** `oc edit configmap gen-ai-aa-mcp-servers -n redhat-ods-applications` e adicione só a chave `OCP-Cluster-MCP` do arquivo.

No Playground: painel de configurações → aba **MCP** → marque **OCP-Cluster-MCP** →
ícone de chave inglesa para ver as ferramentas → pergunte, por exemplo:

- "Qual a versão do OpenShift e há algum ClusterOperator degradado?"
- "Liste os pods com problema no namespace redhat-ods-applications."
- "Quais modelos estão servidos no OpenShift AI e estão prontos?"
- "Mostre os últimos eventos de aviso do cluster."

Documentação: [Playground prerequisites](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/experimenting_with_models_in_the_gen_ai_playground/playground-prerequisites_rhoai-user)
e [Testing with MCP servers](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/experimenting_with_models_in_the_gen_ai_playground/testing-with-model-context-protocol-servers_rhoai-user).

## 3. Usar pelo app cliente OGX

O cliente usa a Responses API do OGX (`/v1/responses`, compatível com OpenAI) e passa o
servidor MCP como ferramenta; o próprio OGX chama o MCP e devolve a resposta final.
Rode de um workbench do OpenShift AI ou de qualquer pod que alcance o serviço do OGX:

```bash
oc get ogxserver -A                      # ache o servidor OGX e o projeto
oc get svc -n <projeto> | grep ogx       # ache o Service (porta 8321)

pip install -r ogx-client/requirements.txt
export OGX_URL=http://<servico-ogx>.<projeto>.svc.cluster.local:8321
export MAAS_TOKEN=<token>    # só se o modelo vier do Models as a Service
python ogx-client/ask_cluster.py -v "Quais pods estão com problema no cluster?"
```

Se o modelo vem do **Models as a Service (MaaS)**, o OGX do Playground não guarda o token:
o Playground envia o token da subscription em cada chamada, no header
`X-OGX-Provider-Data`. Fora do Playground (incluindo o código do **View code**), envie
esse header você mesmo, senão a chamada falha com erro 500 (um 401 do MaaS). O
`ask_cluster.py` faz isso quando `MAAS_TOKEN` está definido.

Se ainda não tiver um OGXServer, use `deploy/ogx/ogxserver.yaml` como ponto de partida
(confira os campos com `oc explain ogxserver.spec`).

## Desenvolvimento local

```bash
cd mcp-server
pip install -r requirements.txt pytest
python -m pytest -q
python -m app.server      # usa o seu ~/.kube/config fora do cluster
```

Para inspecionar as ferramentas: `npx @modelcontextprotocol/inspector` apontando para
`http://localhost:8000/mcp` (transporte Streamable HTTP).

## Observações

- OGX e o Gen AI Playground estão em **Technology Preview** no OpenShift AI 3.5; nomes de campos podem mudar.
- O SDK `mcp` está fixado em `<2` porque a versão 2.x mudou a API (`FastMCP` virou `MCPServer`).
