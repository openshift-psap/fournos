import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from kubernetes import client, config

from fournos.api.v1.router import router as v1_router
from fournos.core.clusters import ClusterRegistry
from fournos.core.kueue import KueueClient
from fournos.core.tekton import TektonClient
from fournos.settings import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=settings.log_level)

    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded local kubeconfig")

    core_v1 = client.CoreV1Api()
    custom_objects = client.CustomObjectsApi()

    app.state.cluster_registry = ClusterRegistry(core_v1)
    app.state.tekton = TektonClient(custom_objects)
    app.state.kueue = KueueClient(custom_objects)

    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Fournos",
        description="Benchmark job scheduler",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(v1_router, prefix="/api/v1")

    @app.get("/healthz", tags=["health"])
    async def healthz():
        return {"status": "ok"}

    return app


app = create_app()
