"""Lifecycle tests — completion cleanup, deletion cleanup, job listing."""

import time

from tests.conftest import (
    GROUP,
    NAMESPACE,
    PLURAL,
    VERSION,
    create_job,
    pipelinerun_exists,
    poll_phase,
    poll_resource_gone,
    workload_exists,
)


def test_workload_cleaned_after_completion(k8s):
    """After a job reaches Succeeded, the operator deletes the Kueue Workload."""
    create_job(
        k8s,
        "test-wl-cleanup",
        {
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )

    phase = poll_phase(
        k8s,
        "test-wl-cleanup",
        terminal={"Succeeded", "Failed"},
        timeout=60,
    )
    assert phase == "Succeeded"

    poll_resource_gone(workload_exists, "test-wl-cleanup")


def test_delete_cleans_up_resources(k8s):
    """Deleting a FournosJob triggers cleanup of Workload and PipelineRun."""
    create_job(
        k8s,
        "test-delete",
        {
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )

    poll_phase(
        k8s,
        "test-delete",
        terminal={"Running", "Succeeded", "Failed"},
        timeout=30,
    )
    assert pipelinerun_exists("test-delete")

    k8s.delete_namespaced_custom_object(
        GROUP,
        VERSION,
        NAMESPACE,
        PLURAL,
        "test-delete",
    )

    poll_resource_gone(workload_exists, "test-delete")
    poll_resource_gone(pipelinerun_exists, "test-delete")


def test_list_multiple_jobs(k8s):
    """Multiple FournosJobs are visible via the K8s API."""
    create_job(
        k8s,
        "test-list-a",
        {
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    create_job(
        k8s,
        "test-list-b",
        {
            "cluster": "cluster-2",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )

    jobs = k8s.list_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURAL)
    names = {j["metadata"]["name"] for j in jobs["items"]}
    assert "test-list-a" in names
    assert "test-list-b" in names
    assert len(jobs["items"]) == 2


def test_filter_jobs_by_phase(k8s):
    """Jobs at different phases are distinguishable via status."""
    create_job(
        k8s,
        "test-filter-ok",
        {
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    create_job(
        k8s,
        "test-filter-stuck",
        {
            "hardware": {"gpuType": "a100", "gpuCount": 100},
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )

    poll_phase(
        k8s,
        "test-filter-ok",
        terminal={"Admitted", "Running", "Succeeded", "Failed"},
        timeout=30,
    )
    time.sleep(3)

    jobs = k8s.list_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURAL)
    phases = {
        j["metadata"]["name"]: j.get("status", {}).get("phase", "")
        for j in jobs["items"]
    }
    assert phases["test-filter-stuck"] == "Pending"
    assert phases["test-filter-ok"] in ("Admitted", "Running", "Succeeded")
