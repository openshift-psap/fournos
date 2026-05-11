from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from kubernetes import client

from fournos.core.constants import LABEL_MANAGED_BY, LABEL_VAULT_ENTRY
from fournos.settings import settings

logger = logging.getLogger(__name__)


def _build_cluster_name_regex(pattern: str) -> re.Pattern[str]:
    """Convert a format-string pattern like ``kubeconfig-{cluster}`` to a regex."""
    escaped = re.escape(pattern)
    regex = escaped.replace(r"\{cluster\}", r"(?P<cluster>.+)")
    return re.compile(f"^{regex}$")


_CLUSTER_NAME_RE: re.Pattern[str] | None = None


def _get_cluster_name_re() -> re.Pattern[str]:
    global _CLUSTER_NAME_RE
    if _CLUSTER_NAME_RE is None:
        _CLUSTER_NAME_RE = _build_cluster_name_regex(settings.kubeconfig_secret_pattern)
    return _CLUSTER_NAME_RE


def extract_cluster_name(secret_name: str) -> str | None:
    """Extract the cluster name from a kubeconfig secret name.

    Returns ``None`` if the secret name does not match the pattern.
    """
    m = _get_cluster_name_re().match(secret_name)
    return m.group("cluster") if m else None


def list_kubeconfig_secrets(k8s: client.CoreV1Api) -> list[str]:
    """List all secrets in the secrets namespace that match the kubeconfig pattern."""
    result = k8s.list_namespaced_secret(settings.secrets_namespace)
    pattern = _get_cluster_name_re()
    return [
        s.metadata.name
        for s in result.items
        if pattern.match(s.metadata.name)
    ]


@dataclass(frozen=True)
class ResolvedSecret:
    """A secret that has been copied into the pod namespace."""

    name: str
    original_name: str
    keys: list[str]


class ClusterRegistry:
    def __init__(self, k8s_client: client.CoreV1Api) -> None:
        self._k8s = k8s_client

    def resolve_kubeconfig_secret(self, cluster_name: str) -> str:
        """Return the Secret name that holds the kubeconfig for *cluster_name*."""
        return settings.kubeconfig_secret_pattern.format(cluster=cluster_name)

    def copy_kubeconfig_secret(
        self, cluster_name: str, fjob_name: str, owner_ref: dict
    ) -> str:
        """Copy the kubeconfig Secret for *cluster_name* into the operator namespace.

        Returns the name of the copied Secret (``<fjob_name>-kubeconfig``).
        Idempotent: a 409 (AlreadyExists) is silently ignored.
        """
        source_name = self.resolve_kubeconfig_secret(cluster_name)
        source = self._k8s.read_namespaced_secret(
            source_name, settings.secrets_namespace
        )

        copied_name = f"{fjob_name}-kubeconfig"

        copy_body = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=copied_name,
                namespace=settings.workload_namespace,
                labels={LABEL_MANAGED_BY: "fournos"},
                owner_references=[
                    client.V1OwnerReference(
                        api_version=owner_ref["apiVersion"],
                        kind=owner_ref["kind"],
                        name=owner_ref["name"],
                        uid=owner_ref["uid"],
                        controller=False,
                        block_owner_deletion=True,
                    )
                ],
            ),
            type=source.type,
            data=source.data,
        )

        try:
            self._k8s.create_namespaced_secret(settings.workload_namespace, copy_body)
            logger.info(
                "Copied kubeconfig %s from %s as %s",
                source_name,
                settings.secrets_namespace,
                copied_name,
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 409:
                logger.debug("Kubeconfig copy %s already exists (409)", copied_name)
            else:
                raise

        return copied_name

    def cluster_exists(self, cluster_name: str) -> bool:
        """Return True if the kubeconfig Secret for *cluster_name* exists."""
        secret_name = self.resolve_kubeconfig_secret(cluster_name)
        try:
            self._k8s.read_namespaced_secret(secret_name, settings.secrets_namespace)
            return True
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return False
            raise

    @staticmethod
    def _vault_secret_name(ref: str) -> str:
        """Apply the vault secret naming pattern to a user-supplied ref."""
        return settings.vault_secret_pattern.format(entry=ref)

    def _resolve_secret_ref(self, ref: str) -> client.V1Secret:
        """Verify that *ref* is a Vault-synced K8s Secret and return it.

        Users supply refs without the ``vault-`` prefix; the pattern from
        ``settings.vault_secret_pattern`` is applied to derive the real
        Secret name.  The Secret is read from ``secrets_namespace``.
        The ``fournos.dev/vault-entry=true`` label is checked to confirm
        the Secret was actually imported from Vault.

        Raises ``KeyError`` if the Secret does not exist or is not
        a Vault-synced secret.
        """
        secret_name = self._vault_secret_name(ref)
        try:
            secret = self._k8s.read_namespaced_secret(
                secret_name, settings.secrets_namespace
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                raise KeyError(
                    f"Secret {secret_name!r} (ref {ref!r}) not found in "
                    f"namespace {settings.secrets_namespace}"
                ) from exc
            raise
        labels = secret.metadata.labels or {}
        if labels.get(LABEL_VAULT_ENTRY) != "true":
            raise KeyError(
                f"Secret {secret_name!r} exists but is not a Vault-synced secret "
                f"(missing {LABEL_VAULT_ENTRY}=true label)"
            )
        logger.debug("Validated secretRef %s -> %s", ref, secret_name)
        return secret

    def resolve_secret_refs(self, refs: list[str]) -> list[str]:
        """Resolve a list of secretRefs to their K8s Secret names."""
        return [self._resolve_secret_ref(r).metadata.name for r in refs]

    def copy_secret(self, ref: str, fjob_name: str, owner_ref: dict) -> ResolvedSecret:
        """Copy a Vault-synced Secret from the secrets namespace into the pod namespace.

        *ref* is the user-supplied name (without ``vault-`` prefix).
        The copy is named ``<fjob_name>-<ref>`` and carries an ownerReference
        back to the FournosJob so K8s GC cleans it up automatically.
        Idempotent: a 409 (AlreadyExists) is silently ignored.
        """
        source = self._resolve_secret_ref(ref)
        secret_name = source.metadata.name

        keys = sorted((source.data or {}).keys())
        copied_name = f"{fjob_name}-{ref}"

        copy_body = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=copied_name,
                namespace=settings.workload_namespace,
                labels={
                    LABEL_MANAGED_BY: "fournos",
                    LABEL_VAULT_ENTRY: "true",
                },
                owner_references=[
                    client.V1OwnerReference(
                        api_version=owner_ref["apiVersion"],
                        kind=owner_ref["kind"],
                        name=owner_ref["name"],
                        uid=owner_ref["uid"],
                        controller=False,
                        block_owner_deletion=True,
                    )
                ],
            ),
            type=source.type,
            data=source.data,
        )

        try:
            self._k8s.create_namespaced_secret(settings.workload_namespace, copy_body)
            logger.info(
                "Copied secret %s (ref %s) from %s as %s",
                secret_name,
                ref,
                settings.secrets_namespace,
                copied_name,
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 409:
                logger.debug("Secret copy %s already exists (409)", copied_name)
            else:
                raise

        return ResolvedSecret(name=copied_name, original_name=ref, keys=keys)

    def copy_secrets(
        self, refs: list[str], fjob_name: str, owner_ref: dict
    ) -> list[ResolvedSecret]:
        """Copy all *refs* and return the resolved list."""
        return [self.copy_secret(r, fjob_name, owner_ref) for r in refs]
