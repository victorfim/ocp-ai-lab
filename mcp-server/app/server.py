"""MCP server with read-only tools about the running OpenShift cluster.

Transport: Streamable HTTP on http://0.0.0.0:8000/mcp (what OGX and the
OpenShift AI playground connect to). Health check: GET /healthz.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import cluster

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("ocp-cluster-mcp")

mcp = FastMCP(
    name="ocp-cluster",
    instructions=(
        "Ferramentas somente leitura para consultar o cluster OpenShift em execução: "
        "versão e atualizações, operadores, nós e capacidade, projetos e consumo de CPU, "
        "memória e disco, pods, logs, deployments, eventos, rotas e modelos servidos pelo "
        "OpenShift AI. Nenhuma ferramenta altera o cluster."
    ),
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8000")),
    stateless_http=True,
    json_response=True,
)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _limit(items: list, limit: int) -> dict[str, Any]:
    limit = max(1, min(limit, cluster.MAX_ITEMS))
    return {"total": len(items), "returned": min(len(items), limit), "items": items[:limit]}


@mcp.tool()
def get_cluster_version() -> dict[str, Any]:
    """Versão do OpenShift, canal de atualização e se há uma atualização em andamento."""
    try:
        cv = cluster.custom().get_cluster_custom_object(
            "config.openshift.io", "v1", "clusterversions", "version"
        )
    except ApiException as exc:
        return cluster.api_error(exc)
    status = cv.get("status", {})
    history = status.get("history", [])
    conds = status.get("conditions", [])
    return {
        "version": status.get("desired", {}).get("version"),
        "channel": cv.get("spec", {}).get("channel"),
        "cluster_id": cv.get("spec", {}).get("clusterID"),
        "update_in_progress": cluster._condition(conds, "Progressing"),
        "available_updates": [u.get("version") for u in status.get("availableUpdates") or []][:10],
        "last_update": history[0] if history else None,
    }


@mcp.tool()
def list_cluster_operators(only_unhealthy: bool = False) -> dict[str, Any]:
    """Lista os ClusterOperators do OpenShift e o estado de cada um.

    Args:
        only_unhealthy: se true, retorna só operadores indisponíveis ou degradados.
    """
    try:
        data = cluster.custom().list_cluster_custom_object(
            "config.openshift.io", "v1", "clusteroperators"
        )
    except ApiException as exc:
        return cluster.api_error(exc)
    items = [cluster.summarize_cluster_operator(co) for co in data.get("items", [])]
    if only_unhealthy:
        items = [i for i in items if i["available"] != "True" or i["degraded"] == "True"]
    return _limit(items, cluster.MAX_ITEMS)


@mcp.tool()
def list_nodes(role: str = "") -> dict[str, Any]:
    """Lista os nós do cluster com papel, estado, CPU, memória, GPU, tipo e família da instância.

    Args:
        role: filtra por papel, por exemplo "master", "worker" ou "infra". Vazio = todos.
    """
    try:
        nodes = cluster.core().list_node().items
    except ApiException as exc:
        return cluster.api_error(exc)
    items = []
    for n in nodes:
        item = cluster.summarize_node(n)
        d = cluster.node_details(n)
        item.update(
            instance_type=d["instance_type"],
            instance_family=d["instance_family"],
            architecture=d["architecture"],
            zone=d["zone"],
        )
        items.append(item)
    if role:
        items = [i for i in items if role in i["roles"]]
    return _limit(items, cluster.MAX_ITEMS)


@mcp.tool()
def list_namespaces(name_contains: str = "", limit: int = 100) -> dict[str, Any]:
    """Lista os namespaces (projetos) do cluster e o estado de cada um.

    Args:
        name_contains: filtra namespaces cujo nome contém este texto.
        limit: número máximo de itens retornados.
    """
    try:
        nss = cluster.core().list_namespace().items
    except ApiException as exc:
        return cluster.api_error(exc)
    items = [
        {
            "name": ns.metadata.name,
            "phase": ns.status.phase,
            "display_name": (ns.metadata.annotations or {}).get("openshift.io/display-name"),
            "data_science_project": (ns.metadata.labels or {}).get(
                "opendatahub.io/dashboard"
            )
            == "true",
            "age": cluster._age(ns.metadata.creation_timestamp),
        }
        for ns in nss
        if name_contains.lower() in ns.metadata.name.lower()
    ]
    return _limit(items, limit)


@mcp.tool()
def list_pods(namespace: str = "", only_problematic: bool = False, limit: int = 100) -> dict[str, Any]:
    """Lista pods com fase, prontidão, reinícios e motivo de falha.

    Args:
        namespace: namespace a consultar. Vazio = todos os namespaces.
        only_problematic: se true, retorna só pods com falha, pendentes, não prontos
            ou com muitos reinícios (bom para perguntas como "o que está quebrado?").
        limit: número máximo de itens retornados.
    """
    try:
        api = cluster.core()
        pods = (
            api.list_namespaced_pod(namespace) if namespace else api.list_pod_for_all_namespaces()
        ).items
    except ApiException as exc:
        return cluster.api_error(exc)
    items = [cluster.summarize_pod(p) for p in pods]
    if only_problematic:
        items = [i for i in items if cluster.pod_is_problematic(i)]
    return _limit(items, limit)


@mcp.tool()
def get_pod_logs(namespace: str, pod: str, container: str = "", tail_lines: int = 100, previous: bool = False) -> dict[str, Any]:
    """Retorna as últimas linhas de log de um pod.

    Args:
        namespace: namespace do pod.
        pod: nome do pod.
        container: nome do container (obrigatório se o pod tiver mais de um).
        tail_lines: quantas linhas do final do log retornar (máximo 500).
        previous: se true, lê o log da execução anterior (útil em CrashLoopBackOff).
    """
    kwargs: dict[str, Any] = {
        "tail_lines": max(1, min(tail_lines, cluster.MAX_LOG_LINES)),
        "previous": previous,
    }
    if container:
        kwargs["container"] = container
    try:
        text = cluster.core().read_namespaced_pod_log(pod, namespace, **kwargs)
    except ApiException as exc:
        return cluster.api_error(exc)
    return {"namespace": namespace, "pod": pod, "container": container or None, "log": text}


@mcp.tool()
def list_deployments(namespace: str = "", only_unavailable: bool = False, limit: int = 100) -> dict[str, Any]:
    """Lista Deployments com réplicas desejadas, prontas e imagens.

    Args:
        namespace: namespace a consultar. Vazio = todos os namespaces.
        only_unavailable: se true, retorna só deployments com menos réplicas prontas que o desejado.
        limit: número máximo de itens retornados.
    """
    try:
        api = cluster.apps()
        deps = (
            api.list_namespaced_deployment(namespace)
            if namespace
            else api.list_deployment_for_all_namespaces()
        ).items
    except ApiException as exc:
        return cluster.api_error(exc)
    items = [cluster.summarize_deployment(d) for d in deps]
    if only_unavailable:
        items = [i for i in items if (i["desired"] or 0) > i["ready"]]
    return _limit(items, limit)


@mcp.tool()
def list_events(namespace: str = "", only_warnings: bool = True, limit: int = 50) -> dict[str, Any]:
    """Lista os eventos mais recentes do cluster (por padrão, só avisos).

    Args:
        namespace: namespace a consultar. Vazio = todos os namespaces.
        only_warnings: se true, retorna só eventos do tipo Warning.
        limit: número máximo de eventos, do mais recente para o mais antigo.
    """
    kwargs = {"field_selector": "type=Warning"} if only_warnings else {}
    try:
        api = cluster.core()
        evs = (
            api.list_namespaced_event(namespace, **kwargs)
            if namespace
            else api.list_event_for_all_namespaces(**kwargs)
        ).items
    except ApiException as exc:
        return cluster.api_error(exc)
    items = sorted((cluster.summarize_event(e) for e in evs), key=lambda i: i["_sort"], reverse=True)
    for i in items:
        i.pop("_sort")
    return _limit(items, limit)


@mcp.tool()
def list_routes(namespace: str = "", limit: int = 100) -> dict[str, Any]:
    """Lista as Routes do OpenShift (URLs externas) e o serviço de destino.

    Args:
        namespace: namespace a consultar. Vazio = todos os namespaces.
        limit: número máximo de itens retornados.
    """
    try:
        api = cluster.custom()
        data = (
            api.list_namespaced_custom_object("route.openshift.io", "v1", namespace, "routes")
            if namespace
            else api.list_cluster_custom_object("route.openshift.io", "v1", "routes")
        )
    except ApiException as exc:
        return cluster.api_error(exc)
    return _limit([cluster.summarize_route(r) for r in data.get("items", [])], limit)


@mcp.tool()
def list_inference_services(namespace: str = "") -> dict[str, Any]:
    """Lista os modelos servidos pelo OpenShift AI (InferenceServices do KServe).

    Args:
        namespace: namespace (projeto de ciência de dados) a consultar. Vazio = todos.
    """
    try:
        api = cluster.custom()
        data = (
            api.list_namespaced_custom_object(
                "serving.kserve.io", "v1beta1", namespace, "inferenceservices"
            )
            if namespace
            else api.list_cluster_custom_object("serving.kserve.io", "v1beta1", "inferenceservices")
        )
    except ApiException as exc:
        return cluster.api_error(exc)
    return _limit([cluster.summarize_inference_service(i) for i in data.get("items", [])], cluster.MAX_ITEMS)


# ---------------------------------------------------------------- capacity & updates


@mcp.tool()
def get_node_resources(node: str = "", role: str = "") -> dict[str, Any]:
    """Capacidade de cada nó: CPU, memória, família da instância, quanto está reservado
    (requests dos pods) e quanto está em uso agora. Inclui totais por família de instância.

    Args:
        node: nome de um nó específico. Vazio = todos.
        role: filtra por papel, por exemplo "master", "worker" ou "infra".
    """
    try:
        api = cluster.core()
        nodes = [api.read_node(node)] if node else api.list_node().items
        pods = (
            api.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node}")
            if node
            else api.list_pod_for_all_namespaces()
        ).items
    except ApiException as exc:
        return cluster.api_error(exc)
    try:
        usage = cluster.node_metrics()
    except ApiException:
        usage = {}

    req: dict[str, list[float]] = {}
    pod_count: dict[str, int] = {}
    for p in pods:
        if not p.spec.node_name or not cluster.pod_is_active(p):
            continue
        cpu_r, mem_r, _, _ = cluster.pod_requests(p)
        acc = req.setdefault(p.spec.node_name, [0.0, 0.0])
        acc[0] += cpu_r
        acc[1] += mem_r
        pod_count[p.spec.node_name] = pod_count.get(p.spec.node_name, 0) + 1

    items, families = [], {}
    total_mem = total_mem_req = 0.0
    for n in nodes:
        base = cluster.summarize_node(n)
        if role and role not in base["roles"]:
            continue
        d = cluster.node_details(n)
        name = base["name"]
        cpu_req, mem_req = req.get(name, [0.0, 0.0])
        total_mem += d["memory_allocatable_bytes"]
        total_mem_req += mem_req
        u = usage.get(name)
        items.append({
            "name": name,
            "roles": base["roles"],
            "ready": base["ready"],
            "instance_type": d["instance_type"],
            "instance_family": d["instance_family"],
            "architecture": d["architecture"],
            "zone": d["zone"],
            "cpu": {
                "capacity_cores": d["cpu_cores"],
                "allocatable_cores": d["cpu_allocatable_cores"],
                "requested_cores": round(cpu_req, 2),
                "requested_pct": cluster.pct(cpu_req, d["cpu_allocatable_cores"]),
                "used_cores": round(u["cpu"], 2) if u else None,
                "used_pct": cluster.pct(u["cpu"], d["cpu_allocatable_cores"]) if u else None,
            },
            "memory": {
                "capacity": cluster.fmt_bytes(d["memory_bytes"]),
                "allocatable": cluster.fmt_bytes(d["memory_allocatable_bytes"]),
                "requested": cluster.fmt_bytes(mem_req),
                "requested_pct": cluster.pct(mem_req, d["memory_allocatable_bytes"]),
                "used": cluster.fmt_bytes(u["memory"]) if u else None,
                "used_pct": cluster.pct(u["memory"], d["memory_allocatable_bytes"]) if u else None,
            },
            "pods": {"running": pod_count.get(name, 0), "max": d["max_pods"]},
            "ephemeral_storage": cluster.fmt_bytes(d["ephemeral_storage_bytes"]),
        })
        fam = families.setdefault(
            d["instance_family"] or "desconhecida",
            {"nodes": 0, "instance_types": set(), "cpu_cores": 0.0, "memory_bytes": 0.0},
        )
        fam["nodes"] += 1
        fam["instance_types"].add(d["instance_type"] or "desconhecido")
        fam["cpu_cores"] += d["cpu_cores"]
        fam["memory_bytes"] += d["memory_bytes"]

    total_cpu = sum(i["cpu"]["allocatable_cores"] for i in items)
    total_cpu_req = sum(i["cpu"]["requested_cores"] for i in items)
    return {
        "nodes": items,
        "by_instance_family": [
            {
                "family": k,
                "nodes": v["nodes"],
                "instance_types": sorted(v["instance_types"]),
                "cpu_cores": v["cpu_cores"],
                "memory": cluster.fmt_bytes(v["memory_bytes"]),
            }
            for k, v in sorted(families.items())
        ],
        "totals": {
            "nodes": len(items),
            "cpu_allocatable_cores": round(total_cpu, 2),
            "cpu_requested_pct": cluster.pct(total_cpu_req, total_cpu),
            "memory_allocatable": cluster.fmt_bytes(total_mem),
            "memory_requested_pct": cluster.pct(total_mem_req, total_mem),
        },
        "usage_source": "metrics.k8s.io" if usage else "indisponível (sem métricas de uso)",
    }


@mcp.tool()
def check_cluster_updates() -> dict[str, Any]:
    """Verifica atualizações do OpenShift: versão atual, canal, versões disponíveis,
    atualizações com riscos conhecidos, se a próxima versão minor está bloqueada e se há
    atualização em andamento."""
    try:
        cv = cluster.custom().get_cluster_custom_object(
            "config.openshift.io", "v1", "clusterversions", "version"
        )
    except ApiException as exc:
        return cluster.api_error(exc)
    return cluster.summarize_updates(cv)


@mcp.tool()
def check_operator_updates(only_pending: bool = True, namespace: str = "") -> dict[str, Any]:
    """Verifica atualizações dos operadores instalados (OLM Subscriptions): versão instalada,
    última versão do canal e se a atualização espera aprovação manual.

    Args:
        only_pending: se true, retorna só operadores com atualização pendente.
        namespace: namespace a consultar. Vazio = todos.
    """
    try:
        api = cluster.custom()
        data = (
            api.list_namespaced_custom_object("operators.coreos.com", "v1alpha1", namespace, "subscriptions")
            if namespace
            else api.list_cluster_custom_object("operators.coreos.com", "v1alpha1", "subscriptions")
        )
    except ApiException as exc:
        return cluster.api_error(exc)
    subs = [cluster.summarize_subscription(s) for s in data.get("items", [])]
    pending = [s for s in subs if s["update_pending"]]
    return {
        "operators_total": len(subs),
        "updates_pending": len(pending),
        "awaiting_manual_approval": sum(1 for s in pending if s["needs_manual_approval"]),
        "items": pending if only_pending else subs,
    }


SYSTEM_NS_PREFIXES = ("openshift", "kube-", "default", "redhat-", "rhods-", "nvidia-", "istio-")


@mcp.tool()
def get_project_capacity(
    namespace: str = "", include_system: bool = False, sort_by: str = "cpu", limit: int = 20
) -> dict[str, Any]:
    """Capacidade e consumo dos projetos (namespaces): uso real de CPU e memória, requests e
    limits dos pods, quotas (ResourceQuota) e disco (PVCs: tamanho solicitado e uso real).

    Args:
        namespace: projeto específico. Vazio = todos, ordenados pelo maior consumo.
        include_system: se true, inclui namespaces do sistema (openshift-*, kube-*, ...).
        sort_by: "cpu", "memory" ou "disk".
        limit: número máximo de projetos retornados.
    """
    try:
        api = cluster.core()
        pods = (api.list_namespaced_pod(namespace) if namespace else api.list_pod_for_all_namespaces()).items
        pvcs = (
            api.list_namespaced_persistent_volume_claim(namespace)
            if namespace
            else api.list_persistent_volume_claim_for_all_namespaces()
        ).items
        quotas = (
            api.list_namespaced_resource_quota(namespace)
            if namespace
            else api.list_resource_quota_for_all_namespaces()
        ).items
    except ApiException as exc:
        return cluster.api_error(exc)

    projects: dict[str, dict[str, Any]] = {}

    def proj(ns: str) -> dict[str, Any]:
        return projects.setdefault(ns, {
            "pods": 0, "cpu_used": None, "mem_used": None,
            "cpu_req": 0.0, "cpu_lim": 0.0, "mem_req": 0.0, "mem_lim": 0.0,
            "pvcs": 0, "disk_requested": 0.0, "disk_used": None, "disk_capacity": None, "quotas": [],
        })

    for p in pods:
        if not cluster.pod_is_active(p):
            continue
        pr = proj(p.metadata.namespace)
        pr["pods"] += 1
        cr, mr, cl, ml = cluster.pod_requests(p)
        pr["cpu_req"] += cr
        pr["mem_req"] += mr
        pr["cpu_lim"] += cl
        pr["mem_lim"] += ml

    usage_source = "metrics.k8s.io"
    try:
        for m in cluster.pod_metrics(namespace):
            pr = proj(m["metadata"]["namespace"])
            for c in m.get("containers", []):
                pr["cpu_used"] = (pr["cpu_used"] or 0.0) + cluster.parse_quantity(c["usage"].get("cpu"))
                pr["mem_used"] = (pr["mem_used"] or 0.0) + cluster.parse_quantity(c["usage"].get("memory"))
    except ApiException:
        usage_source = "indisponível (sem métricas de uso)"

    for pvc in pvcs:
        pr = proj(pvc.metadata.namespace)
        pr["pvcs"] += 1
        req = (pvc.spec.resources.requests or {}) if pvc.spec.resources else {}
        pr["disk_requested"] += cluster.parse_quantity(req.get("storage"))

    ns_filter = f'{{namespace="{namespace}"}}' if namespace else ""
    disk_used = cluster.prometheus_by_label(
        f"sum by (namespace) (kubelet_volume_stats_used_bytes{ns_filter})", "namespace"
    )
    disk_cap = cluster.prometheus_by_label(
        f"sum by (namespace) (kubelet_volume_stats_capacity_bytes{ns_filter})", "namespace"
    )
    for ns, v in (disk_used or {}).items():
        proj(ns)["disk_used"] = v
    for ns, v in (disk_cap or {}).items():
        proj(ns)["disk_capacity"] = v

    for q in quotas:
        hard, used = (q.status.hard or {}), (q.status.used or {})
        proj(q.metadata.namespace)["quotas"].append({
            "name": q.metadata.name,
            "limits": {k: {"used": used.get(k), "hard": v} for k, v in hard.items()},
        })

    items = []
    for ns, pr in projects.items():
        if not namespace and not include_system and ns.startswith(SYSTEM_NS_PREFIXES):
            continue
        items.append({
            "namespace": ns,
            "pods_running": pr["pods"],
            "cpu": {
                "used_cores": round(pr["cpu_used"], 3) if pr["cpu_used"] is not None else None,
                "requested_cores": round(pr["cpu_req"], 3),
                "limit_cores": round(pr["cpu_lim"], 3),
            },
            "memory": {
                "used": cluster.fmt_bytes(pr["mem_used"]) if pr["mem_used"] is not None else None,
                "requested": cluster.fmt_bytes(pr["mem_req"]),
                "limit": cluster.fmt_bytes(pr["mem_lim"]),
            },
            "disk": {
                "pvcs": pr["pvcs"],
                "requested": cluster.fmt_bytes(pr["disk_requested"]),
                "used": cluster.fmt_bytes(pr["disk_used"]) if pr["disk_used"] is not None else None,
                "used_pct": cluster.pct(pr["disk_used"], pr["disk_capacity"])
                if pr["disk_used"] is not None and pr["disk_capacity"]
                else None,
            },
            "quotas": pr["quotas"],
            "_sort": {
                "cpu": pr["cpu_used"] if pr["cpu_used"] is not None else pr["cpu_req"],
                "memory": pr["mem_used"] if pr["mem_used"] is not None else pr["mem_req"],
                "disk": pr["disk_used"] if pr["disk_used"] is not None else pr["disk_requested"],
            }.get(sort_by, pr["cpu_req"]),
        })
    items.sort(key=lambda i: i["_sort"] or 0, reverse=True)
    for i in items:
        i.pop("_sort")
    result = _limit(items, limit)
    result["sources"] = {
        "cpu_memory_usage": usage_source,
        "disk_usage": "Prometheus (kubelet_volume_stats)" if disk_used is not None
        else "indisponível: mostrando só o tamanho solicitado dos PVCs",
    }
    return result


def main() -> None:
    log.info("Starting ocp-cluster MCP server on %s:%s/mcp", mcp.settings.host, mcp.settings.port)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
