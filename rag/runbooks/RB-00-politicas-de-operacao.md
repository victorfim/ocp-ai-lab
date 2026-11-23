# RB-00 Políticas de operação do cluster

> Documento de exemplo do laboratório ocp-ai-lab. As regras, nomes de times e canais são
> fictícios e servem para testar RAG: o modelo só sabe disso se consultar a base.

## Severidades

| Severidade | Quando usar | Tempo para primeira resposta | Quem aciona |
|---|---|---|---|
| SEV1 | Cluster ou OpenShift AI fora do ar, API do cluster indisponível, mais de um nó NotReady | 15 minutos | Plantão de plataforma, via canal `#ocp-plantao` e telefone |
| SEV2 | Um ClusterOperator degradado, um nó NotReady, modelo servido indisponível | 1 hora | Plantão de plataforma, via `#ocp-plantao` |
| SEV3 | Pod de aplicação em falha, PVC acima de 85%, atualização pendente | 1 dia útil | Time dono do projeto, via chamado na fila `PLAT-OCP` |
| SEV4 | Dúvida, melhoria, limpeza | 5 dias úteis | Fila `PLAT-OCP` |

## Janela de mudança

- Mudanças no cluster (upgrade do OpenShift, upgrade de operadores, troca de MachineSet) só
  acontecem na janela de **terça-feira, das 22h às 02h (horário de Brasília)**.
- Fora da janela, só correções de SEV1 e SEV2, com aprovação do líder do plantão registrada
  no chamado.
- Congelamento: nenhuma mudança entre **15 de dezembro e 6 de janeiro**, exceto SEV1.

## Regras gerais

1. O assistente de SRE do Playground tem acesso **somente leitura**. Ele investiga e sugere
   comandos; quem executa é uma pessoa do plantão.
2. Todo incidente SEV1 ou SEV2 precisa de um chamado na fila `PLAT-OCP` com as evidências
   (saída das ferramentas MCP ou dos comandos `oc`).
3. Nunca reinicie pods do namespace `openshift-*` sem seguir o runbook específico.
4. Depois de resolver um SEV1 ou SEV2, escreva o post-mortem em até **3 dias úteis**.

## Contatos

| Time | Responsabilidade | Canal |
|---|---|---|
| Plataforma OpenShift | Cluster, nós, operadores, upgrades | `#ocp-plantao` |
| MLOps | OpenShift AI, modelos servidos, GPU, Playground | `#mlops-suporte` |
| Storage | PVCs, StorageClasses, backups | `#storage-suporte` |
