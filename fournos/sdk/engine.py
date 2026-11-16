"""Fournos Engine SDK — optional helper for engines that want decorator-style integration.

Most users should NOT need this. The built-in `fournos-generic` pipeline runs
any container image with zero Fournos awareness. This SDK exists for advanced
engines (like FORGE) that want tighter integration with FournosJob lifecycle
events (resolve vs run) inside their own image.

Usage (for advanced engines only):

    from fournos.sdk.engine import FournosEngine

    engine = FournosEngine("my-engine")

    @engine.on_resolve
    def resolve(config, ctx):
        ...

    @engine.on_run
    def run(config, ctx):
        ...

    if __name__ == "__main__":
        engine.main()
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field


@dataclass
class EngineContext:
    """Read-only context passed to resolve/run callbacks."""

    fjob_name: str
    namespace: str
    step: str
    artifact_dir: str
    fjob_spec: dict = field(default_factory=dict)


def _sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        print(f"ERROR: command {args!r} failed: {msg}", file=sys.stderr)
        raise subprocess.CalledProcessError(
            result.returncode, args, output=result.stdout, stderr=result.stderr
        )
    return result


class FournosEngine:
    def __init__(self, engine_name: str) -> None:
        self.engine_name = engine_name
        self._resolve_fn = None
        self._run_fn = None

    def on_resolve(self, fn):
        """Register the resolve callback. Receives (config, ctx)."""
        self._resolve_fn = fn
        return fn

    def on_run(self, fn):
        """Register the run callback. Receives (config, ctx)."""
        self._run_fn = fn
        return fn

    def _build_context(self) -> EngineContext:
        fjob_name = os.environ.get("FJOB_NAME")
        namespace = os.environ.get("FOURNOS_WORKLOAD_NAMESPACE")
        if not fjob_name or not namespace:
            print(
                "ERROR: FJOB_NAME and FOURNOS_WORKLOAD_NAMESPACE must be set",
                file=sys.stderr,
            )
            sys.exit(1)

        return EngineContext(
            fjob_name=fjob_name,
            namespace=namespace,
            step=os.environ.get("FOURNOS_STEP", "run"),
            artifact_dir=os.environ.get("ARTIFACT_DIR", "/tmp/artifacts"),
        )

    def _fetch_fjob(self, ctx: EngineContext) -> dict:
        result = _sh(
            "oc", "get", f"fjob/{ctx.fjob_name}",
            "-n", ctx.namespace, "-o", "json",
        )
        fjob = json.loads(result.stdout)
        os.makedirs(ctx.artifact_dir, exist_ok=True)
        with open(os.path.join(ctx.artifact_dir, "fournos_fjob.json"), "w") as f:
            json.dump(fjob, f, indent=2)
        return fjob

    def _extract_config(self, fjob: dict) -> dict:
        spec = fjob.get("spec", {})
        return (spec.get("executionEngine") or {}).get(self.engine_name) or {}

    def main(self) -> None:
        """Call from ``if __name__ == "__main__"``. Handles everything."""
        ctx = self._build_context()
        fjob = self._fetch_fjob(ctx)
        ctx.fjob_spec = fjob.get("spec", {})
        config = self._extract_config(fjob)

        try:
            if ctx.step == "resolve-fournos-config":
                if self._resolve_fn:
                    self._resolve_fn(config, ctx)
                return

            if not self._run_fn:
                print("ERROR: no @engine.on_run handler registered", file=sys.stderr)
                sys.exit(1)
            self._run_fn(config, ctx)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
