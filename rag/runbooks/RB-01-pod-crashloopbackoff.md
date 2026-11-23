# RB-01 Pod em CrashLoopBackOff ou com muitos reinícios

> Documento de exemplo do laboratório ocp-ai-lab (procedimento e limites fictícios).

## Sintoma

O pod aparece com status `CrashLoopBackOff`, `Error` ou `OOMKilled`, ou tem mais de
**5 reinícios na última hora**. Severidade padrão: **SEV3**. Se o pod for de um namespace
`openshift-*` ou do OpenShift AI (`redhat-ods-*`), trate como **SEV2**.

## Diagnóstico com o assistente (ferramentas MCP)

1. `list_pods(namespace=<projeto>, only_problematic=true)`: confirme o pod, o motivo e o
   número de reinícios.
2. `get_pod_logs(namespace=<projeto>, pod=<pod>, previous=true)`: logs da execução que
   falhou. Sem `previous=true` você vê o container novo, que geralmente ainda não falhou.
3. `list_events(namespace=<projeto>, only_warnings=true)`: procure `BackOff`, `FailedMount`,
   `Unhealthy` (probe falhando) e `FailedScheduling`.
4. `get_project_capacity(namespace=<projeto>)`: confira se a quota ou o limit de memória está
   no limite.

## Causas comuns e ação recomendada

| Evidência | Causa provável | Ação (executada por uma pessoa) |
|---|---|---|
| Motivo `OOMKilled` | Limit de memória baixo | Aumentar o limit em 50%: `oc set resources deploy/<app> -n <projeto> --limits=memory=<novo>` |
| Evento `Unhealthy` de liveness | Probe agressiva ou app lenta para subir | Aumentar `initialDelaySeconds` ou usar `startupProbe` |
| Log com erro de conexão ou variável ausente | Configuração ou dependência | Corrigir ConfigMap ou Secret; o assistente não lê Secrets |
| Evento `FailedMount` | PVC ou Secret inexistente | Ver RB-04 (storage) |
| Erro de permissão em arquivo | Imagem espera rodar como root | Ajustar a imagem para UID arbitrário; **não** conceder SCC `anyuid` sem aprovação do time de plataforma |

## Regras deste runbook

- Não apague o pod em loop "para ver se resolve": o controlador recria igual. Primeiro
  colete os logs com `previous=true`.
- Se a causa for OOMKilled pela **terceira vez em 7 dias** no mesmo workload, abra um
  chamado SEV3 para o time dono revisar o dimensionamento.
