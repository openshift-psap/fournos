import logging

from kubernetes import client

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
