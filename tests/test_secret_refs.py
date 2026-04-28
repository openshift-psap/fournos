"""End-to-end tests — Vault sync -> secretRef resolution -> PipelineRun.

SecretRefs live on the FournosJob spec and are populated by Forge during
the Resolving phase.  The Vault HTTP layer is mocked so no real Vault is
needed, but secrets are created on the live cluster by the sync script,
then consumed by a FournosJob whose spec.secretRefs references them.

Both tests use a noop resolve Job to avoid races with the mock resolver,
and supply ``secretRefs`` directly in the FournosJob spec.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from unittest import mock

import pytest
from kubernetes import client, config

from fournos.core.constants import LABEL_VAULT_ENTRY
from tests.conftest import (
    NAMESPACE,
    SECRETS_NAMESPACE,
    create_job,
    create_noop_resolve_job,
    get_pipelinerun_param,
    get_pipelinerun_volumes,
    job_status_summary,
    poll_phase,
)

# ---------------------------------------------------------------------------
# Import the sync script as a module (it lives outside the package tree).
# ---------------------------------------------------------------------------

_script = (
    pathlib.Path(__file__).resolve().parents[1] / "hacks" / "sync_vault_secrets.py"
)
_spec = importlib.util.spec_from_file_location("sync_vault_secrets", _script)
svs = importlib.util.module_from_spec(_spec)
sys.modules["sync_vault_secrets"] = svs
_spec.loader.exec_module(svs)

# Fake Vault data: one entry named "e2e-creds" with two keys
# (plus a secretsync/ metadata key that should be filtered out).
VAULT_ENTRY = "e2e-creds"
VAULT_SECRET = f"vault-{VAULT_ENTRY}"
VAULT_DATA = {
    "username": "admin",
    "password": "s3cret",
    "secretsync/target-namespace": "should-be-filtered",
}


@pytest.fixture(scope="session")
def core_v1():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api()


def _delete_secret_if_exists(v1, name: str, namespace: str = SECRETS_NAMESPACE) -> None:
    try:
        v1.delete_namespaced_secret(name, namespace)
    except client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_vault_sync_then_fjob(k8s, core_v1):
    """Sync a mocked Vault entry, then verify a FournosJob passes it to PipelineRun.

    A noop resolve Job is pre-created so the mock resolver doesn't
    overwrite ``secretRefs``.  The FournosJob carries ``secretRefs``
    directly in its spec, avoiding any race with the resolver.
    """

    with (
        mock.patch.object(svs, "vault_list", return_value=[VAULT_ENTRY]),
        mock.patch.object(svs, "vault_read", return_value=VAULT_DATA),
    ):
        rc = svs.sync(
            vault_addr="https://vault.fake.test",
            vault_token="s.fake",
            kv_mount="kv",
            secret_path="selfservice/e2e",
            namespace=SECRETS_NAMESPACE,
            dry_run=False,
        )
    assert rc == 0, "sync_vault_secrets.sync() returned non-zero"

    expected_copy = f"test-e2e-secret-{VAULT_ENTRY}"

    try:
        secret = core_v1.read_namespaced_secret(VAULT_SECRET, SECRETS_NAMESPACE)
        assert secret.metadata.labels[LABEL_VAULT_ENTRY] == "true"

        create_noop_resolve_job("test-e2e-secret")

        create_job(
            k8s,
            "test-e2e-secret",
            {
                "cluster": "cluster-1",
                "secretRefs": [VAULT_ENTRY],
                "forge": {
                    "project": "testproj/llmd",
                    "args": ["cks", "internal-test"],
                },
            },
        )

        phase = poll_phase(
            k8s,
            "test-e2e-secret",
            terminal={"Running", "Succeeded", "Failed"},
            timeout=90,
        )
        assert phase in ("Running", "Succeeded"), job_status_summary(
            k8s, "test-e2e-secret"
        )

        refs_param = get_pipelinerun_param("test-e2e-secret", "secret-refs")
        assert refs_param == [VAULT_ENTRY], (
            f"PipelineRun secret-refs should be {[VAULT_ENTRY]!r}, got {refs_param!r}"
        )

        volumes = get_pipelinerun_volumes("test-e2e-secret")
        vault_vol = next((v for v in volumes if v.get("name") == "vault-secrets"), None)
        assert vault_vol is not None, (
            f"Expected a 'vault-secrets' projected volume, got {volumes!r}"
        )
        sources = vault_vol.get("projected", {}).get("sources", [])
        source_names = [s.get("secret", {}).get("name", "") for s in sources]
        assert expected_copy in source_names, (
            f"Projected volume should reference copied secret {expected_copy!r}, "
            f"got sources: {source_names!r}"
        )

        copied = core_v1.read_namespaced_secret(expected_copy, NAMESPACE)
        owner_refs = copied.metadata.owner_references or []
        assert any(
            o.kind == "FournosJob" and o.name == "test-e2e-secret" for o in owner_refs
        ), f"Copied secret should have FournosJob ownerRef, got {owner_refs!r}"
        assert sorted(copied.data.keys()) == ["password", "username"], (
            f"Copied secret data keys mismatch: {sorted(copied.data.keys())}"
        )

        phase = poll_phase(
            k8s,
            "test-e2e-secret",
            terminal={"Succeeded", "Failed"},
            timeout=60,
        )
        assert phase == "Succeeded", job_status_summary(k8s, "test-e2e-secret")

    finally:
        _delete_secret_if_exists(core_v1, VAULT_SECRET)
        _delete_secret_if_exists(core_v1, expected_copy, namespace=NAMESPACE)


def test_missing_secret_ref_fails(k8s):
    """A secretRef with no matching labelled Secret fails the job.

    A noop resolve Job is pre-created so the mock resolver doesn't
    inject valid secretRefs.  The FournosJob carries a nonexistent ref
    directly in its spec.
    """
    create_noop_resolve_job("test-missing-ref")

    create_job(
        k8s,
        "test-missing-ref",
        {
            "cluster": "cluster-1",
            "secretRefs": ["nonexistent-vault-entry"],
            "forge": {
                "project": "testproj/llmd",
                "args": ["cks", "internal-test"],
            },
        },
    )

    phase = poll_phase(
        k8s,
        "test-missing-ref",
        terminal={"Failed"},
        message_substring="not found in namespace",
        timeout=60,
    )
    assert phase == "Failed", job_status_summary(k8s, "test-missing-ref")
