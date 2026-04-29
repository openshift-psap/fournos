"""Tests for PSAPCluster manager."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

from fournos.core.psapcluster import PSAPClusterManager


class TestClusterQueueName:

    def test_prefix(self) -> None:
        assert PSAPClusterManager.cluster_queue_name("cluster-1") == "fournos-cluster-1"

    def test_mgmt(self) -> None:
        assert PSAPClusterManager.cluster_queue_name("psap-mgmt") == "fournos-psap-mgmt"


class TestEnsureResourceFlavor:

    def _make_manager(self) -> tuple[PSAPClusterManager, MagicMock]:
        mock_k8s = MagicMock()
        return PSAPClusterManager(mock_k8s), mock_k8s

    def test_creates_flavor(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.create_cluster_custom_object.return_value = {"metadata": {"name": "cluster-1"}}

        result = mgr.ensure_resource_flavor("cluster-1")

        assert result["metadata"]["name"] == "cluster-1"
        body = mock_k8s.create_cluster_custom_object.call_args[1]["body"]
        assert body["metadata"]["name"] == "cluster-1"
        assert body["spec"]["nodeLabels"]["fournos.dev/cluster"] == "cluster-1"
        assert body["metadata"]["labels"]["fournos.dev/psapcluster"] == "cluster-1"

    def test_idempotent_on_conflict(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.create_cluster_custom_object.side_effect = ApiException(status=409)
        mock_k8s.get_cluster_custom_object.return_value = {"metadata": {"name": "cluster-1"}}

        result = mgr.ensure_resource_flavor("cluster-1")

        assert result["metadata"]["name"] == "cluster-1"
        mock_k8s.get_cluster_custom_object.assert_called_once()

    def test_other_error_raises(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.create_cluster_custom_object.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            mgr.ensure_resource_flavor("cluster-1")


class TestEnsureClusterQueue:

    def _make_manager(self) -> tuple[PSAPClusterManager, MagicMock]:
        mock_k8s = MagicMock()
        return PSAPClusterManager(mock_k8s), mock_k8s

    def test_creates_cq_with_gpu_resources(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.create_cluster_custom_object.return_value = {
            "metadata": {"name": "fournos-cluster-1"}
        }

        mgr.ensure_cluster_queue("cluster-1", gpu_resources=[("a100", 8)])

        body = mock_k8s.create_cluster_custom_object.call_args[1]["body"]
        assert body["metadata"]["name"] == "fournos-cluster-1"
        assert body["spec"]["cohort"] == "fournos"
        assert body["spec"]["stopPolicy"] == "None"
        assert body["spec"]["namespaceSelector"]["matchLabels"]["fournos.dev/queue-access"] == "true"

        flavors = body["spec"]["resourceGroups"][0]["flavors"]
        assert len(flavors) == 1
        assert flavors[0]["name"] == "cluster-1"

        resources = {r["name"]: r["nominalQuota"] for r in flavors[0]["resources"]}
        assert resources["fournos/gpu-a100"] == 8
        assert resources["fournos/cluster-slot"] == 100

    def test_creates_cq_without_gpu(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.create_cluster_custom_object.return_value = {
            "metadata": {"name": "fournos-cluster-1"}
        }

        mgr.ensure_cluster_queue("cluster-1")

        body = mock_k8s.create_cluster_custom_object.call_args[1]["body"]
        flavors = body["spec"]["resourceGroups"][0]["flavors"]
        resources = {r["name"]: r["nominalQuota"] for r in flavors[0]["resources"]}
        assert "fournos/cluster-slot" in resources
        assert len(resources) == 1

    def test_creates_cq_with_stop_policy(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.create_cluster_custom_object.return_value = {}

        mgr.ensure_cluster_queue("cluster-1", stop_policy="Hold")

        body = mock_k8s.create_cluster_custom_object.call_args[1]["body"]
        assert body["spec"]["stopPolicy"] == "Hold"

    def test_idempotent_on_conflict(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.create_cluster_custom_object.side_effect = ApiException(status=409)
        mock_k8s.get_cluster_custom_object.return_value = {
            "metadata": {"name": "fournos-cluster-1"}
        }

        result = mgr.ensure_cluster_queue("cluster-1")
        assert result["metadata"]["name"] == "fournos-cluster-1"


class TestEnsureLocalQueue:

    def _make_manager(self) -> tuple[PSAPClusterManager, MagicMock]:
        mock_k8s = MagicMock()
        return PSAPClusterManager(mock_k8s), mock_k8s

    def test_creates_local_queue(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "fournos-cluster-1"}
        }

        mgr.ensure_local_queue("cluster-1", "psap-automation")

        body = mock_k8s.create_namespaced_custom_object.call_args[1]["body"]
        assert body["metadata"]["name"] == "fournos-cluster-1"
        assert body["metadata"]["namespace"] == "psap-automation"
        assert body["spec"]["clusterQueue"] == "fournos-cluster-1"

    def test_idempotent_on_conflict(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.create_namespaced_custom_object.side_effect = ApiException(status=409)
        mock_k8s.get_namespaced_custom_object.return_value = {}

        mgr.ensure_local_queue("cluster-1", "psap-automation")
        mock_k8s.get_namespaced_custom_object.assert_called_once()


class TestSetStopPolicy:

    def _make_manager(self) -> tuple[PSAPClusterManager, MagicMock]:
        mock_k8s = MagicMock()
        return PSAPClusterManager(mock_k8s), mock_k8s

    def test_set_hold(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.patch_cluster_custom_object.return_value = {}

        mgr.set_cluster_queue_stop_policy("cluster-1", "Hold")

        mock_k8s.patch_cluster_custom_object.assert_called_once()
        body = mock_k8s.patch_cluster_custom_object.call_args[1]["body"]
        assert body["spec"]["stopPolicy"] == "Hold"
        assert mock_k8s.patch_cluster_custom_object.call_args[1]["name"] == "fournos-cluster-1"

    def test_set_hold_and_drain(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.patch_cluster_custom_object.return_value = {}

        mgr.set_cluster_queue_stop_policy("cluster-1", "HoldAndDrain")

        body = mock_k8s.patch_cluster_custom_object.call_args[1]["body"]
        assert body["spec"]["stopPolicy"] == "HoldAndDrain"

    def test_set_none(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.patch_cluster_custom_object.return_value = {}

        mgr.set_cluster_queue_stop_policy("cluster-1", "None")

        body = mock_k8s.patch_cluster_custom_object.call_args[1]["body"]
        assert body["spec"]["stopPolicy"] == "None"


class TestUpdateClusterQueueQuotas:

    def _make_manager(self) -> tuple[PSAPClusterManager, MagicMock]:
        mock_k8s = MagicMock()
        return PSAPClusterManager(mock_k8s), mock_k8s

    def test_updates_quotas(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.patch_cluster_custom_object.return_value = {}

        mgr.update_cluster_queue_quotas("cluster-1", [("a100", 8), ("h200", 4)])

        body = mock_k8s.patch_cluster_custom_object.call_args[1]["body"]
        flavors = body["spec"]["resourceGroups"][0]["flavors"]
        resources = {r["name"]: r["nominalQuota"] for r in flavors[0]["resources"]}
        assert resources["fournos/gpu-a100"] == 8
        assert resources["fournos/gpu-h200"] == 4
        assert resources["fournos/cluster-slot"] == 100


class TestGetClusterQueueOrNone:

    def _make_manager(self) -> tuple[PSAPClusterManager, MagicMock]:
        mock_k8s = MagicMock()
        return PSAPClusterManager(mock_k8s), mock_k8s

    def test_returns_cq(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.get_cluster_custom_object.return_value = {"metadata": {"name": "fournos-cluster-1"}}

        result = mgr.get_cluster_queue_or_none("cluster-1")
        assert result is not None

    def test_returns_none_on_404(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.get_cluster_custom_object.side_effect = ApiException(status=404)

        result = mgr.get_cluster_queue_or_none("cluster-1")
        assert result is None

    def test_raises_on_other_error(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.get_cluster_custom_object.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            mgr.get_cluster_queue_or_none("cluster-1")


class TestGetResourceFlavorOrNone:

    def _make_manager(self) -> tuple[PSAPClusterManager, MagicMock]:
        mock_k8s = MagicMock()
        return PSAPClusterManager(mock_k8s), mock_k8s

    def test_returns_flavor(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.get_cluster_custom_object.return_value = {"metadata": {"name": "cluster-1"}}

        result = mgr.get_resource_flavor_or_none("cluster-1")
        assert result is not None

    def test_returns_none_on_404(self) -> None:
        mgr, mock_k8s = self._make_manager()
        mock_k8s.get_cluster_custom_object.side_effect = ApiException(status=404)

        result = mgr.get_resource_flavor_or_none("cluster-1")
        assert result is None
