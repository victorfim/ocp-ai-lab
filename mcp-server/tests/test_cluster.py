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
            "describe_nodes", "check_updates", "get_project_capacity"} <= names


def test_quantities():
    assert cluster.cores("7500m") == 7.5
    assert cluster.cores("16") == 16
    assert round(cluster.cores("250000000n"), 2) == 0.25
    assert cluster.gib("32Gi") == 32
    assert cluster.gib(None) == 0
    assert cluster.instance_family("m6a.4xlarge") == "m6a"
    assert cluster.instance_family(None) is None


def test_node_capacity_and_families():
    def node(name, itype, cpu, mem):
        return NS(
            metadata=meta(name, labels={"node-role.kubernetes.io/worker": "",
                                        "node.kubernetes.io/instance-type": itype,
                                        "kubernetes.io/arch": "amd64",
                                        "topology.kubernetes.io/zone": "us-east-2a"}),
            status=NS(capacity={"cpu": cpu, "memory": mem, "pods": "250"},
                      allocatable={"cpu": "15500m", "memory": "60Gi"},
                      node_info=NS(os_image="RHCOS", kernel_version="5.14")),
        )
    a = cluster.summarize_node_capacity(node("a", "m6a.4xlarge", "16", "64Gi"),
                                        {"cpu": "3100m", "memory": "30Gi"})
    assert a["family"] == "m6a" and a["cpu_cores"]["capacity"] == 16
    assert a["usage"] == {"cpu_cores": 3.1, "cpu_percent": 20.0, "memory_gib": 30.0, "memory_percent": 50.0}
    b = cluster.summarize_node_capacity(node("b", "m6a.2xlarge", "8", "32Gi"))
    assert "usage" not in b
    fams = cluster.summarize_families([a, b])
    assert fams == [{"family": "m6a", "instance_types": ["m6a.2xlarge", "m6a.4xlarge"],
                     "nodes": 2, "cpu_cores": 24.0, "memory_gib": 96.0}]


def test_cluster_updates():
    cv = {
        "spec": {"channel": "stable-4.20"},
        "status": {
            "desired": {"version": "4.20.0"},
            "availableUpdates": [{"version": "4.20.9"}, {"version": "4.20.10"}],
            "conditionalUpdates": [{"release": {"version": "4.20.11"}, "risks": [{"name": "SomeBug"}],
                                    "conditions": [{"type": "Recommended", "status": "False"}]}],
            "conditions": [{"type": "Progressing", "status": "False"},
                           {"type": "Upgradeable", "status": "False", "message": "admin ack needed"}],
            "history": [{"version": "4.20.0", "state": "Completed"}],
        },
    }
    u = cluster.summarize_cluster_updates(cv)
    assert u["latest_available"] == "4.20.10"
    assert u["available_updates"] == ["4.20.10", "4.20.9"]
    assert u["conditional_updates"][0] == {"version": "4.20.11", "recommended": "False", "risks": ["SomeBug"]}
    assert u["upgradeable"] == "False" and not u["update_in_progress"]


def test_subscription():
    sub = {"metadata": {"name": "s", "namespace": "ops"},
           "spec": {"name": "cert-manager", "channel": "stable", "installPlanApproval": "Manual"},
           "status": {"installedCSV": "cm.v1.18", "currentCSV": "cm.v1.19",
                      "installPlanRef": {"name": "install-x"}, "state": "UpgradePending"}}
    s = cluster.summarize_subscription(sub, {"ops/install-x": ["cm.v1.19"]})
    assert s["update_available"] and s["waiting_manual_approval"] == ["cm.v1.19"]
    same = dict(sub, status={"installedCSV": "a", "currentCSV": "a", "state": "AtLatestKnown"})
    assert not cluster.summarize_subscription(same, {})["update_available"]


def test_pod_resources_and_metrics():
    c = NS(resources=NS(requests={"cpu": "500m", "memory": "1Gi"}, limits={"cpu": "1", "memory": "2Gi"}))
    pod = NS(spec=NS(containers=[c, NS(resources=None)]))
    assert cluster.container_resources(pod) == {"cpu_requests": 0.5, "cpu_limits": 1.0,
                                                "memory_requests": 1.0, "memory_limits": 2.0}
    cpu, mem = cluster.pod_metrics_usage({"containers": [{"usage": {"cpu": "250m", "memory": "512Mi"}}]})
    assert cpu == 0.25 and mem == 0.5
