import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path
import os
import logging

logger = logging.getLogger(__name__)


def ensure_oc_available():
    """Check if oc is available in PATH, download it to /tmp if not"""
    # Check if oc is already available
    if shutil.which("oc"):
        logger.info("OpenShift CLI (oc) is already available")
        return

    logger.info("OpenShift CLI (oc) not found in PATH, downloading to /tmp...")

    # Download URL for latest stable OpenShift client
    download_url = "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz"

    # Create temporary directory for download
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir = Path(temp_dir)
        tar_file = temp_dir / "openshift-client-linux.tar.gz"

        # Download the tar.gz file
        logger.info(f"Downloading oc from {download_url}")
        urllib.request.urlretrieve(download_url, tar_file)

        # Extract the tar file (with safe extraction filter)
        with tarfile.open(tar_file, "r:gz") as tar:
            tar.extract("oc", temp_dir, filter="data")

        # Copy to /tmp and make executable
        oc_binary = temp_dir / "oc"
        target_path = Path("/tmp/oc")
        shutil.copy2(oc_binary, target_path)
        target_path.chmod(0o755)

        # Add /tmp to PATH if not already there
        current_path = os.environ.get("PATH", "")
        if "/tmp" not in current_path:
            os.environ["PATH"] = f"/tmp:{current_path}"
            logger.info("Added /tmp to PATH")

        logger.info(f"OpenShift CLI (oc) installed to: {target_path}")
