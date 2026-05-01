"""Shared operator state — populated by startup(), read by handlers and GC."""

from __future__ import annotations

from dataclasses import dataclass

from fournos.core.clusters import ClusterRegistry
from fournos.core.discovery import ClusterDiscovery
from fournos.core.gpu_discovery import GPUDiscoveryClient
from fournos.core.kueue import KueueClient
from fournos.core.resolve import ResolveClient
from fournos.core.tekton import TektonClient


@dataclass
class _OperatorState:
    kueue: KueueClient | None = None
    tekton: TektonClient | None = None
    registry: ClusterRegistry | None = None
    resolve: ResolveClient | None = None
    gpu_discovery: GPUDiscoveryClient | None = None
    discovery: ClusterDiscovery | None = None


ctx = _OperatorState()
