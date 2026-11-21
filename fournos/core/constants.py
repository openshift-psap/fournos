from enum import StrEnum

LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
LABEL_JOB_NAME = "fournos.dev/job-name"
LABEL_EXCLUSIVE_CLUSTER = "fournos.dev/exclusive-cluster"
LABEL_VAULT_ENTRY = "fournos.dev/vault-entry"
LABEL_RECURRING_PARENT = "fournos.dev/recurring-parent"
ANNOTATION_TRIGGER_NOW = "fournos.dev/trigger-now"
# Published by Hearth on the ResourceFlavor: JSON dict mapping a GPU short
# name to the free GPU counts of the nodes that hold it.
ANNOTATION_FREE_GPU_BLOCKS = "fournos.dev/free-gpu-blocks"

CLUSTER_SLOT_RESOURCE = "fournos/cluster-slot"
MAX_CLUSTER_SLOTS = 100


class Phase(StrEnum):
    SCHEDULED = "Scheduled"
    RECURRING = "Recurring"
    RESOLVING = "Resolving"
    PENDING = "Pending"
    ADMITTED = "Admitted"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    STOPPING = "Stopping"
    STOPPED = "Stopped"


TERMINAL_PHASES = frozenset({Phase.SUCCEEDED, Phase.FAILED, Phase.STOPPED})
LOCK_HOLDING_PHASES = frozenset({Phase.ADMITTED, Phase.RUNNING, Phase.STOPPING})


class Shutdown(StrEnum):
    STOP = "Stop"
    TERMINATE = "Terminate"
