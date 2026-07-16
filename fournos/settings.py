from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "FOURNOS_"}

    workload_namespace: str = Field(
        description="Namespace for FournosJobs and execution resources"
    )
    secrets_namespace: str = Field(
        default="psap-secrets",
        description="Namespace where kubeconfig and vault-synced secrets are stored",
    )
    tekton_dashboard_url: str = ""
    kubeconfig_secret_pattern: str = "kubeconfig-{cluster}"
    vault_secret_pattern: str = "vault-{entry}"
    kueue_local_queue_name: str = "fournos-queue"
    gpu_resource_prefix: str = "fournos/gpu-"
    gc_interval_sec: float = Field(default=300.0, gt=0)
    log_level: str = "INFO"
    resolve_deadline_sec: int = Field(default=300, gt=0)
    resolve_job_template: str = "config/forge/resolve_job.yaml"
    artifact_pvc_size: str = "10Gi"
    pipeline_timeout: str = Field(
        default="25h0m0s",
        description="Default timeout for entire PipelineRun (e.g., '25h0m0s', '90m')",
    )
    pipeline_tasks_timeout: str = Field(
        default="24h0m0s",
        description="Cumulative timeout across non-finally tasks in PipelineRun (e.g., '24h0m0s', '90m')",
    )
    pipeline_finally_timeout: str = Field(
        default="1h0m0s",
        description="Default timeout for finally tasks in PipelineRun (e.g., '1h0m0s', '30m')",
    )


settings = Settings()
