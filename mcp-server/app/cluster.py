"""Read-only queries against the Kubernetes/OpenShift API.

Every function here only performs GET/LIST calls. The ServiceAccount used in
the cluster is bound to a ClusterRole that only allows get/list/watch, so even
a bug here cannot change the cluster.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

MAX_ITEMS = int(os.environ.get("MCP_MAX_ITEMS", "200"))
MAX_LOG_LINES = 500


@lru_cache(maxsize=1)
def _load_config() -> None:
    """Use the in-cluster ServiceAccount, or the local kubeconfig when run outside."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def core() -> client.CoreV1Api:
    _load_config()
    return client.CoreV1Api()


def apps() -> client.AppsV1Api:
    _load_config()
    return client.AppsV1Api()


def custom() -> client.CustomObjectsApi:
    _load_config()
    return client.CustomObjectsApi()


# ---------------------------------------------------------------- helpers


def _age(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - ts).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _condition(conditions: list[dict] | None, ctype: str) -> str | None:
    for cond in conditions or []:
        if cond.get("type") == ctype:
            return cond.get("status")
    return None


def api_error(exc: ApiException) -> dict[str, Any]:
    if exc.status == 403:
        hint = "O ServiceAccount do servidor MCP não tem permissão de leitura para este recurso."
    elif exc.status == 404:
        hint = "Recurso não encontrado (o nome, o namespace ou o tipo de recurso não existe neste cluster)."
    else:
        hint = "Erro ao consultar a API do cluster."
    return {"error": hint, "status": exc.status, "reason": exc.reason}


# ---------------------------------------------------------------- summaries


def summarize_node(node: Any) -> dict[str, Any]:
    labels = node.metadata.labels or {}
    roles = sorted(
        k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")
    )
    ready = next(
        (c.status for c in (node.status.conditions or []) if c.type == "Ready"), "Unknown"
    )
    pressure = [
        c.type
        for c in (node.status.conditions or [])
        if c.type != "Ready" and c.status == "True"
    ]
    cap = node.status.capacity or {}
    alloc = node.status.allocatable or {}
    info = node.status.node_info
    return {
        "name": node.metadata.name,
        "roles": roles or ["worker"],
        "ready": ready,
        "problems": pressure,
        "unschedulable": bool(node.spec.unschedulable),
        "cpu": {"capacity": cap.get("cpu"), "allocatable": alloc.get("cpu")},
        "memory": {"capacity": cap.get("memory"), "allocatable": alloc.get("memory")},
        "gpu": cap.get("nvidia.com/gpu") or cap.get("amd.com/gpu"),
        "kubelet": getattr(info, "kubelet_version", None),
        "os": getattr(info, "os_image", None),
        "age": _age(node.metadata.creation_timestamp),
    }


def summarize_pod(pod: Any) -> dict[str, Any]:
    statuses = pod.status.container_statuses or []
    ready = sum(1 for s in statuses if s.ready)
    restarts = sum(s.restart_count or 0 for s in statuses)
    reasons = []
    for s in statuses:
        state = s.state
        if state and state.waiting and state.waiting.reason:
            reasons.append(state.waiting.reason)
        elif state and state.terminated and state.terminated.reason:
            reasons.append(state.terminated.reason)
    return {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "phase": pod.status.phase,
        "ready": f"{ready}/{len(statuses)}",
        "restarts": restarts,
        "reasons": sorted(set(reasons)),
        "node": pod.spec.node_name,
        "age": _age(pod.metadata.creation_timestamp),
    }


def pod_is_problematic(summary: dict[str, Any]) -> bool:
    if summary["phase"] in ("Failed", "Pending", "Unknown"):
        return True
    if summary["phase"] == "Succeeded":
        return False
    ready, total = summary["ready"].split("/")
    return ready != total or bool(summary["reasons"]) or summary["restarts"] > 5


def summarize_deployment(dep: Any) -> dict[str, Any]:
    st = dep.status
    return {
        "name": dep.metadata.name,
        "namespace": dep.metadata.namespace,
        "desired": dep.spec.replicas,
        "ready": st.ready_replicas or 0,
        "available": st.available_replicas or 0,
        "updated": st.updated_replicas or 0,
        "images": [c.image for c in dep.spec.template.spec.containers],
        "age": _age(dep.metadata.creation_timestamp),
    }


def summarize_event(ev: Any) -> dict[str, Any]:
    obj = ev.involved_object
    last = ev.last_timestamp or ev.event_time or ev.metadata.creation_timestamp
    return {
        "type": ev.type,
        "reason": ev.reason,
        "object": f"{obj.kind}/{obj.name}",
        "namespace": ev.metadata.namespace,
        "message": (ev.message or "").strip()[:500],
        "count": ev.count,
        "last_seen": _age(last),
        "_sort": last.timestamp() if last else 0,
    }


def summarize_cluster_operator(co: dict) -> dict[str, Any]:
    conds = co.get("status", {}).get("conditions", [])
    versions = co.get("status", {}).get("versions", [])
    message = next(
        (
            c.get("message")
            for c in conds
            if (c.get("type") == "Degraded" and c.get("status") == "True")
            or (c.get("type") == "Available" and c.get("status") == "False")
        ),
        None,
    )
    return {
        "name": co["metadata"]["name"],
        "version": next((v["version"] for v in versions if v.get("name") == "operator"), None),
        "available": _condition(conds, "Available"),
        "progressing": _condition(conds, "Progressing"),
        "degraded": _condition(conds, "Degraded"),
        "message": (message or "")[:500] or None,
    }


def summarize_route(route: dict) -> dict[str, Any]:
    spec = route.get("spec", {})
    tls = spec.get("tls")
    host = spec.get("host")
    return {
        "name": route["metadata"]["name"],
        "namespace": route["metadata"]["namespace"],
        "url": f"{'https' if tls else 'http'}://{host}{spec.get('path', '')}" if host else None,
        "service": spec.get("to", {}).get("name"),
        "tls": (tls or {}).get("termination"),
    }


def summarize_inference_service(isvc: dict) -> dict[str, Any]:
    status = isvc.get("status", {})
    predictor = isvc.get("spec", {}).get("predictor", {})
    model = predictor.get("model", {})
    return {
        "name": isvc["metadata"]["name"],
        "namespace": isvc["metadata"]["namespace"],
        "ready": _condition(status.get("conditions"), "Ready"),
        "url": status.get("url"),
        "runtime": model.get("runtime"),
        "model_format": model.get("modelFormat", {}).get("name"),
        "storage_uri": model.get("storageUri"),
    }
