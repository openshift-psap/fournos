from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, Request

from fournos.core.kueue import KueueClient
from fournos.core.tekton import TektonClient
from fournos.models import (
    JobListResponse,
    JobStatus,
    JobStatusResponse,
    JobSubmitRequest,
)
from fournos.settings import settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# GET /api/v1/jobs
# ---------------------------------------------------------------------------


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    request: Request,
    status: JobStatus | None = Query(None, description="Filter by job status"),
):
    tekton: TektonClient = request.app.state.tekton
    kueue: KueueClient = request.app.state.kueue

    prs = await asyncio.to_thread(tekton.list_pipeline_runs)
    wls = await asyncio.to_thread(kueue.list_workloads)

    pr_job_ids: set[str] = set()
    results: list[JobStatusResponse] = []

    for pr in prs:
        job_id = pr["metadata"].get("labels", {}).get("fournos.dev/job-id", "")
        pr_job_ids.add(job_id)
        results.append(_pr_to_response(pr))

    for wl in wls:
        job_id = wl["metadata"].get("labels", {}).get("fournos.dev/job-id", "")
        if job_id not in pr_job_ids:
            results.append(_wl_to_response(wl))

    if status is not None:
        results = [r for r in results if r.status == status]

    return JobListResponse(jobs=results, count=len(results))


# ---------------------------------------------------------------------------
# POST /api/v1/jobs
# ---------------------------------------------------------------------------


@router.post("/jobs", response_model=JobStatusResponse, status_code=201)
async def submit_job(request: Request, body: JobSubmitRequest):
    if body.cluster and body.hardware:
        raise HTTPException(400, "Specify either 'cluster' or 'hardware', not both")
    if not body.cluster and not body.hardware:
        raise HTTPException(400, "Must specify either 'cluster' or 'hardware'")

    job_id = uuid.uuid4().hex[:12]

    if body.cluster:
        return await _submit_explicit(request, job_id, body)
    return await _submit_hardware(request, job_id, body)


async def _submit_explicit(
    request: Request, job_id: str, body: JobSubmitRequest
) -> JobStatusResponse:
    """Mode A: explicit cluster — resolve kubeconfig, create PipelineRun immediately."""
    registry = request.app.state.cluster_registry
    tekton: TektonClient = request.app.state.tekton

    cluster = body.cluster
    if not await asyncio.to_thread(registry.cluster_exists, cluster):
        raise HTTPException(404, f"Cluster '{cluster}' not found")

    kubeconfig_secret = registry.resolve_kubeconfig_secret(cluster)

    pr = await asyncio.to_thread(
        tekton.create_pipeline_run,
        job_id=job_id,
        job_name=body.name,
        pipeline=body.pipeline,
        forge_project=body.forge.project,
        forge_preset=body.forge.preset,
        forge_args=body.forge.args,
        kubeconfig_secret=kubeconfig_secret,
        gpu_count=0,
        secrets=body.secrets,
        cluster=cluster,
        mode="explicit",
    )

    return _pr_to_response(pr)


async def _submit_hardware(
    request: Request, job_id: str, body: JobSubmitRequest
) -> JobStatusResponse:
    """Mode B: hardware request — create Kueue Workload, poll admission in background."""
    kueue: KueueClient = request.app.state.kueue
    hw = body.hardware

    wl = await asyncio.to_thread(
        kueue.create_workload,
        job_id=job_id,
        job_name=body.name,
        gpu_type=hw.gpu_type,
        gpu_count=hw.gpu_count,
        priority=body.priority,
    )

    asyncio.create_task(_wait_and_launch(request, job_id, body))
    return _wl_to_response(wl)


async def _wait_and_launch(
    request: Request, job_id: str, body: JobSubmitRequest
) -> None:
    """Background coroutine: wait for Kueue admission then create the PipelineRun."""
    kueue: KueueClient = request.app.state.kueue
    tekton: TektonClient = request.app.state.tekton
    registry = request.app.state.cluster_registry

    try:
        cluster = await kueue.poll_admission(job_id)
        if cluster is None:
            logger.info("Job %s: Workload deleted before admission, abandoning", job_id)
            return

        kubeconfig_secret = registry.resolve_kubeconfig_secret(cluster)

        await asyncio.to_thread(
            tekton.create_pipeline_run,
            job_id=job_id,
            job_name=body.name,
            pipeline=body.pipeline,
            forge_project=body.forge.project,
            forge_preset=body.forge.preset,
            forge_args=body.forge.args,
            kubeconfig_secret=kubeconfig_secret,
            gpu_count=body.hardware.gpu_count,
            secrets=body.secrets,
            cluster=cluster,
            mode="hardware",
        )

        logger.info("Job %s launched on cluster %s", job_id, cluster)

    except Exception as exc:
        logger.exception("Failed to launch job %s after admission", job_id)
        try:
            await asyncio.to_thread(kueue.annotate_workload_error, job_id, str(exc))
        except Exception:
            logger.exception("Failed to annotate error on Workload for job %s", job_id)


