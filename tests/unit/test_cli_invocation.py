"""Invocation resolution — specs/fournos-cli.allium, entity CliInvocation.

Covers the derived precedence chains:
  owner:      --owner flag > config file > FOURNOS_OWNER env > $USER
  namespace:  -n flag > config file > FOURNOS_WORKLOAD_NAMESPACE env > context
  kubeconfig: --kubeconfig flag > config file > client default
"""

from __future__ import annotations

from tests.unit.fakes import make_invocation


class TestResolvedOwner:
    def test_flag_wins_over_config_env_and_user(self):
        inv = make_invocation(
            owner_flag="flag-owner",
            config_file_owner="config-owner",
            env_owner="env-owner",
            system_user="alice",
        )
        assert inv.resolved_owner == "flag-owner"

    def test_config_file_beats_env_and_user(self):
        inv = make_invocation(
            config_file_owner="config-owner",
            env_owner="env-owner",
            system_user="alice",
        )
        assert inv.resolved_owner == "config-owner"

    def test_env_beats_system_user(self):
        inv = make_invocation(env_owner="env-owner", system_user="alice")
        assert inv.resolved_owner == "env-owner"

    def test_falls_back_to_system_user(self):
        inv = make_invocation(system_user="alice")
        assert inv.resolved_owner == "alice"

    def test_none_when_all_sources_absent(self):
        inv = make_invocation(system_user=None)
        assert inv.resolved_owner is None


class TestResolvedNamespace:
    def test_flag_wins_over_config_env_and_context(self):
        inv = make_invocation(
            namespace_flag="flag-ns",
            config_file_namespace="config-ns",
            env_namespace="env-ns",
            context_namespace="ctx-ns",
        )
        assert inv.resolved_namespace == "flag-ns"

    def test_config_file_beats_env_and_context(self):
        inv = make_invocation(
            config_file_namespace="config-ns",
            env_namespace="env-ns",
            context_namespace="ctx-ns",
        )
        assert inv.resolved_namespace == "config-ns"

    def test_env_beats_context(self):
        inv = make_invocation(env_namespace="env-ns", context_namespace="ctx-ns")
        assert inv.resolved_namespace == "env-ns"

    def test_falls_back_to_context_namespace(self):
        inv = make_invocation(context_namespace="ctx-ns")
        assert inv.resolved_namespace == "ctx-ns"


class TestResolvedKubeconfig:
    def test_flag_wins_over_config_file_and_default(self):
        inv = make_invocation(
            kubeconfig_flag="/tmp/flag-kubeconfig",
            config_file_kubeconfig="/tmp/config-kubeconfig",
            default_kubeconfig="/home/alice/.kube/config",
        )
        assert inv.resolved_kubeconfig == "/tmp/flag-kubeconfig"

    def test_config_file_beats_default(self):
        inv = make_invocation(
            config_file_kubeconfig="/tmp/config-kubeconfig",
            default_kubeconfig="/home/alice/.kube/config",
        )
        assert inv.resolved_kubeconfig == "/tmp/config-kubeconfig"

    def test_falls_back_to_default(self):
        inv = make_invocation(default_kubeconfig="/home/alice/.kube/config")
        assert inv.resolved_kubeconfig == "/home/alice/.kube/config"
