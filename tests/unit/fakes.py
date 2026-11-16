"""In-memory fakes modelling the cluster contract the CLI talks to.

``FakeCluster`` implements the ``fournos.cli.ClusterClient`` port the way
``specs/cluster-resources.allium`` describes the cluster: execution pods are
selected by the ``fournos.dev/job-name`` label. Owner filtering is resolved
by the command layer (pod owner label ?? the owning FournosJob's owner), so
the port does not take an owner.
"""

from __future__ import annotations

from fournos.cli import ExecutionPod, FJob, Invocation, JobPhase, PodPhase


def make_invocation(**overrides) -> Invocation:
    """Build an Invocation with sensible defaults; overrides win."""
    defaults = {
        "system_user": "alice",
        "context_namespace": "ns",
        "default_kubeconfig": "/home/alice/.kube/config",
    }
    defaults.update(overrides)
    return Invocation(**defaults)


def make_fjob(
    name: str,
    *,
    namespace: str = "ns",
    owner: str | None = "alice",
    phase: JobPhase = JobPhase.RUNNING,
    **overrides,
) -> FJob:
    fields = {
        "name": name,
        "namespace": namespace,
        "owner": owner,
        "phase": phase,
        "cluster": "cluster-1",
        "pipeline_run": f"{name}-pr",
        "dashboard_url": f"https://tekton/{name}",
    }
    fields.update(overrides)
    return FJob(**fields)


def make_pod(
    name: str,
    *,
    job_name: str | None = None,
    namespace: str = "ns",
    owner_label: str | None = "alice",
    status: PodPhase = PodPhase.RUNNING,
    started_at=None,
    restarts: int = 0,
) -> ExecutionPod:
    from datetime import UTC, datetime, timedelta

    return ExecutionPod(
        name=name,
        namespace=namespace,
        job_name=job_name or name.removesuffix("-pod"),
        owner_label=owner_label,
        status=status,
        started_at=started_at or datetime.now(UTC) - timedelta(hours=1),
        restarts=restarts,
    )


class FakeCluster:
    """ClusterClient implementation backed by in-memory FJobs and pods."""

    def __init__(self, fjobs=(), pods=()):
        self._fjobs: dict[str, FJob] = {j.name: j for j in fjobs}
        self._pods: list[ExecutionPod] = list(pods)
        self.calls: list[tuple] = []

    # -- ClusterClient port --------------------------------------------------

    def list_fjobs(self, namespace: str) -> list[FJob]:
        self.calls.append(("list_fjobs", namespace))
        return [j for j in self._fjobs.values() if j.namespace == namespace]

    def list_pods(
        self,
        namespace: str,
        fjob_name: str | None = None,
    ) -> list[ExecutionPod]:
        self.calls.append(("list_pods", namespace, fjob_name))
        pods = [p for p in self._pods if p.namespace == namespace]
        if fjob_name is not None:
            pods = [p for p in pods if p.job_name == fjob_name]
        return pods

    def get_fjob(self, namespace: str, name: str) -> FJob | None:
        job = self._fjobs.get(name)
        return job if job is not None and job.namespace == namespace else None

    def set_shutdown(self, namespace: str, name: str, mode) -> FJob:
        job = self.get_fjob(namespace, name)
        if job is None:
            raise LookupError(f"no fjob {name} in namespace {namespace}")
        job.shutdown = mode
        return job

    def delete_fjob(self, namespace: str, name: str) -> None:
        job = self.get_fjob(namespace, name)
        if job is not None:
            del self._fjobs[name]

    # -- inspection helpers --------------------------------------------------

    def pod_call(self, index: int = -1) -> tuple:
        """Return the recorded list_pods call args (namespace, fjob)."""
        calls = [c for c in self.calls if c[0] == "list_pods"]
        return calls[index][1:]

    def has_fjob(self, name: str) -> bool:
        return name in self._fjobs
