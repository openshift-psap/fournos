"""Console entry point — ``fournos`` command.

  fournos get fjobs [--all | --owner OWNER] [-n NAMESPACE]
  fournos get pods [FJOB] [--all | --owner OWNER] [-n NAMESPACE]
  fournos logs FJOB [-C POD] [-f]
  fournos stop FJOB [--yes]
  fournos terminate FJOB
  fournos delete FJOB [--yes]

FJOB arguments accept a bare name or a ``fjob/name`` prefix.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import yaml

from fournos.cli.cluster import KubeClusterClient
from fournos.cli.commands import (
    apply_confirmation,
    get_fjob_row,
    list_fjob_rows,
    list_pod_rows,
    open_log_stream,
    request_deletion,
    request_shutdown,
)
from fournos.cli.invocation import Invocation
from fournos.cli.model import CommandFailed, ConfirmationPrompt, LogStream
from fournos.core.constants import Shutdown

DEFAULT_CONFIG_FILE = Path.home() / ".config" / "fournos" / "fournos-cli.yaml"
WATCH_INTERVAL_SEC = 2


def _load_config_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        print(
            f"warning: could not parse config file {path}; ignoring it", file=sys.stderr
        )
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _context_namespace(kubeconfig: str | None) -> str:
    """Namespace of the kubeconfig's current context ("default" when unset)."""
    try:
        from kubernetes import config as k8s_config

        _, current = k8s_config.list_kube_config_contexts(config_file=kubeconfig)
        return current.get("context", {}).get("namespace") or "default"
    except Exception:
        return "default"


def _build_invocation(args: argparse.Namespace) -> Invocation:
    config = _load_config_file(Path(args.config))
    kubeconfig_flag = args.kubeconfig
    default_kubeconfig = os.environ.get("KUBECONFIG") or str(
        Path.home() / ".kube" / "config"
    )
    return Invocation(
        owner_flag=args.owner,
        config_file_owner=config.get("owner"),
        env_owner=os.environ.get("FOURNOS_OWNER"),
        system_user=os.environ.get("USER") or getpass.getuser(),
        namespace_flag=args.namespace,
        config_file_namespace=config.get("namespace"),
        env_namespace=os.environ.get("FOURNOS_WORKLOAD_NAMESPACE"),
        context_namespace=_context_namespace(kubeconfig_flag or default_kubeconfig),
        kubeconfig_flag=kubeconfig_flag,
        config_file_kubeconfig=config.get("kubeconfig"),
        default_kubeconfig=default_kubeconfig,
    )


def _fjob_name(raw: str) -> str:
    for prefix in ("fjob/", "fjobs/", "fj/"):
        if raw.startswith(prefix):
            return raw[len(prefix) :]
    return raw


# -- output --------------------------------------------------------------------


