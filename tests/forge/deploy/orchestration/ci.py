#!/usr/bin/env python3
"""
Fournos-Deploy Project CI Operations
"""

from projects.core.library import ci as ci_lib, config, env
from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME
from projects.fournos_launcher.orchestration import utils

import deploy as fournos_deploy

import click
import types
import logging
import os
import json

logger = logging.getLogger(__name__)


@click.group()
@click.option("--project-source", help="Path to FOURNOS source directory")
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx, project_source):
    """FOURNOS Deploy Project CI Operations for FORGE."""
    ctx.ensure_object(types.SimpleNamespace)
    fournos_deploy.init()
    utils.ensure_oc_available()

    # Apply CLI configuration overrides
    if project_source:
        config.project.set_config("fournos_deploy.fournos_source.path", project_source)
        logger.info(f"Using FOURNOS source path: {project_source}")

    # Set commit from PULL_PULL_SHA if available
    pull_sha = os.environ.get("PULL_PULL_SHA")
    if pull_sha:
        config.project.set_config("fournos_deploy.build.commit", pull_sha)
        logger.info(f"Using commit from PULL_PULL_SHA: {pull_sha}")

    # Set repository name from PR metadata for fork support
    if env.ARTIFACT_DIR:
        pr_metadata_path = env.ARTIFACT_DIR / CI_METADATA_DIRNAME / "pull_request.json"
        if pr_metadata_path.exists():
            try:
                with open(pr_metadata_path, "r") as f:
                    pr_metadata = json.load(f)

                # Extract the source repository name from fork PR metadata
                repo_full_name = (
                    pr_metadata.get("head", {}).get("repo", {}).get("full_name")
                )
                if repo_full_name:
                    config.project.set_config(
                        "fournos_deploy.build.repo_name", repo_full_name
                    )
                    logger.info(f"Using repository from PR metadata: {repo_full_name}")
                else:
                    logger.warning("Could not find head.repo.full_name in PR metadata")
            except (json.JSONDecodeError, KeyError, FileNotFoundError) as e:
                logger.warning(f"Failed to parse PR metadata: {e}")
        else:
            logger.debug(f"PR metadata file not found: {pr_metadata_path}")

    # Verify OpenShift authentication early
    from projects.core.library import run

    result = run.run("oc whoami", check=False, capture_stdout=True)
    if result.returncode != 0:
        print(f"❌ OpenShift authentication failed: {result.stderr}")
        raise RuntimeError("Not authenticated with OpenShift cluster")

    print(f"✅ Authenticated as OpenShift user: {result.stdout.strip()}")


@main.command()
@click.pass_context
@ci_lib.safe_ci_command
def deploy(ctx):
    """Complete FOURNOS deployment with cleanup (clean slate deployment)."""
    return fournos_deploy.cleanup_and_deploy()


if __name__ == "__main__":
    main()
