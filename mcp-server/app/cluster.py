"""Read-only queries against the Kubernetes/OpenShift API.

Every function here only performs GET/LIST calls. The ServiceAccount used in
the cluster is bound to a ClusterRole that only allows get/list/watch, so even
a bug here cannot change the cluster.
"""

from __future__ import annotations

import os
import re
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


# ---------------------------------------------------------------- quantities

_BIN = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DEC = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}


def parse_quantity(value: Any) -> float:
    """Parse a Kubernetes quantity ("250m", "2", "1536Mi", "1500n", "1e3") into a float."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    for suffix, mult in _BIN.items():
        if s.endswith(suffix):
            return float(s[: -len(suffix)]) * mult
    if s[-1:] in _DEC and not s[-1:].isdigit():
        try:
            return float(s[:-1]) * _DEC[s[-1]]
        except ValueError:
            pass
    return float(s)


def fmt_cores(cores: float) -> str:
    return f"{cores:.2f}" if cores < 10 else f"{cores:.1f}"


def fmt_bytes(n: float) -> str:
    for unit, size in (("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if abs(n) >= size:
            return f"{n / size:.1f} {unit}"
    return f"{int(n)} B"


def pct(part: float, total: float) -> float | None:
    return round(100 * part / total, 1) if total else None


# ---------------------------------------------------------------- node details


def instance_family(instance_type: str | None) -> str | None:
    """Best-effort instance family from the cloud instance type.

    AWS "m6i.2xlarge" -> "m6i"; Azure "Standard_D8s_v5" -> "D8s_v5" family "Ds_v5";
    GCP "n2-standard-8" -> "n2-standard". Unknown formats return the type itself.
    """
    if not instance_type:
        return None
    t = instance_type
    if "." in t:  # AWS / IBM-style
        return t.split(".", 1)[0]
    if t.startswith("Standard_"):  # Azure
        return re.sub(r"(\D+)\d+", r"\1", t.removeprefix("Standard_"), count=1)
    parts = t.split("-")
    if len(parts) >= 3 and parts[-1].isdigit():  # GCP
        return "-".join(parts[:-1])
    return t


def node_details(node: Any) -> dict[str, Any]:
    labels = node.metadata.labels or {}
    itype = labels.get("node.kubernetes.io/instance-type") or labels.get("beta.kubernetes.io/instance-type")
    info = node.status.node_info
    cap = node.status.capacity or {}
    alloc = node.status.allocatable or {}
    return {
        "instance_type": itype,
        "instance_family": instance_family(itype),
        "architecture": getattr(info, "architecture", None) or labels.get("kubernetes.io/arch"),
        "zone": labels.get("topology.kubernetes.io/zone"),
        "region": labels.get("topology.kubernetes.io/region"),
        "cpu_cores": parse_quantity(cap.get("cpu")),
        "cpu_allocatable_cores": parse_quantity(alloc.get("cpu")),
        "memory_bytes": parse_quantity(cap.get("memory")),
        "memory_allocatable_bytes": parse_quantity(alloc.get("memory")),
        "ephemeral_storage_bytes": parse_quantity(cap.get("ephemeral-storage")),
        "max_pods": int(parse_quantity(alloc.get("pods"))) or None,
    }


def pod_requests(pod: Any) -> tuple[float, float, float, float]:
    """(cpu_request_cores, mem_request_bytes, cpu_limit_cores, mem_limit_bytes) of a pod.

    Uses max(sum(containers), max(initContainers)), like the scheduler.
    """
    def total(containers: list | None, kind: str, res: str) -> list[float]:
        vals = []
        for c in containers or []:
            rr = getattr(c.resources, kind, None) if c.resources else None
            vals.append(parse_quantity((rr or {}).get(res)))
        return vals

    out = []
    for kind in ("requests", "limits"):
        for res in ("cpu", "memory"):
            main = sum(total(pod.spec.containers, kind, res))
            init = max(total(pod.spec.init_containers, kind, res), default=0.0)
            out.append(max(main, init))
    return out[0], out[1], out[2], out[3]


def pod_is_active(pod: Any) -> bool:
    return pod.status.phase not in ("Succeeded", "Failed")


# ---------------------------------------------------------------- updates


def _semver_key(v: str) -> tuple:
    out = []
    for p in v.split("-")[0].split("."):
        out.append(int(p) if p.isdigit() else 0)
    return tuple(out)


def summarize_updates(cv: dict) -> dict[str, Any]:
    spec, status = cv.get("spec", {}), cv.get("status", {})
    conds = status.get("conditions", [])

    def cond(t: str) -> dict:
        return next((c for c in conds if c.get("type") == t), {})

    current = status.get("desired", {}).get("version")
    available = sorted(
        {u.get("version") for u in status.get("availableUpdates") or [] if u.get("version")},
        key=_semver_key,
    )
    cur_minor = _semver_key(current or "0.0")[:2]
    conditional = []
    for cu in status.get("conditionalUpdates") or []:
        rel = cu.get("release", {})
        risks = [r.get("name") for r in cu.get("risks", [])]
        recommended = _condition(cu.get("conditions"), "Recommended")
        conditional.append({"version": rel.get("version"), "recommended": recommended, "risks": risks})
    conditional.sort(key=lambda c: _semver_key(c["version"] or "0"))
    upgradeable = cond("Upgradeable")
    progressing = cond("Progressing")
    history = status.get("history") or []
    return {
        "current_version": current,
        "channel": spec.get("channel"),
        "update_available": bool(available),
        "latest_available": available[-1] if available else None,
        "latest_patch_same_minor": next(
            (v for v in reversed(available) if _semver_key(v)[:2] == cur_minor), None
        ),
        "available_updates": available,
        "conditional_updates_with_risks": [c for c in conditional if c["recommended"] != "True"],
        "minor_upgrade_blocked": upgradeable.get("status") == "False",
        "minor_upgrade_blocked_reason": (upgradeable.get("message") or "")[:500] or None
        if upgradeable.get("status") == "False"
        else None,
        "update_in_progress": progressing.get("status") == "True",
        "progress_message": (progressing.get("message") or "")[:300] or None,
        "last_updates": [
            {"version": h.get("version"), "state": h.get("state"), "completed": h.get("completionTime")}
            for h in history[:3]
        ],
    }


def summarize_subscription(sub: dict) -> dict[str, Any]:
    spec, status = sub.get("spec", {}), sub.get("status", {})
    installed, current = status.get("installedCSV"), status.get("currentCSV")
    state = status.get("state")
    pending = bool(current and installed and current != installed) or state == "UpgradePending"
    return {
        "name": sub["metadata"]["name"],
        "namespace": sub["metadata"]["namespace"],
        "package": spec.get("name"),
        "channel": spec.get("channel"),
        "approval": spec.get("installPlanApproval", "Automatic"),
        "installed": installed,
        "latest_in_channel": current,
        "state": state,
        "update_pending": pending,
        "needs_manual_approval": pending and spec.get("installPlanApproval") == "Manual",
    }


# ---------------------------------------------------------------- metrics


def pod_metrics(namespace: str = "") -> list[dict]:
    """Live pod usage from metrics.k8s.io (the same source as `oc adm top pods`)."""
    api = custom()
    if namespace:
        data = api.list_namespaced_custom_object("metrics.k8s.io", "v1beta1", namespace, "pods")
    else:
        data = api.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods")
    return data.get("items", [])


def node_metrics() -> dict[str, dict]:
    data = custom().list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")
    return {
        i["metadata"]["name"]: {
            "cpu": parse_quantity(i["usage"].get("cpu")),
            "memory": parse_quantity(i["usage"].get("memory")),
        }
        for i in data.get("items", [])
    }


PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL", "https://thanos-querier.openshift-monitoring.svc.cluster.local:9091"
)
_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


def prometheus_query(query: str) -> list[dict] | None:
    """Instant PromQL query against the OpenShift monitoring stack (Thanos querier).

    Uses the pod's ServiceAccount token (needs the cluster-monitoring-view role).
    Returns None when monitoring is not reachable, so callers can degrade gracefully.
    """
    import json
    import ssl
    import urllib.parse
    import urllib.request

    try:
        with open(f"{_SA_DIR}/token") as f:
            token = f.read().strip()
    except OSError:
        token = os.environ.get("PROMETHEUS_TOKEN")
    if not token:
        return None
    ca = f"{_SA_DIR}/service-ca.crt"
    ctx = ssl.create_default_context(cafile=ca) if os.path.exists(ca) else ssl.create_default_context()
    url = f"{PROMETHEUS_URL}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            body = json.load(resp)
    except Exception:  # noqa: BLE001 - monitoring is optional
        return None
    if body.get("status") != "success":
        return None
    return body.get("data", {}).get("result", [])


def prometheus_by_label(query: str, label: str) -> dict[str, float] | None:
    result = prometheus_query(query)
    if result is None:
        return None
    return {r["metric"].get(label, ""): float(r["value"][1]) for r in result}
