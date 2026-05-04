"""Validation tests — spec validation catches invalid configurations early.

Tests cover on_create rejections (unknown cluster, exclusive without cluster)
and admission-phase errors (no flavor assigned).  Resolving-phase validation
(GPU type, execution engine failures) lives in test_resolving.py.
"""

import json
import subprocess

from fournos.core.constants import Phase
from tests.conftest import (
    NAMESPACE,
    create_job,
    get_job,
    job_status_summary,
    poll_phase,
    poll_resource_gone,
    workload_exists,
)


def test_unknown_cluster(k8s):
    """Referencing a cluster with no matching ResourceFlavor → immediate Failed."""
    create_job(
        k8s,
        "test-unknown",
        {
            "cluster": "no-such-cluster",
            "executionEngine": "forge",
            "executionEngineSpec": {
                "resolveImage": "fournos-mock-resolve:dev",
                "project": "testproj/llmd",
                "args": ["cks", "internal-test"],
            },
        },
    )

    phase = poll_phase(
        k8s,
        "test-unknown",
        terminal={Phase.FAILED},
        timeout=15,
    )
    assert phase == Phase.FAILED, job_status_summary(k8s, "test-unknown")

    job = get_job(k8s, "test-unknown")
    msg = job["status"]["message"].lower()
    assert "not found" in msg, (
        f"Failure message should mention 'not found', got: {msg!r}"
    )


def test_admitted_without_flavor(k8s):
    """Workload admitted but no flavor in podSetAssignments → Failed."""
    create_job(
        k8s,
        "test-no-flavor",
        {
            "exclusive": False,
            "hardware": {"gpuType": "a100", "gpuCount": 999},
            "executionEngine": "forge",
            "executionEngineSpec": {
                "resolveImage": "fournos-mock-resolve:dev",
                "project": "testproj/llmd",
                "args": ["cks", "internal-test"],
            },
        },
    )

    poll_phase(k8s, "test-no-flavor", terminal={Phase.PENDING}, timeout=45)

    patch = {
        "status": {
            "conditions": [
                {
                    "type": "Admitted",
                    "status": "True",
                    "reason": "Admitted",
                    "message": "Admitted by test",
                    "lastTransitionTime": "2000-01-01T00:00:00Z",
                }
            ],
        },
    }
    subprocess.run(
        [
            "kubectl",
            "patch",
            "workload",
            "test-no-flavor",
            "-n",
            NAMESPACE,
            "--type",
            "merge",
            "--subresource",
            "status",
            "-p",
            json.dumps(patch),
        ],
        check=True,
        capture_output=True,
    )

    phase = poll_phase(
        k8s,
        "test-no-flavor",
        terminal={Phase.FAILED},
        timeout=30,
    )
    assert phase == Phase.FAILED, job_status_summary(k8s, "test-no-flavor")

    job = get_job(k8s, "test-no-flavor")
    msg = job["status"]["message"].lower()
    assert "no flavor" in msg, (
        f"Failure message should mention 'no flavor', got: {msg!r}"
    )

    conditions = {c["type"]: c for c in job["status"].get("conditions", [])}
    assert "WorkloadAdmitted" in conditions, (
        f"Missing WorkloadAdmitted condition; got types: {list(conditions)}"
    )
    assert conditions["WorkloadAdmitted"]["status"] == "False", (
        f"WorkloadAdmitted should be False; got {conditions['WorkloadAdmitted']}"
    )
    assert conditions["WorkloadAdmitted"]["reason"] == "NoFlavorAssigned", (
        f"WorkloadAdmitted reason should be NoFlavorAssigned; "
        f"got {conditions['WorkloadAdmitted'].get('reason')!r}"
    )

    poll_resource_gone(workload_exists, "test-no-flavor")


def test_implicit_exclusive_without_cluster_fails(k8s):
    """Hardware-only job (no cluster, no explicit exclusive) fails: CRD defaults exclusive to true."""
    create_job(
        k8s,
        "test-implicit-excl",
        {
            "hardware": {"gpuType": "a100", "gpuCount": 2},
            "executionEngine": "forge",
            "executionEngineSpec": {
                "resolveImage": "fournos-mock-resolve:dev",
                "project": "testproj/llmd",
                "args": ["cks", "internal-test"],
            },
        },
    )

    phase = poll_phase(
        k8s,
        "test-implicit-excl",
        terminal={Phase.FAILED},
        timeout=15,
    )
    assert phase == Phase.FAILED, job_status_summary(k8s, "test-implicit-excl")

    job = get_job(k8s, "test-implicit-excl")
    msg = job["status"]["message"].lower()
    assert "cluster" in msg and "exclusive" in msg, (
        f"Failure message should mention both 'exclusive' and 'cluster', got: {msg!r}"
    )
