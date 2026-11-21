"""Check a job's GPU shape against the free GPUs of individual nodes.

Kueue accounts for GPU quota per cluster, not per node. A job whose replicas
each need more GPUs than one node has can be admitted by quota and then stay
Pending forever while the scheduler finds no node for its pods. Hearth
publishes the free GPUs of every node on the ResourceFlavor; this module
interprets that data and decides whether the replicas can be placed.
"""

from __future__ import annotations

import json
import logging

from fournos.core.constants import ANNOTATION_FREE_GPU_BLOCKS

logger = logging.getLogger(__name__)


def parse_free_blocks(annotation: object) -> dict[str, list[int]] | None:
    """Parse the ResourceFlavor annotation into ``{gpu_type: [free, ...]}``.

    Returns ``None`` when the data is missing or malformed, which callers
    must treat as "unknown" rather than "no free GPUs".
    """
    if not isinstance(annotation, str):
        return None
    try:
        parsed = json.loads(annotation)
    except ValueError:
        logger.warning(
            "Malformed %s annotation: %r", ANNOTATION_FREE_GPU_BLOCKS, annotation
        )
        return None
    if not isinstance(parsed, dict):
        return None

    blocks: dict[str, list[int]] = {}
    for gpu_type, sizes in parsed.items():
        if not isinstance(sizes, list):
            return None
        try:
            blocks[str(gpu_type)] = [int(size) for size in sizes]
        except (TypeError, ValueError):
            return None
    return blocks


def replica_shape(hardware: dict) -> tuple[int, int]:
    """Return ``(replicas, gpus_per_replica)`` for a resolved job.

    Jobs resolved before Forge reported the replica shape carry only
    ``gpuCount``, which described one replica, so they are treated as a
    single replica of that size.
    """
    gpu_count = int(hardware.get("gpuCount") or 0)
    replicas_value = hardware.get("replicas")
    replicas = 1 if replicas_value is None else int(replicas_value)
    gpus_per_replica = int(hardware.get("gpusPerReplica") or gpu_count)
    return max(replicas, 0), max(gpus_per_replica, 0)


def fits_on_nodes(free_blocks: list[int], replicas: int, gpus_per_replica: int) -> bool:
    """Can every replica be placed on a node?

    Each replica is one pod needing ``gpus_per_replica`` GPUs from a single
    node, and a node may host more than one replica. Items are identical, so
    filling the largest blocks first is optimal.
    """
    if replicas <= 0 or gpus_per_replica <= 0:
        return True
    remaining = replicas
    for free in sorted(free_blocks, reverse=True):
        remaining -= free // gpus_per_replica
        if remaining <= 0:
            return True
    return False


def shape_message(
    gpu_type: str, replicas: int, gpus_per_replica: int, free_blocks: list[int]
) -> str:
    available = (
        ", ".join(str(size) for size in sorted(free_blocks, reverse=True)) or "none"
    )
    return (
        f"Cannot place {replicas} replica(s) of {gpus_per_replica} "
        f"'{gpu_type}' GPU(s): free GPUs per node are {available}"
    )
