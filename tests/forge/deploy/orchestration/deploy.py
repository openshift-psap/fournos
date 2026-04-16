from projects.core.library import env, config, run, vault
from projects.cluster.toolbox.build_image.main import run as build_image_toolbox
from projects.cluster.toolbox.rebuild_image.main import run as rebuild_image_toolbox

import pathlib
import logging
import os
import yaml
from pathlib import Path

logger = logging.getLogger(__name__)


def _istag_exists(istag_name: str, namespace: str) -> bool:
    """
    Check if an ImageStreamTag exists in the given namespace

    Args:
        istag_name: ImageStreamTag name in format 'imagestream:tag'
        namespace: Target namespace

    Returns:
        bool: True if the ImageStreamTag exists, False otherwise
    """
    result = run.run(
        f"oc get istag {istag_name} -n {namespace}",
        check=False,
        capture_stdout=True,
    )
    return result.returncode == 0


def _apply_manifest_replacements(manifest_file):
    """
    Apply text replacements to a manifest file

    Args:
        manifest_file: Path to the manifest file

    Returns:
        str: Processed manifest content with replacements applied
    """
    # Get replacements configuration
    replacements_config = config.project.get_config(
        "fournos_deploy.manifests.replace", print=False
    )

    # Prepare replacements with config value resolution
    resolved_replacements = {}
    for key in replacements_config.keys():
        resolved_value = config.project.get_config(
            f"fournos_deploy.manifests.replace.{key}", print=False
        )
        resolved_replacements[key] = str(resolved_value)
        logger.info(f"Replacement: ${key} -> {resolved_value}")

    # Read manifest content
    with open(manifest_file, "r") as f:
        manifest_content = f.read()

    # Apply text replacements
    for key, value in resolved_replacements.items():
        manifest_content = manifest_content.replace(f"${{{key}}}", value)

    return manifest_content


def ensure_namespace():
    """
    Create namespace with labels if it doesn't exist

    Returns:
        str: The namespace name
    """
    namespace_config = config.project.get_config("fournos_deploy.namespace")
    namespace = namespace_config["name"]
    namespace_labels = namespace_config.get("labels", {})

    # Create namespace if it doesn't exist
    result = run.run(
        f"oc create namespace {namespace}", check=False, capture_stderr=True
    )

    if result.returncode == 0:
        logger.info(f"Created namespace: {namespace}")
    elif "already exists" in result.stderr:
        logger.info(f"Namespace {namespace} already exists. Skip its configuration.")
        return namespace
    else:
        raise RuntimeError(f"Failed to create namespace {namespace}: {result.stderr}")

    # Apply namespace labels if any
    if not namespace_labels:
        return namespace

    labels_args = []
    for key, value in namespace_labels.items():
        labels_args.append(f"{key}={value}")

    labels_str = " ".join(labels_args)
    result = run.run(f"oc label namespace {namespace} {labels_str}")
    if result.returncode == 0:
        logger.info(f"Applied labels to namespace: {labels_str}")
    else:
        logger.warning(f"Failed to apply labels to namespace: {result.stderr}")

    return namespace


