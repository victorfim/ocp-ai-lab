from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from app import cluster

NOW = datetime.now(timezone.utc)


def meta(name, ns=None, labels=None, age_h=2):
    return NS(name=name, namespace=ns, labels=labels or {}, annotations={},
              creation_timestamp=NOW - timedelta(hours=age_h))


def test_age():
    assert cluster._age(NOW - timedelta(days=3)) == "3d"
    assert cluster._age(NOW - timedelta(minutes=5)) == "5m"
    assert cluster._age(None) is None


def test_summarize_node():
    node = NS(
        metadata=meta("m1", labels={"node-role.kubernetes.io/master": "", "node-role.kubernetes.io/worker": ""}),
        spec=NS(unschedulable=None),
        status=NS(
            conditions=[NS(type="Ready", status="True"), NS(type="MemoryPressure", status="True")],
            capacity={"cpu": "8", "memory": "32Gi", "nvidia.com/gpu": "1"},
            allocatable={"cpu": "7500m", "memory": "30Gi"},
            node_info=NS(kubelet_version="v1.32", os_image="RHCOS"),
        ),
    )
    s = cluster.summarize_node(node)
    assert s["roles"] == ["master", "worker"]
    assert s["ready"] == "True"
    assert s["problems"] == ["MemoryPressure"]
    assert s["gpu"] == "1"


def _pod(phase, ready, restarts=0, waiting=None):
    state = NS(waiting=NS(reason=waiting) if waiting else None, terminated=None)
    return NS(
        metadata=meta("p", "ns"),
        spec=NS(node_name="n1"),
        status=NS(phase=phase, container_statuses=[NS(ready=ready, restart_count=restarts, state=state)]),
    )


def test_pod_problematic():
    ok = cluster.summarize_pod(_pod("Running", True))
    assert ok["ready"] == "1/1" and not cluster.pod_is_problematic(ok)
    crash = cluster.summarize_pod(_pod("Running", False, 12, "CrashLoopBackOff"))
    assert crash["reasons"] == ["CrashLoopBackOff"] and cluster.pod_is_problematic(crash)
    assert cluster.pod_is_problematic(cluster.summarize_pod(_pod("Pending", False)))
    assert not cluster.pod_is_problematic(cluster.summarize_pod(_pod("Succeeded", False)))


def test_cluster_operator():
    co = {
        "metadata": {"name": "ingress"},
        "status": {
            "versions": [{"name": "operator", "version": "4.20.1"}],
            "conditions": [
                {"type": "Available", "status": "True"},
                {"type": "Degraded", "status": "True", "message": "router pods down"},
            ],
        },
    }
    s = cluster.summarize_cluster_operator(co)
    assert s == {"name": "ingress", "version": "4.20.1", "available": "True",
                 "progressing": None, "degraded": "True", "message": "router pods down"}


def test_route_and_isvc():
    r = cluster.summarize_route({"metadata": {"name": "r", "namespace": "n"},
                                 "spec": {"host": "a.example.com", "tls": {"termination": "edge"}, "to": {"name": "svc"}}})
    assert r["url"] == "https://a.example.com"
    i = cluster.summarize_inference_service({
        "metadata": {"name": "llama", "namespace": "ai"},
        "spec": {"predictor": {"model": {"runtime": "vllm", "modelFormat": {"name": "vLLM"}}}},
        "status": {"url": "http://llama.ai.svc", "conditions": [{"type": "Ready", "status": "True"}]},
    })
    assert i["ready"] == "True" and i["runtime"] == "vllm"


def test_tools_are_registered():
    import asyncio
    from app.server import mcp
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"get_cluster_version", "list_pods", "get_pod_logs", "list_inference_services",
            "get_node_resources", "check_cluster_updates", "check_operator_updates",
            "get_project_capacity"} <= names
    assert len(names) == 14
