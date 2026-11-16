"""CLI surface wiring — specs/fournos-cli.allium surface FournosCli.

Verifies the argparse boundary matches the surface's provides contract
(single-job get, watch flags) and the config file contract
(~/.config/fournos/fournos-cli.yaml with owner, namespace, kubeconfig keys).
"""

from __future__ import annotations

from pathlib import Path

from fournos.cli.main import (
    DEFAULT_CONFIG_FILE,
    _fjob_name,
    _load_config_file,
    build_parser,
)


class TestParserSurface:
    def test_get_fjobs_accepts_single_job_positional(self):
        args = build_parser().parse_args(["get", "fjobs", "myjob"])
        assert args.fjob == "myjob"
        assert args.all is False

    def test_get_fjobs_all_flag(self):
        args = build_parser().parse_args(["get", "fjobs", "--all", "-w"])
        assert args.all is True
        assert args.watch is True

    def test_get_fjobs_watch_defaults_off(self):
        args = build_parser().parse_args(["get", "fjobs"])
        assert args.watch is False
        assert args.all is False

    def test_get_pods_all_flag(self):
        args = build_parser().parse_args(["get", "pods", "--all"])
        assert args.all is True

    def test_get_pods_watch_flag(self):
        args = build_parser().parse_args(["get", "pods", "-w"])
        assert args.watch is True
        assert args.all is False

    def test_fjob_prefix_is_stripped(self):
        assert _fjob_name("fjob/myjob") == "myjob"
        assert _fjob_name("fj/myjob") == "myjob"
        assert _fjob_name("myjob") == "myjob"


class TestConfigFile:
    def test_default_config_path_is_xdg_fournos(self):
        assert DEFAULT_CONFIG_FILE == (
            Path.home() / ".config" / "fournos" / "fournos-cli.yaml"
        )

    def test_loads_owner_namespace_kubeconfig_keys(self, tmp_path):
        cfg = tmp_path / "fournos-cli.yaml"
        cfg.write_text("owner: alice\nnamespace: bench\nkubeconfig: /tmp/kube\n")
        loaded = _load_config_file(cfg)
        assert loaded == {
            "owner": "alice",
            "namespace": "bench",
            "kubeconfig": "/tmp/kube",
        }

    def test_missing_file_returns_empty(self, tmp_path):
        assert _load_config_file(tmp_path / "nope.yaml") == {}

    def test_non_mapping_file_returns_empty(self, tmp_path):
        cfg = tmp_path / "bad.yaml"
        cfg.write_text("- just\n- a\n- list\n")
        assert _load_config_file(cfg) == {}
