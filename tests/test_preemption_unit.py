"""Unit tests for one-way handoff after Kueue evicts a Fournos Workload."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fournos.core.constants import Phase
from fournos.core.kueue import KueueClient
from fournos.core.tekton import (
    ANNOTATION_PREEMPTION_CLEANUP_PROJECT,
    ANNOTATION_PREEMPTION_CLEANUP_TASK,
    PREEMPTION_CLEANUP_RESULT,
    PREEMPTION_CLEANUP_SERVICE_ACCOUNT,
    TektonClient,
)
from fournos.operator import reconcile
from fournos.state import ctx

NAME = "test-preempted"
NAMESPACE = "fournos-system"
CLEANUP_TASK = "04-post-cleanup"
CLEANUP_PROJECT = "rhaiis"
CLEANUP_RUN = f"{NAME}-cleanup"
BODY = {"metadata": {"name": NAME, "uid": "test-uid"}}
NORMAL_SPEC = {"cluster": "cluster-1", "exclusive": True}


@pytest.fixture(autouse=True)
def _clean_around_test():
    """Override the integration fixture: these tests never contact Kubernetes."""


def _evicted_workload() -> dict:
    # Kueue leaves Admitted=True while it waits for the owner to stop an
    # evicted workload.  Checking Admitted alone would start this job.
    return {
        "status": {
            "conditions": [
                {"type": "Admitted", "status": "True"},
                {
                    "type": "Evicted",
                    "status": "True",
                    "reason": "Preempted",
                    "message": "Preempted by a higher-priority workload",
                },
            ],
            "admission": {
                "podSetAssignments": [
                    {"flavors": {"fournos/cluster-slot": "cluster-1"}}
                ]
            },
        }
    }


def _pipeline_run(
    *,
    completed: bool = False,
    cleanup_annotation: bool = True,
    child_task: str | None = CLEANUP_TASK,
) -> dict:
    annotations = (
        {"fournos.dev/preemption-cleanup-task": CLEANUP_TASK}
        if cleanup_annotation
        else {}
    )
    status: dict = {
        "conditions": [
            {
                "type": "Succeeded",
                "status": "False" if completed else "Unknown",
                "reason": "Cancelled" if completed else "Running",
                "message": "PipelineRun cancelled" if completed else "Still running",
            }
        ]
    }
    if completed:
        status["completionTime"] = "2026-11-10T17:00:00Z"
    if child_task is not None:
        status["childReferences"] = [
            {
                "apiVersion": "tekton.dev/v1",
                "kind": "TaskRun",
                "name": CLEANUP_RUN,
                "pipelineTaskName": child_task,
            }
        ]
    return {
        "metadata": {"name": NAME, "annotations": annotations},
        "spec": {
            "pipelineRef": {"name": "fournos-full"},
            "taskRunTemplate": {
                "serviceAccountName": PREEMPTION_CLEANUP_SERVICE_ACCOUNT
            },
        },
        "status": status,
    }


def _task_run(*, succeeded: bool, verified: bool = True) -> dict:
    return {
        "metadata": {"name": CLEANUP_RUN},
        "status": {
            "completionTime": "2026-11-10T16:59:59Z",
            "results": (
                [{"name": PREEMPTION_CLEANUP_RESULT, "value": "true"}]
                if verified
                else []
            ),
            "conditions": [
                {
                    "type": "Succeeded",
                    "status": "True" if succeeded else "False",
                    "reason": "Succeeded" if succeeded else "Failed",
                }
            ],
        },
    }


class FakeKueue:
    def __init__(self, workload: dict | None):
        self.workload = workload
        self.deleted: list[str] = []

    def get_workload_or_none(self, name: str) -> dict | None:
        assert name == NAME
        return self.workload

    def delete_workload(self, name: str) -> None:
        assert name == NAME
        self.deleted.append(name)
        self.workload = None


class FakeTekton:
    def __init__(self, pipeline_run: dict | None = None):
        self.pipeline_run = pipeline_run
        self.task_runs: dict[str, dict] = {}
        self.cancelled: list[tuple[str, bool]] = []
        self.created: list[dict] = []

    def get_pipeline_run_or_none(self, name: str) -> dict | None:
        assert name == NAME
        return self.pipeline_run

    def get_task_run_or_none(self, name: str) -> dict | None:
        return self.task_runs.get(name)

    def cancel_pipeline_run(self, name: str, *, graceful: bool = True) -> None:
        self.cancelled.append((name, graceful))

    def create_pipeline_run(self, **kwargs) -> dict:
        self.created.append(kwargs)
        return {"metadata": {"name": kwargs["name"]}}


class FakeRegistry:
    def copy_kubeconfig_secret(self, cluster: str, name: str, owner_ref: dict) -> str:
        return f"{name}-kubeconfig"

    def copy_secrets(self, secret_refs: list, name: str, owner_ref: dict) -> list:
        return []


class FakeTektonApi:
    def __init__(self, pipeline: dict):
        self.pipeline = pipeline
        self.created_body: dict | None = None

    def get_namespaced_custom_object(self, **kwargs) -> dict:
        assert kwargs["plural"] == "pipelines"
        return self.pipeline

    def create_namespaced_custom_object(self, **kwargs) -> dict:
        self.created_body = kwargs["body"]
        return self.created_body


@pytest.fixture
def clients(monkeypatch):
    kueue = FakeKueue(_evicted_workload())
    tekton = FakeTekton()
    monkeypatch.setattr(ctx, "kueue", kueue)
    monkeypatch.setattr(ctx, "tekton", tekton)
    monkeypatch.setattr(ctx, "registry", FakeRegistry())
    return kueue, tekton


def _reconcile(status: dict, *, spec: dict = NORMAL_SPEC) -> dict:
    patch = SimpleNamespace(status={})
    reconcile(spec, NAME, NAMESPACE, status, patch, BODY)
    return {**status, **patch.status}


@pytest.mark.parametrize("phase", [Phase.PENDING, Phase.ADMITTED])
def test_eviction_before_pipeline_creation_releases_without_starting(
    clients, phase: Phase
) -> None:
    kueue, tekton = clients

    stopping = _reconcile({"phase": phase, "conditions": []})
    assert stopping["phase"] == Phase.STOPPING
    assert stopping["stopReason"] == "Preempted"
    assert kueue.deleted == []

    result = _reconcile(stopping)

    assert result["phase"] == Phase.STOPPED
    assert result["stopReason"] == "Preempted"
    assert "preempt" in result["message"].lower()
    assert kueue.deleted == [NAME]
    assert tekton.created == []
    assert tekton.cancelled == []


def test_evicted_lock_only_job_releases_its_workload(clients) -> None:
    kueue, tekton = clients

    spec = {"cluster": "cluster-1", "exclusive": True, "lockOnly": True}
    stopping = _reconcile(
        {"phase": Phase.ADMITTED, "conditions": []},
        spec=spec,
    )
    assert stopping["phase"] == Phase.STOPPING
    assert kueue.deleted == []

    result = _reconcile(stopping, spec=spec)

    assert result["phase"] == Phase.STOPPED
    assert result["stopReason"] == "Preempted"
    assert kueue.deleted == [NAME]
    assert tekton.created == []


def test_running_eviction_cancels_gracefully_and_keeps_quota(clients) -> None:
    kueue, tekton = clients
    tekton.pipeline_run = _pipeline_run()

    stopping = _reconcile({"phase": Phase.RUNNING, "conditions": []})

    assert stopping["phase"] == Phase.STOPPING
    assert stopping["stopReason"] == "Preempted"
    assert tekton.cancelled == [(NAME, True)]
    assert kueue.deleted == []

    still_stopping = _reconcile(stopping)
    assert still_stopping["phase"] == Phase.STOPPING
    assert kueue.deleted == []


def test_late_pipeline_run_is_cancelled_before_release(clients) -> None:
    kueue, tekton = clients

    stopping = _reconcile({"phase": Phase.ADMITTED, "conditions": []})
    assert stopping["phase"] == Phase.STOPPING
    assert kueue.deleted == []

    tekton.pipeline_run = _pipeline_run()
    stopping = _reconcile(stopping)
    assert stopping["pipelineRun"] == NAME
    assert tekton.cancelled == [(NAME, True)]
    assert kueue.deleted == []

    tekton.pipeline_run = _pipeline_run(completed=True)
    tekton.task_runs[CLEANUP_RUN] = _task_run(succeeded=True)
    result = _reconcile(stopping)
    assert result["phase"] == Phase.STOPPED
    assert kueue.deleted == [NAME]


def test_eviction_during_user_shutdown_switches_to_verified_cleanup(clients) -> None:
    kueue, tekton = clients
    tekton.pipeline_run = _pipeline_run(completed=True, cleanup_annotation=False)

    stopping = _reconcile(
        {"phase": Phase.STOPPING, "pipelineRun": NAME, "conditions": []}
    )
    assert stopping["stopReason"] == "Preempted"
    assert kueue.deleted == []

    result = _reconcile(stopping)
    assert result["phase"] == Phase.STOPPING
    assert kueue.deleted == []


def test_cleanup_success_releases_workload_and_finishes_preemption(clients) -> None:
    kueue, tekton = clients
    tekton.pipeline_run = _pipeline_run(completed=True)
    tekton.task_runs[CLEANUP_RUN] = _task_run(succeeded=True)

    result = _reconcile(
        {
            "phase": Phase.STOPPING,
            "stopReason": "Preempted",
            "pipelineRun": NAME,
            "conditions": [],
        }
    )

    assert result["phase"] == Phase.STOPPED
    assert result["stopReason"] == "Preempted"
    assert "preempt" in result["message"].lower()
    assert kueue.deleted == [NAME]


@pytest.mark.parametrize(
    "failure",
    [
        "no-annotation",
        "no-child",
        "wrong-child",
        "no-task-run",
        "failed-task-run",
        "no-verification-result",
        "no-pipeline-run",
        "wrong-service-account",
    ],
)
def test_missing_or_failed_cleanup_keeps_quota(clients, failure: str) -> None:
    kueue, tekton = clients
    tekton.pipeline_run = _pipeline_run(completed=True)
    tekton.task_runs[CLEANUP_RUN] = _task_run(succeeded=True)

    if failure == "no-annotation":
        tekton.pipeline_run = _pipeline_run(completed=True, cleanup_annotation=False)
    elif failure == "no-child":
        tekton.pipeline_run = _pipeline_run(completed=True, child_task=None)
    elif failure == "wrong-child":
        tekton.pipeline_run = _pipeline_run(
            completed=True, child_task="05-export-artifacts"
        )
    elif failure == "no-task-run":
        tekton.task_runs.clear()
    elif failure == "failed-task-run":
        tekton.task_runs[CLEANUP_RUN] = _task_run(succeeded=False)
    elif failure == "no-verification-result":
        tekton.task_runs[CLEANUP_RUN] = _task_run(succeeded=True, verified=False)
    elif failure == "no-pipeline-run":
        tekton.pipeline_run = None
    elif failure == "wrong-service-account":
        tekton.pipeline_run["spec"]["taskRunTemplate"]["serviceAccountName"] = (
            "pipeline"
        )

    result = _reconcile(
        {
            "phase": Phase.STOPPING,
            "stopReason": "Preempted",
            "pipelineRun": NAME,
            "conditions": [],
        }
    )

    assert result["phase"] == Phase.STOPPING
    assert kueue.deleted == []


def test_clusterless_job_does_not_consult_kueue_eviction(clients) -> None:
    kueue, tekton = clients
    kueue.workload = None

    result = _reconcile(
        {"phase": Phase.ADMITTED, "conditions": []},
        spec={"clusterless": True, "exclusive": False},
    )

    assert result["phase"] == Phase.RUNNING
    assert "stopReason" not in result
    assert kueue.deleted == []
    assert len(tekton.created) == 1


@pytest.mark.parametrize(
    ("job_env", "allow_cleanup_contract"),
    [({}, True), ({"PULL_NUMBER": "123"}, False), ({"PULL_PULL_SHA": "abc"}, False)],
)
def test_pr_checkout_does_not_claim_deployed_cleanup_contract(
    clients, job_env: dict, allow_cleanup_contract: bool
) -> None:
    kueue, tekton = clients
    kueue.workload = None

    result = _reconcile(
        {"phase": Phase.ADMITTED, "cluster": "cluster-1", "conditions": []},
        spec={
            "cluster": "cluster-1",
            "exclusive": True,
            "executionEngine": {"forge": {"project": "rhaiis"}},
            "env": job_env,
        },
    )

    assert result["phase"] == Phase.RUNNING
    assert tekton.created[0]["engine_project"] == "rhaiis"
    assert tekton.created[0]["allow_cleanup_contract"] is allow_cleanup_contract


def test_evicted_workload_is_not_considered_admitted() -> None:
    workload = _evicted_workload()

    assert KueueClient.get_eviction_condition(workload)["reason"] == "Preempted"
    assert not KueueClient.is_admitted(workload)

    workload["status"]["conditions"][1]["status"] = "False"
    assert KueueClient.get_eviction_condition(workload) is None
    assert KueueClient.is_admitted(workload)


def test_pipeline_run_snapshots_its_cleanup_task_contract() -> None:
    pipeline = {
        "metadata": {
            "annotations": {
                ANNOTATION_PREEMPTION_CLEANUP_TASK: CLEANUP_TASK,
                ANNOTATION_PREEMPTION_CLEANUP_PROJECT: CLEANUP_PROJECT,
            }
        },
        "spec": {"finally": [{"name": CLEANUP_TASK}]},
    }
    api = FakeTektonApi(pipeline)

    TektonClient(api).create_pipeline_run(
        name=NAME,
        pipeline="fournos-full",
        kubeconfig_secret=f"{NAME}-kubeconfig",
        resolved_secrets=[],
        cluster="cluster-1",
        engine_project=CLEANUP_PROJECT,
    )

    assert api.created_body is not None
    annotations = api.created_body["metadata"]["annotations"]
    assert annotations[ANNOTATION_PREEMPTION_CLEANUP_TASK] == CLEANUP_TASK
    assert annotations[ANNOTATION_PREEMPTION_CLEANUP_PROJECT] == CLEANUP_PROJECT
    assert (
        api.created_body["spec"]["taskRunTemplate"]["serviceAccountName"]
        == PREEMPTION_CLEANUP_SERVICE_ACCOUNT
    )

    pipeline["metadata"]["annotations"][ANNOTATION_PREEMPTION_CLEANUP_TASK] = (
        "renamed-cleanup"
    )
    assert annotations[ANNOTATION_PREEMPTION_CLEANUP_TASK] == CLEANUP_TASK


def test_pipeline_run_rejects_cleanup_contract_outside_finally() -> None:
    pipeline = {
        "metadata": {
            "annotations": {
                ANNOTATION_PREEMPTION_CLEANUP_TASK: CLEANUP_TASK,
                ANNOTATION_PREEMPTION_CLEANUP_PROJECT: CLEANUP_PROJECT,
            }
        },
        "spec": {"finally": [{"name": "some-other-task"}]},
    }
    api = FakeTektonApi(pipeline)

    with pytest.raises(ValueError, match="not a finally task"):
        TektonClient(api).create_pipeline_run(
            name=NAME,
            pipeline="fournos-full",
            kubeconfig_secret=f"{NAME}-kubeconfig",
            resolved_secrets=[],
            cluster="cluster-1",
            engine_project=CLEANUP_PROJECT,
        )

    assert api.created_body is None


def test_pipeline_run_does_not_claim_another_projects_cleanup_contract() -> None:
    pipeline = {
        "metadata": {
            "annotations": {
                ANNOTATION_PREEMPTION_CLEANUP_TASK: CLEANUP_TASK,
                ANNOTATION_PREEMPTION_CLEANUP_PROJECT: CLEANUP_PROJECT,
            }
        },
        "spec": {"finally": [{"name": CLEANUP_TASK}]},
    }
    api = FakeTektonApi(pipeline)

    TektonClient(api).create_pipeline_run(
        name=NAME,
        pipeline="forge-full",
        kubeconfig_secret=f"{NAME}-kubeconfig",
        resolved_secrets=[],
        cluster="cluster-1",
        engine_project="llm-d",
    )

    assert (
        ANNOTATION_PREEMPTION_CLEANUP_TASK
        not in api.created_body["metadata"]["annotations"]
    )
    assert "serviceAccountName" not in api.created_body["spec"]["taskRunTemplate"]


def test_pr_run_uses_readonly_account_without_contract() -> None:
    pipeline = {
        "metadata": {
            "annotations": {
                ANNOTATION_PREEMPTION_CLEANUP_TASK: CLEANUP_TASK,
                ANNOTATION_PREEMPTION_CLEANUP_PROJECT: CLEANUP_PROJECT,
            }
        },
        "spec": {"finally": [{"name": CLEANUP_TASK}]},
    }
    api = FakeTektonApi(pipeline)

    TektonClient(api).create_pipeline_run(
        name=NAME,
        pipeline="forge-full",
        kubeconfig_secret=f"{NAME}-kubeconfig",
        resolved_secrets=[],
        cluster="cluster-1",
        engine_project=CLEANUP_PROJECT,
        allow_cleanup_contract=False,
    )

    assert api.created_body is not None
    assert (
        api.created_body["spec"]["taskRunTemplate"]["serviceAccountName"]
        == PREEMPTION_CLEANUP_SERVICE_ACCOUNT
    )
    assert (
        ANNOTATION_PREEMPTION_CLEANUP_TASK
        not in api.created_body["metadata"]["annotations"]
    )
