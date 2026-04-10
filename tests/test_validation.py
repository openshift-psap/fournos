"""Validation tests — spec validation catches invalid configurations early."""

import json
import subprocess

from tests.conftest import (
    NAMESPACE,
    create_job,
    get_job,
    job_status_summary,
    poll_phase,
    poll_resource_gone,
    workload_exists,
)


def test_neither_cluster_nor_hardware(k8s):
    """Missing both cluster and hardware → immediate Failed with message."""
    create_job(
        k8s,
        "test-no-target",
        {
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    phase = poll_phase(
        k8s,
        "test-no-target",
        terminal={"Failed"},
        timeout=15,
    )
    assert phase == "Failed", job_status_summary(k8s, "test-no-target")

    job = get_job(k8s, "test-no-target")
    msg = job["status"]["message"].lower()
    assert "cluster" in msg or "hardware" in msg, (
        f"Failure message should mention 'cluster' or 'hardware', got: {msg!r}"
    )


def test_unknown_cluster(k8s):
    """Referencing a cluster with no matching ResourceFlavor → immediate Failed."""
    create_job(
        k8s,
        "test-unknown",
        {
            "cluster": "no-such-cluster",
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    phase = poll_phase(
        k8s,
        "test-unknown",
        terminal={"Failed"},
        timeout=15,
    )
    assert phase == "Failed", job_status_summary(k8s, "test-unknown")

    job = get_job(k8s, "test-unknown")
    msg = job["status"]["message"].lower()
    assert "not found" in msg, (
        f"Failure message should mention 'not found', got: {msg!r}"
    )


def test_unknown_gpu_type(k8s):
    """Requesting a GPU type with no quota in any ClusterQueue → immediate Failed."""
    create_job(
        k8s,
        "test-bad-gpu",
        {
            "hardware": {"gpuType": "acbd1234", "gpuCount": 2},
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    phase = poll_phase(
        k8s,
        "test-bad-gpu",
        terminal={"Failed"},
        timeout=15,
    )
    assert phase == "Failed", job_status_summary(k8s, "test-bad-gpu")

    job = get_job(k8s, "test-bad-gpu")
    msg = job["status"]["message"]
    assert "acbd1234" in msg.lower(), (
        f"Failure message should mention the GPU type 'acbd1234', got: {msg!r}"
    )
    assert "not available" in msg.lower(), (
        f"Failure message should say 'not available', got: {msg!r}"
    )


def test_admitted_without_flavor(k8s):
    """Workload admitted but no flavor in podSetAssignments → Failed."""
    create_job(
        k8s,
        "test-no-flavor",
        {
            "hardware": {"gpuType": "a100", "gpuCount": 999},
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    poll_phase(k8s, "test-no-flavor", terminal={"Pending"}, timeout=15)

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
        terminal={"Failed"},
        timeout=30,
    )
    assert phase == "Failed", job_status_summary(k8s, "test-no-flavor")

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
