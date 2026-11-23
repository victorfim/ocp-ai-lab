from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from kubernetes.client.rest import ApiException

from app import cluster, server

NOW = datetime.now(timezone.utc)


def test_parse_quantity():
    assert cluster.parse_quantity("250m") == 0.25
    assert cluster.parse_quantity("2") == 2
    assert cluster.parse_quantity("1500000n") == pytest.approx(0.0015)
    assert cluster.parse_quantity("1Gi") == 2**30
    assert cluster.parse_quantity("16374812Ki") == 16374812 * 1024
    assert cluster.parse_quantity("1G") == 1e9
    assert cluster.parse_quantity(None) == 0
    assert cluster.fmt_bytes(1.5 * 2**30) == "1.5 GiB"


@pytest.mark.parametrize("itype,family", [
    ("m6i.2xlarge", "m6i"),
    ("Standard_D8s_v5", "Ds_v5"),
    ("n2-standard-8", "n2-standard"),
    ("bx2-8x32", "bx2-8x32"),
    (None, None),
])
def test_instance_family(itype, family):
    assert cluster.instance_family(itype) == family


def test_summarize_updates():
    cv = {
        "spec": {"channel": "stable-4.20"},
        "status": {
            "desired": {"version": "4.20.0"},
            "availableUpdates": [{"version": "4.20.10"}, {"version": "4.20.46"}, {"version": "4.20.9"}],
            "conditionalUpdates": [
                {"release": {"version": "4.20.40"}, "risks": [{"name": "SomeBug"}],
                 "conditions": [{"type": "Recommended", "status": "False"}]},
            ],
            "conditions": [{"type": "Upgradeable", "status": "False", "message": "admin ack required"},
                           {"type": "Progressing", "status": "False"}],
            "history": [{"version": "4.20.0", "state": "Completed", "completionTime": "x"}],
        },
    }
    s = cluster.summarize_updates(cv)
    assert s["available_updates"] == ["4.20.9", "4.20.10", "4.20.46"]
    assert s["latest_available"] == "4.20.46"
    assert s["latest_patch_same_minor"] == "4.20.46"
    assert s["conditional_updates_with_risks"][0]["risks"] == ["SomeBug"]
    assert s["minor_upgrade_blocked"] and s["minor_upgrade_blocked_reason"] == "admin ack required"
    assert s["update_in_progress"] is False


def test_summarize_subscription():
    sub = {"metadata": {"name": "rhods", "namespace": "redhat-ods-operator"},
           "spec": {"name": "rhods-operator", "channel": "fast", "installPlanApproval": "Manual"},
           "status": {"installedCSV": "rhods.3.5.0", "currentCSV": "rhods.3.5.1", "state": "UpgradePending"}}
    s = cluster.summarize_subscription(sub)
    assert s["update_pending"] and s["needs_manual_approval"]


def _container(cpu=None, mem=None, lcpu=None, lmem=None):
    req = {k: v for k, v in (("cpu", cpu), ("memory", mem)) if v}
    lim = {k: v for k, v in (("cpu", lcpu), ("memory", lmem)) if v}
    return NS(resources=NS(requests=req, limits=lim))


def _pod(ns, node, containers, phase="Running", init=None):
    return NS(metadata=NS(namespace=ns, name="p", creation_timestamp=NOW),
              spec=NS(node_name=node, containers=containers, init_containers=init),
              status=NS(phase=phase, container_statuses=[]))


def test_pod_requests_uses_init_max():
    p = _pod("a", "n1", [_container("100m", "64Mi"), _container("200m", "64Mi", "1", "1Gi")],
             init=[_container("500m", "32Mi")])
    cpu, mem, lcpu, lmem = cluster.pod_requests(p)
    assert cpu == 0.5 and mem == 128 * 2**20 and lcpu == 1 and lmem == 2**30


class FakeCore:
    def __init__(self, pods, pvcs=(), quotas=(), nodes=()):
        self._pods, self._pvcs, self._quotas, self._nodes = list(pods), list(pvcs), list(quotas), list(nodes)

    def list_pod_for_all_namespaces(self, **kw):
        return NS(items=self._pods)

    def list_namespaced_pod(self, ns):
        return NS(items=[p for p in self._pods if p.metadata.namespace == ns])

    def list_persistent_volume_claim_for_all_namespaces(self):
        return NS(items=self._pvcs)

    def list_namespaced_persistent_volume_claim(self, ns):
        return NS(items=[p for p in self._pvcs if p.metadata.namespace == ns])

    def list_resource_quota_for_all_namespaces(self):
        return NS(items=self._quotas)

    def list_namespaced_resource_quota(self, ns):
        return NS(items=[q for q in self._quotas if q.metadata.namespace == ns])

    def list_node(self):
        return NS(items=self._nodes)