# ---------------------------------------------------------------------------
# POST /api/v1/job/{job_id}/complete
# ---------------------------------------------------------------------------


@router.post("/job/{job_id}/complete", status_code=204)
async def complete_job(request: Request, job_id: str):
    """Callback for Tekton pipelines to signal completion and release Kueue quota."""
    kueue: KueueClient = request.app.state.kueue
    await asyncio.to_thread(kueue.delete_workload, job_id)


# ---------------------------------------------------------------------------
# GET /api/v1/job/{job_id}
# ---------------------------------------------------------------------------


@router.get("/job/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    request: Request,
    job_id: str,
    wait: bool = Query(False, description="Long-poll until terminal state"),
):
    response = await _get_job_response(request, job_id)

    if wait and response.status in (
        JobStatus.PENDING,
        JobStatus.ADMITTED,
        JobStatus.RUNNING,
    ):
        response = await _poll_until_terminal(request, job_id)

    return response


async def _get_job_response(request: Request, job_id: str) -> JobStatusResponse:
    """Look up a single job — try PipelineRun first, fall back to Workload."""
    tekton: TektonClient = request.app.state.tekton
    kueue: KueueClient = request.app.state.kueue

    pr = await asyncio.to_thread(tekton.get_pipeline_run_or_none, job_id)
    if pr:
        return _pr_to_response(pr)

    wl = await asyncio.to_thread(kueue.get_workload_or_none, job_id)
    if wl:
        return _wl_to_response(wl)

    raise HTTPException(404, f"Job '{job_id}' not found")


async def _poll_until_terminal(request: Request, job_id: str) -> JobStatusResponse:
    for _ in range(720):  # ~1 hour at 5 s intervals
        await asyncio.sleep(5)
        response = await _get_job_response(request, job_id)
        if response.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
            return response
    raise HTTPException(
        504, f"Job '{job_id}' did not reach a terminal state within the wait timeout"
    )


# ---------------------------------------------------------------------------
# K8s resource → API response converters
# ---------------------------------------------------------------------------


def _pr_to_response(pr: dict) -> JobStatusResponse:
    meta = pr["metadata"]
    labels = meta.get("labels", {})
    annotations = meta.get("annotations", {})

    job_id = labels.get("fournos.dev/job-id", "")
    pr_name = meta["name"]
    status_str = TektonClient.extract_status(pr)

    dashboard_url = None
    if settings.tekton_dashboard_url:
        base = settings.tekton_dashboard_url.rstrip("/")
        dashboard_url = (
            f"{base}/#/namespaces/{settings.namespace}/pipelineruns/{pr_name}"
        )

    return JobStatusResponse(
        id=job_id,
        name=annotations.get("fournos.dev/job-name", ""),
        status=JobStatus(status_str),
        cluster=annotations.get("fournos.dev/cluster"),
        pipeline_run=pr_name,
        dashboard_url=dashboard_url,
    )


def _wl_to_response(wl: dict) -> JobStatusResponse:
    meta = wl["metadata"]
    labels = meta.get("labels", {})
    annotations = meta.get("annotations", {})

    job_id = labels.get("fournos.dev/job-id", "")
    error = annotations.get("fournos.dev/error")

    if error:
        status = JobStatus.FAILED
        cluster = KueueClient.get_assigned_flavor(wl)
    elif KueueClient.is_admitted(wl):
        status = JobStatus.ADMITTED
        cluster = KueueClient.get_assigned_flavor(wl)
    else:
        status = JobStatus.PENDING
        cluster = None

    return JobStatusResponse(
        id=job_id,
        name=annotations.get("fournos.dev/job-name", ""),
        status=status,
        cluster=cluster,
        message=error,
    )
