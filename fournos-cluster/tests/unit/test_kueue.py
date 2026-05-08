"""Tests for KueueClient flavor management."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

from fournos_cluster.core.kueue import KueueClient


class TestCreateFlavor:

    def test_creates_with_correct_spec(self) -> None:
        mock_custom = MagicMock()
        kueue = KueueClient(mock_custom)

        kueue.create_flavor("my-cluster")

        mock_custom.create_cluster_custom_object.assert_called_once()
        body = mock_custom.create_cluster_custom_object.call_args[1]["body"]
        assert body["metadata"]["name"] == "my-cluster"
        assert body["spec"]["nodeLabels"] == {"fournos.dev/cluster": "my-cluster"}

    def test_already_exists_returns_none(self) -> None:
        mock_custom = MagicMock()
        mock_custom.create_cluster_custom_object.side_effect = ApiException(status=409)
        kueue = KueueClient(mock_custom)

        result = kueue.create_flavor("my-cluster")

        assert result is None

    def test_api_error_propagates(self) -> None:
        mock_custom = MagicMock()
        mock_custom.create_cluster_custom_object.side_effect = ApiException(status=500)
        kueue = KueueClient(mock_custom)

        with pytest.raises(ApiException):
            kueue.create_flavor("my-cluster")


class TestAddFlavorToClusterQueue:

    def _make_cq(self, existing_flavors: list[dict] | None = None) -> dict:
        return {
            "spec": {
                "resourceGroups": [
                    {
                        "coveredResources": [
                            "fournos/cluster-slot",
                            "fournos/gpu-a100",
                        ],
                        "flavors": existing_flavors or [
                            {
                                "name": "existing-cluster",
                                "resources": [
                                    {"name": "fournos/cluster-slot", "nominalQuota": 100},
                                    {"name": "fournos/gpu-a100", "nominalQuota": 8},
                                ],
                            }
                        ],
                    }
                ]
            }
        }

    def test_adds_new_flavor_entry(self) -> None:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.return_value = self._make_cq()
        kueue = KueueClient(mock_custom)

        kueue.add_flavor_to_cluster_queue("new-cluster")

        mock_custom.patch_cluster_custom_object.assert_called_once()
        body = mock_custom.patch_cluster_custom_object.call_args[1]["body"]
        flavors = body["spec"]["resourceGroups"][0]["flavors"]
        assert len(flavors) == 2
        new_flavor = flavors[1]
        assert new_flavor["name"] == "new-cluster"
        resources = {r["name"]: r["nominalQuota"] for r in new_flavor["resources"]}
        assert resources["fournos/cluster-slot"] == 100
        assert resources["fournos/gpu-a100"] == 0

    def test_flavor_already_in_cq(self) -> None:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.return_value = self._make_cq()
        kueue = KueueClient(mock_custom)

        result = kueue.add_flavor_to_cluster_queue("existing-cluster")

        assert result is None
        mock_custom.patch_cluster_custom_object.assert_not_called()

    def test_cq_not_found(self) -> None:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.side_effect = ApiException(status=404)
        kueue = KueueClient(mock_custom)

        result = kueue.add_flavor_to_cluster_queue("new-cluster")

        assert result is None

    def test_preserves_existing_flavors(self) -> None:
        existing = [
            {
                "name": "cluster-a",
                "resources": [
                    {"name": "fournos/cluster-slot", "nominalQuota": 100},
                    {"name": "fournos/gpu-a100", "nominalQuota": 4},
                ],
            },
            {
                "name": "cluster-b",
                "resources": [
                    {"name": "fournos/cluster-slot", "nominalQuota": 100},
                    {"name": "fournos/gpu-h100", "nominalQuota": 8},
                ],
            },
        ]
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.return_value = self._make_cq(existing)
        kueue = KueueClient(mock_custom)

        kueue.add_flavor_to_cluster_queue("cluster-c")

        body = mock_custom.patch_cluster_custom_object.call_args[1]["body"]
        flavors = body["spec"]["resourceGroups"][0]["flavors"]
        assert len(flavors) == 3
        assert flavors[0]["name"] == "cluster-a"
        assert flavors[1]["name"] == "cluster-b"


class TestUpdateFlavorQuotas:

    def _make_cq_with_flavor(self, flavor_name: str) -> dict:
        return {
            "spec": {
                "resourceGroups": [
                    {
                        "coveredResources": ["fournos/cluster-slot"],
                        "flavors": [
                            {
                                "name": flavor_name,
                                "resources": [
                                    {"name": "fournos/cluster-slot", "nominalQuota": 100},
                                ],
                            }
                        ],
                    }
                ]
            }
        }

    def test_updates_gpu_quotas(self) -> None:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.return_value = self._make_cq_with_flavor("cluster-1")
        kueue = KueueClient(mock_custom)

        kueue.update_flavor_quotas("cluster-1", [("a100", 8)])

        mock_custom.patch_cluster_custom_object.assert_called_once()
        body = mock_custom.patch_cluster_custom_object.call_args[1]["body"]
        rg = body["spec"]["resourceGroups"][0]
        assert "fournos/gpu-a100" in rg["coveredResources"]

        flavor = rg["flavors"][0]
        resources = {r["name"]: r["nominalQuota"] for r in flavor["resources"]}
        assert resources["fournos/gpu-a100"] == 8
        assert resources["fournos/cluster-slot"] == 100

    def test_flavor_not_found(self) -> None:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.return_value = self._make_cq_with_flavor("other")
        kueue = KueueClient(mock_custom)

        result = kueue.update_flavor_quotas("missing", [("a100", 8)])

        assert result is None
        mock_custom.patch_cluster_custom_object.assert_not_called()

    def test_cq_not_found(self) -> None:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.side_effect = ApiException(status=404)
        kueue = KueueClient(mock_custom)

        result = kueue.update_flavor_quotas("cluster-1", [("a100", 8)])

        assert result is None


class TestListFlavors:

    def test_returns_flavor_names(self) -> None:
        mock_custom = MagicMock()
        mock_custom.list_cluster_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "cluster-a"}},
                {"metadata": {"name": "cluster-b"}},
            ]
        }
        kueue = KueueClient(mock_custom)

        result = kueue.list_flavors()

        assert result == {"cluster-a", "cluster-b"}

    def test_empty(self) -> None:
        mock_custom = MagicMock()
        mock_custom.list_cluster_custom_object.return_value = {"items": []}
        kueue = KueueClient(mock_custom)

        assert kueue.list_flavors() == set()
