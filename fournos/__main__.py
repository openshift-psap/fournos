"""Entry point: bridges FOURNOS_WORKLOAD_NAMESPACE env var to kopf --namespace."""

import os
import sys

ns = os.environ.get("FOURNOS_WORKLOAD_NAMESPACE")
if not ns:
    print("ERROR: FOURNOS_WORKLOAD_NAMESPACE env var is required", file=sys.stderr)
    sys.exit(1)

# Build the kopf CLI invocation, forwarding any extra args (e.g. --liveness)
sys.argv = [
    "kopf",
    "run",
    "-m",
    "fournos.operator",
    "--namespace",
    ns,
    *sys.argv[1:],
]

from kopf.cli import main  # noqa: E402

main()
