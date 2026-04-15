#!/usr/bin/env python3
"""
Fournos-Deploy Project CI Operations
"""

from projects.core.library import ci as ci_lib
from projects.fournos_launcher.orchestration import utils

import deploy as fournos_deploy

import click
import types


@click.group()
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx):
    """FOURNOS Deploy Project CI Operations for FORGE."""
    ctx.ensure_object(types.SimpleNamespace)
    fournos_deploy.init()
    utils.ensure_oc_available()

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
