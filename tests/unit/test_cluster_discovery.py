"""Tests for cluster auto-discovery."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from kubernetes.client.exceptions import ApiException

from fournos.core.clusters import (
    _build_cluster_name_regex,
    extract_cluster_name,
    list_kubeconfig_secrets,
)
from fournos.core.discovery import ClusterDiscovery


# ---------------------------------------------------------------------------
# extract_cluster_name
# ---------------------------------------------------------------------------


class TestExtractClusterName:

    def test_default_pattern(self) -> None:
        assert extract_cluster_name("kubeconfig-cluster-1") == "cluster-1"

    def test_no_match(self) -> None:
        assert extract_cluster_name("other-secret") is None

    def test_cluster_name_with_dashes(self) -> None:
        assert extract_cluster_name("kubeconfig-my-gpu-cluster") == "my-gpu-cluster"

    def test_exact_prefix_no_cluster(self) -> None:
        assert extract_cluster_name("kubeconfig-") is None

    def test_unrelated_secret(self) -> None:
        assert extract_cluster_name("vault-my-credentials") is None


class TestBuildClusterNameRegex:

    def test_default_pattern(self) -> None:
        pattern = _build_cluster_name_regex("kubeconfig-{cluster}")
        assert pattern.match("kubeconfig-foo").group("cluster") == "foo"
        assert pattern.match("kubeconfig-a-b-c").group("cluster") == "a-b-c"
        assert pattern.match("other-foo") is None

    def test_custom_pattern(self) -> None:
        pattern = _build_cluster_name_regex("kc.{cluster}.secret")
        assert pattern.match("kc.my-cluster.secret").group("cluster") == "my-cluster"
        assert pattern.match("kc..secret") is None


# ---------------------------------------------------------------------------
# list_kubeconfig_secrets
# ---------------------------------------------------------------------------


class TestListKubeconfigSecrets:

    def test_returns_matching_secrets(self) -> None:
        mock_k8s = MagicMock()
        s1 = MagicMock()
        s1.metadata.name = "kubeconfig-cluster-1"
        s2 = MagicMock()
        s2.metadata.name = "vault-my-creds"
        s3 = MagicMock()
        s3.metadata.name = "kubeconfig-cluster-2"
        mock_k8s.list_namespaced_secret.return_value.items = [s1, s2, s3]

        result = list_kubeconfig_secrets(mock_k8s)

        assert result == ["kubeconfig-cluster-1", "kubeconfig-cluster-2"]

    def test_empty_namespace(self) -> None:
        mock_k8s = MagicMock()
        mock_k8s.list_namespaced_secret.return_value.items = []

        result = list_kubeconfig_secrets(mock_k8s)

        assert result == []


# ---------------------------------------------------------------------------
# KueueClient.create_flavor
# ---------------------------------------------------------------------------


class TestCreateFlavor:

    def test_creates_with_correct_spec(self) -> None:
        from fournos.core.kueue import KueueClient

        mock_custom = MagicMock()
        kueue = KueueClient(mock_custom)

        kueue.create_flavor("my-cluster")

        mock_custom.create_cluster_custom_object.assert_called_once()
        body = mock_custom.create_cluster_custom_object.call_args[1]["body"]
        assert body["metadata"]["name"] == "my-cluster"
        assert body["spec"]["nodeLabels"] == {"fournos.dev/cluster": "my-cluster"}

    def test_already_exists_returns_none(self) -> None:
        from fournos.core.kueue import KueueClient

        mock_custom = MagicMock()
        mock_custom.create_cluster_custom_object.side_effect = ApiException(status=409)
        kueue = KueueClient(mock_custom)

        result = kueue.create_flavor("my-cluster")

        assert result is None

    def test_api_error_propagates(self) -> None:
        from fournos.core.kueue import KueueClient

        mock_custom = MagicMock()
        mock_custom.create_cluster_custom_object.side_effect = ApiException(status=500)
        kueue = KueueClient(mock_custom)

        with pytest.raises(ApiException):
            kueue.create_flavor("my-cluster")


# ---------------------------------------------------------------------------
# KueueClient.add_flavor_to_cluster_queue
# ---------------------------------------------------------------------------


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
        from fournos.core.kueue import KueueClient

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
        from fournos.core.kueue import KueueClient

        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.return_value = self._make_cq()
        kueue = KueueClient(mock_custom)

        result = kueue.add_flavor_to_cluster_queue("existing-cluster")

        assert result is None
        mock_custom.patch_cluster_custom_object.assert_not_called()

    def test_cq_not_found(self) -> None:
        from fournos.core.kueue import KueueClient

        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.side_effect = ApiException(status=404)
        kueue = KueueClient(mock_custom)

        result = kueue.add_flavor_to_cluster_queue("new-cluster")

        assert result is None

    def test_preserves_existing_flavors(self) -> None:
        from fournos.core.kueue import KueueClient

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
        assert flavors[0]["resources"][0]["nominalQuota"] == 100
        assert flavors[1]["name"] == "cluster-b"


# ---------------------------------------------------------------------------
# ClusterDiscovery.scan
# ---------------------------------------------------------------------------


class TestClusterDiscoveryScan:

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.mock_k8s_core = MagicMock()
        self.mock_k8s_custom = MagicMock()
        self.mock_kueue = MagicMock()
        self.mock_kueue.list_flavors.return_value = set()
        self.discovery = ClusterDiscovery(
            self.mock_k8s_core, self.mock_k8s_custom, self.mock_kueue
        )

    def _set_secrets(self, names: list[str]) -> None:
        items = []
        for n in names:
            s = MagicMock()
            s.metadata.name = n
            items.append(s)
        self.mock_k8s_core.list_namespaced_secret.return_value.items = items

    def _set_existing_psapclusters(self, names: list[str]) -> None:
        self.mock_k8s_custom.list_namespaced_custom_object.return_value = {
            "items": [{"metadata": {"name": n}} for n in names]
        }

    def test_discovers_new_cluster(self) -> None:
        self._set_secrets(["kubeconfig-cluster-1"])
        self._set_existing_psapclusters([])

        result = self.discovery.scan()

        assert result == ["cluster-1"]
        self.mock_kueue.create_flavor.assert_called_once_with("cluster-1")
        self.mock_kueue.add_flavor_to_cluster_queue.assert_called_once_with("cluster-1")
        self.mock_k8s_custom.create_namespaced_custom_object.assert_called_once()

    def test_existing_cluster_skipped(self) -> None:
        self._set_secrets(["kubeconfig-cluster-1"])
        self._set_existing_psapclusters(["cluster-1"])

        result = self.discovery.scan()

        assert result == []
        self.mock_kueue.create_flavor.assert_not_called()
        self.mock_k8s_custom.create_namespaced_custom_object.assert_not_called()

    def test_multiple_clusters(self) -> None:
        self._set_secrets([
            "kubeconfig-cluster-1",
            "kubeconfig-cluster-2",
            "kubeconfig-cluster-3",
        ])
        self._set_existing_psapclusters(["cluster-1"])

        result = self.discovery.scan()

        assert result == ["cluster-2", "cluster-3"]
        assert self.mock_kueue.create_flavor.call_count == 2
        assert self.mock_k8s_custom.create_namespaced_custom_object.call_count == 2

    def test_idempotent_409(self) -> None:
        self._set_secrets(["kubeconfig-cluster-1"])
        self._set_existing_psapclusters([])
        self.mock_k8s_custom.create_namespaced_custom_object.side_effect = ApiException(
            status=409
        )

        result = self.discovery.scan()

        assert result == ["cluster-1"]

    def test_non_matching_secrets_ignored(self) -> None:
        self._set_secrets(["vault-my-creds", "some-other-secret"])
        self._set_existing_psapclusters([])

        result = self.discovery.scan()

        assert result == []
        self.mock_kueue.create_flavor.assert_not_called()

    def test_psapcluster_has_auto_discovered_label(self) -> None:
        self._set_secrets(["kubeconfig-cluster-1"])
        self._set_existing_psapclusters([])

        self.discovery.scan()

        body = self.mock_k8s_custom.create_namespaced_custom_object.call_args[1]["body"]
        assert body["metadata"]["labels"]["fournos.dev/auto-discovered"] == "true"

    def test_psapcluster_spec_matches(self) -> None:
        self._set_secrets(["kubeconfig-cluster-1"])
        self._set_existing_psapclusters([])

        self.discovery.scan()

        body = self.mock_k8s_custom.create_namespaced_custom_object.call_args[1]["body"]
        assert body["spec"]["kubeconfigSecret"] == "kubeconfig-cluster-1"
        assert body["metadata"]["name"] == "cluster-1"

    def test_skips_flavor_creation_when_already_exists(self) -> None:
        self._set_secrets(["kubeconfig-cluster-1"])
        self._set_existing_psapclusters([])
        self.mock_kueue.list_flavors.return_value = {"cluster-1"}

        self.discovery.scan()

        self.mock_kueue.create_flavor.assert_not_called()
        self.mock_kueue.add_flavor_to_cluster_queue.assert_called_once_with("cluster-1")

    def test_empty_secrets_returns_empty(self) -> None:
        self._set_secrets([])
        self._set_existing_psapclusters([])

        result = self.discovery.scan()

        assert result == []
        self.mock_kueue.list_flavors.assert_not_called()

    def test_api_error_propagates(self) -> None:
        self._set_secrets(["kubeconfig-cluster-1"])
        self._set_existing_psapclusters([])
        self.mock_k8s_custom.create_namespaced_custom_object.side_effect = ApiException(
            status=500
        )

        with pytest.raises(ApiException):
            self.discovery.scan()


# ---------------------------------------------------------------------------
# scan_clusters handler
# ---------------------------------------------------------------------------


class TestScanClustersHandler:

    @patch("fournos.handlers.discovery.ctx")
    def test_calls_scan(self, mock_ctx: MagicMock) -> None:
        from fournos.handlers.discovery import scan_clusters

        mock_ctx.discovery.scan.return_value = ["cluster-1"]

        result = scan_clusters()

        mock_ctx.discovery.scan.assert_called_once()
        assert result == ["cluster-1"]

    @patch("fournos.handlers.discovery.ctx")
    def test_exception_logged_not_raised(self, mock_ctx: MagicMock) -> None:
        from fournos.handlers.discovery import scan_clusters

        mock_ctx.discovery.scan.side_effect = RuntimeError("boom")

        result = scan_clusters()

        assert result == []
