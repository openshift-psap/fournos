import asyncio

from fastapi import APIRouter, HTTPException, Request

from fournos.core.tekton import TektonClient
from fournos.models import ArtifactsResponse

router = APIRouter()


@router.get("/job/{job_id}/artifacts", response_model=ArtifactsResponse)
async def get_artifacts(request: Request, job_id: str):
    tekton: TektonClient = request.app.state.tekton
    pr = await asyncio.to_thread(tekton.get_pipeline_run_or_none, job_id)
    if not pr:
        raise HTTPException(404, f"Job '{job_id}' not found or has no PipelineRun yet")

    # TODO: retrieve artifacts from PipelineRun results / object storage
    return ArtifactsResponse(id=job_id)
