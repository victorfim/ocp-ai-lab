"""Read-only queries against the Kubernetes/OpenShift API.

Every function here only performs GET/LIST calls. The ServiceAccount used in
the cluster is bound to a ClusterRole that only allows get/list/watch, so even
a bug here cannot change the cluster.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import urllib3
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.utils.quantity import parse_quantity

log = logging.getLogger("ocp-cluster-mcp")

MAX_ITEMS = int(os.environ.get("MCP_MAX_ITEMS", "200"))
MAX_LOG_LINES = 500

# Thanos Querier (cluster monitoring). Only reachable from inside the cluster and
# only if the ServiceAccount is bound to the "cluster-monitoring-view" ClusterRole.
PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL", "https://thanos-querier.openshift-monitoring.svc:9091"
)
_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
GIB = 1024**3


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


def cores(value: Any) -> float:
    """Kubernetes CPU quantity ("7500m", "8", "1234567n") -> cores."""
    if value in (None, ""):
        return 0.0
    return float(parse_quantity(value))


def gib(value: Any) -> float:
    """Kubernetes memory/storage quantity ("32Gi", "1500M", bytes) -> GiB."""
    if value in (None, ""):
        return 0.0
    return float(parse_quantity(value)) / GIB


def r2(value: float) -> float:
    return round(value, 2)


def pct(used: float, total: float) -> float | None:
    return round(100 * used / total, 1) if total else None


def instance_family(instance_type: str | None) -> str | None:
    """Cloud instance family: "m6a.4xlarge" -> "m6a", "Standard_D8s_v5" -> "Standard_D8s_v5"."""
    if not instance_type:
        return None
    return instance_type.split(".", 1)[0]


def version_key(version: str) -> tuple:
    parts = []
    for p in version.split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def prometheus_query(query: str) -> list[dict] | None:
    """Instant PromQL query against Thanos Querier. Returns None when unavailable
    (running outside the cluster, no permission, or monitoring not reachable)."""
    token_file, ca_file = _SA_DIR / "token", _SA_DIR / "service-ca.crt"
    if not token_file.exists():
        return None
    try:
        http = urllib3.PoolManager(
            cert_reqs="CERT_REQUIRED",
            ca_certs=str(ca_file) if ca_file.exists() else None,
            timeout=urllib3.Timeout(total=15),
            retries=False,
        )
        resp = http.request(
            "GET",
            f"{PROMETHEUS_URL}/api/v1/query",
            fields={"query": query},
            headers={"Authorization": f"Bearer {token_file.read_text().strip()}"},
        )
        if resp.status != 200:
            log.warning("Prometheus query failed: HTTP %s", resp.status)
            return None
        return json.loads(resp.data)["data"]["result"]
    except Exception as exc:  # monitoring is optional: never fail the tool because of it
        log.warning("Prometheus query failed: %s", exc)
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


# ---------------------------------------------------------------- capacity


def summarize_node_capacity(node: Any, usage: dict | None = None) -> dict[str, Any]:
    """Node hardware: instance type/family, CPU, memory, GPU and (optionally) current usage."""
    labels = node.metadata.labels or {}
    cap = node.status.capacity or {}
    alloc = node.status.allocatable or {}
    info = node.status.node_info
    itype = labels.get("node.kubernetes.io/instance-type") or labels.get(
        "beta.kubernetes.io/instance-type"
    )
    cpu_alloc, mem_alloc = cores(alloc.get("cpu")), gib(alloc.get("memory"))
    out = {
        "name": node.metadata.name,
        "roles": sorted(
            k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")
        )
        or ["worker"],
        "instance_type": itype,
        "family": instance_family(itype),
        "arch": labels.get("kubernetes.io/arch") or getattr(info, "architecture", None),
        "zone": labels.get("topology.kubernetes.io/zone"),
        "cpu_cores": {"capacity": r2(cores(cap.get("cpu"))), "allocatable": r2(cpu_alloc)},
        "memory_gib": {"capacity": r2(gib(cap.get("memory"))), "allocatable": r2(mem_alloc)},
        "ephemeral_storage_gib": {
            "capacity": r2(gib(cap.get("ephemeral-storage"))),
            "allocatable": r2(gib(alloc.get("ephemeral-storage"))),
        },
        "gpu": cap.get("nvidia.com/gpu") or cap.get("amd.com/gpu"),
        "max_pods": cap.get("pods"),
        "os": getattr(info, "os_image", None),
        "kernel": getattr(info, "kernel_version", None),
    }
    if usage:
        cpu_used, mem_used = cores(usage.get("cpu")), gib(usage.get("memory"))
        out["usage"] = {
            "cpu_cores": r2(cpu_used),
            "cpu_percent": pct(cpu_used, cpu_alloc),
            "memory_gib": r2(mem_used),
            "memory_percent": pct(mem_used, mem_alloc),
        }
    return out


def summarize_families(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Totals per instance family (how many nodes, total CPU and memory)."""
    fam: dict[str, dict[str, Any]] = {}
    for n in nodes:
        key = n["family"] or "desconhecida"
        f = fam.setdefault(
            key, {"family": key, "instance_types": set(), "nodes": 0, "cpu_cores": 0.0, "memory_gib": 0.0}
        )
        f["nodes"] += 1
        f["cpu_cores"] += n["cpu_cores"]["capacity"]
        f["memory_gib"] += n["memory_gib"]["capacity"]
        if n["instance_type"]:
            f["instance_types"].add(n["instance_type"])
    return [
        {**f, "instance_types": sorted(f["instance_types"]), "cpu_cores": r2(f["cpu_cores"]),
         "memory_gib": r2(f["memory_gib"])}
        for f in sorted(fam.values(), key=lambda x: x["family"])
    ]


