"""Resolving phase tests — execution engine resolution via K8s Jobs.

Tests cover the happy path (with and without user-provided hardware),
hardware validation (invalid GPU type, missing hardware), resolve Job
failures, shutdown during Resolving, and cleanup via ownerReference cascade.
"""

from fournos.core.constants import Phase
from tests.conftest import (
    GROUP,
    NAMESPACE,
    PLURAL,
    VERSION,
    create_failing_resolve_job,
    create_job,
    create_noop_resolve_job,
    get_job,
    get_workload_gpu_request,
    job_status_summary,
    poll_phase,
    poll_resource_gone,
    resolve_job_exists,
)


def _set_shutdown(k8s, name: str, mode: str) -> None:
    k8s.patch_namespaced_custom_object(
        GROUP,
        VERSION,
        NAMESPACE,
        PLURAL,
        name,
        body={"spec": {"shutdown": mode}},
    )


def test_happy_path_with_hardware(k8s):
    """Job with spec.hardware: user-provided values take precedence over the execution engine."""
    create_job(
        k8s,
        "test-resolve-hw",
        {
            "cluster": "cluster-3",
            "hardware": {"gpuType": "h200", "gpuCount": 4},
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
        "test-resolve-hw",
        terminal={Phase.RESOLVING},
        timeout=15,
    )
    assert phase == Phase.RESOLVING, job_status_summary(k8s, "test-resolve-hw")
    assert resolve_job_exists("test-resolve-hw"), (
        "Resolve Job should be created during Resolving"
    )

    poll_phase(
        k8s,
        "test-resolve-hw",
        terminal={Phase.PENDING, Phase.ADMITTED, Phase.RUNNING},
        timeout=60,
    )

    gpu_req = get_workload_gpu_request("test-resolve-hw", "h200")
    assert gpu_req == 4, (
        f"Workload should request 4 GPUs from spec.hardware; got {gpu_req}"
    )

    phase = poll_phase(
        k8s,
        "test-resolve-hw",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=90,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-resolve-hw")

    job = get_job(k8s, "test-resolve-hw")
    conditions = {c["type"]: c for c in job["status"].get("conditions", [])}
    assert "Resolved" in conditions, (
        f"Missing Resolved condition; got {list(conditions)}"
    )
    assert conditions["Resolved"]["status"] == "True", (
        f"Resolved should be True; got {conditions['Resolved']}"
    )


def test_happy_path_without_hardware(k8s):
    """Job without spec.hardware: the execution engine populates it during resolution."""
    create_job(
        k8s,
        "test-resolve-nohw",
        {
            "exclusive": False,
            "executionEngine": "forge",
            "executionEngineSpec": {
                "resolveImage": "fournos-mock-resolve:dev",
                "project": "testproj/llmd",
                "args": ["cks", "internal-test"],
            },
        },
    )

    poll_phase(
        k8s,
        "test-resolve-nohw",
        terminal={Phase.RESOLVING},
        timeout=15,
    )

    poll_phase(
        k8s,
        "test-resolve-nohw",
        terminal={Phase.PENDING, Phase.ADMITTED, Phase.RUNNING},
        timeout=60,
    )

    gpu_req = get_workload_gpu_request("test-resolve-nohw", "a100")
    assert gpu_req == 2, (
        f"Workload should request 2 a100 GPUs from resolved spec; got {gpu_req}"
    )

    phase = poll_phase(
        k8s,
        "test-resolve-nohw",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=90,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-resolve-nohw")

    job = get_job(k8s, "test-resolve-nohw")
    assert job["status"].get("cluster"), "Job should have a cluster assigned by Kueue"


def test_cluster_pin_without_hardware(k8s):
    """Cluster pin without hardware — the execution engine provides hardware, Kueue pins to cluster."""
    create_job(
        k8s,
        "test-resolve-pin",
        {
            "cluster": "cluster-1",
            "executionEngine": "forge",
            "executionEngineSpec": {
                "resolveImage": "fournos-mock-resolve:dev",
                "project": "testproj/llmd",
                "args": ["cks", "internal-test"],
            },
        },
    )

    poll_phase(
        k8s,
        "test-resolve-pin",
        terminal={Phase.PENDING, Phase.ADMITTED, Phase.RUNNING},
        timeout=60,
    )

    gpu_req = get_workload_gpu_request("test-resolve-pin", "a100")
    assert gpu_req == 2, (
        f"Workload should request 2 a100 GPUs from resolved spec; got {gpu_req}"
    )

    phase = poll_phase(
        k8s,
        "test-resolve-pin",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=90,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-resolve-pin")

    job = get_job(k8s, "test-resolve-pin")
    assert job["status"]["cluster"] == "cluster-1", (
        f"Expected cluster-1, got {job['status'].get('cluster')!r}"
    )


def test_shutdown_during_resolving(k8s):
    """Stopping a job during Resolving transitions to Stopped."""
    create_job(
        k8s,
        "test-resolve-stop",
        {
            "cluster": "cluster-1",
            "hardware": {"gpuType": "a100", "gpuCount": 2},
            "executionEngine": "forge",
            "executionEngineSpec": {
                "resolveImage": "fournos-mock-resolve:dev",
                "project": "testproj/llmd",
                "args": ["cks", "internal-test"],
            },
        },
    )

    poll_phase(
        k8s,
        "test-resolve-stop",
        terminal={Phase.RESOLVING},
        timeout=15,
    )

    _set_shutdown(k8s, "test-resolve-stop", "Stop")

    phase = poll_phase(
        k8s,
        "test-resolve-stop",
        terminal={Phase.STOPPED},
        timeout=30,
    )
    assert phase == Phase.STOPPED, job_status_summary(k8s, "test-resolve-stop")

    job = get_job(k8s, "test-resolve-stop")
    assert job["status"]["message"] == "Job stopped by user"


def test_delete_during_resolving_cleans_up(k8s):
    """Deleting a FournosJob during Resolving cleans up via ownerReference cascade."""
    create_job(
        k8s,
        "test-resolve-del",
        {
            "cluster": "cluster-1",
            "hardware": {"gpuType": "a100", "gpuCount": 2},
            "executionEngine": "forge",
            "executionEngineSpec": {
                "resolveImage": "fournos-mock-resolve:dev",
                "project": "testproj/llmd",
                "args": ["cks", "internal-test"],
            },
        },
    )

    poll_phase(
        k8s,
        "test-resolve-del",
        terminal={Phase.RESOLVING},
        timeout=15,
    )
    assert resolve_job_exists("test-resolve-del"), (
        "Resolve Job should exist during Resolving"
    )

    k8s.delete_namespaced_custom_object(
        GROUP,
        VERSION,
        NAMESPACE,
        PLURAL,
        "test-resolve-del",
    )

    poll_resource_gone(resolve_job_exists, "test-resolve-del", timeout=30)


def test_unknown_gpu_type(k8s):
    """Requesting a GPU type with no quota in any ClusterQueue -> Failed."""
    create_job(
        k8s,
        "test-bad-gpu",
        {
            "exclusive": False,
            "hardware": {"gpuType": "acbd1234", "gpuCount": 2},
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
        "test-bad-gpu",
        terminal={Phase.FAILED},
        timeout=90,
    )
    assert phase == Phase.FAILED, job_status_summary(k8s, "test-bad-gpu")

    job = get_job(k8s, "test-bad-gpu")
    msg = job["status"]["message"]
    assert "acbd1234" in msg.lower(), (
        f"Failure message should mention the GPU type 'acbd1234', got: {msg!r}"
    )
    assert "not available" in msg.lower(), (
        f"Failure message should say 'not available', got: {msg!r}"
    )


def test_resolve_job_failure(k8s):
    """Resolve Job fails -> FournosJob transitions to Failed.

    A pre-created resolve Job that immediately exits with code 1 simulates
    an execution engine failure.  The operator should detect the failure and
    set phase=Failed with a message mentioning the resolution failure.
    The failed Job is preserved for debugging.
    """
    create_failing_resolve_job("test-resolve-fail")

    create_job(
        k8s,
        "test-resolve-fail",
        {
            "cluster": "cluster-1",
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
        "test-resolve-fail",
        terminal={Phase.FAILED},
        timeout=45,
    )
    assert phase == Phase.FAILED, job_status_summary(k8s, "test-resolve-fail")

    job = get_job(k8s, "test-resolve-fail")
    msg = job["status"]["message"].lower()
    assert "resolution failed" in msg or "resolve" in msg, (
        f"Failure message should mention resolution failure, got: {msg!r}"
    )

    conditions = {c["type"]: c for c in job["status"].get("conditions", [])}
    assert "Resolved" in conditions, (
        f"Missing Resolved condition; got {list(conditions)}"
    )
    assert conditions["Resolved"]["status"] == "False", (
        f"Resolved should be False; got {conditions['Resolved']}"
    )

    assert resolve_job_exists("test-resolve-fail"), (
        "Failed resolve Job should be preserved for debugging"
    )


def test_nonexclusive_cluster_without_hardware_fails(k8s):
    """Non-exclusive + cluster + no hardware → Failed (hardware required).

    A noop resolve Job prevents the execution engine from populating
    hardware.  Since the job is non-exclusive, the missing hardware is
    not allowed (only exclusive+cluster jobs may omit it).
    """
    create_noop_resolve_job("test-nex-nohw")

    create_job(
        k8s,
        "test-nex-nohw",
        {
            "exclusive": False,
            "cluster": "cluster-1",
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
        "test-nex-nohw",
        terminal={Phase.FAILED},
        message_substring="No hardware requirements",
        timeout=45,
    )
    assert phase == Phase.FAILED, job_status_summary(k8s, "test-nex-nohw")

    conditions = {
        c["type"]: c
        for c in get_job(k8s, "test-nex-nohw")["status"].get("conditions", [])
    }
    assert conditions["Resolved"]["status"] == "False"
    assert conditions["Resolved"]["reason"] == "NoHardware"


def test_resolve_empty_hw(k8s):
    """Resolve Job succeeds but doesn't populate spec.hardware -> Failed.

    A pre-created noop resolve Job exits successfully without patching
    the FournosJob spec.  Since no hardware was provided by the user
    either, the job should fail with 'No hardware requirements'.
    """
    create_noop_resolve_job("test-resolve-noconfig")

    create_job(
        k8s,
        "test-resolve-noconfig",
        {
            "exclusive": False,
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
        "test-resolve-noconfig",
        terminal={Phase.FAILED},
        message_substring="No hardware requirements",
        timeout=45,
    )
    assert phase == Phase.FAILED, job_status_summary(k8s, "test-resolve-noconfig")

    conditions = {
        c["type"]: c
        for c in get_job(k8s, "test-resolve-noconfig")["status"].get("conditions", [])
    }
    assert conditions["Resolved"]["status"] == "False"
    assert conditions["Resolved"]["reason"] == "NoHardware"
