"""End-to-end tests — Vault sync -> secretRef resolution -> PipelineRun.

SecretRefs live on the FournosJob spec and are populated by Forge during
the Resolving phase.  The Vault HTTP layer is mocked so no real Vault is
needed, but secrets are created on the live cluster by the sync script,
then consumed by a FournosJob whose spec.secretRefs references them.

Both tests create the FournosJob first.  The operator launches a resolve
Job; after it completes the test patches ``spec.secretRefs`` on the
FournosJob before the operator's next timer tick validates them.
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
    poll_resolve_job_complete,
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

GROUP = "fournos.dev"
VERSION = "v1"


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


def _patch_fjob_secret_refs(k8s, job_name: str, secret_refs: list[str]) -> None:
    """Patch the FournosJob to set ``spec.secretRefs``."""
    k8s.patch_namespaced_custom_object(
        GROUP,
        VERSION,
        NAMESPACE,
        "fournosjobs",
        job_name,
        body={"spec": {"secretRefs": secret_refs}},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_vault_sync_then_fjob(k8s, core_v1):
    """Sync a mocked Vault entry, then verify a FournosJob passes it to PipelineRun.

    The FournosJob is created first.  The operator launches the resolve
    Job.  After the resolve Job completes, the test patches secretRefs
    on the FournosJob before the operator reads the spec for the Pending
    transition.
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
            namespace=NAMESPACE,
            dry_run=False,
        )
    assert rc == 0, "sync_vault_secrets.sync() returned non-zero"

    try:
        secret = core_v1.read_namespaced_secret(VAULT_ENTRY, NAMESPACE)
        assert secret.metadata.labels[LABEL_VAULT_ENTRY] == "true"

        create_job(
            k8s,
            "test-e2e-secret",
            {
                "cluster": "cluster-1",
                "forge": {
                    "project": "testproj/llmd",
                    "args": ["cks", "internal-test"],
                },
            },
        )

        poll_resolve_job_complete("test-e2e-secret")
        _patch_fjob_secret_refs(k8s, "test-e2e-secret", [VAULT_ENTRY])

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
        assert VAULT_ENTRY in refs_param, (
            f"PipelineRun secret-refs should contain {VAULT_ENTRY!r}, "
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
        _delete_secret_if_exists(core_v1, VAULT_ENTRY)


def test_missing_secret_ref_fails(k8s):
    """A secretRef with no matching labelled Secret fails the job.

    The test waits for the resolve Job to complete, then patches
    secretRefs on the FournosJob to reference a nonexistent secret.
    """
    create_job(
        k8s,
        "test-missing-ref",
        {
            "cluster": "cluster-1",
            "forge": {
                "project": "testproj/llmd",
                "args": ["cks", "internal-test"],
            },
        },
    )

    poll_resolve_job_complete("test-missing-ref")
    _patch_fjob_secret_refs(k8s, "test-missing-ref", ["nonexistent-vault-entry"])

    phase = poll_phase(
        k8s,
        "test-missing-ref",
        terminal={"Failed"},
        message_substring="not found in namespace",
        timeout=60,
    )
    assert phase == "Failed", job_status_summary(k8s, "test-missing-ref")
