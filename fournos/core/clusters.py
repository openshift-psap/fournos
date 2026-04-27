from __future__ import annotations

import logging
from dataclasses import dataclass

from kubernetes import client

from fournos.core.constants import LABEL_MANAGED_BY, LABEL_VAULT_ENTRY
from fournos.settings import settings

logger = logging.getLogger(__name__)


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

    def _resolve_secret_ref(self, ref: str) -> str:
        """Verify that *ref* is a Vault-synced K8s Secret and return its name.

        The Secret is read from ``secrets_namespace``.  The
        ``fournos.dev/vault-entry=true`` label is checked to confirm
        the Secret was actually imported from Vault.

        Raises ``KeyError`` if the Secret does not exist or is not
        a Vault-synced secret.
        """
        try:
            secret = self._k8s.read_namespaced_secret(ref, settings.secrets_namespace)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                raise KeyError(
                    f"Secret {ref!r} not found in namespace "
                    f"{settings.secrets_namespace}"
                ) from exc
            raise
        labels = secret.metadata.labels or {}
        if labels.get(LABEL_VAULT_ENTRY) != "true":
            raise KeyError(
                f"Secret {ref!r} exists but is not a Vault-synced secret "
                f"(missing {LABEL_VAULT_ENTRY}=true label)"
            )
        logger.debug("Validated secretRef %s", ref)
        return ref

    def resolve_secret_refs(self, refs: list[str]) -> list[str]:
        """Resolve a list of secretRefs to their K8s Secret names."""
        return [self._resolve_secret_ref(r) for r in refs]

    def copy_secret(self, ref: str, fjob_name: str, owner_ref: dict) -> ResolvedSecret:
        """Copy a Vault-synced Secret from the secrets namespace into the pod namespace.

        The copy is named ``<fjob_name>-<ref>`` and carries an ownerReference
        back to the FournosJob so K8s GC cleans it up automatically.
        Idempotent: a 409 (AlreadyExists) is silently ignored.
        """
        source = self._k8s.read_namespaced_secret(ref, settings.secrets_namespace)

        keys = sorted((source.data or {}).keys())
        copied_name = f"{fjob_name}-{ref}"

        copy_body = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=copied_name,
                namespace=settings.namespace,
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
            self._k8s.create_namespaced_secret(settings.namespace, copy_body)
            logger.info(
                "Copied secret %s from %s as %s",
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
