import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from kubernetes import client, config

from fournos.api.v1.router import router as v1_router
from fournos.core.clusters import ClusterRegistry
from fournos.core.kueue import KueueClient
from fournos.core.reconciler import run_reconciler
from fournos.core.tekton import TektonClient
from fournos.settings import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

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

    reconciler_task = asyncio.create_task(
        run_reconciler(
            app.state.kueue,
            app.state.tekton,
            settings.reconcile_interval_sec,
        )
    )

    yield

    reconciler_task.cancel()
    try:
        await reconciler_task
    except asyncio.CancelledError:
        pass


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
