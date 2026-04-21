from enum import StrEnum

LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
LABEL_JOB_NAME = "fournos.dev/job-name"
LABEL_EXCLUSIVE_CLUSTER = "fournos.dev/exclusive-cluster"

CLUSTER_SLOT_RESOURCE = "fournos/cluster-slot"
MAX_CLUSTER_SLOTS = 100


class Phase(StrEnum):
    PENDING = "Pending"
    ADMITTED = "Admitted"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    ABORTED = "Aborted"


TERMINAL_PHASES = frozenset({Phase.SUCCEEDED, Phase.FAILED, Phase.ABORTED})
LOCK_HOLDING_PHASES = frozenset({Phase.ADMITTED, Phase.RUNNING})
