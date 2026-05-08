import kopf

from fournos_cluster import operator  # noqa: F401 — registers kopf handlers
from fournos_cluster.settings import settings

kopf.run(
    namespaces=[settings.namespace, settings.secrets_namespace],
    liveness_endpoint="http://0.0.0.0:8080/healthz",
)
