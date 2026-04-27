"""Scheduling tests — cluster pinning, hardware requests, Kueue admission."""

import yaml

from fournos.core.constants import Phase
from tests.conftest import (
    create_job,
    get_job,
    get_k8s_resource,
    get_pipelinerun_param,
    get_workload_flavor,
    get_workload_node_selector,
    job_status_summary,
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
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    # Poll to Running so the Workload still exists for inspection
    # (the operator deletes it on Succeeded).
    poll_phase(
        k8s,
        "test-cluster",
        terminal={Phase.RUNNING, Phase.SUCCEEDED, Phase.FAILED},
        timeout=30,
    )

    ns = get_workload_node_selector("test-cluster")
    assert ns == {"fournos.dev/cluster": "cluster-2"}, (
        f"Workload nodeSelector should pin to cluster-2, got {ns}"
    )
    flavor = get_workload_flavor("test-cluster")
    assert flavor == "cluster-2", f"Workload flavor should be cluster-2, got {flavor!r}"
    secret = get_pipelinerun_param("test-cluster", "kubeconfig-secret")
    assert secret == "cluster-2-kubeconfig", (
        f"PipelineRun kubeconfig-secret should be cluster-2-kubeconfig, got {secret!r}"
    )

    phase = poll_phase(
        k8s,
        "test-cluster",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-cluster")

    job = get_job(k8s, "test-cluster")
    assert job["status"]["cluster"] == "cluster-2", (
        f"Expected cluster cluster-2, got {job['status'].get('cluster')!r}"
    )


def test_hardware_request(k8s):
    """Hardware request: Kueue picks a cluster with available GPU quota."""
    create_job(
        k8s,
        "test-hardware",
        {
            "hardware": {"gpuType": "a100", "gpuCount": 2},
            "forge": {"project": "testproj/llmd", "args": ["llama3", "internal-test"]},
            "priority": "nightly",
        },
    )

    phase = poll_phase(
        k8s,
        "test-hardware",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-hardware")

    job = get_job(k8s, "test-hardware")
    cluster = job["status"].get("cluster")
    assert cluster in ("cluster-1", "cluster-2"), (
        f"Expected cluster-1 or cluster-2, got {cluster!r}"
    )


def test_cluster_and_hardware(k8s):
    """Both cluster and hardware — pinned to cluster-4 which has H200 quota."""
    create_job(
        k8s,
        "test-cluster-hw",
        {
            "cluster": "cluster-4",
            "hardware": {"gpuType": "h200", "gpuCount": 2},
            "forge": {"project": "testproj/llmd", "args": ["llama3", "internal-test"]},
        },
    )

    # Poll to Running so the Workload still exists for inspection.
    poll_phase(
        k8s,
        "test-cluster-hw",
        terminal={Phase.RUNNING, Phase.SUCCEEDED, Phase.FAILED},
        timeout=30,
    )

    ns = get_workload_node_selector("test-cluster-hw")
    assert ns == {"fournos.dev/cluster": "cluster-4"}, (
        f"Workload nodeSelector should pin to cluster-4, got {ns}"
    )
    flavor = get_workload_flavor("test-cluster-hw")
    assert flavor == "cluster-4", f"Workload flavor should be cluster-4, got {flavor!r}"
    secret = get_pipelinerun_param("test-cluster-hw", "kubeconfig-secret")
    assert secret == "cluster-4-kubeconfig", (
        f"PipelineRun kubeconfig-secret should be cluster-4-kubeconfig, got {secret!r}"
    )

    phase = poll_phase(
        k8s,
        "test-cluster-hw",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-cluster-hw")

    job = get_job(k8s, "test-cluster-hw")
    assert job["status"]["cluster"] == "cluster-4", (
        f"Expected cluster cluster-4, got {job['status'].get('cluster')!r}"
    )


def test_alternative_pipeline_selection(k8s):
    """Alternative pipeline selection with cluster pinning."""
    create_job(
        k8s,
        "test-run-only",
        {
            "pipeline": "fournos-run-only",
            "cluster": "cluster-2",
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    phase = poll_phase(
        k8s,
        "test-run-only",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-run-only")

    pr = get_k8s_resource("pipelinerun", "test-run-only")
    pipeline_ref = pr["spec"]["pipelineRef"]["name"]
    assert pipeline_ref == "fournos-run-only", (
        f"PipelineRun should reference fournos-run-only, got {pipeline_ref!r}"
    )


def test_inadmissible_stays_pending(k8s):
    """Hardware request exceeding all cluster quotas stays Pending with admission detail."""
    create_job(
        k8s,
        "test-inadmissible",
        {
            "hardware": {"gpuType": "a100", "gpuCount": 100},
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    poll_phase(
        k8s,
        "test-inadmissible",
        terminal={Phase.PENDING},
        timeout=45,
    )
    phase = poll_phase(
        k8s,
        "test-inadmissible",
        terminal={Phase.RUNNING, Phase.ADMITTED, Phase.SUCCEEDED, Phase.FAILED},
        interval=3,
        timeout=15,
        raise_on_timeout=False,
    )
    assert phase == Phase.PENDING, (
        f"Inadmissible job should stay Pending, got {phase!r}"
    )
    assert workload_exists("test-inadmissible"), (
        "Workload test-inadmissible should still exist"
    )

    job = get_job(k8s, "test-inadmissible")
    assert job["status"].get("message"), (
        "Pending job should have a status message explaining the admission state"
    )

    conditions = job["status"].get("conditions", [])
    wl_cond = next(
        (c for c in conditions if c["type"] == "WorkloadAdmitted"),
        None,
    )
    assert wl_cond is not None, (
        f"Pending job should have a WorkloadAdmitted condition; got types: "
        f"{[c['type'] for c in conditions]}"
    )
    assert wl_cond["status"] == "False", (
        f"WorkloadAdmitted should be False for inadmissible job; got {wl_cond}"
    )


def test_cluster_without_required_gpu_stays_pending(k8s):
    """Requesting A100s on cluster-3 (which has 0 A100 quota) stays Pending."""
    create_job(
        k8s,
        "test-wrong-gpu",
        {
            "cluster": "cluster-3",
            "hardware": {"gpuType": "a100", "gpuCount": 2},
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    poll_phase(
        k8s,
        "test-wrong-gpu",
        terminal={Phase.PENDING},
        timeout=45,
    )
    phase = poll_phase(
        k8s,
        "test-wrong-gpu",
        terminal={Phase.RUNNING, Phase.ADMITTED, Phase.SUCCEEDED, Phase.FAILED},
        interval=3,
        timeout=15,
        raise_on_timeout=False,
    )
    assert phase == Phase.PENDING, (
        f"Job requesting A100s on cluster-3 should stay Pending, got {phase!r}"
    )


def test_optional_spec_fields(k8s):
    """displayName, owner, args, configOverrides, and env are all forwarded correctly."""
    overrides = {
        "config": {
            "batch_size": 64,
            "vllm": {"version": "0.15.1"},
        },
        "logging": {"enabled": True, "level": "debug"},
        "tags": ["special", "demo"],
    }
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
                "args": ["cks", "internal-test"],
                "configOverrides": overrides,
            },
            "env": env,
        },
    )

    job = get_job(k8s, "test-opts")
    assert job["spec"]["owner"] == "perf-team", (
        f"Owner should be perf-team, got {job['spec'].get('owner')!r}"
    )

    poll_phase(
        k8s,
        "test-opts",
        terminal={Phase.RUNNING, Phase.SUCCEEDED, Phase.FAILED},
        timeout=30,
    )

    job_name_param = get_pipelinerun_param("test-opts", "job-name")
    assert job_name_param == "nightly-llama3-benchmark", (
        f"PipelineRun job-name param should be the displayName, got {job_name_param!r}"
    )

    forge_param = yaml.safe_load(get_pipelinerun_param("test-opts", "forge-config"))
    assert forge_param["project"] == "testproj/llmd", (
        f"forge.project mismatch: {forge_param.get('project')!r}"
    )
    assert forge_param["args"] == ["cks", "internal-test"], (
        f"forge.args mismatch: {forge_param.get('args')!r}"
    )
    assert forge_param.get("configOverrides") == overrides, (
        f"forge.configOverrides mismatch: {forge_param.get('configOverrides')}"
    )

    env_raw = get_pipelinerun_param("test-opts", "env")
    env_param = dict(line.split("=", 1) for line in env_raw.strip().splitlines())
    assert env_param == env, f"env param mismatch: {env_param}"
