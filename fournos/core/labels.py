"""Internal label set carried from a FournosJob spec to managed resources.

The resource builders accept a generic KV label map, so new propagation
needs are a handler-side change only. Today the single derived label is
``fournos.dev/owner``, stamped when the job declares an owner.
"""

from __future__ import annotations

from fournos.core.constants import LABEL_JOB_NAME, LABEL_MANAGED_BY, LABEL_OWNER


def base_labels(name: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Base labels every managed resource carries, plus any extra KV labels."""
    labels: dict[str, str] = {LABEL_MANAGED_BY: "fournos", LABEL_JOB_NAME: name}
    if extra:
        labels.update(extra)
    return labels


def job_labels(spec: dict) -> dict[str, str]:
    """Labels to propagate for a FournosJob spec: its owner, when set."""
    owner = spec.get("owner")
    return {LABEL_OWNER: owner} if owner else {}
