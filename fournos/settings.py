from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "FOURNOS_"}

    namespace: str = Field(description="Kubernetes namespace (FOURNOS_NAMESPACE)")
    tekton_dashboard_url: str = ""
    kubeconfig_secret_pattern: str = "{cluster}-kubeconfig"
    kueue_local_queue_name: str = "fournos-queue"
    gpu_resource_prefix: str = "fournos/gpu-"
    gc_interval_sec: float = Field(default=300.0, gt=0)
    log_level: str = "INFO"


settings = Settings()
