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
        "versão e atualizações disponíveis, operadores, nós e seu hardware, projetos e o "
        "consumo de CPU, memória e disco de cada um, pods, logs, deployments, eventos, "
        "rotas e modelos servidos pelo OpenShift AI. Nenhuma ferramenta altera o cluster."
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
    """Lista os nós do cluster com papel, estado, CPU, memória e GPU.

    Args:
        role: filtra por papel, por exemplo "master", "worker" ou "infra". Vazio = todos.
    """
    try:
        nodes = cluster.core().list_node().items
    except ApiException as exc:
        return cluster.api_error(exc)
    items = [cluster.summarize_node(n) for n in nodes]
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


@mcp.tool()
def describe_nodes(role: str = "", include_usage: bool = True) -> dict[str, Any]:
    """Descreve o hardware de cada nó: tipo e família da instância, zona, arquitetura,
    CPU, memória, disco efêmero e GPU, com totais por família.

    Args:
        role: filtra por papel, por exemplo "master", "worker" ou "worker-gpu". Vazio = todos.
        include_usage: se true, inclui o uso atual de CPU e memória de cada nó (metrics API).
    """
    try:
        nodes = cluster.core().list_node().items
    except ApiException as exc:
        return cluster.api_error(exc)
    usage: dict[str, dict] = {}
    usage_note = None
    if include_usage:
        try:
            data = cluster.custom().list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")
            usage = {m["metadata"]["name"]: m.get("usage", {}) for m in data.get("items", [])}
        except ApiException as exc:
            usage_note = f"Uso atual indisponível: {cluster.api_error(exc)['error']}"
    items = [cluster.summarize_node_capacity(n, usage.get(n.metadata.name)) for n in nodes]
    if role:
        items = [i for i in items if role in i["roles"]]
    items.sort(key=lambda i: (i["roles"], i["name"]))
    result: dict[str, Any] = {
        "total_nodes": len(items),
        "total_cpu_cores": cluster.r2(sum(i["cpu_cores"]["capacity"] for i in items)),
        "total_memory_gib": cluster.r2(sum(i["memory_gib"]["capacity"] for i in items)),
        "families": cluster.summarize_families(items),
        "nodes": items[: cluster.MAX_ITEMS],
    }
    if usage_note:
        result["note"] = usage_note
    return result


@mcp.tool()
def check_updates(include_operators: bool = True) -> dict[str, Any]:
    """Verifica atualizações: versões do OpenShift disponíveis no canal (recomendadas e
    condicionais, com riscos), se o cluster pode ser atualizado, e operadores (OLM)
    com versão mais nova ou aguardando aprovação manual.

    Args:
        include_operators: se true, também verifica os operadores instalados via OperatorHub.
    """
    try:
        cv = cluster.custom().get_cluster_custom_object(
            "config.openshift.io", "v1", "clusterversions", "version"
        )
    except ApiException as exc:
        return cluster.api_error(exc)
    result: dict[str, Any] = {"cluster": cluster.summarize_cluster_updates(cv)}
    if not include_operators:
        return result
    try:
        api = cluster.custom()
        subs = api.list_cluster_custom_object("operators.coreos.com", "v1alpha1", "subscriptions")
        plans = api.list_cluster_custom_object("operators.coreos.com", "v1alpha1", "installplans")
    except ApiException as exc:
        result["operators"] = cluster.api_error(exc)
        return result
    pending = {
        f"{p['metadata']['namespace']}/{p['metadata']['name']}": p.get("spec", {}).get(
            "clusterServiceVersionNames", []
        )
        for p in plans.get("items", [])
        if not p.get("spec", {}).get("approved", True)
    }
    ops = [cluster.summarize_subscription(s, pending) for s in subs.get("items", [])]
    ops.sort(key=lambda o: (not o["update_available"], o["name"]))
    result["operators"] = {
        "total": len(ops),
        "with_update": sum(1 for o in ops if o["update_available"]),
        "items": ops[: cluster.MAX_ITEMS],
    }
    return result