def _deploy_manifest_list(
    manifest_files, namespace, fournos_source, skip_kinds, file_prefix="manifest"
):
    """
    Deploy a list of manifest files with common processing logic

    Args:
        manifest_files: List of manifest file paths relative to fournos_source
        namespace: Target namespace name for deployment
        fournos_source: Path to FOURNOS source directory
        skip_kinds: Set of Kubernetes kinds to skip
        file_prefix: Prefix for processed file names

    Returns:
        int: 0 on success, raises exception on failure
    """
    if not manifest_files:
        logger.warning(f"No {file_prefix} files configured for deployment")
        return 0

    # Process and apply each configured manifest
    skipped_manifests = []

    # Create output directory for processed manifests
    manifests_dir = env.ARTIFACT_DIR / "src" / f"{file_prefix}s"
    manifests_dir.mkdir(exist_ok=True, parents=True)

    for manifest_path in manifest_files:
        manifest_file = fournos_source / manifest_path

        if not manifest_file.exists():
            raise FileNotFoundError(
                f"{file_prefix.title()} file not found: {manifest_path}"
            )

        logger.info(f"Processing: {manifest_path}")

        # Apply replacements
        manifest_content = _apply_manifest_replacements(manifest_file)

        # Parse YAML to check for skip_kinds
        docs = list(yaml.safe_load_all(manifest_content))

        should_skip = False
        for doc in docs:
            if doc and doc.get("kind") in skip_kinds:
                logger.info(
                    f"Skipping {manifest_path}: contains {doc.get('kind')} (in skip_kinds)"
                )
                skipped_manifests.append(manifest_path)
                should_skip = True
                break

        if should_skip:
            continue

        # Write processed manifest to temporary file
        processed_file = manifests_dir / f"processed-{manifest_file.name}"
        with open(processed_file, "w") as f:
            f.write(manifest_content)

        # Determine whether to use 'create' or 'apply' based on generateName
        has_generate_name = any(
            doc and doc.get("metadata", {}).get("generateName") for doc in docs if doc
        )

        if has_generate_name:
            # Use 'create' for objects with generateName
            oc_command = f"oc create -f {processed_file} -n {namespace}"
            action = "created"
        else:
            # Use 'apply' for objects with name
            oc_command = f"oc apply -f {processed_file} -n {namespace}"
            action = "applied"

        result = run.run(oc_command, check=False, capture_stderr=True)

        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to {action.split()[0]} {file_prefix} {manifest_path}: {result.stderr}"
            )

        logger.info(f"✅ Successfully {action} {manifest_path}")

    # Summary
    successful_count = len(manifest_files) - len(skipped_manifests)
    logger.info(f"{file_prefix.title()} deployment summary:")
    logger.info(f"  ✅ Applied: {successful_count}")
    logger.info(f"  ⏭️  Skipped: {len(skipped_manifests)}")

    if skipped_manifests:
        logger.info(f"Skipped {file_prefix}s: {skipped_manifests}")

    logger.info(f"✅ {file_prefix.title()} stored in {manifests_dir}")

    return 0


def _setup_kubeconfig_from_vault():
    """
    Setup KUBECONFIG from vault with fallback logic

    Priority:
    1. If vault kubeconfig is available, use it
    2. If vault kubeconfig is missing but KUBECONFIG env var is set, keep existing
    3. Otherwise, raise exception

    Raises:
        RuntimeError: If neither vault kubeconfig nor existing KUBECONFIG is available
    """

    # Check if vault configuration exists
    vaults = config.project.get_config("vaults", print=False)
    vault.init(vaults, strict=False)

    # Try to get kubeconfig vault configuration
    kubeconfig_vault_name = config.project.get_config(
        "secrets.kubeconfig.vault.name", print=False
    )
    kubeconfig_vault_key = config.project.get_config(
        "secrets.kubeconfig.vault.key", print=False
    )

    # Try to get kubeconfig path from vault
    kubeconfig_path = vault.get_vault_content_path(
        kubeconfig_vault_name, kubeconfig_vault_key
    )
    if kubeconfig_path and Path(kubeconfig_path).exists():
        os.environ["KUBECONFIG"] = str(kubeconfig_path)
        logger.info(f"✅ Set KUBECONFIG from vault: {kubeconfig_path}")
        return

    logger.info("Vault kubeconfig file does not exist or is empty")

    if "KUBECONFIG" in os.environ:
        logger.info("Using existing KUBECONFIG environment variable")
        return

    raise RuntimeError("Neither vault kubeconfig nor existing KUBECONFIG is available")


def init():
    env.init()
    run.init()
    config.init(pathlib.Path(__file__).parent)
    _setup_kubeconfig_from_vault()


def build_image():
    """
    Build FOURNOS image using Shipwright

    Returns:
        int: 0 on success, 1 on failure
    """
    logger.info("=== Building FOURNOS Image ===")

    # Get configuration parameters
    build_config = config.project.get_config("fournos_deploy.build")
    namespace = config.project.get_config("fournos_deploy.namespace.name")
    force_rebuild = config.project.get_config(
        "fournos_deploy.images.fournos.force_rebuild", print=False
    )

    # Get commit from configuration
    commit = build_config.get("commit", "main")

    logger.info(f"Building image from commit: {commit}")
    logger.info(f"Repository: {build_config['repo_name']}")
    logger.info(
        f"Target: {build_config['imagestream_name']}:{build_config['tag_name']}"
    )
    logger.info(f"Namespace: {namespace}")
    logger.info(f"Force rebuild: {force_rebuild}")

    # Check if image already exists and force_rebuild is False
    if not force_rebuild:
        logger.info("Checking if image already exists...")

        # Check if ImageStreamTag exists
        istag_name = f"{build_config['imagestream_name']}:{build_config['tag_name']}"
        if _istag_exists(istag_name, namespace):
            logger.info(f"✅ Image {istag_name} already exists, skipping build")
            return 0

        logger.info(f"Image {istag_name} does not exist, proceeding with build")

    # Call the build toolbox
    result = build_image_toolbox(
        repo_name=build_config["repo_name"],
        commit=commit,
        imagestream_name=build_config["imagestream_name"],
        tag_name=build_config["tag_name"],
        dockerfile_path=build_config.get(
            "dockerfile_path", "projects/core/image/Containerfile"
        ),
        namespace=namespace,
        timeout_minutes=build_config.get("timeout_minutes", 30),
    )

    if not result:
        logger.error("❌ Image build failed")
        return 1

    logger.info("✅ Image build completed successfully")
    return 0


