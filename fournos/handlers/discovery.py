"""Cluster auto-discovery handler — thin wrapper around ClusterDiscovery."""

from __future__ import annotations

import logging

from fournos.state import ctx

logger = logging.getLogger(__name__)


def scan_clusters() -> list[str]:
    """Run a single cluster discovery scan. Returns newly discovered cluster names."""
    try:
        discovered = ctx.discovery.scan()
        if discovered:
            logger.info("Auto-discovered %d new cluster(s): %s", len(discovered), discovered)
        return discovered
    except Exception:
        logger.exception("Cluster discovery scan failed")
        return []
