from pathlib import Path
from types import SimpleNamespace

import pytest

from fournos.core.kueue import KueueClient
from fournos.handlers.lifecycle import on_create
from fournos.handlers.resolving import _resolve_hardware
from fournos.settings import settings
from fournos.state import ctx


@pytest.fixture(autouse=True)
def _clean_around_test():
    """Override integration cleanup; GPU-pool tests use a fake Kueue client."""


class FakeK8sClient:
    def __init__(self, queues=None, flavors=None, cluster_queue_name="fournos-queue"):
        self.queues = queues or []
        self.flavors = flavors or []
        self.cluster_queue_name = cluster_queue_name
        self.body = None

    def get_namespaced_custom_object(self, *, plural, **kwargs):
        assert plural == "localqueues"
        assert kwargs["namespace"] == settings.workload_namespace
        assert kwargs["name"] == settings.kueue_local_queue_name
        return {"spec": {"clusterQueue": self.cluster_queue_name}}

    def get_cluster_custom_object(self, *, plural, name, **kwargs):
        assert plural == "clusterqueues"
        return next(queue for queue in self.queues if queue["metadata"]["name"] == name)

    def list_cluster_custom_object(self, *, plural, **kwargs):
        if plural == "resourceflavors":
            return {"items": self.flavors}
        raise AssertionError(f"Unexpected Kueue resource list: {plural}")

    def create_namespaced_custom_object(self, **kwargs):
        self.body = kwargs["body"]
        return self.body


def _queue_flavor(name: str, gpu_quota: int) -> dict:
    return {
        "name": name,
        "resources": [
            {"name": "fournos/gpu-h200", "nominalQuota": gpu_quota},
            {"name": "fournos/cluster-slot", "nominalQuota": 100},
        ],
    }


def _resource_flavor(name: str, pool: str) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {
            "nodeLabels": {
                "fournos.dev/cluster": name,
                "fournos.dev/gpu-pool-h200": pool,
            }
        },
    }


def test_list_gpu_pools_uses_only_flavors_with_positive_gpu_quota() -> None:
    api = FakeK8sClient(
        queues=[
            {
                "metadata": {"name": "fournos-queue"},
                "spec": {
                    "resourceGroups": [
                        {
                            "coveredResources": [
                                "fournos/gpu-h200",
                                "fournos/cluster-slot",
                            ],
                            "flavors": [
                                _queue_flavor("cluster-a", 4),
                                _queue_flavor("cluster-b", 4),
                                _queue_flavor("cluster-disabled", 0),
                            ],
                        }
                    ]
                },
            }
        ],
        flavors=[
            _resource_flavor("cluster-a", "h200-pool"),
            _resource_flavor("cluster-b", "h200-pool"),
            _resource_flavor("cluster-disabled", "h200-pool"),
            _resource_flavor("not-in-queue", "h200-pool"),
        ],
    )

    assert KueueClient(api).list_gpu_pools() == {"h200": {"h200-pool"}}


def test_pool_in_an_unrelated_cluster_queue_is_not_available() -> None:
    api = FakeK8sClient(
        queues=[
            {
                "metadata": {"name": "fournos-queue"},
                "spec": {"resourceGroups": []},
            },
            {
                "metadata": {"name": "other-queue"},
                "spec": {
                    "resourceGroups": [
                        {
                            "coveredResources": [
                                "fournos/gpu-h200",
                                "fournos/cluster-slot",
                            ],
                            "flavors": [_queue_flavor("cluster-a", 8)],
                        }
                    ]
                },
            },
        ],
        flavors=[_resource_flavor("cluster-a", "h200-pool")],
    )

    assert KueueClient(api).list_gpu_pools() == {}


def test_pool_lookup_rejects_local_queue_without_cluster_queue() -> None:
    api = FakeK8sClient(cluster_queue_name="")

    with pytest.raises(ValueError, match="has no ClusterQueue"):
        KueueClient(api).list_gpu_pools()


def test_pool_requires_positive_slot_quota_in_the_gpu_resource_group() -> None:
    flavor = _queue_flavor("cluster-a", 8)
    flavor["resources"][1]["nominalQuota"] = 0
    api = FakeK8sClient(
        queues=[
            {
                "metadata": {"name": "fournos-queue"},
                "spec": {
                    "resourceGroups": [
                        {
                            "coveredResources": [
                                "fournos/gpu-h200",
                                "fournos/cluster-slot",
                            ],
                            "flavors": [flavor],
                        }
                    ]
                },
            }
        ],
        flavors=[_resource_flavor("cluster-a", "h200-pool")],
    )

    assert KueueClient(api).list_gpu_pools() == {}


def test_pool_rejects_gpu_and_slot_quotas_in_separate_groups() -> None:
    api = FakeK8sClient(
        queues=[
            {
                "metadata": {"name": "fournos-queue"},
                "spec": {
                    "resourceGroups": [
                        {
                            "coveredResources": ["fournos/gpu-h200"],
                            "flavors": [_queue_flavor("cluster-a", 8)],
                        },
                        {
                            "coveredResources": ["fournos/cluster-slot"],
                            "flavors": [_queue_flavor("cluster-a", 8)],
                        },
                    ]
                },
            }
        ],
        flavors=[_resource_flavor("cluster-a", "h200-pool")],
    )

    assert KueueClient(api).list_gpu_pools() == {}


