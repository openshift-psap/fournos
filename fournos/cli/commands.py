"""Command logic — one function per rule family in specs/fournos-cli.allium.

Each function takes the ClusterClient port and an Invocation, and returns
either a success value (rows, a LogStream, an applied result) or a
``CommandFailed`` carrying the failure reason and a hint.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from fournos.core.constants import Shutdown

from .cluster import ClusterClient
from .invocation import Invocation
from .model import (
    CommandFailed,
    ConfirmationAction,
    ConfirmationPrompt,
    DeletionApplied,
    ExecutionPod,
    FailureReason,
    FJob,
    FJobRow,
    LogStream,
    PodPhase,
    PodRow,
)


def _resolve_owner_filter(
    invocation: Invocation, all_jobs: bool, owner_filter: str | None
) -> str | None | CommandFailed:
    # --owner always filters, whoever it names; otherwise mine is the default
    # scope, opted out of with --all.
    if owner_filter is not None:
        return owner_filter
    if all_jobs:
        return None
    owner = invocation.resolved_owner
    if owner is None:
        return CommandFailed(
            invocation,
            FailureReason.OWNER_UNRESOLVED,
            hint="pass --owner or --all, set FOURNOS_OWNER or $USER, "
            "or set owner in the fournos config file",
        )
    return owner


# -- get fjobs -----------------------------------------------------------------


def list_fjob_rows(
    cluster: ClusterClient,
    invocation: Invocation,
    *,
    all_jobs: bool = False,
    owner_filter: str | None = None,
) -> list[FJobRow] | CommandFailed:
    owner = _resolve_owner_filter(invocation, all_jobs, owner_filter)
    if isinstance(owner, CommandFailed):
        return owner
    namespace = invocation.resolved_namespace
    jobs = cluster.list_fjobs(namespace)
    if owner is not None:
        jobs = [job for job in jobs if job.owner == owner]
    return [FJobRow(invocation=invocation, job=job) for job in jobs]


# -- get fjobs <name> ----------------------------------------------------------


def get_fjob_row(
    cluster: ClusterClient,
    invocation: Invocation,
    name: str,
) -> FJobRow | CommandFailed:
    """Single-job get — mirrors `oc get fjob <name>`: no owner filtering."""
    namespace = invocation.resolved_namespace
    job = cluster.get_fjob(namespace, name)
    if job is None:
        return CommandFailed(
            invocation,
            FailureReason.FJOB_NOT_FOUND,
            hint=f"no FournosJob named {name!r} in namespace {namespace!r}",
        )
    return FJobRow(invocation=invocation, job=job)


# -- get pods ------------------------------------------------------------------


def _pod_owner(pod: ExecutionPod, job_owners: dict[str, str | None]) -> str | None:
    """Effective owner: the pod's owner label, falling back to the owning
    FournosJob's owner for pods created before the label was stamped."""
    if pod.owner_label is not None:
        return pod.owner_label
    return job_owners.get(pod.job_name)


def list_pod_rows(
    cluster: ClusterClient,
    invocation: Invocation,
    *,
    fjob_name: str | None = None,
    all_jobs: bool = False,
    owner_filter: str | None = None,
) -> list[PodRow] | CommandFailed:
    owner = _resolve_owner_filter(invocation, all_jobs, owner_filter)
    if isinstance(owner, CommandFailed):
        return owner
    namespace = invocation.resolved_namespace
    pods = cluster.list_pods(namespace, fjob_name=fjob_name)
    # The owner fallback resolves through the owning FournosJobs, so owner
    # filtering happens here rather than in the pod label selector.
    job_owners = {job.name: job.owner for job in cluster.list_fjobs(namespace)}
    rows: list[PodRow] = []
    for pod in pods:
        pod_owner = _pod_owner(pod, job_owners)
        if owner is not None and pod_owner != owner:
            continue
        rows.append(PodRow(invocation=invocation, pod=pod, owner=pod_owner))
    return rows


# -- logs ----------------------------------------------------------------------


def _newest_first(pods: list[ExecutionPod]) -> list[ExecutionPod]:
    return sorted(
        pods,
        key=lambda pod: pod.started_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )


