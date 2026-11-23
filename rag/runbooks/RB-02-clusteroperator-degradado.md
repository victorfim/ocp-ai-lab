# RB-02 ClusterOperator degradado ou indisponível

> Documento de exemplo do laboratório ocp-ai-lab (procedimento e limites fictícios).

## Sintoma

`list_cluster_operators(only_unhealthy=true)` mostra um operador com `Degraded=True`,
`Available=False` ou `Progressing=True` por mais de **30 minutos** fora de um upgrade.

Severidade: **SEV2** para um operador. **SEV1** se for `kube-apiserver`, `etcd`,
`authentication`, `ingress` ou `network`, ou se houver mais de um degradado.

## Diagnóstico com o assistente

1. `get_cluster_version`: há upgrade em andamento? Durante upgrade, `Progressing=True` é
   normal; espere terminar antes de abrir incidente.
2. `list_cluster_operators(only_unhealthy=true)`: leia a mensagem da condição. Ela costuma
   apontar o namespace e o recurso com problema.
3. `list_pods(namespace=openshift-<operador>, only_problematic=true)` e `get_pod_logs` do pod
   do operador.
4. `list_nodes`: muitos operadores degradam quando um nó master está NotReady (ver RB-03).

## Ações recomendadas

- `ingress` degradado: confira se os pods do router têm réplicas prontas
  (`list_deployments(namespace=openshift-ingress)`) e se há nós worker suficientes.
- `image-registry` degradado: geralmente é storage. Ver RB-04.
- `machine-config` degradado: um nó não aplicou a configuração. Nunca force o
  `machine-config` manualmente; escale para o time de plataforma.
- Em qualquer caso, colete `oc adm must-gather` **antes** de abrir caso no suporte da Red Hat.

## Regras deste runbook

- Não edite recursos gerenciados por operadores (`openshift-*`) à mão: o operador desfaz a
  mudança e o caso fica mais difícil de investigar.
- Caso de suporte na Red Hat: abrir com severidade 2 se o SEV interno for SEV1 ou SEV2.
