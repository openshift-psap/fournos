"""CliInvocation — how one CLI run resolves owner, namespace and kubeconfig.

Precedence chains (specs/fournos-cli.allium, entity CliInvocation):
  owner:      --owner flag > config file > FOURNOS_OWNER env > system user
  namespace:  -n flag > config file > FOURNOS_WORKLOAD_NAMESPACE env > context
  kubeconfig: --kubeconfig flag > config file > client default
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Invocation:
    owner_flag: str | None = None
    config_file_owner: str | None = None
    env_owner: str | None = None
    system_user: str | None = None

    namespace_flag: str | None = None
    config_file_namespace: str | None = None
    env_namespace: str | None = None
    context_namespace: str | None = None

    kubeconfig_flag: str | None = None
    config_file_kubeconfig: str | None = None
    default_kubeconfig: str | None = None

    @property
    def resolved_owner(self) -> str | None:
        return (
            self.owner_flag
            or self.config_file_owner
            or self.env_owner
            or self.system_user
        )

    @property
    def resolved_namespace(self) -> str | None:
        return (
            self.namespace_flag
            or self.config_file_namespace
            or self.env_namespace
            or self.context_namespace
        )

    @property
    def resolved_kubeconfig(self) -> str | None:
        return (
            self.kubeconfig_flag
            or self.config_file_kubeconfig
            or self.default_kubeconfig
        )
