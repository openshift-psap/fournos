from projects.core.library import env, config, run

import pathlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def init():
    env.init()
    run.init()
    config.init(pathlib.Path(__file__).parent)


def build_image():
    """
    Build FOURNOS image using Shipwright

    Returns:
        int: 0 on success, 1 on failure
    """
    logger.info("=== Building FOURNOS Image ===")

    # Import and call the build_image toolbox
    from projects.cluster.toolbox.build_image.main import run as build_image_toolbox

    # Get configuration parameters
    build_config = config.project.get_config("fournos_deploy.build")
    namespace = config.project.get_config("fournos_deploy.namespace")

    # Allow environment variable overrides
    pr_number = os.environ.get("PULL_NUMBER")
    if pr_number:
        commit = f"refs/pull/{pr_number}/head"
    else:
        commit = os.environ.get("PULL_PULL_SHA") or build_config.get("commit", "main")

    logger.info(f"Building image from commit: {commit}")
    logger.info(f"Repository: {build_config['repo_name']}")
    logger.info(f"Target: {build_config['imagestream_name']}:{build_config['tag_name']}")
    logger.info(f"Namespace: {namespace}")

    # Call the build toolbox
    result = build_image_toolbox(
        repo_name=build_config["repo_name"],
        commit=commit,
        imagestream_name=build_config["imagestream_name"],
        tag_name=build_config["tag_name"],
        dockerfile_path=build_config.get("dockerfile_path", "projects/core/image/Containerfile"),
        namespace=namespace,
        timeout_minutes=build_config.get("timeout_minutes", 30)
    )

    if not result:
        logger.error("❌ Image build failed")
        return 1

    logger.info("✅ Image build completed successfully")
    return 0


def deploy_manifests():
    """
    Deploy FOURNOS manifests from source directory

    Returns:
        int: 0 on success, 1 on failure
    """
    logger.info("=== Deploying FOURNOS Manifests ===")

    # Get configuration
    namespace = config.project.get_config("fournos_deploy.namespace")
    fournos_source = Path(config.project.get_config("fournos_deploy.fournos_source.path"))
    deploy_config = config.project.get_config("fournos_deploy.deploy")

    if not fournos_source.exists():
        raise ValueError(f"FOURNOS source directory not found: {fournos_source}")

    logger.info(f"Deploying from: {fournos_source}")
    logger.info(f"Target namespace: {namespace}")

    # Find manifest files (look for yaml/yml files)
    manifest_files = list(fournos_source.glob("**/*.yaml")) + list(fournos_source.glob("**/*.yml"))

    if not manifest_files:
        logger.warning("No YAML manifest files found in source directory")
        return 0

    logger.info(f"Found {len(manifest_files)} manifest files")

    # Create namespace if it doesn't exist
    result = run.run(f"oc create namespace {namespace}", check=False)
    if result.returncode == 0:
        logger.info(f"Created namespace: {namespace}")
    else:
        logger.info(f"Namespace {namespace} already exists or creation failed (continuing)")

    # Apply manifests
    failed_manifests = []
    for manifest_file in manifest_files:
        logger.info(f"Applying: {manifest_file.relative_to(fournos_source)}")

        # Apply with namespace override
        result = run.run(
            f"oc apply -f {manifest_file} -n {namespace}",
            check=False
        )

        if result.returncode != 0:
            logger.warning(f"Failed to apply {manifest_file.name}")
            failed_manifests.append(manifest_file.name)
        else:
            logger.debug(f"Successfully applied {manifest_file.name}")

    if failed_manifests:
        logger.warning(f"Failed to apply {len(failed_manifests)} manifests: {failed_manifests}")
        # Don't fail the deployment for individual manifest failures

    # Wait for deployments to be ready if configured
    if deploy_config.get("wait_for_rollout", True):
        logger.info("Waiting for deployments to be ready...")
        timeout = deploy_config.get("rollout_timeout", 300)

        # Get deployments in the namespace
        result = run.run(
            f"oc get deployments -n {namespace} -o jsonpath='{{.items[*].metadata.name}}'",
            check=False
        )

        if result.returncode == 0 and result.stdout.strip():
            deployments = result.stdout.strip().split()
            logger.info(f"Waiting for {len(deployments)} deployments: {deployments}")

            for deployment in deployments:
                logger.info(f"Waiting for deployment {deployment}...")
                result = run.run(
                    f"oc rollout status deployment/{deployment} -n {namespace} --timeout={timeout}s",
                    check=False
                )

                if result.returncode == 0:
                    logger.info(f"✅ Deployment {deployment} is ready")
                else:
                    logger.warning(f"⚠️ Deployment {deployment} failed to become ready within timeout")
        else:
            logger.info("No deployments found to wait for")

    logger.info("✅ FOURNOS manifests deployment completed")

    return 0


def deploy_forge_config():
    """
    Deploy FORGE configuration for FOURNOS

    Returns:
        int: 0 on success, 1 on failure
    """
    logger.info("=== Deploying FORGE Configuration ===")

    namespace = config.project.get_config("fournos_deploy.namespace")

    # TODO: Define what FORGE configuration needs to be deployed
    # This could include:
    # - ConfigMaps with FORGE settings
    # - Secrets for FORGE integration
    # - RBAC permissions
    # - Custom resources

    logger.info("FORGE configuration deployment is not yet implemented")
    logger.info("This step would typically include:")
    logger.info("- ConfigMaps with FORGE settings")
    logger.info("- Integration secrets")
    logger.info("- RBAC permissions")
    logger.info("- Custom resource definitions")

    return 0


def deploy():
    """
    Complete FOURNOS deployment including image build and manifest deployment

    Returns:
        int: 0 on success, non-zero on failure
    """
    logger.info("=== Starting Complete FOURNOS Deployment ===")

    total_errors = 0

    # Step 1: Build image
    logger.info("Step 1: Building FOURNOS image...")
    result = build_image()
    if result != 0:
        logger.error("Image build failed, aborting deployment")
        return result
    total_errors += result

    # Step 2: Deploy manifests
    logger.info("Step 2: Deploying FOURNOS manifests...")
    result = deploy_manifests()
    total_errors += result

    # Step 3: Deploy FORGE configuration
    logger.info("Step 3: Deploying FORGE configuration...")
    result = deploy_forge_config()
    total_errors += result

    if total_errors == 0:
        logger.info("✅ Complete FOURNOS deployment succeeded")
    else:
        logger.error(f"❌ FOURNOS deployment completed with {total_errors} error(s)")

    return min(total_errors, 1)  # Return 1 if any errors occurred
