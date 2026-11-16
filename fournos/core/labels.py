"""Internal label set carried from a FournosJob spec to managed resources.

The resource builders accept a generic KV label map, so new propagation
needs are a handler-side change only. Today the single derived label is
``fournos.dev/owner``, stamped when the job declares an owner.
"""

from __future__ import annotations

import re

from fournos.core.constants import (
    ANNOTATION_ORIGINAL_OWNER,
    LABEL_JOB_NAME,
    LABEL_MANAGED_BY,
    LABEL_OWNER,
)

_LABEL_VALUE_RE = re.compile(r"[^a-zA-Z0-9._-]")


def sanitize_label_value(value: str) -> str:
    """Transform *value* into a valid Kubernetes label value.

    Replaces characters outside ``[a-zA-Z0-9._-]`` with underscores,
    strips leading/trailing non-alphanumeric characters, and truncates
    to 63 characters.  Already-valid values pass through unchanged.
    """
    if not value:
        return ""
    sanitized = _LABEL_VALUE_RE.sub("_", value)
    sanitized = sanitized.strip("._-")
    sanitized = sanitized[:63]
    sanitized = sanitized.rstrip("._-")
    return sanitized


def base_labels(name: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Base labels every managed resource carries, plus any extra KV labels."""
    labels: dict[str, str] = {LABEL_MANAGED_BY: "fournos", LABEL_JOB_NAME: name}
    if extra:
        labels.update(extra)
    return labels


def job_labels(spec: dict) -> dict[str, str]:
    """Labels to propagate for a FournosJob spec: its owner, when set."""
    owner = spec.get("owner")
    if not owner:
        return {}
    sanitized = sanitize_label_value(owner)
    if not sanitized:
        return {}
    return {LABEL_OWNER: sanitized}


def job_annotations(spec: dict) -> dict[str, str]:
    """Annotations preserving original owner when it differs from the label value."""
    owner = spec.get("owner")
    if not owner:
        return {}
    sanitized = sanitize_label_value(owner)
    if sanitized != owner:
        return {ANNOTATION_ORIGINAL_OWNER: owner}
    return {}
