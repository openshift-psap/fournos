from app import k8s_client
from app.k8s_client import _gpu_pools_from_kueue


def test_gpu_pools_only_include_queued_flavors_with_positive_quota() -> None:
    cluster_queue = {
        "spec": {
            "resourceGroups": [
                {
                    "coveredResources": ["fournos/gpu-h200", "fournos/cluster-slot"],
                    "flavors": [
                        {
                            "name": "hera",
                            "resources": [
                                {"name": "fournos/gpu-h200", "nominalQuota": 8},
                                {"name": "fournos/cluster-slot", "nominalQuota": 100},
                            ],
                        },
                        {
                            "name": "old-zeus",
                            "resources": [
                                {"name": "fournos/gpu-h200", "nominalQuota": 0},
                                {"name": "fournos/cluster-slot", "nominalQuota": 100},
                            ],
                        },
                    ],
                }
            ]
        }
    }
    resource_flavors = [
        {
            "metadata": {"name": "hera"},
            "spec": {"nodeLabels": {"fournos.dev/gpu-pool-h200": "h200-pool"}},
        },
        {
            "metadata": {"name": "old-zeus"},
            "spec": {"nodeLabels": {"fournos.dev/gpu-pool-h200": "h200-pool"}},
        },
        {
            "metadata": {"name": "unconfigured"},
            "spec": {"nodeLabels": {"fournos.dev/gpu-pool-h200": "h200-pool"}},
        },
    ]

    assert _gpu_pools_from_kueue(cluster_queue, resource_flavors) == [
        {"gpu_type": "h200", "name": "h200-pool"}
    ]


def test_gpu_pool_choices_require_slot_quota_in_the_same_group() -> None:
    queue = {
        "spec": {
            "resourceGroups": [
                {
                    "coveredResources": ["fournos/gpu-h200", "fournos/cluster-slot"],
                    "flavors": [
                        {
                            "name": "hera",
                            "resources": [
                                {"name": "fournos/gpu-h200", "nominalQuota": 8},
                                {"name": "fournos/cluster-slot", "nominalQuota": 0},
                            ],
                        }
                    ],
                }
            ]
        }
    }
    flavors = [
        {
            "metadata": {"name": "hera"},
            "spec": {"nodeLabels": {"fournos.dev/gpu-pool-h200": "h200-pool"}},
        }
    ]

    assert _gpu_pools_from_kueue(queue, flavors) == []


def test_gpu_pool_discovery_uses_the_dashboard_local_queue(monkeypatch) -> None:
    class FakeApi:
        def get_namespaced_custom_object(self, *, namespace, plural, name, **kwargs):
            assert namespace == k8s_client.settings.fournos_namespace
            assert plural == "localqueues"
            assert name == k8s_client.settings.kueue_local_queue_name
            return {"spec": {"clusterQueue": "dashboard-queue"}}

        def get_cluster_custom_object(self, *, plural, name, **kwargs):
            assert plural == "clusterqueues"
            assert name == "dashboard-queue"
            return {
                "spec": {
                    "resourceGroups": [
                        {
                            "coveredResources": [
                                "fournos/gpu-h200",
                                "fournos/cluster-slot",
                            ],
                            "flavors": [
                                {
                                    "name": "zeus",
                                    "resources": [
                                        {"name": "fournos/gpu-h200", "nominalQuota": 8},
                                        {
                                            "name": "fournos/cluster-slot",
                                            "nominalQuota": 100,
                                        },
                                    ],
                                }
                            ],
                        }
                    ]
                }
            }

        def list_cluster_custom_object(self, *, plural, **kwargs):
            assert plural == "resourceflavors"
            return {
                "items": [
                    {
                        "metadata": {"name": "zeus"},
                        "spec": {
                            "nodeLabels": {
                                "fournos.dev/gpu-pool-h200": "rhaiis-cpt-pool"
                            }
                        },
                    }
                ]
            }

    monkeypatch.setattr(k8s_client, "_ensure_loaded", lambda: None)
    monkeypatch.setattr(k8s_client, "_custom_api", FakeApi())

    assert k8s_client.list_gpu_pools() == [
        {"gpu_type": "h200", "name": "rhaiis-cpt-pool"}
    ]
