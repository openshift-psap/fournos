"""Scheduling tests — cluster pinning, hardware requests, Kueue admission."""

import json

from tests.conftest import (
    create_job,
    get_job,
    get_k8s_resource,
    get_pipelinerun_param,
    get_workload_flavor,
    get_workload_node_selector,
    poll_phase,
    workload_exists,
)


def test_cluster_pinned(k8s):
    """Cluster-pinned job: Kueue pins via nodeSelector, PipelineRun gets the right kubeconfig."""
    create_job(
        k8s,
        "test-cluster",
        {
            "cluster": "cluster-2",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )

    # Poll to Running so the Workload still exists for inspection
    # (the operator deletes it on Succeeded).
    poll_phase(
        k8s,
        "test-cluster",
        terminal={"Running", "Succeeded", "Failed"},
        timeout=30,
    )

    assert get_workload_node_selector("test-cluster") == {
        "fournos.dev/cluster": "cluster-2",
    }
    assert get_workload_flavor("test-cluster") == "cluster-2"
    assert (
        get_pipelinerun_param("test-cluster", "kubeconfig-secret")
        == "cluster-2-kubeconfig"
    )

    phase = poll_phase(
        k8s,
        "test-cluster",
        terminal={"Succeeded", "Failed"},
        timeout=60,
    )
    assert phase == "Succeeded"

    job = get_job(k8s, "test-cluster")
    assert job["status"]["cluster"] == "cluster-2"


def test_hardware_request(k8s):
    """Hardware request: Kueue picks a cluster with available GPU quota."""
    create_job(
        k8s,
        "test-hardware",
        {
            "hardware": {"gpuType": "a100", "gpuCount": 2},
            "forge": {"project": "testproj/llmd", "preset": "llama3"},
            "priority": "nightly",
        },
    )

    phase = poll_phase(
        k8s,
        "test-hardware",
        terminal={"Succeeded", "Failed"},
        timeout=60,
    )
    assert phase == "Succeeded"

    job = get_job(k8s, "test-hardware")
    assert job["status"].get("cluster") in ("cluster-1", "cluster-2")


def test_cluster_and_hardware(k8s):
    """Both cluster and hardware — pinned to cluster-4 which has H200 quota."""
    create_job(
        k8s,
        "test-cluster-hw",
        {
            "cluster": "cluster-4",
            "hardware": {"gpuType": "h200", "gpuCount": 2},
            "forge": {"project": "testproj/llmd", "preset": "llama3"},
        },
    )

    # Poll to Running so the Workload still exists for inspection.
    poll_phase(
        k8s,
        "test-cluster-hw",
        terminal={"Running", "Succeeded", "Failed"},
        timeout=30,
    )

    assert get_workload_node_selector("test-cluster-hw") == {
        "fournos.dev/cluster": "cluster-4",
    }
    assert get_workload_flavor("test-cluster-hw") == "cluster-4"
    assert (
        get_pipelinerun_param("test-cluster-hw", "kubeconfig-secret")
        == "cluster-4-kubeconfig"
    )

    phase = poll_phase(
        k8s,
        "test-cluster-hw",
        terminal={"Succeeded", "Failed"},
        timeout=60,
    )
    assert phase == "Succeeded"

    job = get_job(k8s, "test-cluster-hw")
    assert job["status"]["cluster"] == "cluster-4"


def test_alternative_pipeline_selection(k8s):
    """Alternative pipeline selection with cluster pinning."""
    create_job(
        k8s,
        "test-run-only",
        {
            "pipeline": "fournos-run-only",
            "cluster": "cluster-2",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )

    phase = poll_phase(
        k8s,
        "test-run-only",
        terminal={"Succeeded", "Failed"},
        timeout=60,
    )
    assert phase == "Succeeded"

    pr = get_k8s_resource("pipelinerun", "fournos-test-run-only")
    assert pr["spec"]["pipelineRef"]["name"] == "fournos-run-only"


def test_inadmissible_stays_pending(k8s):
    """Hardware request exceeding all cluster quotas stays Pending."""
    create_job(
        k8s,
        "test-inadmissible",
        {
            "hardware": {"gpuType": "a100", "gpuCount": 100},
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )

    phase = poll_phase(
        k8s,
        "test-inadmissible",
        terminal={"Running", "Admitted", "Succeeded", "Failed"},
        interval=3,
        timeout=15,
        raise_on_timeout=False,
    )
    assert phase == "Pending"
    assert workload_exists("test-inadmissible")


def test_cluster_without_required_gpu_stays_pending(k8s):
    """Requesting A100s on cluster-3 (which has 0 A100 quota) stays Pending."""
    create_job(
        k8s,
        "test-wrong-gpu",
        {
            "cluster": "cluster-3",
            "hardware": {"gpuType": "a100", "gpuCount": 2},
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )

    phase = poll_phase(
        k8s,
        "test-wrong-gpu",
        terminal={"Running", "Admitted", "Succeeded", "Failed"},
        interval=3,
        timeout=15,
        raise_on_timeout=False,
    )
    assert phase == "Pending"


def test_optional_spec_fields(k8s):
    """displayName, owner, configOverrides, and env are all forwarded correctly."""
    overrides = {"batch_size": "64", "lr": "0.001"}
    env = {"OCPCI_SUITE": "regression", "OCPCI_VARIANT": "nightly"}

    create_job(
        k8s,
        "test-opts",
        {
            "owner": "perf-team",
            "displayName": "nightly-llama3-benchmark",
            "cluster": "cluster-1",
            "forge": {
                "project": "testproj/llmd",
                "preset": "cks",
                "configOverrides": overrides,
            },
            "env": env,
        },
    )

    job = get_job(k8s, "test-opts")
    assert job["spec"]["owner"] == "perf-team"

    poll_phase(
        k8s,
        "test-opts",
        terminal={"Running", "Succeeded", "Failed"},
        timeout=30,
    )

    assert get_pipelinerun_param("test-opts", "job-name") == "nightly-llama3-benchmark"
    assert (
        json.loads(get_pipelinerun_param("test-opts", "forge-config-overrides"))
        == overrides
    )
    assert json.loads(get_pipelinerun_param("test-opts", "env")) == env
