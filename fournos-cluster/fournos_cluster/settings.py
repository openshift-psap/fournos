from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "FOURNOS_CLUSTER_"}

    namespace: str = Field(description="Kubernetes namespace (FOURNOS_CLUSTER_NAMESPACE)")
    secrets_namespace: str = Field(
        default="psap-secrets",
        description="Namespace where kubeconfig secrets are stored",
    )
    kubeconfig_secret_pattern: str = "kubeconfig-{cluster}"
    kueue_cluster_queue_name: str = "fournos-queue"
    gpu_resource_prefix: str = "fournos/gpu-"
    reconcile_interval_sec: float = Field(
        default=30.0,
        gt=0,
        description="Timer reconciliation interval for FournosCluster objects",
    )
    gpu_discovery_default_interval_sec: float = Field(
        default=300.0,
        gt=0,
        description="Default GPU discovery interval when not specified per-cluster",
    )
    gpu_discovery_timeout_sec: int = Field(
        default=10,
        gt=0,
        description="Connection timeout for target cluster API calls",
    )
    log_level: str = "INFO"


settings = Settings()