@mcp.tool()
def get_project_capacity(namespace: str = "", sort_by: str = "cpu", limit: int = 20) -> dict[str, Any]:
    """Capacidade e consumo por projeto (namespace): uso atual de CPU e memória, requests
    e limits dos pods, quotas (ResourceQuota) e disco (PVCs: capacidade solicitada e,
    quando o monitoramento permite, espaço realmente usado).

    Args:
        namespace: um projeto específico. Vazio = todos os projetos.
        sort_by: ordenação quando vários projetos: "cpu", "memory" ou "disk".
        limit: número máximo de projetos retornados.
    """
    core, custom = cluster.core(), cluster.custom()
    try:
        if namespace:
            pods = core.list_namespaced_pod(namespace).items
            pvcs = core.list_namespaced_persistent_volume_claim(namespace).items
            quotas = core.list_namespaced_resource_quota(namespace).items
        else:
            pods = core.list_pod_for_all_namespaces().items
            pvcs = core.list_persistent_volume_claim_for_all_namespaces().items
            quotas = core.list_resource_quota_for_all_namespaces().items
    except ApiException as exc:
        return cluster.api_error(exc)

    notes: list[str] = []
    projects: dict[str, dict[str, Any]] = {}

    def proj(ns: str) -> dict[str, Any]:
        return projects.setdefault(
            ns,
            {
                "namespace": ns,
                "running_pods": 0,
                "cpu_usage_cores": 0.0,
                "memory_usage_gib": 0.0,
                "cpu_requests_cores": 0.0,
                "cpu_limits_cores": 0.0,
                "memory_requests_gib": 0.0,
                "memory_limits_gib": 0.0,
                "pvc_count": 0,
                "disk_requested_gib": 0.0,
                "disk_used_gib": None,
                "quotas": [],
            },
        )

    for pod in pods:
        if pod.status.phase not in ("Running", "Pending"):
            continue
        p = proj(pod.metadata.namespace)
        p["running_pods"] += 1
        res = cluster.container_resources(pod)
        p["cpu_requests_cores"] += res["cpu_requests"]
        p["cpu_limits_cores"] += res["cpu_limits"]
        p["memory_requests_gib"] += res["memory_requests"]
        p["memory_limits_gib"] += res["memory_limits"]

    try:
        metrics = (
            custom.list_namespaced_custom_object("metrics.k8s.io", "v1beta1", namespace, "pods")
            if namespace
            else custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods")
        )
        for m in metrics.get("items", []):
            cpu, mem = cluster.pod_metrics_usage(m)
            p = proj(m["metadata"]["namespace"])
            p["cpu_usage_cores"] += cpu
            p["memory_usage_gib"] += mem
    except ApiException as exc:
        notes.append(f"Uso atual de CPU/memória indisponível: {cluster.api_error(exc)['error']}")

    for pvc in pvcs:
        p = proj(pvc.metadata.namespace)
        p["pvc_count"] += 1
        size = (pvc.status.capacity or {}).get("storage") or (
            pvc.spec.resources.requests or {}
        ).get("storage")
        p["disk_requested_gib"] += cluster.gib(size)

    if pvcs:
        ns_filter = f'namespace="{namespace}"' if namespace else ""
        used = cluster.prometheus_query(
            f"sum by (namespace) (kubelet_volume_stats_used_bytes{{{ns_filter}}})"
        )
        if used is None:
            notes.append(
                "Espaço usado em disco indisponível (requer acesso ao monitoramento do cluster); "
                "mostrando só a capacidade solicitada nos PVCs."
            )
        else:
            for r in used:
                ns = r["metric"].get("namespace")
                if ns in projects:
                    projects[ns]["disk_used_gib"] = cluster.r2(float(r["value"][1]) / cluster.GIB)

    for q in quotas:
        hard, used_q = q.status.hard or {}, q.status.used or {}
        proj(q.metadata.namespace)["quotas"].append(
            {"name": q.metadata.name, "hard": hard, "used": used_q}
        )

    items = []
    for p in projects.values():
        for k, v in list(p.items()):
            if isinstance(v, float):
                p[k] = cluster.r2(v)
        items.append(p)
    key = {
        "memory": lambda i: i["memory_usage_gib"],
        "disk": lambda i: i["disk_used_gib"] if i["disk_used_gib"] is not None else i["disk_requested_gib"],
    }.get(sort_by, lambda i: i["cpu_usage_cores"])
    items.sort(key=key, reverse=True)

    result: dict[str, Any] = {
        "cluster_totals": {
            "projects": len(items),
            "cpu_usage_cores": cluster.r2(sum(i["cpu_usage_cores"] for i in items)),
            "memory_usage_gib": cluster.r2(sum(i["memory_usage_gib"] for i in items)),
            "disk_requested_gib": cluster.r2(sum(i["disk_requested_gib"] for i in items)),
        },
        **_limit(items, limit),
    }
    if notes:
        result["notes"] = notes
    return result


def main() -> None:
    log.info("Starting ocp-cluster MCP server on %s:%s/mcp", mcp.settings.host, mcp.settings.port)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