def deploy_manifests():
    """
    Deploy FOURNOS manifests from configured manifest list

    Returns:
        int: 0 on success, raises exception on failure
    """
    logger.info("=== Deploying FOURNOS Manifests ===")

    # Get configuration
    fournos_source = Path(
        config.project.get_config("fournos_deploy.fournos_source.path")
    )
    deploy_config = config.project.get_config("fournos_deploy.deploy")
    manifests_config = config.project.get_config("fournos_deploy.manifests")

    if not fournos_source.exists():
        raise ValueError(f"FOURNOS source directory not found: {fournos_source}")

    # Ensure namespace exists
    namespace = ensure_namespace()

    logger.info(f"Deploying from: {fournos_source}")
    logger.info(f"Target namespace: {namespace}")

    # Get manifest deployment configuration
    skip_kinds = set(manifests_config["skip_kinds"])
    rbac_files = manifests_config["rbac"]
    crd_files = manifests_config["crd"]
    manifest_files = rbac_files + crd_files

    logger.info(
        f"Will deploy {len(rbac_files)} RBAC and {len(crd_files)} CRD manifest files"
    )
    logger.info(f"Skipping kinds: {list(skip_kinds)}")

    # Deploy the manifests using common helper
    _deploy_manifest_list(
        manifest_files, namespace, fournos_source, skip_kinds, "manifest"
    )

    # Wait for deployments to be ready if configured
    if deploy_config["wait_for_rollout"]:
        logger.info("Waiting for deployments to be ready...")
        timeout = deploy_config["rollout_timeout"]

        # Get deployments in the namespace
        result = run.run(
            f"oc get deployments -n {namespace} -o jsonpath='{{.items[*].metadata.name}}'",
            check=False,
            capture_stdout=True,
        )

        if result.returncode == 0 and result.stdout.strip():
            deployments = result.stdout.strip().split()
            logger.info(f"Waiting for {len(deployments)} deployments: {deployments}")

            for deployment in deployments:
                logger.info(f"Waiting for deployment {deployment}...")
                result = run.run(
                    f"oc rollout status deployment/{deployment} -n {namespace} --timeout={timeout}s",
                    check=False,
                )

                if result.returncode == 0:
                    logger.info(f"✅ Deployment {deployment} is ready")
                else:
                    raise RuntimeError(
                        f"Deployment {deployment} failed to become ready within {timeout}s timeout"
                    )
        else:
            logger.info("No deployments found to wait for")

    logger.info("✅ FOURNOS manifests deployment completed")

    return 0


def deploy_fournos_workload():
    """
    Deploy FOURNOS deployment with built image

    Returns:
        int: 0 on success, raises exception on failure
    """
    logger.info("=== Deploying FOURNOS Workload ===")

    # Get configuration
    fournos_source = Path(
        config.project.get_config("fournos_deploy.fournos_source.path")
    )
    manifests_config = config.project.get_config("fournos_deploy.manifests")
    build_config = config.project.get_config("fournos_deploy.build")

    # Ensure namespace exists
    namespace = ensure_namespace()

    deployment_path = manifests_config["deploy"]["fournos"]
    deployment_file = fournos_source / deployment_path

    if not deployment_file.exists():
        raise FileNotFoundError(f"Deployment manifest not found: {deployment_path}")

    logger.info(f"Deploying from: {deployment_file}")
    logger.info(f"Target namespace: {namespace}")

    # Calculate built image name
    image_registry = "image-registry.openshift-image-registry.svc:5000"
    image_name = f"{image_registry}/{namespace}/{build_config['imagestream_name']}:{build_config['tag_name']}"
    logger.info(f"Using built image: {image_name}")

    # Apply text replacements
    manifest_content = _apply_manifest_replacements(deployment_file)

    # Parse and update the deployment with the built image
    docs = list(yaml.safe_load_all(manifest_content))

    for doc in docs:
        if doc and doc.get("kind") == "Deployment":
            # Update the image in the deployment spec
            containers = doc["spec"]["template"]["spec"]["containers"]
            for container in containers:
                # Update the image to use our built image
                container["image"] = image_name
                logger.info(
                    f"Updated container '{container.get('name', 'unnamed')}' image to: {image_name}"
                )

    # Write updated manifest back to YAML
    updated_content = yaml.dump_all(docs, default_flow_style=False)

    # Write processed manifest to temporary file
    deploy_dir = env.ARTIFACT_DIR / "src" / "manifests"
    deploy_dir.mkdir(exist_ok=True, parents=True)
    processed_file = deploy_dir / f"processed-{deployment_file.name}"
    with open(processed_file, "w") as f:
        f.write(updated_content)

    # Apply the processed manifest
    result = run.run(f"oc apply -f {processed_file} -n {namespace}", check=False)

    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to apply deployment {deployment_path}: {result.stderr}"
        )

    logger.info("✅ Successfully deployed FOURNOS workload")

    return 0


