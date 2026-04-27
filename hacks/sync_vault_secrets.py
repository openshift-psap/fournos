#!/usr/bin/env python3
"""Synchronize secrets from a HashiCorp Vault to Kubernetes.

Reads entries from a Vault KV v2 engine and creates one Kubernetes
Opaque Secret per vault entry in the dedicated secrets namespace.
Existing secrets are updated in-place.

Required environment variables
------------------------------
* ``VAULT_ADDR``  — Vault server URL (e.g. ``https://vault.example.com``)
* ``VAULT_TOKEN`` — Vault authentication token
* ``VAULT_SECRET_PATH`` — path within the KV engine (e.g. ``path/to/secrets``)

Optional environment variables
------------------------------
* ``VAULT_KV_MOUNT`` — KV v2 engine mount point (default: ``kv``)
* ``FOURNOS_SECRETS_NAMESPACE`` — target Kubernetes namespace (can also
  use ``-n``).  Defaults to ``psap-secrets``.

Prerequisites
-------------
* ``kubectl`` / ``oc`` context pointing at the target cluster, or
  in-cluster service-account credentials.

Examples
--------
Sync all vault entries under the configured path::

    export VAULT_ADDR="https://vault.example.com"
    export VAULT_TOKEN="s.xxxxx"
    export VAULT_SECRET_PATH="path/to/secrets"
    python hacks/sync_vault_secrets.py -n psap-secrets

Preview without touching the cluster::

    python hacks/sync_vault_secrets.py -n psap-secrets --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import urllib.error
import urllib.request

from fournos.core.constants import LABEL_VAULT_ENTRY
from fournos.settings import settings

logger = logging.getLogger(__name__)

KV_MOUNT_DEFAULT = "kv"

LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
ANNOTATION_VAULT_ADDR = "fournos.dev/vault-addr"
ANNOTATION_VAULT_PATH = "fournos.dev/vault-path"
MANAGER_VALUE = "fournos-vault-sync"


# ---------------------------------------------------------------------------
# Vault helpers
# ---------------------------------------------------------------------------


def _vault_request(
    addr: str,
    path: str,
    token: str,
    method: str = "GET",
) -> dict:
    url = f"{addr}/v1/{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("X-Vault-Token", token)
    logger.debug("%s %s", method, url)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Vault {method} {url} failed (HTTP {exc.code}): {exc.reason}"
        ) from exc


def vault_list(
    addr: str,
    mount: str,
    path: str,
    token: str,
) -> list[str]:
    """List entries under a KV v2 path."""
    api_path = f"{mount}/metadata/{path}"
    data = _vault_request(addr, api_path, token, method="LIST")
    return data.get("data", {}).get("keys", [])


def vault_read(
    addr: str,
    mount: str,
    path: str,
    token: str,
) -> dict[str, str]:
    """Read key-value pairs from a single KV v2 secret."""
    api_path = f"{mount}/data/{path}"
    data = _vault_request(addr, api_path, token)
    return data.get("data", {}).get("data", {})


# ---------------------------------------------------------------------------
# Kubernetes helpers (lazy import so --dry-run works without kubeconfig)
# ---------------------------------------------------------------------------


def _k8s_core_api():
    from kubernetes import client, config as k8s_config

    try:
        k8s_config.load_kube_config()
    except k8s_config.ConfigException:
        k8s_config.load_incluster_config()
    return client.CoreV1Api()


def _apply_secret(
    v1,
    name: str,
    namespace: str,
    data: dict[str, str],
    vault_addr: str,
    vault_full_path: str,
):
    """Create or update an Opaque Secret."""
    from kubernetes.client import V1ObjectMeta, V1Secret
    from kubernetes.client.exceptions import ApiException

    secret = V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels={
                LABEL_MANAGED_BY: MANAGER_VALUE,
                LABEL_VAULT_ENTRY: "true",
            },
            annotations={
                ANNOTATION_VAULT_ADDR: vault_addr,
                ANNOTATION_VAULT_PATH: vault_full_path,
            },
        ),
        type="Opaque",
        string_data=data,
    )

    try:
        v1.read_namespaced_secret(name, namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        v1.create_namespaced_secret(namespace, secret)
        logger.info("Created  Secret %s/%s", namespace, name)
    else:
        v1.replace_namespaced_secret(name, namespace, secret)
        logger.info("Updated  Secret %s/%s", namespace, name)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _is_vault_metadata(key: str) -> bool:
    return key.startswith("secretsync/")


_SECRET_KEY_RE = re.compile(r"^[-._a-zA-Z0-9]+$")


def _is_valid_k8s_key(key: str) -> bool:
    """Return True if *key* is a valid Kubernetes Secret data key."""
    return bool(key) and len(key) <= 253 and bool(_SECRET_KEY_RE.match(key))


_DNS_1123_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$")
_DNS_1123_MAX_LEN = 253


def is_valid_k8s_name(name: str) -> bool:
    """Return True if *name* is a valid DNS-1123 subdomain."""
    return bool(name) and len(name) <= _DNS_1123_MAX_LEN and _DNS_1123_RE.match(name)


def sync(
    *,
    vault_addr: str,
    vault_token: str,
    kv_mount: str,
    secret_path: str,
    namespace: str,
    dry_run: bool,
) -> int:
    logger.info("Listing vaults under %s/%s ...", kv_mount, secret_path)
    entries = vault_list(vault_addr, kv_mount, secret_path, vault_token)
    names = [e.rstrip("/") for e in entries if not e.endswith("/")]
    if not names:
        logger.warning("No vault entries found under %s/%s", kv_mount, secret_path)
        return 0
    logger.info("Found %d vault entries: %s", len(names), ", ".join(names))

    v1 = None if dry_run else _k8s_core_api()

    errors = 0
    for vault_name in names:
        full_path = f"{secret_path}/{vault_name}"

        if not is_valid_k8s_name(vault_name):
            logger.error(
                "Vault entry %r is not a valid DNS-1123 name, skipping",
                vault_name,
            )
            errors += 1
            continue

        secret_name = settings.vault_secret_pattern.format(entry=vault_name)

        try:
            logger.info("Reading %s/%s ...", kv_mount, full_path)
            kv_data = vault_read(vault_addr, kv_mount, full_path, vault_token)
        except Exception:
            logger.exception("Failed to read vault entry %s", full_path)
            errors += 1
            continue

        if not kv_data:
            logger.warning("Vault entry %s is empty, skipping", full_path)
            continue

        safe_data: dict[str, str] = {}
        for k, v in kv_data.items():
            if _is_vault_metadata(k):
                continue
            if not _is_valid_k8s_key(k):
                logger.error(
                    "Key %r in vault entry %s is not a valid K8s Secret key, skipping",
                    k,
                    full_path,
                )
                errors += 1
                continue
            safe_data[k] = v

        logger.info(
            "  %d key(s): %s",
            len(safe_data),
            ", ".join(safe_data.keys()),
        )

        if dry_run:
            print(f"[dry-run] Would create/update Secret {namespace}/{secret_name}")
            for key in safe_data:
                print(f"  {key}: <{len(str(safe_data[key]))} chars>")
            continue

        try:
            _apply_secret(
                v1,
                secret_name,
                namespace,
                safe_data,
                vault_addr=vault_addr,
                vault_full_path=f"{kv_mount}/{full_path}",
            )
        except Exception:
            logger.exception(
                "Failed to apply Secret %s/%s",
                namespace,
                secret_name,
            )
            errors += 1

    return 1 if errors else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync secrets from a HashiCorp Vault to Kubernetes.",
        epilog="Set VAULT_ADDR, VAULT_TOKEN, and VAULT_SECRET_PATH in the environment.",
    )
    parser.add_argument(
        "-n",
        "--namespace",
        default=os.environ.get("FOURNOS_SECRETS_NAMESPACE", "psap-secrets"),
        help="Target Kubernetes namespace (default: $FOURNOS_SECRETS_NAMESPACE or psap-secrets).",
    )
    parser.add_argument(
        "--vault-addr",
        default=os.environ.get("VAULT_ADDR", ""),
        help="Vault server URL (default: $VAULT_ADDR).",
    )
    parser.add_argument(
        "--kv-mount",
        default=os.environ.get("VAULT_KV_MOUNT", KV_MOUNT_DEFAULT),
        help=f"KV v2 engine mount point (default: {KV_MOUNT_DEFAULT}).",
    )
    parser.add_argument(
        "--secret-path",
        default=os.environ.get("VAULT_SECRET_PATH", ""),
        help="Path within the KV engine (default: $VAULT_SECRET_PATH).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without touching the cluster.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-5s %(message)s",
    )
    logging.getLogger("kubernetes").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    vault_token = os.environ.get("VAULT_TOKEN", "")
    if not vault_token:
        logger.error("VAULT_TOKEN environment variable is not set")
        return 1

    if not args.vault_addr:
        logger.error("VAULT_ADDR environment variable is not set (or use --vault-addr)")
        return 1

    if not args.secret_path:
        logger.error(
            "VAULT_SECRET_PATH environment variable is not set (or use --secret-path)"
        )
        return 1

    if not args.namespace:
        logger.error("--namespace is required (or set FOURNOS_SECRETS_NAMESPACE)")
        return 1

    return sync(
        vault_addr=args.vault_addr,
        vault_token=vault_token,
        kv_mount=args.kv_mount,
        secret_path=args.secret_path,
        namespace=args.namespace,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
