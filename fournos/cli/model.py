"""Domain types for the fournos CLI — mirrors specs/fournos-cli.allium.

The CLI speaks in its own vocabulary (FJob, ExecutionPod, rows, prompts,
streams) so command logic never touches Kubernetes objects directly. The
cluster adapter in ``fournos.cli.cluster`` translates CRs into these types.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from fournos.core.constants import Phase, Shutdown

if TYPE_CHECKING:
    from fournos.cli.invocation import Invocation

# re-exported under the spec's name; values match the controller's CR status
JobPhase = Phase


class PodPhase(StrEnum):
    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"


class FailureReason(StrEnum):
    OWNER_UNRESOLVED = "owner-unresolved"
    FJOB_NOT_FOUND = "fjob-not-found"
    NO_RUNNING_POD = "no-running-pod"
    POD_NOT_FOUND = "pod-not-found"


class ConfirmationAction(StrEnum):
    SHUTDOWN = "shutdown"
    DELETE = "delete"


@dataclass
class FJob:
    name: str
    namespace: str
    owner: str | None = None
    phase: JobPhase = JobPhase.SCHEDULED
    cluster: str | None = None
    pipeline_run: str | None = None
    dashboard_url: str | None = None
    created_at: datetime | None = None
    shutdown: Shutdown | None = None


@dataclass
class ExecutionPod:
    name: str
    namespace: str
    job_name: str | None = None
    owner_label: str | None = None
    status: PodPhase = PodPhase.PENDING
    started_at: datetime | None = None
    restarts: int = 0


@dataclass
class CommandFailed:
    invocation: Invocation
    reason: FailureReason
    job: FJob | None = None
    hint: str | None = None


@dataclass
class ConfirmationPrompt:
    invocation: Invocation
    job: FJob
    action: ConfirmationAction
    shutdown_mode: Shutdown | None = None

    @property
    def warns_foreign_job(self) -> bool:
        return self.job.owner != self.invocation.resolved_owner


@dataclass
class LogStream:
    invocation: Invocation
    job: FJob
    offered_pods: list[ExecutionPod]
    pod: ExecutionPod
    follow: bool = False


@dataclass
class DeletionApplied:
    invocation: Invocation
    job: FJob


def _age(since: datetime | None):
    if since is None:
        return None
    return datetime.now(UTC) - since


@dataclass
class FJobRow:
    invocation: Invocation
    job: FJob

    @property
    def shown_name(self) -> str:
        return self.job.name

    @property
    def shown_owner(self) -> str | None:
        return self.job.owner

    @property
    def shown_phase(self) -> JobPhase:
        return self.job.phase

    @property
    def shown_cluster(self) -> str | None:
        return self.job.cluster

    @property
    def shown_age(self):
        return _age(self.job.created_at)


@dataclass
class PodRow:
    invocation: Invocation
    pod: ExecutionPod
    # effective owner: the pod's owner label ?? the owning FournosJob's owner,
    # resolved when the row is built (commands layer)
    owner: str | None = None

    @property
    def shown_name(self) -> str:
        return self.pod.name

    @property
    def shown_fjob(self) -> str | None:
        return self.pod.job_name

    @property
    def shown_owner(self) -> str | None:
        return self.owner

    @property
    def shown_status(self) -> PodPhase:
        return self.pod.status

    @property
    def shown_restarts(self) -> int:
        return self.pod.restarts

    @property
    def shown_age(self):
        return _age(self.pod.started_at)
