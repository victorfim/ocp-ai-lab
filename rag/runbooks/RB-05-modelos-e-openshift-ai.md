# RB-05 Modelo servido indisponível no OpenShift AI

> Documento de exemplo do laboratório ocp-ai-lab (procedimento e limites fictícios).

## Sintoma

- `list_inference_services` mostra um InferenceService com `Ready=False`.
- O Playground responde erro 500 ou não lista o modelo.
- O modelo responde, mas não chama ferramentas MCP.

Severidade: modelo de produção indisponível é **SEV2**; modelo de laboratório é **SEV3**.

## Diagnóstico com o assistente

1. `list_inference_services(namespace=<projeto>)`: estado, URL e motivo.
2. `list_pods(namespace=<projeto>, only_problematic=true)` e `get_pod_logs` do pod
   `*-predictor-*`. Erros comuns: `CUDA out of memory`, falha ao baixar o modelo, versão do
   vLLM incompatível com a arquitetura.
3. `get_node_resources`: há GPU livre no nó g6e?
4. `list_events(namespace=<projeto>, only_warnings=true)`: `Insufficient nvidia.com/gpu`.

## Causas comuns

| Evidência | Causa | Ação |
|---|---|---|
| `CUDA out of memory` | Contexto grande demais para a GPU | Reduzir `--max-model-len` ou `--gpu-memory-utilization` |
| `Insufficient nvidia.com/gpu` | GPU ocupada por outro modelo | Ver RB-03; GPU nova só com aprovação do MLOps |
| Modelo não chama ferramentas | vLLM sem tool calling | Adicionar `--enable-auto-tool-choice` e `--tool-call-parser=<parser>` (por exemplo `llama3_json` para Llama 3.x) |
| Erro 500 com 401 do MaaS fora do Playground | Falta o token da subscription | Enviar o header `X-OGX-Provider-Data` com `vllm_api_token` |
| Modelo não usa a base de conhecimento | Prompt não pede a busca | Instruir no prompt: "use a ferramenta knowledge_search" |

## Regras deste runbook

- Modelos com mais de **8B parâmetros** precisam de aprovação do time MLOps antes de subir,
  porque o laboratório tem uma única GPU.
- Não troque o modelo do Playground em horário de demonstração agendada sem avisar em
  `#mlops-suporte`.
