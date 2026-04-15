#!/usr/bin/env python3
"""
Fournos-Deploy Project CI Operations
"""

from projects.core.library import ci as ci_lib
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


@main.command()
@click.pass_context
@ci_lib.safe_ci_command
def deploy(ctx):
    """Complete FOURNOS deployment (build + deploy manifests + deploy config)."""
    return fournos_deploy.deploy()


if __name__ == "__main__":
    main()