def _fmt_age(age: timedelta | None) -> str:
    if age is None:
        return "?"
    seconds = int(age.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    header = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(header)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def _print_fjob_rows(rows) -> None:
    _print_table(
        ["NAME", "OWNER", "PHASE", "CLUSTER", "AGE"],
        [
            [
                r.shown_name,
                r.shown_owner or "-",
                r.shown_phase.value,
                r.shown_cluster or "-",
                _fmt_age(r.shown_age),
            ]
            for r in rows
        ],
    )


def _print_pod_rows(rows) -> None:
    _print_table(
        ["NAME", "FJOB", "OWNER", "STATUS", "RESTARTS", "AGE"],
        [
            [
                r.shown_name,
                r.shown_fjob or "-",
                r.shown_owner or "-",
                r.shown_status.value,
                str(r.shown_restarts),
                _fmt_age(r.shown_age),
            ]
            for r in rows
        ],
    )


# -- command handlers ----------------------------------------------------------


def _watch_loop(refresh) -> int:
    """Re-run refresh() (which prints) every interval until interrupted."""
    try:
        while True:
            # clear screen + home cursor, the way kubectl refreshes `get -w`
            print("\033[2J\033[H", end="")
            code = refresh()
            if code != 0:
                return code
            time.sleep(WATCH_INTERVAL_SEC)
    except KeyboardInterrupt:
        return 0


def _run_get_fjobs(args) -> int:
    invocation = _build_invocation(args)
    cluster = KubeClusterClient(invocation.resolved_kubeconfig)

    if args.fjob:
        # single-job get: no owner filtering, mirrors `oc get fjob <name>`
        name = _fjob_name(args.fjob)

        def refresh() -> int:
            row = get_fjob_row(cluster, invocation, name)
            if isinstance(row, CommandFailed):
                return _fail(row)
            _print_fjob_rows([row])
            return 0

    else:

        def refresh() -> int:
            rows = list_fjob_rows(
                cluster, invocation, all_jobs=args.all, owner_filter=args.owner
            )
            if isinstance(rows, CommandFailed):
                return _fail(rows)
            if not rows:
                print(f"no FournosJobs in namespace {invocation.resolved_namespace}")
                return 0
            _print_fjob_rows(rows)
            return 0

    if args.watch:
        return _watch_loop(refresh)
    return refresh()


def _run_get_pods(args) -> int:
    invocation = _build_invocation(args)
    cluster = KubeClusterClient(invocation.resolved_kubeconfig)
    fjob = _fjob_name(args.fjob) if args.fjob else None

    def refresh() -> int:
        rows = list_pod_rows(
            cluster,
            invocation,
            fjob_name=fjob,
            all_jobs=args.all,
            owner_filter=args.owner,
        )
        if isinstance(rows, CommandFailed):
            return _fail(rows)
        if not rows:
            print(f"no execution pods in namespace {invocation.resolved_namespace}")
            return 0
        _print_pod_rows(rows)
        return 0

    if args.watch:
        return _watch_loop(refresh)
    return refresh()


def _run_logs(args) -> int:
    invocation = _build_invocation(args)
    cluster = KubeClusterClient(invocation.resolved_kubeconfig)
    stream = open_log_stream(
        cluster,
        invocation,
        _fjob_name(args.fjob),
        pod_name=args.container,
        follow=args.follow,
    )
    if isinstance(stream, CommandFailed):
        return _fail(stream)
    assert isinstance(stream, LogStream)
    if len(stream.offered_pods) > 1:
        print(
            f"{len(stream.offered_pods)} running pods for {stream.job.name}, "
            f"streaming {stream.pod.name} (newest first: "
            + ", ".join(p.name for p in stream.offered_pods)
            + ")"
        )
    _stream_logs(cluster, stream)
    return 0


def _stream_logs(cluster: KubeClusterClient, stream: LogStream) -> None:
    response = cluster.read_pod_logs(
        stream.pod.namespace, stream.pod.name, follow=stream.follow
    )
    try:
        if stream.follow:
            for chunk in response.stream():
                sys.stdout.write(chunk.decode(errors="replace"))
                sys.stdout.flush()
        else:
            sys.stdout.write(response.read().decode(errors="replace"))
    except BrokenPipeError:
        pass


def _run_shutdown(args, mode: Shutdown) -> int:
    invocation = _build_invocation(args)
    cluster = KubeClusterClient(invocation.resolved_kubeconfig)
    result = request_shutdown(
        cluster, invocation, _fjob_name(args.fjob), mode, assume_yes=args.yes
    )
    return _handle_mutation_result(cluster, result)


def _run_delete(args) -> int:
    invocation = _build_invocation(args)
    cluster = KubeClusterClient(invocation.resolved_kubeconfig)
    result = request_deletion(
        cluster, invocation, _fjob_name(args.fjob), assume_yes=args.yes
    )
    return _handle_mutation_result(cluster, result)


def _handle_mutation_result(cluster, result) -> int:
    if isinstance(result, CommandFailed):
        return _fail(result)
    if isinstance(result, ConfirmationPrompt):
        if not _confirm(result):
            print("aborted")
            return 1
        apply_confirmation(cluster, result)
    print("done")
    return 0


def _confirm(prompt: ConfirmationPrompt) -> bool:
    verb = (
        f"shut down ({prompt.shutdown_mode.value})"
        if prompt.action.value == "shutdown"
        else "delete"
    )
    if prompt.warns_foreign_job:
        print(
            f"warning: job {prompt.job.name!r} is owned by "
            f"{prompt.job.owner!r}, not by you"
        )
    answer = input(f"{verb.capitalize()} FournosJob {prompt.job.name!r}? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def _fail(failure: CommandFailed) -> int:
    parts = [f"error: {failure.reason.value.replace('-', ' ')}"]
    if failure.job is not None:
        parts.append(f"job {failure.job.name!r}")
    if failure.hint:
        parts.append(failure.hint)
    print(" — ".join(parts), file=sys.stderr)
    return 1


# -- wiring --------------------------------------------------------------------


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--owner",
        help="job owner to filter by (default: your jobs; --all lists everyone's)",
    )
    parser.add_argument("-n", "--namespace", help="namespace to look in")
    parser.add_argument(
        "--kubeconfig", help="kubeconfig file to use (default: $KUBECONFIG)"
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_FILE),
        help=f"config file (default: {DEFAULT_CONFIG_FILE})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fournos", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    get = sub.add_parser("get", help="list jobs or pods")
    get_sub = get.add_subparsers(dest="resource", required=True)

    fjobs = get_sub.add_parser("fjobs", help="list FournosJobs")
    fjobs.add_argument(
        "fjob", nargs="?", help="show a single FournosJob (no owner filtering)"
    )
    fjobs.add_argument(
        "--all", action="store_true", help="list every job (default: only yours)"
    )
    fjobs.add_argument(
        "-w",
        "--watch",
        action="store_true",
        help="refresh the listing until interrupted",
    )
    _add_common_flags(fjobs)
    fjobs.set_defaults(func=_run_get_fjobs)

    pods = get_sub.add_parser("pods", help="list execution pods")
    pods.add_argument("fjob", nargs="?", help="restrict to one FournosJob")
    pods.add_argument(
        "--all", action="store_true", help="list every pod (default: only yours)"
    )
    pods.add_argument(
        "-w",
        "--watch",
        action="store_true",
        help="refresh the listing until interrupted",
    )
    _add_common_flags(pods)
    pods.set_defaults(func=_run_get_pods)

    logs = sub.add_parser("logs", help="stream logs of a job's active pod")
    logs.add_argument("fjob", help="FournosJob name (fjob/name or bare name)")
    logs.add_argument(
        "-C", "--container", dest="container", help="pod name to stream (see get pods)"
    )
    logs.add_argument(
        "-f", "--follow", action="store_true", help="follow the log stream"
    )
    _add_common_flags(logs)
    logs.set_defaults(func=_run_logs)

    stop = sub.add_parser("stop", help="graceful shutdown (runs finally tasks)")
    stop.add_argument("fjob", help="FournosJob name")
    stop.add_argument("--yes", action="store_true", help="skip confirmation")
    _add_common_flags(stop)
    stop.set_defaults(func=lambda args: _run_shutdown(args, Shutdown.STOP))

    terminate = sub.add_parser(
        "terminate", help="immediate shutdown (skips finally tasks)"
    )
    terminate.add_argument("fjob", help="FournosJob name")
    terminate.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    _add_common_flags(terminate)
    terminate.set_defaults(func=lambda args: _run_shutdown(args, Shutdown.TERMINATE))

    delete = sub.add_parser("delete", help="delete the FournosJob")
    delete.add_argument("fjob", help="FournosJob name")
    delete.add_argument("--yes", action="store_true", help="skip confirmation")
    _add_common_flags(delete)
    delete.set_defaults(func=_run_delete)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
