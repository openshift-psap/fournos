from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "FOURNOS_"}

    namespace: str = "psap-automation"
    tekton_dashboard_url: str = ""
    kubeconfig_secret_pattern: str = "{cluster}-kubeconfig"
    kueue_local_queue_name: str = "fournos-queue"
    gpu_resource_prefix: str = "fournos/gpu-"
    admission_poll_interval_sec: float = 5.0
    admission_poll_timeout_sec: float = 3600.0
    reconcile_interval_sec: float = 60.0
    log_level: str = "INFO"


settings = Settings()
