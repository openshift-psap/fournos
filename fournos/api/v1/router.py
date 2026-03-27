from fastapi import APIRouter

from fournos.api.v1.artifacts import router as artifacts_router
from fournos.api.v1.jobs import router as jobs_router

router = APIRouter()
router.include_router(jobs_router, tags=["jobs"])
router.include_router(artifacts_router, tags=["artifacts"])
