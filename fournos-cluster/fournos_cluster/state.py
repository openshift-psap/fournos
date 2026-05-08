from __future__ import annotations

from dataclasses import dataclass

from fournos_cluster.core.gpu_discovery import GPUDiscoveryClient
from fournos_cluster.core.kueue import KueueClient


@dataclass
class _ControllerState:
    kueue: KueueClient | None = None
    gpu_discovery: GPUDiscoveryClient | None = None


ctx = _ControllerState()
