from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class HardwareRequest(BaseModel):
    gpu_type: str = Field(..., description="GPU type, e.g. A100, H200")
    gpu_count: int = Field(..., ge=1, description="Number of GPUs requested")


class ForgeConfig(BaseModel):
    project: str = Field(..., description="FORGE project, e.g. testproj/llmd")
    preset: str = Field(..., description="FORGE preset, e.g. cks, llama3")
    args: list[str] = Field(
        default_factory=list, description="Additional CLI arguments"
    )


class JobSubmitRequest(BaseModel):
    name: str
    pipeline: str = Field(default="fournos-full")
    cluster: str | None = Field(
        default=None, description="Explicit cluster name (Mode A, bypasses Kueue)"
    )
    hardware: HardwareRequest | None = Field(
        default=None, description="Hardware request (Mode B, scheduled via Kueue)"
    )
    forge: ForgeConfig
    secrets: list[str] = Field(default_factory=list)
    priority: str | None = Field(
        default=None, description="WorkloadPriorityClass name (Mode B only)"
    )


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    ADMITTED = "admitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobStatusResponse(BaseModel):
    id: str
    name: str
    status: JobStatus
    cluster: str | None = None
    pipeline_run: str | None = None
    dashboard_url: str | None = None
    message: str | None = None


class JobListResponse(BaseModel):
    jobs: list[JobStatusResponse]
    count: int


class ArtifactsResponse(BaseModel):
    id: str
    artifacts: list[str] = Field(default_factory=list)
    mlflow_url: str | None = None