# ---------------------------------------------------------------- updates


def summarize_cluster_updates(cv: dict) -> dict[str, Any]:
    spec, status = cv.get("spec", {}), cv.get("status", {})
    conds = status.get("conditions", [])
    upgradeable = next((c for c in conds if c.get("type") == "Upgradeable"), None)
    progressing = next((c for c in conds if c.get("type") == "Progressing"), {})
    retrieved = next((c for c in conds if c.get("type") == "RetrievedUpdates"), None)
    available = sorted(
        {u.get("version") for u in status.get("availableUpdates") or [] if u.get("version")},
        key=version_key,
        reverse=True,
    )
    conditional = []
    for cu in status.get("conditionalUpdates") or []:
        recommended = _condition(cu.get("conditions"), "Recommended")
        conditional.append(
            {
                "version": cu.get("release", {}).get("version"),
                "recommended": recommended,
                "risks": [r.get("name") for r in cu.get("risks") or []],
            }
        )
    conditional.sort(key=lambda c: version_key(c["version"] or "0"), reverse=True)
    history = status.get("history") or []
    return {
        "current_version": status.get("desired", {}).get("version"),
        "channel": spec.get("channel"),
        "update_in_progress": progressing.get("status") == "True",
        "progress_message": progressing.get("message"),
        "upgradeable": (upgradeable or {}).get("status", "True"),
        "upgradeable_message": (upgradeable or {}).get("message"),
        "update_service_reachable": (retrieved or {}).get("status"),
        "update_service_message": (retrieved or {}).get("message")
        if (retrieved or {}).get("status") != "True"
        else None,
        "latest_available": available[0] if available else None,
        "available_updates": available,
        "conditional_updates": conditional,
        "last_update": {
            k: history[0].get(k) for k in ("version", "state", "startedTime", "completionTime")
        }
        if history
        else None,
    }


def summarize_subscription(sub: dict, pending_plans: dict[str, list[str]]) -> dict[str, Any]:
    """OLM operator: installed vs latest CSV in the subscribed channel."""
    meta, spec, status = sub["metadata"], sub.get("spec", {}), sub.get("status", {})
    installed, current = status.get("installedCSV"), status.get("currentCSV")
    ns = meta["namespace"]
    plan = (status.get("installPlanRef") or {}).get("name")
    waiting = [c for c in pending_plans.get(f"{ns}/{plan}", [])] if plan else []
    return {
        "name": spec.get("name") or meta["name"],
        "namespace": ns,
        "channel": spec.get("channel"),
        "approval": spec.get("installPlanApproval"),
        "installed_version": installed,
        "latest_in_channel": current,
        "update_available": bool(installed and current and installed != current) or bool(waiting),
        "waiting_manual_approval": waiting,
        "state": status.get("state"),
    }


# ---------------------------------------------------------------- projects


def container_resources(pod: Any) -> dict[str, float]:
    """Sum of CPU/memory requests and limits of a pod's containers."""
    tot = {"cpu_requests": 0.0, "cpu_limits": 0.0, "memory_requests": 0.0, "memory_limits": 0.0}
    for c in pod.spec.containers or []:
        res = c.resources
        req = (res.requests if res else None) or {}
        lim = (res.limits if res else None) or {}
        tot["cpu_requests"] += cores(req.get("cpu"))
        tot["cpu_limits"] += cores(lim.get("cpu"))
        tot["memory_requests"] += gib(req.get("memory"))
        tot["memory_limits"] += gib(lim.get("memory"))
    return tot


def pod_metrics_usage(pod_metric: dict) -> tuple[float, float]:
    cpu = mem = 0.0
    for c in pod_metric.get("containers", []):
        cpu += cores(c.get("usage", {}).get("cpu"))
        mem += gib(c.get("usage", {}).get("memory"))
    return cpu, mem