def open_log_stream(
    cluster: ClusterClient,
    invocation: Invocation,
    name: str,
    *,
    pod_name: str | None = None,
    follow: bool = False,
) -> LogStream | CommandFailed:
    namespace = invocation.resolved_namespace
    job = cluster.get_fjob(namespace, name)
    if job is None:
        return CommandFailed(
            invocation,
            FailureReason.FJOB_NOT_FOUND,
            hint=f"no FournosJob named {name!r} in namespace {namespace!r}",
        )

    running = [
        pod
        for pod in cluster.list_pods(namespace, fjob_name=name)
        if pod.status is PodPhase.RUNNING
    ]
    offered = _newest_first(running)

    if pod_name is not None:
        selected = next((pod for pod in offered if pod.name == pod_name), None)
        if selected is None:
            return CommandFailed(
                invocation,
                FailureReason.POD_NOT_FOUND,
                job=job,
                hint=f"no running pod named {pod_name!r} for job {name!r}; "
                "run without -C to list the available pods",
            )
    else:
        if not offered:
            return CommandFailed(
                invocation,
                FailureReason.NO_RUNNING_POD,
                job=job,
                hint=job.dashboard_url,
            )
        selected = offered[0]

    return LogStream(
        invocation=invocation,
        job=job,
        offered_pods=offered,
        pod=selected,
        follow=follow,
    )


# -- stop / terminate / delete -------------------------------------------------


def request_shutdown(
    cluster: ClusterClient,
    invocation: Invocation,
    name: str,
    mode: Shutdown,
    *,
    assume_yes: bool = False,
) -> FJob | ConfirmationPrompt | CommandFailed:
    namespace = invocation.resolved_namespace
    job = cluster.get_fjob(namespace, name)
    if job is None:
        return CommandFailed(
            invocation,
            FailureReason.FJOB_NOT_FOUND,
            hint=f"no FournosJob named {name!r} in namespace {namespace!r}",
        )
    # Graceful stop with --yes goes straight through; terminate always prompts
    # (it skips the finally tasks, so it is never applied silently).
    if mode is Shutdown.STOP and assume_yes:
        cluster.set_shutdown(namespace, name, mode)
        return replace(job, shutdown=mode)
    return ConfirmationPrompt(
        invocation=invocation,
        job=job,
        action=ConfirmationAction.SHUTDOWN,
        shutdown_mode=mode,
    )


def request_deletion(
    cluster: ClusterClient,
    invocation: Invocation,
    name: str,
    *,
    assume_yes: bool = False,
) -> DeletionApplied | ConfirmationPrompt | CommandFailed:
    namespace = invocation.resolved_namespace
    job = cluster.get_fjob(namespace, name)
    if job is None:
        return CommandFailed(
            invocation,
            FailureReason.FJOB_NOT_FOUND,
            hint=f"no FournosJob named {name!r} in namespace {namespace!r}",
        )
    if assume_yes:
        cluster.delete_fjob(namespace, name)
        return DeletionApplied(invocation=invocation, job=job)
    return ConfirmationPrompt(
        invocation=invocation,
        job=job,
        action=ConfirmationAction.DELETE,
    )


def apply_shutdown_confirmation(
    cluster: ClusterClient, prompt: ConfirmationPrompt
) -> FJob:
    if prompt.action is not ConfirmationAction.SHUTDOWN or prompt.shutdown_mode is None:
        raise ValueError(f"not a shutdown prompt: action={prompt.action}")
    cluster.set_shutdown(prompt.job.namespace, prompt.job.name, prompt.shutdown_mode)
    return replace(prompt.job, shutdown=prompt.shutdown_mode)


def apply_deletion_confirmation(
    cluster: ClusterClient, prompt: ConfirmationPrompt
) -> DeletionApplied:
    if prompt.action is not ConfirmationAction.DELETE:
        raise ValueError(f"not a deletion prompt: action={prompt.action}")
    cluster.delete_fjob(prompt.job.namespace, prompt.job.name)
    return DeletionApplied(invocation=prompt.invocation, job=prompt.job)


def apply_confirmation(
    cluster: ClusterClient, prompt: ConfirmationPrompt
) -> FJob | DeletionApplied:
    if prompt.action is ConfirmationAction.SHUTDOWN:
        return apply_shutdown_confirmation(cluster, prompt)
    return apply_deletion_confirmation(cluster, prompt)
