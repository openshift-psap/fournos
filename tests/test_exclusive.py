"""Exclusive cluster locking tests — exclusive jobs lock clusters, blocking others."""

import subprocess

import pytest

from fournos.core.constants import Phase
from tests.conftest import (
    NAMESPACE,
    create_job,
    get_job,
    get_workload_excluded_clusters,
    job_status_summary,
    poll_phase,
    workload_exists,
)

MOCK_SLEEP_SECONDS = "15"


@pytest.fixture(autouse=True)
def _slow_mock_pipeline():
    """Bump mock pipeline sleep so exclusive jobs stay Running long enough."""
    subprocess.run(
        [
            "kubectl",
            "patch",
            "configmap",
            "fournos-mock-config",
            "-n",
            NAMESPACE,
            "-p",
            f'{{"data":{{"sleep":"{MOCK_SLEEP_SECONDS}"}}}}',
        ],
        check=True,
        capture_output=True,
    )
    yield
    subprocess.run(
        [
            "kubectl",
            "patch",
            "configmap",
            "fournos-mock-config",
            "-n",
            NAMESPACE,
            "-p",
            '{"data":{"sleep":"3"}}',
        ],
        check=True,
        capture_output=True,
    )


def test_exclusive_happy_path(k8s):
    """Exclusive job on an empty cluster proceeds through the full lifecycle."""
    create_job(
        k8s,
        "test-excl-happy",
        {
            "cluster": "cluster-2",
            "exclusive": True,
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    phase = poll_phase(
        k8s,
        "test-excl-happy",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-excl-happy")

    job = get_job(k8s, "test-excl-happy")
    assert job["status"]["cluster"] == "cluster-2"

    labels = job["metadata"].get("labels", {})
    assert labels.get("fournos.dev/exclusive-cluster") == "cluster-2", (
        f"Exclusive label should be set; got labels {labels}"
    )


def test_exclusive_waits_for_cluster_to_clear(k8s):
    """Exclusive job enters Blocked when cluster is occupied, proceeds after it clears."""
    create_job(
        k8s,
        "test-occupant",
        {
            "cluster": "cluster-2",
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )
    poll_phase(k8s, "test-occupant", terminal={Phase.RUNNING}, timeout=30)

    create_job(
        k8s,
        "test-excl-wait",
        {
            "cluster": "cluster-2",
            "exclusive": True,
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    phase = poll_phase(
        k8s,
        "test-excl-wait",
        terminal={Phase.BLOCKED, Phase.PENDING, Phase.ADMITTED, Phase.RUNNING},
        timeout=15,
    )
    assert phase == Phase.BLOCKED, (
        f"Exclusive job should be Blocked while cluster is occupied, got {phase!r}. "
        + job_status_summary(k8s, "test-excl-wait")
    )

    job = get_job(k8s, "test-excl-wait")
    assert "occupied" in job["status"]["message"].lower(), (
        f"Message should mention 'occupied'; got {job['status']['message']!r}"
    )
    conditions = {c["type"]: c for c in job["status"].get("conditions", [])}
    assert "ClusterLocked" in conditions, (
        f"Missing ClusterLocked condition; got types: {list(conditions)}"
    )
    assert conditions["ClusterLocked"]["status"] == "True", (
        f"ClusterLocked should be True while blocked; got {conditions['ClusterLocked']}"
    )
    assert conditions["ClusterLocked"]["reason"] == "ClusterOccupied", (
        f"ClusterLocked reason should be ClusterOccupied; "
        f"got {conditions['ClusterLocked'].get('reason')!r}"
    )
    assert not workload_exists("test-excl-wait"), (
        "No Workload should be created while Blocked"
    )

    # Wait for the occupant to finish, then the exclusive job should unblock.
    poll_phase(
        k8s,
        "test-occupant",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )

    phase = poll_phase(
        k8s,
        "test-excl-wait",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-excl-wait")


def test_lock_blocks_cluster_pinned_job(k8s):
    """A non-exclusive cluster-pinned job enters Blocked when the cluster is locked."""
    create_job(
        k8s,
        "test-excl-lock",
        {
            "cluster": "cluster-2",
            "exclusive": True,
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )
    poll_phase(k8s, "test-excl-lock", terminal={Phase.RUNNING}, timeout=30)

    create_job(
        k8s,
        "test-blocked-pin",
        {
            "cluster": "cluster-2",
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    phase = poll_phase(
        k8s,
        "test-blocked-pin",
        terminal={Phase.BLOCKED, Phase.PENDING, Phase.ADMITTED, Phase.RUNNING},
        timeout=15,
    )
    assert phase == Phase.BLOCKED, (
        f"Cluster-pinned job should be Blocked while cluster is locked, got {phase!r}. "
        + job_status_summary(k8s, "test-blocked-pin")
    )

    job = get_job(k8s, "test-blocked-pin")
    assert "locked" in job["status"]["message"].lower(), (
        f"Message should mention 'locked'; got {job['status']['message']!r}"
    )
    conditions = {c["type"]: c for c in job["status"].get("conditions", [])}
    assert "ClusterLocked" in conditions, (
        f"Missing ClusterLocked condition; got types: {list(conditions)}"
    )
    assert conditions["ClusterLocked"]["status"] == "True", (
        f"ClusterLocked should be True while blocked; got {conditions['ClusterLocked']}"
    )
    assert conditions["ClusterLocked"]["reason"] == "ClusterLocked", (
        f"ClusterLocked reason should be ClusterLocked; "
        f"got {conditions['ClusterLocked'].get('reason')!r}"
    )

    # Wait for the exclusive job to finish, then the blocked job should proceed.
    poll_phase(
        k8s,
        "test-excl-lock",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )

    phase = poll_phase(
        k8s,
        "test-blocked-pin",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-blocked-pin")


def test_lock_blocks_hardware_only_job(k8s):
    """Hardware-only job gets nodeAffinity excluding locked clusters, lands on the only free one."""
    create_job(
        k8s,
        "test-excl-hw",
        {
            "cluster": "cluster-1",
            "exclusive": True,
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )
    poll_phase(k8s, "test-excl-hw", terminal={Phase.RUNNING}, timeout=30)

    create_job(
        k8s,
        "test-hw-avoid",
        {
            "hardware": {"gpuType": "a100", "gpuCount": 2},
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    poll_phase(
        k8s,
        "test-hw-avoid",
        terminal={
            Phase.PENDING,
            Phase.ADMITTED,
            Phase.RUNNING,
            Phase.SUCCEEDED,
            Phase.FAILED,
        },
        timeout=15,
    )

    excluded = get_workload_excluded_clusters("test-hw-avoid")
    assert "cluster-1" in excluded, (
        f"Workload should exclude locked cluster-1, got exclusions: {excluded}"
    )

    phase = poll_phase(
        k8s,
        "test-hw-avoid",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-hw-avoid")

    job = get_job(k8s, "test-hw-avoid")
    assert job["status"]["cluster"] == "cluster-2", (
        f"Hardware-only job should land on cluster-2 (only unlocked A100 cluster), "
        f"got {job['status'].get('cluster')!r}"
    )


def test_exclusive_without_cluster_fails(k8s):
    """exclusive: true without spec.cluster fails immediately."""
    create_job(
        k8s,
        "test-excl-nocluster",
        {
            "exclusive": True,
            "hardware": {"gpuType": "a100", "gpuCount": 2},
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    phase = poll_phase(
        k8s,
        "test-excl-nocluster",
        terminal={Phase.FAILED},
        timeout=15,
    )
    assert phase == Phase.FAILED, job_status_summary(k8s, "test-excl-nocluster")

    job = get_job(k8s, "test-excl-nocluster")
    msg = job["status"]["message"].lower()
    assert "cluster" in msg and "exclusive" in msg, (
        f"Failure message should mention both 'exclusive' and 'cluster', got: {msg!r}"
    )


def test_lock_released_on_completion(k8s):
    """After an exclusive job succeeds, previously-blocked jobs transition to Pending."""
    create_job(
        k8s,
        "test-excl-release",
        {
            "cluster": "cluster-1",
            "exclusive": True,
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )
    poll_phase(k8s, "test-excl-release", terminal={Phase.RUNNING}, timeout=30)

    create_job(
        k8s,
        "test-waiting",
        {
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    phase = poll_phase(
        k8s,
        "test-waiting",
        terminal={Phase.BLOCKED, Phase.PENDING},
        timeout=15,
    )
    assert phase == Phase.BLOCKED, (
        f"Job should be Blocked while cluster is locked, got {phase!r}. "
        + job_status_summary(k8s, "test-waiting")
    )
    job = get_job(k8s, "test-waiting")
    conditions = {c["type"]: c for c in job["status"].get("conditions", [])}
    assert "ClusterLocked" in conditions, (
        f"Missing ClusterLocked condition; got types: {list(conditions)}"
    )
    assert conditions["ClusterLocked"]["status"] == "True", (
        f"ClusterLocked should be True while blocked; got {conditions['ClusterLocked']}"
    )

    # Wait for the exclusive job to complete.
    poll_phase(
        k8s,
        "test-excl-release",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )

    # The waiting job should now proceed past Blocked.
    phase = poll_phase(
        k8s,
        "test-waiting",
        terminal={
            Phase.PENDING,
            Phase.ADMITTED,
            Phase.RUNNING,
            Phase.SUCCEEDED,
            Phase.FAILED,
        },
        timeout=30,
    )
    assert phase != Phase.BLOCKED, (
        f"Job should have left Blocked after lock release, got {phase!r}. "
        + job_status_summary(k8s, "test-waiting")
    )
