"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ["DATABASE_URL"]
    )

    fournos_namespace: str = field(
        default_factory=lambda: os.environ.get("FOURNOS_NAMESPACE", "fournos-jobs")
    )

    kubeconfig_path: str | None = field(
        default_factory=lambda: os.environ.get("KUBECONFIG")
    )

    forge_repo_path: str | None = field(
        default_factory=lambda: os.environ.get("FORGE_REPO_PATH")
    )

    projects_config_path: str = field(
        default_factory=lambda: os.environ.get("PROJECTS_CONFIG_PATH", "/etc/fournos-dashboard/projects.yaml")
    )

    fournos_api_group: str = "fournos.dev"
    fournos_api_version: str = "v1"
    fournos_job_plural: str = "fournosjobs"

    tekton_api_group: str = "tekton.dev"
    tekton_api_version: str = "v1"
    tekton_pipelinerun_plural: str = "pipelineruns"

    log_level: str = field(
        default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO")
    )

    forge_github_repo: str = field(
        default_factory=lambda: os.environ.get("FORGE_GITHUB_REPO", "openshift-psap/forge")
    )

    k8s_request_timeout_seconds: int = field(
        default_factory=lambda: int(os.environ.get("K8S_REQUEST_TIMEOUT", "30"))
    )

    jobs_poll_interval_seconds: int = 5

    default_pipelines: tuple[str, ...] = (
        "forge-full",
        "forge-prepare-test",
        "forge-test-only",
        "forge-prepare-only",
        "forge-replot",
    )


settings = Settings()
