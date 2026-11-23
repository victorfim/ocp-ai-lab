# RB-04 PVC cheio, pendente ou com erro de montagem

> Documento de exemplo do laboratório ocp-ai-lab (procedimento e limites fictícios).

## Sintoma

- `get_project_capacity` mostra um PVC com uso acima de **85%** (atenção) ou **95%** (crítico).
- PVC em `Pending`, ou evento `FailedMount` / `FailedAttachVolume` em um pod.

Severidade: PVC acima de 85% é **SEV3**; acima de 95% ou de banco de dados é **SEV2**.

## Diagnóstico com o assistente

1. `get_project_capacity(namespace=<projeto>)`: tamanho e uso real de cada PVC (vem do
   Prometheus via `kubelet_volume_stats_*`).
2. `list_events(namespace=<projeto>, only_warnings=true)`: `ProvisioningFailed`,
   `FailedMount`, `FailedAttachVolume`.
3. `list_pods(namespace=<projeto>, only_problematic=true)`: quem usa o PVC.

## Ações recomendadas

- Expandir o PVC (se a StorageClass tiver `allowVolumeExpansion: true`):
  `oc patch pvc <pvc> -n <projeto> -p '{"spec":{"resources":{"requests":{"storage":"<novo>"}}}}'`.
  Aumente em pelo menos 50% para não voltar ao limite em seguida.
- Volumes EBS (gp3-csi) só anexam em nós da **mesma zona**. `FailedAttachVolume` depois de um
  nó ser trocado geralmente é zona diferente.
- O banco pgvector do Playground (recursos `genai-pgvector-*`) usa um PVC de 5 GiB. Se encher,
  apague documentos antigos da aba Knowledge antes de pedir expansão ao time MLOps.

## Regras deste runbook

- Nunca apague um PVC para "liberar espaço": o dado é perdido. Apagar PVC exige chamado
  aprovado pelo time dono e pelo time de Storage.
- Backups dos PVCs de bancos rodam todo dia às 03h; confirme o último backup antes de
  qualquer expansão de PVC de banco.
