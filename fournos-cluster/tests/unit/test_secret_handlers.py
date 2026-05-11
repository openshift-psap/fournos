"""Tests for kubeconfig secret watch handler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from fournos_cluster.handlers.secret import extract_cluster_name, on_kubeconfig_secret


class TestExtractClusterName:

    def test_default_pattern(self) -> None:
        assert extract_cluster_name("kubeconfig-cluster-1") == "cluster-1"

    def test_no_match(self) -> None:
        assert extract_cluster_name("other-secret") is None

    def test_cluster_name_with_dashes(self) -> None:
        assert extract_cluster_name("kubeconfig-my-gpu-cluster") == "my-gpu-cluster"

    def test_empty_after_prefix(self) -> None:
        assert extract_cluster_name("kubeconfig-") is None


class TestOnKubeconfigSecret:

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        patcher = patch("fournos_cluster.handlers.secret.client")
        self.mock_client = patcher.start()
        self.mock_custom = MagicMock()
        self.mock_client.CustomObjectsApi.return_value = self.mock_custom
        self.mock_client.exceptions.ApiException = ApiException
        yield
        patcher.stop()

    def _make_body(self, name: str, annotations: dict | None = None) -> dict:
        meta = {"name": name}
        if annotations:
            meta["annotations"] = annotations
        return {"metadata": meta}

    def test_creates_fournos_cluster(self) -> None:
        self.mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=404)

        on_kubeconfig_secret(
            body=self._make_body("kubeconfig-test-cluster"),
            name="kubeconfig-test-cluster",
            namespace="psap-secrets",
        )

        self.mock_custom.create_namespaced_custom_object.assert_called_once()
        body = self.mock_custom.create_namespaced_custom_object.call_args[1]["body"]
        assert body["kind"] == "FournosCluster"
        assert body["metadata"]["name"] == "test-cluster"
        assert body["spec"]["kubeconfigSecret"] == "kubeconfig-test-cluster"
        assert body["metadata"]["labels"]["fournos.dev/auto-discovered"] == "true"

    def test_skips_existing_cluster(self) -> None:
        self.mock_custom.get_namespaced_custom_object.return_value = {"metadata": {}}

        on_kubeconfig_secret(
            body=self._make_body("kubeconfig-existing"),
            name="kubeconfig-existing",
            namespace="psap-secrets",
        )

        self.mock_custom.create_namespaced_custom_object.assert_not_called()

    def test_handles_race_409(self) -> None:
        self.mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
        self.mock_custom.create_namespaced_custom_object.side_effect = ApiException(status=409)

        on_kubeconfig_secret(
            body=self._make_body("kubeconfig-race"),
            name="kubeconfig-race",
            namespace="psap-secrets",
        )

    def test_skips_non_matching_name(self) -> None:
        on_kubeconfig_secret(
            body=self._make_body("vault-sync-secret"),
            name="vault-sync-secret",
            namespace="psap-secrets",
        )

        self.mock_custom.get_namespaced_custom_object.assert_not_called()

    def test_sets_owner_from_annotation(self) -> None:
        self.mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=404)

        on_kubeconfig_secret(
            body=self._make_body("kubeconfig-owned", annotations={"fournos.dev/owner": "nathan"}),
            name="kubeconfig-owned",
            namespace="psap-secrets",
        )

        body = self.mock_custom.create_namespaced_custom_object.call_args[1]["body"]
        assert body["spec"]["owner"] == "nathan"

    def test_no_owner_when_annotation_missing(self) -> None:
        self.mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=404)

        on_kubeconfig_secret(
            body=self._make_body("kubeconfig-no-owner"),
            name="kubeconfig-no-owner",
            namespace="psap-secrets",
        )

        body = self.mock_custom.create_namespaced_custom_object.call_args[1]["body"]
        assert "owner" not in body["spec"]