def deploy_workflow_config():
    """
    Deploy FORGE workflow configuration for FOURNOS

    Returns:
        int: 0 on success, raises exception on failure
    """
    logger.info("=== Deploying FORGE Workflow Configuration ===")

    # Get configuration
    fournos_source = Path(
        config.project.get_config("fournos_deploy.fournos_source.path")
    )
    skip_kinds = set(config.project.get_config("fournos_deploy.manifests.skip_kinds"))
    config_manifests = config.project.get_config("fournos_deploy.manifests.config")

    if not fournos_source.exists():
        raise ValueError(f"FOURNOS source directory not found: {fournos_source}")

    # Ensure namespace exists
    namespace = ensure_namespace()

    logger.info(f"Deploying from: {fournos_source}")
    logger.info(f"Target namespace: {namespace}")

    # Combine all manifest files from all config sections
    manifest_files = []
    for section_name, section_files in config_manifests.items():
        manifest_files.extend(section_files)
        logger.info(f"Added {len(section_files)} manifests from {section_name}")

    logger.info(f"Will deploy {len(manifest_files)} total config manifest files")
    logger.info(f"Skipping kinds: {list(skip_kinds)}")

    # Deploy the config manifests using common helper
    _deploy_manifest_list(
        manifest_files, namespace, fournos_source, skip_kinds, "config"
    )

    logger.info("✅ FORGE workflow configuration deployment completed")

    return 0


def rebuild_workflow_images():
    """
    Rebuild the workflow images

    Returns:
        int: 0 on success, raises exception on failure
    """
    logger.info("=== Rebuilding FOURNOS workflow images ===")

    # Get configuration
    namespace = ensure_namespace()
    fournos_source = Path(
        config.project.get_config("fournos_deploy.fournos_source.path")
    )
    to_build_manifests = config.project.get_config("fournos_deploy.manifests.to_build")
    force_rebuild = config.project.get_config(
        "fournos_deploy.images.workflows.force_rebuild", print=False
    )

    logger.info(f"Force rebuild workflows: {force_rebuild}")

    if not to_build_manifests:
        logger.info("No builds configured for rebuild")
        return 0

    logger.info(f"Found {len(to_build_manifests)} build manifest(s) to process")

    # Extract build names from manifests and rebuild each
    skipped_builds = 0
    for manifest_path in to_build_manifests:
        manifest_file = fournos_source / manifest_path

        if not manifest_file.exists():
            raise ValueError(f"Build manifest {manifest_file} doesn't exist")

        logger.info(f"Processing build manifest: {manifest_path}")

        # Apply text replacements to resolve any config references
        manifest_content = _apply_manifest_replacements(manifest_file)

        # Parse YAML to extract build name and output image
        doc = yaml.safe_load(manifest_content)

        if not (doc and doc.get("kind") == "Build"):
            raise ValueError(f"Build manifest {manifest_file} isn't a Build")

        build_name = doc["metadata"]["name"]
        output_image = doc["spec"]["output"]["image"]

        logger.info(f"Rebuilding build: {build_name}")
        logger.info(f"Output image: {output_image}")

        # Check if image already exists and force_rebuild is False
        if not force_rebuild:
            logger.info("Checking if output image already exists...")

            # Extract ImageStreamTag name from output image (everything after last /)
            # Format: image-registry.openshift-image-registry.svc:5000/namespace/imagestream:tag
            istag_name = output_image.split("/")[-1]

            # Check if ImageStreamTag exists
            if _istag_exists(istag_name, namespace):
                logger.info(f"✅ Image {istag_name} already exists, skipping rebuild")
                skipped_builds += 1
                continue

            logger.info(f"Image {istag_name} does not exist, proceeding with rebuild")

        result = rebuild_image_toolbox(
            build_name=build_name, namespace=namespace, timeout_minutes=30
        )

        if not result:
            logger.error(f"❌ Rebuild failed for build: {build_name}")
            raise RuntimeError(f"Rebuild of {manifest_file} failed :/")

        logger.info(f"✅ Rebuild completed successfully for build: {build_name}")

    logger.info(
        f"✅ FORGE image rebuild completed - {skipped_builds} skipped, {len(to_build_manifests) - skipped_builds} rebuilt"
    )

    return 0


