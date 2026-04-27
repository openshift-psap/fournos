"""Unit tests for secret volume injection: copy_secret and _build_secrets_volume."""

from __future__ import annotations

from unittest import mock

import pytest
from kubernetes import client

from fournos.core.clusters import ClusterRegistry, ResolvedSecret
from fournos.core.tekton import _build_secrets_volume


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OWNER_REF = {
    "apiVersion": "fournos.dev/v1",
    "kind": "FournosJob",
    "name": "my-job",
    "uid": "abc-123",
}


@pytest.fixture()
def registry():
    k8s = mock.MagicMock(spec=client.CoreV1Api)
    with mock.patch("fournos.core.clusters.settings") as mock_settings:
        mock_settings.namespace = "pod-ns"
        mock_settings.secrets_namespace = "vault-ns"
        reg = ClusterRegistry(k8s)
        yield reg, k8s


def _make_source_secret(**data_keys):
    """Build a mock V1Secret with the given data keys."""
    source = mock.MagicMock()
    source.data = {k: "base64value" for k in data_keys} if data_keys else data_keys
    source.type = "Opaque"
    return source


# ---------------------------------------------------------------------------
# _build_secrets_volume
# ---------------------------------------------------------------------------


class TestBuildSecretsVolume:
    def test_empty_list_produces_valid_volume(self):
        vol = _build_secrets_volume([])
        assert vol == {
            "name": "vault-secrets",
            "projected": {"sources": []},
        }

    def test_single_secret(self):
        resolved = [
            ResolvedSecret(
                name="my-job-creds", original_name="creds", keys=["pass", "user"]
            )
        ]
        vol = _build_secrets_volume(resolved)
        assert vol["name"] == "vault-secrets"
        sources = vol["projected"]["sources"]
        assert len(sources) == 1
        assert sources[0]["secret"]["name"] == "my-job-creds"
        items = sources[0]["secret"]["items"]
        assert {"key": "pass", "path": "creds/pass"} in items
        assert {"key": "user", "path": "creds/user"} in items

    def test_multiple_secrets_keep_subdirectories(self):
        resolved = [
            ResolvedSecret(name="j-a", original_name="a", keys=["k1"]),
            ResolvedSecret(name="j-b", original_name="b", keys=["k2", "k3"]),
        ]
        vol = _build_secrets_volume(resolved)
        sources = vol["projected"]["sources"]
        assert len(sources) == 2
        assert sources[0]["secret"]["name"] == "j-a"
        assert sources[1]["secret"]["items"] == [
            {"key": "k2", "path": "b/k2"},
            {"key": "k3", "path": "b/k3"},
        ]


# ---------------------------------------------------------------------------
# copy_secret
# ---------------------------------------------------------------------------


class TestCopySecret:
    def test_copies_secret_to_pod_namespace(self, registry):
        reg, k8s = registry
        k8s.read_namespaced_secret.return_value = _make_source_secret(
            user="x", password="y"
        )

        result = reg.copy_secret("creds", "my-job", OWNER_REF)

        assert result.name == "my-job-creds"
        assert result.original_name == "creds"
        assert sorted(result.keys) == ["password", "user"]

        k8s.read_namespaced_secret.assert_called_once_with("creds", "vault-ns")
        k8s.create_namespaced_secret.assert_called_once()
        ns_arg, body_arg = k8s.create_namespaced_secret.call_args[0]
        assert ns_arg == "pod-ns"
        assert body_arg.metadata.name == "my-job-creds"

    def test_idempotent_on_409(self, registry):
        reg, k8s = registry
        k8s.read_namespaced_secret.return_value = _make_source_secret(token="t")
        k8s.create_namespaced_secret.side_effect = client.exceptions.ApiException(
            status=409
        )

        result = reg.copy_secret("tok", "j1", OWNER_REF)
        assert result.name == "j1-tok"
        assert result.keys == ["token"]

    def test_propagates_non_409_api_error(self, registry):
        reg, k8s = registry
        k8s.read_namespaced_secret.return_value = _make_source_secret(x="v")
        k8s.create_namespaced_secret.side_effect = client.exceptions.ApiException(
            status=500
        )

        with pytest.raises(client.exceptions.ApiException):
            reg.copy_secret("x", "j2", OWNER_REF)

    def test_owner_reference_is_non_controller(self, registry):
        reg, k8s = registry
        k8s.read_namespaced_secret.return_value = _make_source_secret(k="v")

        reg.copy_secret("s1", "job1", OWNER_REF)

        body = k8s.create_namespaced_secret.call_args[0][1]
        oref = body.metadata.owner_references[0]
        assert oref.name == "my-job"
        assert oref.uid == "abc-123"
        assert oref.controller is False
        assert oref.block_owner_deletion is True

    def test_labels_include_managed_by_and_vault_entry(self, registry):
        reg, k8s = registry
        k8s.read_namespaced_secret.return_value = _make_source_secret(k="v")

        reg.copy_secret("s1", "job1", OWNER_REF)

        body = k8s.create_namespaced_secret.call_args[0][1]
        labels = body.metadata.labels
        assert labels["app.kubernetes.io/managed-by"] == "fournos"
        assert labels["fournos.dev/vault-entry"] == "true"


class TestCopySecrets:
    def test_copies_multiple(self, registry):
        reg, k8s = registry
        k8s.read_namespaced_secret.return_value = _make_source_secret(k="v")

        results = reg.copy_secrets(["a", "b"], "j", OWNER_REF)
        assert len(results) == 2
        assert results[0].name == "j-a"
        assert results[1].name == "j-b"

    def test_empty_refs_returns_empty_list(self, registry):
        reg, _ = registry
        assert reg.copy_secrets([], "j", OWNER_REF) == []
