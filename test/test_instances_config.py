"""Instances config flag and tunables (``instances.*`` in config.json)."""

from __future__ import annotations


class TestConfig:
    def test_defaults_off_and_tunables(self):
        from kiro_crew.config.loader import InstancesConfig
        from kiro_crew.instances.constants import (
            DEFAULT_CONNECT_TIMEOUT_SECS,
            DEFAULT_MINT_TIMEOUT_SECS,
            DEFAULT_TUNNEL_BASE_PORT,
            DEFAULT_WARM_SET_CAP,
        )

        c = InstancesConfig()
        assert c.enabled is False
        # 0 == automatic: the cap follows the REGISTERED crew count (resolved per
        # request by resolve_warm_set_cap), so no configured crew is ever evicted.
        assert c.warm_set_cap == DEFAULT_WARM_SET_CAP == 0
        assert c.tunnel_base_port == DEFAULT_TUNNEL_BASE_PORT == 7778
        assert DEFAULT_CONNECT_TIMEOUT_SECS == 15.0
        assert c.connect_timeout_secs is None
        assert DEFAULT_MINT_TIMEOUT_SECS == 30.0
        assert c.mint_timeout_secs is None

    def test_clamps_out_of_range(self):
        from kiro_crew.config.loader import InstancesConfig

        # 0 is a legal value (automatic), so only a negative cap is clamped, and
        # it falls back to automatic rather than to the tightest possible cap.
        c = InstancesConfig(warm_set_cap=-3, tunnel_base_port=99999)
        assert c.warm_set_cap == 0
        assert c.tunnel_base_port == 7778

    def test_zero_warm_set_cap_is_kept_as_automatic(self):
        from kiro_crew.config.loader import InstancesConfig

        assert InstancesConfig(warm_set_cap=0).warm_set_cap == 0

    def test_roundtrip_and_schema(self):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.schema import SCHEMA_REGISTRY

        d = KiroCrewConfig().to_dict()
        assert d["instances"] == {
            "enabled": False,
            "warm_set_cap": 0,
            "tunnel_base_port": 7778,
            "ssh_compression": True,
            "connect_timeout_secs": None,
            "mint_timeout_secs": None,
            "max_recovery_attempts": 8,
            "recover_backoff_max_secs": 30.0,
            "probe_failure_threshold": 3,
            "default_destination": "",
        }
        paths = {e.path for e in SCHEMA_REGISTRY}
        for p in (
            "instances",
            "instances.enabled",
            "instances.warm_set_cap",
            "instances.tunnel_base_port",
            "instances.ssh_compression",
            "instances.connect_timeout_secs",
            "instances.mint_timeout_secs",
            "instances.max_recovery_attempts",
            "instances.recover_backoff_max_secs",
            "instances.probe_failure_threshold",
            "instances.default_destination",
        ):
            assert p in paths
        timeout_entry = next(
            e for e in SCHEMA_REGISTRY if e.path == "instances.connect_timeout_secs"
        )
        assert timeout_entry.type == "number"
        assert timeout_entry.nullable is True
        assert timeout_entry.default_value is None

    def test_recovery_knobs_parse_from_config_file(self, tmp_path, monkeypatch):
        import json

        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "instances": {
                        "max_recovery_attempts": 12,
                        "recover_backoff_max_secs": 45.0,
                        "probe_failure_threshold": 5,
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        cfg = KiroCrewConfig.load()
        assert cfg.instances.max_recovery_attempts == 12
        assert cfg.instances.recover_backoff_max_secs == 45.0
        assert cfg.instances.probe_failure_threshold == 5

    def test_recovery_knob_clamps(self):
        from kiro_crew.config.loader import InstancesConfig

        c = InstancesConfig(
            max_recovery_attempts=0, recover_backoff_max_secs=0, probe_failure_threshold=0
        )
        assert c.max_recovery_attempts == 8
        assert c.recover_backoff_max_secs == 30.0
        assert c.probe_failure_threshold == 3

        # Upper bound: a pathological max_recovery_attempts is clamped down to the
        # ceiling (warned, not silently dropped) so it can't spin a near-infinite
        # self-heal loop. The boundary value itself is left untouched.
        from kiro_crew.instances.constants import MAX_RECOVERY_ATTEMPTS_CEILING

        assert MAX_RECOVERY_ATTEMPTS_CEILING == 100
        assert (
            InstancesConfig(max_recovery_attempts=10_000).max_recovery_attempts
            == MAX_RECOVERY_ATTEMPTS_CEILING
        )
        assert (
            InstancesConfig(
                max_recovery_attempts=MAX_RECOVERY_ATTEMPTS_CEILING
            ).max_recovery_attempts
            == MAX_RECOVERY_ATTEMPTS_CEILING
        )

        # recover_backoff_max_secs has the same two-sided guard: a pathological
        # pacing is clamped down to the ceiling so the attempt cap can't be stretched
        # into a multi-day wall-clock window; the boundary value is left untouched.
        from kiro_crew.instances.constants import RECOVER_BACKOFF_MAX_CEILING_SECS

        assert RECOVER_BACKOFF_MAX_CEILING_SECS == 300.0
        assert (
            InstancesConfig(recover_backoff_max_secs=86_400.0).recover_backoff_max_secs
            == RECOVER_BACKOFF_MAX_CEILING_SECS
        )
        assert (
            InstancesConfig(
                recover_backoff_max_secs=RECOVER_BACKOFF_MAX_CEILING_SECS
            ).recover_backoff_max_secs
            == RECOVER_BACKOFF_MAX_CEILING_SECS
        )

    def test_connect_timeout_default_and_clamps(self):
        from kiro_crew.config.loader import InstancesConfig
        from kiro_crew.instances.constants import (
            CONNECT_TIMEOUT_CEILING_SECS,
            DEFAULT_CONNECT_TIMEOUT_SECS,
        )

        # Unset remains distinguishable from an explicit value equal to the SSH
        # default, so the manager can select the transport-specific default.
        c = InstancesConfig()
        assert DEFAULT_CONNECT_TIMEOUT_SECS == 15.0
        assert c.connect_timeout_secs is None

        c = InstancesConfig(connect_timeout_secs=DEFAULT_CONNECT_TIMEOUT_SECS)
        assert c.connect_timeout_secs == DEFAULT_CONNECT_TIMEOUT_SECS

        # Explicit override is honored.
        c = InstancesConfig(connect_timeout_secs=45.0)
        assert c.connect_timeout_secs == 45.0

        # Below 1 falls back to the transport-specific defaults.
        c = InstancesConfig(connect_timeout_secs=0.5)
        assert c.connect_timeout_secs is None

        c = InstancesConfig(connect_timeout_secs=-10.0)
        assert c.connect_timeout_secs is None

        # Above the ceiling is clamped.
        assert CONNECT_TIMEOUT_CEILING_SECS == 120.0
        c = InstancesConfig(connect_timeout_secs=999.0)
        assert c.connect_timeout_secs == CONNECT_TIMEOUT_CEILING_SECS

        # Boundary value itself is left untouched.
        c = InstancesConfig(connect_timeout_secs=CONNECT_TIMEOUT_CEILING_SECS)
        assert c.connect_timeout_secs == CONNECT_TIMEOUT_CEILING_SECS

    def test_connect_timeout_parses_from_config_file(self, tmp_path, monkeypatch):
        import json

        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"instances": {"connect_timeout_secs": 45.0}}))
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        cfg = KiroCrewConfig.load()
        assert cfg.instances.connect_timeout_secs == 45.0

        cfg_file.write_text(json.dumps({"instances": {}}))
        cfg = KiroCrewConfig.load()
        assert cfg.instances.connect_timeout_secs is None

        cfg_file.write_text(json.dumps({"instances": {"connect_timeout_secs": None}}))
        cfg = KiroCrewConfig.load()
        assert cfg.instances.connect_timeout_secs is None

        cfg_file.write_text(json.dumps({"instances": {"connect_timeout_secs": 15.0}}))
        cfg = KiroCrewConfig.load()
        assert cfg.instances.connect_timeout_secs == 15.0

    def test_mint_timeout_default_and_clamps(self):
        from kiro_crew.config.loader import InstancesConfig
        from kiro_crew.instances.constants import (
            DEFAULT_MINT_TIMEOUT_SECS,
            MINT_TIMEOUT_CEILING_SECS,
            MINT_TIMEOUT_FLOOR_SECS,
        )

        # Unset by default; the per-transport defaults live in constants.
        c = InstancesConfig()
        assert c.mint_timeout_secs is None
        assert DEFAULT_MINT_TIMEOUT_SECS == 30.0

        # Explicit override is honored — including the SSH-default value.
        c = InstancesConfig(mint_timeout_secs=60.0)
        assert c.mint_timeout_secs == 60.0
        c = InstancesConfig(mint_timeout_secs=DEFAULT_MINT_TIMEOUT_SECS)
        assert c.mint_timeout_secs == DEFAULT_MINT_TIMEOUT_SECS

        # Below the floor falls back to unset (transport defaults).
        assert MINT_TIMEOUT_FLOOR_SECS == 10.0
        c = InstancesConfig(mint_timeout_secs=5.0)
        assert c.mint_timeout_secs is None

        c = InstancesConfig(mint_timeout_secs=-30.0)
        assert c.mint_timeout_secs is None

        # The floor value itself is left untouched.
        c = InstancesConfig(mint_timeout_secs=MINT_TIMEOUT_FLOOR_SECS)
        assert c.mint_timeout_secs == MINT_TIMEOUT_FLOOR_SECS

        # Above the ceiling is clamped.
        assert MINT_TIMEOUT_CEILING_SECS == 120.0
        c = InstancesConfig(mint_timeout_secs=999.0)
        assert c.mint_timeout_secs == MINT_TIMEOUT_CEILING_SECS

        # Boundary value itself is left untouched.
        c = InstancesConfig(mint_timeout_secs=MINT_TIMEOUT_CEILING_SECS)
        assert c.mint_timeout_secs == MINT_TIMEOUT_CEILING_SECS

    def test_mint_timeout_parses_from_config_file(self, tmp_path, monkeypatch):
        import json

        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"instances": {"mint_timeout_secs": 60.0}}))
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        cfg = KiroCrewConfig.load()
        assert cfg.instances.mint_timeout_secs == 60.0

        cfg_file.write_text(json.dumps({"instances": {}}))
        cfg = KiroCrewConfig.load()
        assert cfg.instances.mint_timeout_secs is None

        cfg_file.write_text(json.dumps({"instances": {"mint_timeout_secs": None}}))
        cfg = KiroCrewConfig.load()
        assert cfg.instances.mint_timeout_secs is None

        cfg_file.write_text(json.dumps({"instances": {"mint_timeout_secs": 30.0}}))
        cfg = KiroCrewConfig.load()
        assert cfg.instances.mint_timeout_secs == 30.0
