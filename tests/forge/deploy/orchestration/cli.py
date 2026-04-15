#!/usr/bin/env python3
"""
FOURNOS Deploy Project CLI Operations

Interactive CLI for FOURNOS deployment with configuration overrides.
"""

import deploy as fournos_deploy
from projects.core.library.cli import safe_cli_command
from projects.core.library import config
from projects.core.library import env, config, run

import sys
import types
import logging
logger = logging.getLogger(__name__)

import click


@click.group()
@click.option('--namespace', help='Target namespace for FOURNOS deployment')
@click.option('--commit', help='Git commit SHA to build (overrides config)')
@click.option('--tag', help='Image tag name (overrides config)')
@click.option('--repo', help='Repository name in owner/repo format')
@click.option('--dockerfile', help='Path to Dockerfile/Containerfile within repository')
@click.option('--timeout', type=int, help='Build timeout in minutes')
@click.pass_context
def main(ctx, namespace, commit, tag, repo, dockerfile, timeout):
    """FOURNOS Deployment CLI Operations."""
    ctx.ensure_object(types.SimpleNamespace)
    fournos_deploy.init()

    # Apply CLI configuration overrides
    if namespace:
        config.project.set_config("fournos_deploy.namespace.name", namespace)
        logger.info(f"Using namespace: {namespace}")

    if commit:
        config.project.set_config("fournos_deploy.build.commit", commit)
        logger.info(f"Using commit: {commit}")

    if tag:
        config.project.set_config("fournos_deploy.build.tag_name", tag)
        logger.info(f"Using image tag: {tag}")

    if repo:
        config.project.set_config("fournos_deploy.build.repo_name", repo)
        logger.info(f"Using repository: {repo}")

    if dockerfile:
        config.project.set_config("fournos_deploy.build.dockerfile_path", dockerfile)
        logger.info(f"Using dockerfile path: {dockerfile}")

    if timeout:
        config.project.set_config("fournos_deploy.build.timeout_minutes", timeout)
        logger.info(f"Using build timeout: {timeout} minutes")


@main.command()
@click.option('--imagestream', help='ImageStream name (overrides config)')
@click.option('--force-rebuild', is_flag=True, help='Force rebuild even if image exists')
@click.pass_context
@safe_cli_command
def build_image(ctx, imagestream, force_rebuild):
    """Build FOURNOS container image using Shipwright."""

    if imagestream:
        config.project.set_config("fournos_deploy.build.imagestream_name", imagestream)
        logger.info(f"Using ImageStream: {imagestream}")

    if force_rebuild:
        config.project.set_config("fournos_deploy.images.fournos.force_rebuild", True)
        logger.info("Force rebuild enabled")

    exit_code = fournos_deploy.build_image()
    sys.exit(exit_code)


@main.command()
@click.option('--source-path', help='Path to FOURNOS source manifests')
@click.option('--no-wait', is_flag=True, help='Do not wait for deployments to be ready')
@click.option('--rollout-timeout', type=int, help='Timeout for rollout in seconds')
@click.pass_context
@safe_cli_command
def deploy_manifests(ctx, source_path, no_wait, rollout_timeout):
    """Deploy FOURNOS manifests to target namespace."""

    if source_path:
        config.project.set_config("fournos_deploy.fournos_source.path", source_path)
        logger.info(f"Using source path: {source_path}")

    if no_wait:
        config.project.set_config("fournos_deploy.deploy.wait_for_rollout", False)
        logger.info("Rollout wait disabled")

    if rollout_timeout:
        config.project.set_config("fournos_deploy.deploy.rollout_timeout", rollout_timeout)
        logger.info(f"Using rollout timeout: {rollout_timeout} seconds")

    exit_code = fournos_deploy.deploy_manifests()
    sys.exit(exit_code)


@main.command()
@click.pass_context
@safe_cli_command
def deploy_workload(ctx):
    """Deploy FOURNOS workload with built image."""
    exit_code = fournos_deploy.deploy_fournos_workload()
    sys.exit(exit_code)


@main.command()
@click.pass_context
@safe_cli_command
def deploy_config(ctx):
    """Deploy FORGE workflow configuration for FOURNOS integration."""
    exit_code = fournos_deploy.deploy_workflow_config()
    sys.exit(exit_code)