class FakeCustom:
    def __init__(self, pod_metrics=None, node_metrics=None):
        self.pm, self.nm = pod_metrics, node_metrics

    def list_cluster_custom_object(self, group, version, plural):
        data = self.pm if plural == "pods" else self.nm
        if data is None:
            raise ApiException(status=404, reason="Not Found")
        return {"items": data}

    def list_namespaced_custom_object(self, group, version, ns, plural):
        return {"items": [i for i in self.pm if i["metadata"]["namespace"] == ns]}


def test_get_project_capacity(monkeypatch):
    pods = [
        _pod("app-a", "n1", [_container("500m", "1Gi", "1", "2Gi")]),
        _pod("app-b", "n1", [_container("100m", "256Mi")]),
        _pod("openshift-dns", "n1", [_container("50m", "64Mi")]),
        _pod("app-a", "n1", [_container("4", "8Gi")], phase="Succeeded"),
    ]
    pvcs = [NS(metadata=NS(namespace="app-a"), spec=NS(resources=NS(requests={"storage": "10Gi"})))]
    quotas = [NS(metadata=NS(namespace="app-a", name="q"),
                 status=NS(hard={"requests.cpu": "2"}, used={"requests.cpu": "500m"}))]
    pm = [{"metadata": {"namespace": "app-a"}, "containers": [{"usage": {"cpu": "250000000n", "memory": "512Mi"}}]}]
    monkeypatch.setattr(cluster, "core", lambda: FakeCore(pods, pvcs, quotas))
    monkeypatch.setattr(cluster, "custom", lambda: FakeCustom(pod_metrics=pm))
    monkeypatch.setattr(cluster, "prometheus_by_label",
                        lambda q, l: {"app-a": 5 * 2**30} if "used" in q else {"app-a": 10 * 2**30})

    r = server.get_project_capacity()
    names = [i["namespace"] for i in r["items"]]
    assert names == ["app-a", "app-b"]  # system ns hidden, sorted by cpu
    a = r["items"][0]
    assert a["pods_running"] == 1
    assert a["cpu"] == {"used_cores": 0.25, "requested_cores": 0.5, "limit_cores": 1.0}
    assert a["memory"]["used"] == "512.0 MiB"
    assert a["disk"] == {"pvcs": 1, "requested": "10.0 GiB", "used": "5.0 GiB", "used_pct": 50.0}
    assert a["quotas"][0]["limits"]["requests.cpu"] == {"used": "500m", "hard": "2"}
    assert r["items"][1]["cpu"]["used_cores"] is None


def test_get_project_capacity_without_monitoring(monkeypatch):
    pvcs = [NS(metadata=NS(namespace="x"), spec=NS(resources=NS(requests={"storage": "1Gi"})))]
    monkeypatch.setattr(cluster, "core", lambda: FakeCore([], pvcs))
    monkeypatch.setattr(cluster, "custom", lambda: FakeCustom(pod_metrics=None))
    monkeypatch.setattr(cluster, "prometheus_by_label", lambda q, l: None)
    r = server.get_project_capacity()
    assert r["items"][0]["disk"]["used"] is None
    assert "indisponível" in r["sources"]["disk_usage"]
    assert "indisponível" in r["sources"]["cpu_memory_usage"]


def test_get_node_resources(monkeypatch):
    node = NS(
        metadata=NS(name="w1", labels={"node-role.kubernetes.io/worker": "",
                                       "node.kubernetes.io/instance-type": "m6i.2xlarge",
                                       "topology.kubernetes.io/zone": "us-east-2a"},
                    creation_timestamp=NOW),
        spec=NS(unschedulable=None),
        status=NS(conditions=[NS(type="Ready", status="True")],
                  capacity={"cpu": "8", "memory": "32Gi", "pods": "250", "ephemeral-storage": "100Gi"},
                  allocatable={"cpu": "7500m", "memory": "30Gi", "pods": "250"},
                  node_info=NS(kubelet_version="v1.33", os_image="RHCOS", architecture="amd64")),
    )
    pods = [_pod("a", "w1", [_container("750m", "3Gi")])]
    monkeypatch.setattr(cluster, "core", lambda: FakeCore(pods, nodes=[node]))
    monkeypatch.setattr(cluster, "custom", lambda: FakeCustom(
        node_metrics=[{"metadata": {"name": "w1"}, "usage": {"cpu": "1500m", "memory": "6Gi"}}]))
    r = server.get_node_resources()
    n = r["nodes"][0]
    assert n["instance_family"] == "m6i" and n["zone"] == "us-east-2a"
    assert n["cpu"]["requested_pct"] == 10.0 and n["cpu"]["used_pct"] == 20.0
    assert n["memory"]["requested_pct"] == 10.0 and n["memory"]["used"] == "6.0 GiB"
    assert r["by_instance_family"][0] == {"family": "m6i", "nodes": 1, "instance_types": ["m6i.2xlarge"],
                                          "cpu_cores": 8.0, "memory": "32.0 GiB"}
    assert r["totals"]["cpu_requested_pct"] == 10.0
