import logging

from kubernetes import client

from fournos.core.constants import LABEL_VAULT_ENTRY
from fournos.settings import settings

logger = logging.getLogger(__name__)


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

        Vault-synced secrets use a ``vault-`` prefix: the K8s Secret
        name is ``vault-<vault-entry-name>``.  The
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
