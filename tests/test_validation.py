"""Validation tests — spec validation catches invalid configurations early."""

import json
import subprocess

from tests.conftest import (
    NAMESPACE,
    create_job,
    get_job,
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
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )

    phase = poll_phase(
        k8s,
        "test-no-target",
        terminal={"Failed"},
        timeout=15,
    )
    assert phase == "Failed"

    job = get_job(k8s, "test-no-target")
    msg = job["status"]["message"].lower()
    assert "cluster" in msg or "hardware" in msg


def test_unknown_cluster(k8s):
    """Referencing a cluster with no matching ResourceFlavor → immediate Failed."""
    create_job(
        k8s,
        "test-unknown",
        {
            "cluster": "no-such-cluster",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )

    phase = poll_phase(
        k8s,
        "test-unknown",
        terminal={"Failed"},
        timeout=15,
    )
    assert phase == "Failed"

    job = get_job(k8s, "test-unknown")
    assert "not found" in job["status"]["message"].lower()


def test_admitted_without_flavor(k8s):
    """Workload admitted but no flavor in podSetAssignments → Failed."""
    create_job(
        k8s,
        "test-no-flavor",
        {
            "hardware": {"gpuType": "a100", "gpuCount": 999},
            "forge": {"project": "testproj/llmd", "preset": "cks"},
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
                }
            ],
        },
    }
    subprocess.run(
        [
            "kubectl",
            "patch",
            "workload",
            "fournos-test-no-flavor",
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
    assert phase == "Failed"

    job = get_job(k8s, "test-no-flavor")
    assert "no flavor" in job["status"]["message"].lower()
    poll_resource_gone(workload_exists, "test-no-flavor")
