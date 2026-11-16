"""KubeClusterClient — the real cluster adapter behind the CLI port.

Verifies the label selectors used for pod lookup (the fournos.dev/owner and
fournos.dev/job-name labels from specs/cluster-resources.allium) and the
mapping between cluster CRs and the CLI's domain types.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fournos.cli import JobPhase, PodPhase, Shutdown
from fournos.cli.cluster import KubeClusterClient

FJOB = {
    "metadata": {
        "name": "job-a",
        "namespace": "ns",
        "creationTimestamp": "2026-11-10T08:00:00Z",
    },
    "spec": {"owner": "alice", "shutdown": "Stop"},
    "status": {
        "phase": "Running",
        "cluster": "cluster-1",
        "pipelineRun": "job-a-pr",
        "dashboardURL": "https://tekton/job-a",
    },
}

POD = {
    "metadata": {
        "name": "job-a-pod",
        "namespace": "ns",
        "labels": {
            "app.kubernetes.io/managed-by": "fournos",
            "fournos.dev/job-name": "job-a",
            "fournos.dev/owner": "alice",
        },
    },
    "spec": {"containers": [{"name": "step"}]},
    "status": {
        "phase": "Running",
        "startTime": "2026-11-10T09:00:00Z",
        "containerStatuses": [{"restartCount": 1}, {"restartCount": 2}],
    },
}


class RecordingApi:
    """Stands in for kubernetes.client.CustomObjectsApi / CoreV1Api."""

    def __init__(self, list_result=None, get_result=None):
        self.calls: list[tuple] = []
        self._list_result = list_result or {"items": []}
        self._get_result = get_result

    def list_namespaced_custom_object(self, **kwargs):
        self.calls.append(("list_custom", kwargs))
        return self._list_result

    def list_namespaced_pod(self, **kwargs):
        self.calls.append(("list_pod", kwargs))
        return self._list_result

    def get_namespaced_custom_object(self, **kwargs):
        self.calls.append(("get_custom", kwargs))
        if self._get_result is None:
            from kubernetes import client

            raise client.exceptions.ApiException(status=404, reason="Not Found")
        return self._get_result

    def patch_namespaced_custom_object(self, **kwargs):
        self.calls.append(("patch_custom", kwargs))
        return self._get_result

    def delete_namespaced_custom_object(self, **kwargs):
        self.calls.append(("delete_custom", kwargs))


class TestListPodsSelectors:
    """Execution pods are selected by the fournos.dev/job-name label — Tekton
    force-overwrites app.kubernetes.io/managed-by=tekton-pipelines on the pods
    it creates, so managed-by=fournos can never match them. Tekton's
    affinity-assistant pods inherit the job-name label but are volume-affinity
    infrastructure, so every lookup excludes them by component label. Owner
    filtering is not part of the selector: it resolves the effective owner per
    pod (label ?? the owning FournosJob's owner) in the command layer."""

    def _client(self, pods=(POD,)):
        api = RecordingApi(list_result={"items": list(pods)})
        return KubeClusterClient(custom_objects=api, core=None), api

    def test_no_filters_uses_job_name_existence_selector(self):
        client, api = self._client()
        pods = client.list_pods("ns")
        assert api.calls[-1] == (
            "list_pod",
            {
                "namespace": "ns",
                "label_selector": (
                    "fournos.dev/job-name,"
                    "app.kubernetes.io/component!=affinity-assistant"
                ),
            },
        )
        assert [p.name for p in pods] == ["job-a-pod"]

    def test_fjob_filter_adds_job_name_equality_selector(self):
        client, api = self._client()
        client.list_pods("ns", fjob_name="job-a")
        assert api.calls[-1][1]["label_selector"] == (
            "fournos.dev/job-name=job-a,app.kubernetes.io/component!=affinity-assistant"
        )


class TestFJobMapping:
    def test_cr_maps_to_domain_type(self):
        api = RecordingApi(list_result={"items": [FJOB]})
        client = KubeClusterClient(custom_objects=api, core=None)
        (job,) = client.list_fjobs("ns")
        assert job.name == "job-a"
        assert job.namespace == "ns"
        assert job.owner == "alice"
        assert job.phase is JobPhase.RUNNING
        assert job.cluster == "cluster-1"
        assert job.pipeline_run == "job-a-pr"
        assert job.dashboard_url == "https://tekton/job-a"
        assert job.shutdown is Shutdown.STOP
        assert job.created_at == datetime(2026, 11, 10, 8, 0, tzinfo=UTC)

    def test_missing_optional_fields_map_to_none(self):
        bare = {
            "metadata": {"name": "job-b", "namespace": "ns"},
            "spec": {},
            "status": {"phase": "Pending"},
        }
        api = RecordingApi(list_result={"items": [bare]})
        client = KubeClusterClient(custom_objects=api, core=None)
        (job,) = client.list_fjobs("ns")
        assert job.owner is None
        assert job.cluster is None
        assert job.pipeline_run is None
        assert job.dashboard_url is None
        assert job.shutdown is None
        assert job.created_at is None

    def test_get_fjob_returns_none_on_404(self):
        client = KubeClusterClient(custom_objects=RecordingApi(), core=None)
        assert client.get_fjob("ns", "nope") is None


class TestPodMapping:
    def test_cr_maps_to_domain_type(self):
        client, _api = (
            KubeClusterClient(
                custom_objects=None, core=RecordingApi(list_result={"items": [POD]})
            ),
            None,
        )
        pods = client.list_pods("ns")
        (pod,) = pods
        assert pod.name == "job-a-pod"
        assert pod.namespace == "ns"
        assert pod.job_name == "job-a"
        assert pod.owner_label == "alice"
        assert pod.status is PodPhase.RUNNING
        assert pod.started_at == datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
        assert pod.restarts == 3  # summed across containers


class TestMutations:
    def test_set_shutdown_patches_spec(self):
        api = RecordingApi(get_result=FJOB)
        client = KubeClusterClient(custom_objects=api, core=None)
        client.set_shutdown("ns", "job-a", Shutdown.TERMINATE)
        kind, kwargs = api.calls[-1]
        assert kind == "patch_custom"
        assert kwargs["body"] == {"spec": {"shutdown": "Terminate"}}

    def test_delete_fjob_calls_delete(self):
        api = RecordingApi()
        client = KubeClusterClient(custom_objects=api, core=None)
        client.delete_fjob("ns", "job-a")
        kind, kwargs = api.calls[-1]
        assert kind == "delete_custom"
        assert kwargs["name"] == "job-a"


INSECURE_KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://fake.example:6443
    insecure-skip-tls-verify: true
  name: fake
contexts:
- context:
    cluster: fake
    namespace: forge-praxis
    user: fake
  name: fake
current-context: fake
users:
- name: fake
  user:
    token: abc
"""


class TestInsecureKubeconfig:
    def test_skip_tls_verify_context_silences_urllib3_warning(self, tmp_path):
        import warnings

        import urllib3
        from kubernetes import client as k8s_client

        kubeconfig = tmp_path / "kubeconfig"
        kubeconfig.write_text(INSECURE_KUBECONFIG)

        saved_filters = warnings.filters[:]
        saved_config = k8s_client.Configuration.get_default_copy()
        try:
            client_ = KubeClusterClient(str(kubeconfig))
            client_._ensure_config()
            assert k8s_client.Configuration.get_default_copy().verify_ssl is False
            # InsecureRequestWarning is in the ignore filter list
            assert any(
                f[0] == "ignore" and f[2] is urllib3.exceptions.InsecureRequestWarning
                for f in warnings.filters
            )
        finally:
            warnings.filters = saved_filters
            k8s_client.Configuration.set_default(saved_config)