@main.command()
@click.pass_context
@safe_cli_command
def rebuild_workflow(ctx):
    """Rebuild FOURNOS workflow images using existing Builds."""
    exit_code = fournos_deploy.rebuild_forge_images()
    sys.exit(exit_code)


@main.command()
@click.option('--skip-build', is_flag=True, help='Skip the image build step')
@click.option('--skip-manifests', is_flag=True, help='Skip the manifest deployment step')
@click.option('--skip-config', is_flag=True, help='Skip the config deployment step')
@click.pass_context
@safe_cli_command
def deploy(ctx, skip_build, skip_manifests, skip_config):
    """Complete FOURNOS deployment (build + deploy manifests + deploy config)."""

    if skip_build:
        logger.info("Skipping image build step")

    if skip_manifests:
        logger.info("Skipping manifest deployment step")

    if skip_config:
        logger.info("Skipping config deployment step")

    # TODO: Implement skip logic in the deploy function
    # For now, just run the complete deploy
    exit_code = fournos_deploy.deploy()
    sys.exit(exit_code)


@main.command()
@click.pass_context
def status(ctx):
    """Show current FOURNOS deployment status."""

    namespace = config.project.get_config("fournos_deploy.namespace.name")

    click.echo(f"=== FOURNOS Deployment Status ===")
    click.echo(f"Namespace: {namespace}")
    click.echo("")

    # Check namespace exists
    result = run.run(f"oc get namespace {namespace}", check=False)
    if result.returncode != 0:
        click.echo("❌ Target namespace does not exist")
        sys.exit(1)

    click.echo("✅ Target namespace exists")

    # Check ImageStream
    imagestream_name = config.project.get_config("fournos_deploy.build.imagestream_name")
    result = run.run(f"oc get imagestream {imagestream_name} -n {namespace}", check=False)
    if result.returncode == 0:
        click.echo(f"✅ ImageStream '{imagestream_name}' exists")
    else:
        click.echo(f"❌ ImageStream '{imagestream_name}' not found")

    # Check deployments
    result = run.run(f"oc get deployments -n {namespace} -o name", check=False, capture_stdout=True)
    if result.returncode == 0 and result.stdout.strip():
        deployments = result.stdout.strip().split('\n')
        click.echo(f"📦 Found {len(deployments)} deployment(s)")
        for deployment in deployments:
            click.echo(f"   • {deployment}")
    else:
        click.echo("❌ No deployments found")

    # Check services
    result = fournos_deploy.run.run(f"oc get services -n {namespace} -o name", check=False, capture_stdout=True)
    if result.returncode == 0 and result.stdout.strip():
        services = result.stdout.strip().split('\n')
        click.echo(f"🌐 Found {len(services)} service(s)")
        for service in services:
            click.echo(f"   • {service}")
    else:
        click.echo("❌ No services found")



@main.command()
@click.pass_context
def config_show(ctx):
    """Show current deployment configuration."""

    try:
        fournos_config = config.project.get_config("fournos_deploy", print=False)

        click.echo("=== Current FOURNOS Deploy Configuration ===")
        click.echo(f"Namespace: {fournos_config.get('namespace', 'not set')}")

        build_config = fournos_config.get('build', {})
        click.echo("")
        click.echo("Build Configuration:")
        click.echo(f"  Repository: {build_config.get('repo_name', 'not set')}")
        click.echo(f"  Commit: {build_config.get('commit', 'not set')}")
        click.echo(f"  ImageStream: {build_config.get('imagestream_name', 'not set')}")
        click.echo(f"  Tag: {build_config.get('tag_name', 'not set')}")
        click.echo(f"  Dockerfile Path: {build_config.get('dockerfile_path', 'not set')}")
        click.echo(f"  Timeout: {build_config.get('timeout_minutes', 'not set')} minutes")

        deploy_config = fournos_config.get('deploy', {})
        click.echo("")
        click.echo("Deploy Configuration:")
        click.echo(f"  Wait for rollout: {deploy_config.get('wait_for_rollout', 'not set')}")
        click.echo(f"  Rollout timeout: {deploy_config.get('rollout_timeout', 'not set')} seconds")

        fournos_source = fournos_config.get('fournos_source', {})
        click.echo("")
        click.echo("Source Configuration:")
        click.echo(f"  FOURNOS path: {fournos_source.get('path', 'not set')}")

    except Exception as e:
        click.echo(f"❌ Error reading configuration: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
