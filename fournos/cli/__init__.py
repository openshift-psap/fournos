"""fournos CLI — lookup and lifecycle commands for FournosJobs.

Public surface (see specs/fournos-cli.allium):
  - domain types: FJob, ExecutionPod, FJobRow, PodRow, LogStream,
    ConfirmationPrompt, DeletionApplied, CommandFailed
  - enums: JobPhase, PodPhase, Shutdown, FailureReason, ConfirmationAction
  - Invocation (owner/namespace/kubeconfig resolution)
  - command functions in fournos.cli.commands
  - the cluster adapter in fournos.cli.cluster
"""

from fournos.cli.cluster import ClusterClient, KubeClusterClient
from fournos.cli.commands import (
    apply_confirmation,
    apply_deletion_confirmation,
    apply_shutdown_confirmation,
    list_fjob_rows,
    list_pod_rows,
    open_log_stream,
    request_deletion,
    request_shutdown,
)
from fournos.cli.invocation import Invocation
from fournos.cli.model import (
    CommandFailed,
    ConfirmationAction,
    ConfirmationPrompt,
    DeletionApplied,
    ExecutionPod,
    FailureReason,
    FJob,
    FJobRow,
    JobPhase,
    LogStream,
    PodPhase,
    PodRow,
    Shutdown,
)

__all__ = [
    "ClusterClient",
    "CommandFailed",
    "ConfirmationAction",
    "ConfirmationPrompt",
    "DeletionApplied",
    "ExecutionPod",
    "FJob",
    "FJobRow",
    "FailureReason",
    "Invocation",
    "JobPhase",
    "KubeClusterClient",
    "LogStream",
    "PodPhase",
    "PodRow",
    "Shutdown",
    "apply_confirmation",
    "apply_deletion_confirmation",
    "apply_shutdown_confirmation",
    "list_fjob_rows",
    "list_pod_rows",
    "open_log_stream",
    "request_deletion",
    "request_shutdown",
]
