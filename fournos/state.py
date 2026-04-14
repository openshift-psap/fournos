"""Shared operator state — populated by startup(), read by handlers and GC."""

from __future__ import annotations

from dataclasses import dataclass

from fournos.core.clusters import ClusterRegistry
from fournos.core.kueue import KueueClient
from fournos.core.tekton import TektonClient


@dataclass
class _OperatorState:
    kueue: KueueClient | None = None
    tekton: TektonClient | None = None
    registry: ClusterRegistry | None = None


ctx = _OperatorState()
