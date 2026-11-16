"""Label propagation — specs/cluster-resources.allium rules
StampJobLabelsOnManagedResource and StampJobLabelsOnExecutionPod, and the
ManagedResourcesCarryJobLabels / ExecutionPodsCarryJobLabels invariants.

The builders carry an internal list of KV labels to apply; today the handler
derives it from the FournosJob spec and it carries fournos.dev/owner when the
job has an owner. Base labels (managed-by, job-name) are always stamped.
"""

from __future__ import annotations

from fournos.core.constants import LABEL_JOB_NAME, LABEL_MANAGED_BY, LABEL_OWNER
from fournos.core.kueue import KueueClient
from fournos.core.resolve import ResolveClient
from fournos.core.tekton import TektonClient

OWNER_REF = {
    "apiVersion": "fournos.dev/v1",
    "kind": "FournosJob",
    "name": "job-a",
    "uid": "uid-1",
}


class _Result:
    def __init__(self, body):
        self._body = body

    def to_dict(self):
        return self._body


class FakeBatchApi:
    def __init__(self):
        self.created: list[tuple[str, dict]] = []

    def create_namespaced_job(self, namespace, body):
        self.created.append((namespace, body))
        return _Result(body)


class FakeCustomApi:
    def __init__(self):
        self.created: list[dict] = []

    def create_namespaced_custom_object(self, *, body, **_):
        self.created.append(body)
        return body


class TestResolveJobStamping:
    def test_owner_label_is_stamped_with_base_labels(self):
        batch = FakeBatchApi()
        ResolveClient(batch).create_job(
            name="job-a",
            owner_ref=OWNER_REF,
            image="img:latest",
            labels={LABEL_OWNER: "alice"},
        )
        _, body = batch.created[0]
        labels = body["metadata"]["labels"]
        pod_labels = body["spec"]["template"]["metadata"]["labels"]
        for stamped in (labels, pod_labels):
            assert stamped[LABEL_MANAGED_BY] == "fournos"
            assert stamped[LABEL_JOB_NAME] == "job-a"
            assert stamped[LABEL_OWNER] == "alice"

    def test_arbitrary_kv_labels_flow_through(self):
        batch = FakeBatchApi()
        ResolveClient(batch).create_job(
            name="job-a",
            owner_ref=OWNER_REF,
            image="img:latest",
            labels={"team": "gpu-bench"},
        )
        _, body = batch.created[0]
        for stamped in (
            body["metadata"]["labels"],
            body["spec"]["template"]["metadata"]["labels"],
        ):
            assert stamped["team"] == "gpu-bench"

    def test_without_labels_only_base_labels_stamped(self):
        batch = FakeBatchApi()
        ResolveClient(batch).create_job(
            name="job-a", owner_ref=OWNER_REF, image="img:latest", labels=None
        )
        _, body = batch.created[0]
        for stamped in (
            body["metadata"]["labels"],
            body["spec"]["template"]["metadata"]["labels"],
        ):
            assert stamped[LABEL_JOB_NAME] == "job-a"
            assert LABEL_OWNER not in stamped


class TestWorkloadStamping:
    def test_owner_label_is_stamped_with_base_labels(self):
        api = FakeCustomApi()
        KueueClient(api).create_workload(name="job-a", labels={LABEL_OWNER: "alice"})
        labels = api.created[0]["metadata"]["labels"]
        assert labels[LABEL_JOB_NAME] == "job-a"
        assert labels[LABEL_OWNER] == "alice"

    def test_without_labels_only_base_labels_stamped(self):
        api = FakeCustomApi()
        KueueClient(api).create_workload(name="job-a", labels=None)
        labels = api.created[0]["metadata"]["labels"]
        assert labels[LABEL_JOB_NAME] == "job-a"
        assert LABEL_OWNER not in labels


class TestPipelineRunStamping:
    def test_owner_label_is_stamped_with_base_labels(self):
        api = FakeCustomApi()
        TektonClient(api).create_pipeline_run(
            name="job-a",
            pipeline="fournos-full",
            kubeconfig_secret=None,
            resolved_secrets=[],
            cluster="cluster-1",
            labels={LABEL_OWNER: "alice"},
        )
        body = api.created[0]
        # ExecutionPodsCarryJobLabels: pods inherit the taskRunTemplate labels
        for stamped in (
            body["metadata"]["labels"],
            body["spec"]["taskRunTemplate"]["metadata"]["labels"],
        ):
            assert stamped[LABEL_JOB_NAME] == "job-a"
            assert stamped[LABEL_OWNER] == "alice"

    def test_without_labels_only_base_labels_stamped(self):
        api = FakeCustomApi()
        TektonClient(api).create_pipeline_run(
            name="job-a",
            pipeline="fournos-full",
            kubeconfig_secret=None,
            resolved_secrets=[],
            cluster="cluster-1",
            labels=None,
        )
        body = api.created[0]
        for stamped in (
            body["metadata"]["labels"],
            body["spec"]["taskRunTemplate"]["metadata"]["labels"],
        ):
            assert stamped[LABEL_JOB_NAME] == "job-a"
            assert LABEL_OWNER not in stamped
