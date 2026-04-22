"""End-to-end tests — Vault sync → secretRef resolution → PipelineRun.

The Vault HTTP layer is mocked so no real Vault is needed, but secrets
are created on the live cluster by the sync script, then consumed by
a FournosJob whose operator resolves them via the fournos.dev/vault-entry
label.
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
    create_job,
    get_pipelinerun_param,
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


def _delete_secret_if_exists(v1, name: str) -> None:
    try:
        v1.delete_namespaced_secret(name, NAMESPACE)
    except client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_vault_sync_then_fjob(k8s, core_v1):
    """Sync a mocked Vault entry, then verify a FournosJob resolves it."""
    expected_k8s_name = svs.k8s_name(VAULT_ENTRY)  # vault-e2e-creds

    # -- Phase 1: run the sync script with mocked Vault HTTP calls ----------
    with (
        mock.patch.object(svs, "vault_list", return_value=[VAULT_ENTRY]),
        mock.patch.object(svs, "vault_read", return_value=VAULT_DATA),
    ):
        rc = svs.sync(
            vault_addr="https://vault.fake.test",
            vault_token="s.fake",
            kv_mount="kv",
            secret_path="selfservice/e2e",
            namespace=NAMESPACE,
            dry_run=False,
        )
    assert rc == 0, "sync_vault_secrets.sync() returned non-zero"

    try:
        # -- Phase 2: verify the K8s Secret was created correctly -----------
        secret = core_v1.read_namespaced_secret(expected_k8s_name, NAMESPACE)
        assert secret.metadata.labels[LABEL_VAULT_ENTRY] == VAULT_ENTRY
        assert (
            secret.metadata.labels["app.kubernetes.io/managed-by"]
            == "fournos-vault-sync"
        )
        assert (
            secret.metadata.annotations["fournos.dev/vault-addr"]
            == "https://vault.fake.test"
        )
        assert "secretsync_target-namespace" not in (secret.data or {}), (
            "secretsync/ metadata keys must be filtered out"
        )

        # -- Phase 3: create a FournosJob referencing the vault entry -------
        create_job(
            k8s,
            "test-e2e-secret",
            {
                "cluster": "cluster-1",
                "forge": {
                    "project": "testproj/llmd",
                    "args": ["cks", "internal-test"],
                },
                "secretRefs": [VAULT_ENTRY],
            },
        )

        poll_phase(
            k8s,
            "test-e2e-secret",
            terminal={"Running", "Succeeded", "Failed"},
            timeout=30,
        )

        # -- Phase 4: verify the PipelineRun received the resolved name -----
        refs_param = get_pipelinerun_param("test-e2e-secret", "secret-refs")
        assert expected_k8s_name in refs_param, (
            f"PipelineRun secret-refs should contain {expected_k8s_name!r}, "
            f"got {refs_param!r}"
        )

        phase = poll_phase(
            k8s,
            "test-e2e-secret",
            terminal={"Succeeded", "Failed"},
            timeout=60,
        )
        assert phase == "Succeeded", job_status_summary(k8s, "test-e2e-secret")

    finally:
        _delete_secret_if_exists(core_v1, expected_k8s_name)


def test_missing_secret_ref_fails(k8s):
    """A secretRef with no matching labelled Secret should fail the job."""
    create_job(
        k8s,
        "test-missing-ref",
        {
            "cluster": "cluster-1",
            "forge": {
                "project": "testproj/llmd",
                "args": ["cks", "internal-test"],
            },
            "secretRefs": ["nonexistent-vault-entry"],
        },
    )

    phase = poll_phase(
        k8s,
        "test-missing-ref",
        terminal={"Failed"},
        message_substring="No Secret with label",
        timeout=30,
    )
    assert phase == "Failed", job_status_summary(k8s, "test-missing-ref")
