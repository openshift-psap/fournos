"""Exclusive cluster locking tests — exclusive jobs lock clusters, blocking others.

An exclusive job requests all 100 cluster-slots for its target cluster,
preventing Kueue from admitting any other job there.  Non-exclusive jobs
request 1 slot each.
"""

import subprocess

import pytest

from fournos.core.constants import MAX_CLUSTER_SLOTS, Phase
from tests.conftest import (
    NAMESPACE,
    create_job,
    get_job,
    get_workload_cluster_slots,
    job_status_summary,
    poll_phase,
    workload_exists,
)

MOCK_SLEEP_SECONDS = "15"


@pytest.fixture(autouse=True)
def _slow_mock_pipeline():
    """Bump mock pipeline sleep so exclusive jobs stay Running long enough."""
    result = subprocess.run(
        [
            "kubectl",
            "get",
            "configmap",
            "fournos-mock-config",
            "-n",
            NAMESPACE,
            "-o",
            "jsonpath={.data.sleep}",
        ],
        capture_output=True,
        text=True,
    )
    original_sleep = result.stdout.strip() or "3"

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
            f'{{"data":{{"sleep":"{original_sleep}"}}}}',
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


def test_exclusive_workload_requests_all_slots(k8s):
    """Exclusive job's Workload requests MAX_CLUSTER_SLOTS cluster-slot units."""
    create_job(
        k8s,
        "test-excl-slots",
        {
            "cluster": "cluster-2",
            "exclusive": True,
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )
    poll_phase(
        k8s,
        "test-excl-slots",
        terminal={Phase.PENDING, Phase.ADMITTED, Phase.RUNNING},
        timeout=45,
    )

    slots = get_workload_cluster_slots("test-excl-slots")
    assert slots == MAX_CLUSTER_SLOTS, (
        f"Exclusive Workload should request {MAX_CLUSTER_SLOTS} slots, got {slots}"
    )


def test_normal_workload_requests_one_slot(k8s):
    """Non-exclusive job's Workload requests exactly 1 cluster-slot unit."""
    create_job(
        k8s,
        "test-normal-slots",
        {
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )
    poll_phase(
        k8s,
        "test-normal-slots",
        terminal={Phase.PENDING, Phase.ADMITTED, Phase.RUNNING},
        timeout=45,
    )

    slots = get_workload_cluster_slots("test-normal-slots")
    assert slots == 1, f"Normal Workload should request 1 slot, got {slots}"


def test_exclusive_blocks_cluster_pinned_job(k8s):
    """While an exclusive job runs, a cluster-pinned job to the same cluster stays Pending."""
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

    poll_phase(
        k8s,
        "test-blocked-pin",
        terminal={Phase.PENDING},
        message_substring="exclusively locked",
        timeout=30,
    )
    assert workload_exists("test-blocked-pin"), (
        "Workload should exist (Kueue holds it pending)"
    )
    msg = get_job(k8s, "test-blocked-pin")["status"]["message"]
    assert "cluster-2" in msg, (
        f"Message should mention the locked cluster, got: {msg!r}"
    )
    assert "test-excl-lock" in msg, (
        f"Message should name the exclusive job holding the lock, got: {msg!r}"
    )

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


def test_exclusive_steers_hardware_only_job(k8s):
    """Hardware-only job lands on the other cluster when one is exclusively locked."""
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

    phase = poll_phase(
        k8s,
        "test-hw-avoid",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-hw-avoid")

    job = get_job(k8s, "test-hw-avoid")
    assert job["status"]["cluster"] == "cluster-2", (
        f"Hardware-only job should land on cluster-2 (only A100 cluster with free slots), "
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


def test_exclusive_waits_for_cluster_to_clear(k8s):
    """Exclusive job stays Pending when cluster is occupied (not enough slots)."""
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

    poll_phase(
        k8s,
        "test-excl-wait",
        terminal={Phase.PENDING},
        message_substring="exclusive access",
        timeout=30,
    )
    msg = get_job(k8s, "test-excl-wait")["status"]["message"]
    assert "cluster-2" in msg, (
        f"Message should mention the target cluster, got: {msg!r}"
    )

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


def test_lock_released_on_completion(k8s):
    """After exclusive job completes, a previously-pending job proceeds to completion."""
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

    poll_phase(
        k8s,
        "test-waiting",
        terminal={Phase.PENDING},
        message_substring="exclusively locked",
        timeout=30,
    )
    msg = get_job(k8s, "test-waiting")["status"]["message"]
    assert "cluster-1" in msg, (
        f"Message should mention the locked cluster, got: {msg!r}"
    )
    assert "test-excl-release" in msg, (
        f"Message should name the exclusive job holding the lock, got: {msg!r}"
    )

    poll_phase(
        k8s,
        "test-excl-release",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )

    phase = poll_phase(
        k8s,
        "test-waiting",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-waiting")
