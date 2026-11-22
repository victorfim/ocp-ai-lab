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
        "versão, operadores, nós, projetos, pods, logs, deployments, eventos, rotas e "
        "modelos servidos pelo OpenShift AI. Nenhuma ferramenta altera o cluster."
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


def main() -> None:
    log.info("Starting ocp-cluster MCP server on %s:%s/mcp", mcp.settings.host, mcp.settings.port)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
