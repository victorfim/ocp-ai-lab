# RB-03 Nó NotReady e falta de capacidade

> Documento de exemplo do laboratório ocp-ai-lab (procedimento e limites fictícios).

## Sintoma

- `list_nodes` mostra um nó com `Ready=False` ou `Unknown`.
- Ou pods ficam `Pending` com evento `FailedScheduling` (`Insufficient cpu`,
  `Insufficient memory` ou `Insufficient nvidia.com/gpu`).

Severidade: um worker NotReady é **SEV2**; um master NotReady ou dois nós quaisquer é **SEV1**.

## Limites de capacidade da casa

| Recurso | Atenção | Crítico | Ação |
|---|---|---|---|
| CPU reservada (requests) no conjunto de workers | 70% | 85% | Acima de 85%, abrir SEV3 para adicionar um nó ao MachineSet |
| Memória reservada (requests) no conjunto de workers | 75% | 90% | Acima de 90%, abrir SEV2 |
| Uso real de CPU de um nó | 80% por 15 min | 95% | Ver quais pods consomem mais com `get_node_resources` |
| GPU (nó g6e) | 1 GPU livre | 0 GPU livre | Novos modelos com GPU só com aprovação do time MLOps |

## Diagnóstico com o assistente

1. `list_nodes`: quais nós, papéis e família da instância.
2. `get_node_resources`: reservado vs. capacidade por nó e totais por família (c6a para
   masters, m6a para workers, g6e para GPU neste laboratório).
3. `list_events(only_warnings=true)`: `NodeNotReady`, `FailedScheduling`, `Evicted`.
4. `get_project_capacity`: quais projetos mais reservam CPU e memória.

## Ações recomendadas

- Nó NotReady há menos de 10 minutos: aguarde, pode ser reinício do kubelet.
- Nó NotReady há mais de 10 minutos: o plantão verifica a instância no provedor de nuvem.
  O `MachineHealthCheck` substitui workers automaticamente após 15 minutos; **masters nunca
  são substituídos automaticamente**.
- Falta de capacidade: antes de pedir nó novo, procure requests superdimensionados
  (request muito maior que o uso real em `get_project_capacity`).
- Escalar MachineSet (pessoa do plantão, na janela de mudança):
  `oc scale machineset <nome> -n openshift-machine-api --replicas=<n>`.
