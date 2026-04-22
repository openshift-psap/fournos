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
            self._k8s.read_namespaced_secret(secret_name, settings.namespace)
            return True
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return False
            raise

    def _resolve_secret_ref(self, ref: str) -> str:
        """Resolve a single secretRef to its K8s Secret name.

        Looks for a Secret labelled ``fournos.dev/vault-entry=<ref>``.
        Raises ``KeyError`` if no matching Secret is found.
        """
        secrets = self._k8s.list_namespaced_secret(
            settings.namespace,
            label_selector=f"{LABEL_VAULT_ENTRY}={ref}",
            limit=1,
        )
        if not secrets.items:
            raise KeyError(
                f"No Secret with label {LABEL_VAULT_ENTRY}={ref} "
                f"found in namespace {settings.namespace}"
            )
        resolved = secrets.items[0].metadata.name
        logger.debug("Resolved secretRef %s → %s", ref, resolved)
        return resolved

    def resolve_secret_refs(self, refs: list[str]) -> list[str]:
        """Resolve a list of secretRefs to their K8s Secret names."""
        return [self._resolve_secret_ref(r) for r in refs]