def test_gpu_pool_workload_keeps_full_gpu_request_on_one_podset() -> None:
    api = FakeK8sClient()

    KueueClient(api).create_workload(
        name="pool-job",
        gpu_type="h200",
        gpu_count=8,
        gpu_pool="h200-pool",
        exclusive=False,
    )

    pod_sets = api.body["spec"]["podSets"]
    assert len(pod_sets) == 1
    assert pod_sets[0]["count"] == 1
    pod_spec = pod_sets[0]["template"]["spec"]
    assert pod_spec["nodeSelector"] == {"fournos.dev/gpu-pool-h200": "h200-pool"}
    requests = pod_spec["containers"][0]["resources"]["requests"]
    assert requests["fournos/gpu-h200"] == "8"
    assert "fournos.dev/cluster" not in pod_spec["nodeSelector"]


def test_gpu_pool_workload_requires_gpu_hardware() -> None:
    api = FakeK8sClient()

    with pytest.raises(ValueError, match="gpu_pool requires a GPU type"):
        KueueClient(api).create_workload(
            name="pool-job", gpu_pool="h200-pool", exclusive=False
        )

    assert api.body is None


def test_gpu_pool_and_specific_cluster_are_mutually_exclusive() -> None:
    api = FakeK8sClient()

    with pytest.raises(ValueError, match="mutually exclusive"):
        KueueClient(api).create_workload(
            name="pool-job",
            gpu_type="h200",
            gpu_count=8,
            cluster="cluster-a",
            gpu_pool="h200-pool",
        )

    assert api.body is None


def test_exclusive_gpu_pool_job_enters_resolving_without_pinning_a_cluster(
    monkeypatch,
) -> None:
    monkeypatch.setattr(ctx, "kueue", SimpleNamespace())
    patch = SimpleNamespace(status={}, meta={})
    spec = {
        "exclusive": True,
        "gpuPool": "h200-pool",
        "executionEngine": {"forge": {"project": "test"}},
    }

    on_create(spec, "pool-job", "fournos", {}, patch, {"metadata": {}})

    assert patch.status["phase"] == "Resolving"
    assert "fournos.dev/exclusive-cluster" not in patch.meta.get("labels", {})


def test_resolve_hardware_accepts_a_pool_with_matching_gpu_type(monkeypatch) -> None:
    monkeypatch.setattr(
        ctx,
        "kueue",
        SimpleNamespace(
            list_gpu_types=lambda: {"h200"},
            list_gpu_pools=lambda: {"h200": {"h200-pool"}},
        ),
    )
    patch = SimpleNamespace(status={})
    spec = {
        "exclusive": True,
        "gpuPool": "h200-pool",
        "hardware": {"gpuType": "h200", "gpuCount": 8},
    }

    assert _resolve_hardware(spec, "pool-job", [], patch) == ("h200", 8)
    assert patch.status == {}


def test_resolve_hardware_rejects_pool_without_requested_gpu_quota(monkeypatch) -> None:
    monkeypatch.setattr(
        ctx,
        "kueue",
        SimpleNamespace(
            list_gpu_types=lambda: {"h200"},
            list_gpu_pools=lambda: {"h200": {"other-pool"}},
        ),
    )
    patch = SimpleNamespace(status={})
    spec = {
        "exclusive": True,
        "gpuPool": "h200-pool",
        "hardware": {"gpuType": "h200", "gpuCount": 8},
    }

    assert _resolve_hardware(spec, "pool-job", [], patch) == (None, None)
    assert patch.status["phase"] == "Failed"
    assert "has no configured 'h200' quota" in patch.status["message"]


def test_sample_cluster_queue_keeps_gpu_and_slot_quotas_per_flavor() -> None:
    import yaml

    config_path = Path(__file__).parents[1] / "config" / "kueue-cluster-config.yaml"
    documents = list(yaml.safe_load_all(config_path.read_text()))
    queue = next(doc for doc in documents if doc and doc.get("kind") == "ClusterQueue")

    resource_groups = queue["spec"]["resourceGroups"]
    assert len(resource_groups) == 1
    resource_group = resource_groups[0]
    assert "fournos/gpu-h200" in resource_group["coveredResources"]
    assert "fournos/cluster-slot" in resource_group["coveredResources"]
    h200_quotas = {
        flavor["name"]: next(
            resource["nominalQuota"]
            for resource in flavor["resources"]
            if resource["name"] == "fournos/gpu-h200"
        )
        for flavor in resource_group["flavors"]
        if any(
            resource["name"] == "fournos/gpu-h200" for resource in flavor["resources"]
        )
    }
    assert h200_quotas == {
        "cluster-1": 0,
        "cluster-2": 0,
        "cluster-3": 8,
        "cluster-4": 8,
    }
