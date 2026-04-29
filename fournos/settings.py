from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "FOURNOS_"}

    namespace: str = Field(description="Kubernetes namespace (FOURNOS_NAMESPACE)")
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
    resolve_image: str = (
        "image-registry.openshift-image-registry.svc:5000/{namespace}/forge-core:main"
    )
    resolve_deadline_sec: int = Field(default=300, gt=0)
    resolve_job_template: str = "config/forge/resolve_job.yaml"
    artifact_pvc_size: str = "1Gi"


settings = Settings()