def cleanup():
    """
    Clean up FOURNOS deployment resources

    Returns:
        int: 0 on success, raises exception on failure
    """
    logger.info("=== Cleaning up FOURNOS Resources ===")

    # Get configuration
    namespace = ensure_namespace()
    cleanup_config = config.project.get_config("fournos_deploy.cleanup")
    cleanup_resources = cleanup_config["resources"]

    # Guard: Early return if no resources configured
    if not cleanup_resources:
        logger.info("No cleanup resources configured")
        return 0

    logger.info(
        f"Will clean up {len(cleanup_resources)} resource types from namespace: {namespace}"
    )

    total_errors = 0
    deleted_resources = 0

    for resource_spec in cleanup_resources:
        logger.info(f"Cleaning up resource: {resource_spec}")

        try:
            # Get list of resources first to see what exists
            result = run.run(
                f"oc get {resource_spec} -n {namespace} --no-headers -o name 2>/dev/null || true",
                check=False,
                capture_stdout=True,
            )

            # Guard: Skip if command failed or no resources found
            if result.returncode != 0 or not result.stdout.strip():
                logger.info(f"No {resource_spec} resources found to delete")
                continue

            resources = result.stdout.strip().split("\n")
            logger.info(f"Found {len(resources)} {resource_spec} resources to delete")

            # Delete all resources of this type
            delete_result = run.run(
                f"oc delete {resource_spec} --all -n {namespace} --ignore-not-found",
                check=False,
            )

            # Guard: Handle deletion failure
            if delete_result.returncode != 0:
                logger.error(
                    f"❌ Failed to delete {resource_spec}: {delete_result.stderr}"
                )
                total_errors += 1
                continue

            deleted_resources += len(resources)
            logger.info(f"✅ Deleted {len(resources)} {resource_spec} resources")

        except Exception as e:
            logger.error(f"❌ Error cleaning up {resource_spec}: {e}")
            total_errors += 1

    # Summary
    if total_errors == 0:
        logger.info(
            f"✅ Cleanup completed successfully - deleted {deleted_resources} resources"
        )
        return 0

    logger.warning(
        f"⚠️ Cleanup completed with {total_errors} error(s) - deleted {deleted_resources} resources"
    )
    return 0


def cleanup_and_deploy():
    """
    Clean up existing resources and perform complete FOURNOS deployment

    This provides a clean slate deployment by first removing existing resources
    and then deploying everything fresh.

    Returns:
        int: 0 on success, non-zero on failure
    """
    logger.info("=== Starting Clean Slate FOURNOS Deployment ===")

    total_errors = 0

    # Step 0: Cleanup existing resources
    logger.info("Step 0: Cleaning up existing FOURNOS resources...")
    result = cleanup()
    total_errors += result

    # Now perform the complete deployment
    logger.info("Starting fresh deployment...")
    result = deploy()
    total_errors += result

    if total_errors == 0:
        logger.info("✅ Clean slate FOURNOS deployment succeeded")
    else:
        logger.error(
            f"❌ Clean slate FOURNOS deployment completed with {total_errors} error(s)"
        )

    return min(total_errors, 1)  # Return 1 if any errors occurred


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

    # Step 3: Deploy FOURNOS workload
    logger.info("Step 3: Deploying FOURNOS workload...")
    result = deploy_fournos_workload()
    total_errors += result

    # Step 4: Deploy FORGE workflow configuration
    logger.info("Step 4: Deploying FORGE workflow configuration...")
    result = deploy_workflow_config()
    total_errors += result

    # Step 5: Rebuild FORGE images
    logger.info("Step 5: Rebuilding FOURNOS workflow images...")
    result = rebuild_workflow_images()
    total_errors += result

    if total_errors == 0:
        logger.info("✅ Complete FOURNOS deployment succeeded")
    else:
        logger.error(f"❌ FOURNOS deployment completed with {total_errors} error(s)")

    return min(total_errors, 1)  # Return 1 if any errors occurred
