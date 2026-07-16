"""Clusterless job tests — jobs running without target cluster access.

Tests cover clusterless job validation, Kueue bypass behavior, and execution
without kubeconfig injection. Clusterless jobs run entirely on the hub cluster.
"""

from fournos.core.constants import Phase
from tests.conftest import (
    create_job,
    get_job,
    get_pipelinerun_param,
    job_status_summary,
    poll_phase,
    workload_exists,
)


def test_clusterless_happy_path(k8s):
    """Clusterless job without hardware: bypasses Kueue, runs directly on hub cluster."""
    create_job(
        k8s,
        "test-clusterless-happy",
        {
            "clusterless": True,
            "exclusive": False,
            "executionEngine": {
                "forge": {
                    "project": "testproj/llmd",
                    "args": ["cks", "internal-test"],
                }
            },
        },
    )

    # Should skip Pending phase and go directly to Admitted
    phase = poll_phase(
        k8s,
        "test-clusterless-happy",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=120,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-clusterless-happy")

    job = get_job(k8s, "test-clusterless-happy")
    assert job["status"]["cluster"] == "[clusterless]"


def test_clusterless_with_hardware(k8s):
    """Clusterless job with hardware specs: bypasses Kueue but includes hardware in spec."""
    create_job(
        k8s,
        "test-clusterless-hw",
        {
            "clusterless": True,
            "exclusive": False,
            "hardware": {"gpuType": "a100", "gpuCount": 2},
            "executionEngine": {
                "forge": {
                    "project": "testproj/llmd",
                    "args": ["cks", "internal-test"],
                }
            },
        },
    )

    phase = poll_phase(
        k8s,
        "test-clusterless-hw",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=120,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-clusterless-hw")

    job = get_job(k8s, "test-clusterless-hw")
    assert job["status"]["cluster"] == "[clusterless]"


def test_clusterless_no_workload_created(k8s):
    """Clusterless jobs should not create Kueue Workloads."""
    create_job(
        k8s,
        "test-clusterless-no-workload",
        {
            "clusterless": True,
            "exclusive": False,
            "executionEngine": {
                "forge": {
                    "project": "testproj/llmd",
                    "args": ["cks", "internal-test"],
                }
            },
        },
    )

    # Wait for job to reach Admitted phase
    poll_phase(
        k8s,
        "test-clusterless-no-workload",
        terminal={Phase.ADMITTED, Phase.RUNNING, Phase.SUCCEEDED, Phase.FAILED},
        timeout=45,
    )

    # Verify no Workload was created
    assert not workload_exists("test-clusterless-no-workload"), (
        "Clusterless jobs should not create Kueue Workloads"
    )


def test_clusterless_kubeconfig_placeholder(k8s):
    """Clusterless jobs should get placeholder kubeconfig parameter."""
    create_job(
        k8s,
        "test-clusterless-kubeconfig",
        {
            "clusterless": True,
            "exclusive": False,
            "executionEngine": {
                "forge": {
                    "project": "testproj/llmd",
                    "args": ["cks", "internal-test"],
                }
            },
        },
    )

    # Wait for PipelineRun creation
    poll_phase(
        k8s,
        "test-clusterless-kubeconfig",
        terminal={Phase.RUNNING, Phase.SUCCEEDED, Phase.FAILED},
        timeout=45,
    )

    kubeconfig_param = get_pipelinerun_param(
        "test-clusterless-kubeconfig", "kubeconfig-secret"
    )
    assert kubeconfig_param == "fournos-clusterless-placeholder", (
        f"Clusterless job should get placeholder kubeconfig, got {kubeconfig_param!r}"
    )


def test_clusterless_with_cluster_fails(k8s):
    """clusterless: true + cluster: name should fail validation."""
    create_job(
        k8s,
        "test-clusterless-cluster-conflict",
        {
            "clusterless": True,
            "cluster": "cluster-1",
            "exclusive": False,
            "executionEngine": {
                "forge": {
                    "project": "testproj/llmd",
                    "args": ["cks", "internal-test"],
                }
            },
        },
    )

    phase = poll_phase(
        k8s,
        "test-clusterless-cluster-conflict",
        terminal={Phase.FAILED},
        timeout=15,
    )
    assert phase == Phase.FAILED, job_status_summary(
        k8s, "test-clusterless-cluster-conflict"
    )

    job = get_job(k8s, "test-clusterless-cluster-conflict")
    msg = job["status"]["message"].lower()
    assert "clusterless" in msg and "cluster" in msg, (
        f"Failure message should mention both 'clusterless' and 'cluster', got: {msg!r}"
    )


def test_clusterless_with_exclusive_fails(k8s):
    """clusterless: true + exclusive: true should fail validation."""
    create_job(
        k8s,
        "test-clusterless-exclusive-conflict",
        {
            "clusterless": True,
            "exclusive": True,
            "executionEngine": {
                "forge": {
                    "project": "testproj/llmd",
                    "args": ["cks", "internal-test"],
                }
            },
        },
    )

    phase = poll_phase(
        k8s,
        "test-clusterless-exclusive-conflict",
        terminal={Phase.FAILED},
        timeout=15,
    )
    assert phase == Phase.FAILED, job_status_summary(
        k8s, "test-clusterless-exclusive-conflict"
    )

    job = get_job(k8s, "test-clusterless-exclusive-conflict")
    msg = job["status"]["message"].lower()
    assert "clusterless" in msg and "exclusive" in msg, (
        f"Failure message should mention both 'clusterless' and 'exclusive', got: {msg!r}"
    )


def test_clusterless_with_lockonly_fails(k8s):
    """clusterless: true + lockOnly: true should fail validation."""
    create_job(
        k8s,
        "test-clusterless-lockonly-conflict",
        {
            "clusterless": True,
            "lockOnly": True,
            "cluster": "cluster-1",
            "executionEngine": {
                "forge": {
                    "project": "testproj/llmd",
                    "args": ["cks", "internal-test"],
                }
            },
        },
    )

    phase = poll_phase(
        k8s,
        "test-clusterless-lockonly-conflict",
        terminal={Phase.FAILED},
        timeout=15,
    )
    assert phase == Phase.FAILED, job_status_summary(
        k8s, "test-clusterless-lockonly-conflict"
    )

    job = get_job(k8s, "test-clusterless-lockonly-conflict")
    msg = job["status"]["message"].lower()
    assert "clusterless" in msg and "lockonly" in msg, (
        f"Failure message should mention both 'clusterless' and 'lockonly', got: {msg!r}"
    )


def test_clusterless_skips_pending_phase(k8s):
    """Clusterless jobs should skip the Pending phase entirely."""
    create_job(
        k8s,
        "test-clusterless-skip-pending",
        {
            "clusterless": True,
            "exclusive": False,
            "executionEngine": {
                "forge": {
                    "project": "testproj/llmd",
                    "args": ["cks", "internal-test"],
                }
            },
        },
    )

    # Should go Resolving → Admitted (skipping Pending)
    poll_phase(
        k8s,
        "test-clusterless-skip-pending",
        terminal={Phase.ADMITTED, Phase.RUNNING, Phase.SUCCEEDED, Phase.FAILED},
        timeout=45,
    )

    # Verify it didn't get stuck in Pending
    job = get_job(k8s, "test-clusterless-skip-pending")
    assert job["status"]["phase"] != Phase.PENDING, (
        "Clusterless job should never enter Pending phase"
    )
