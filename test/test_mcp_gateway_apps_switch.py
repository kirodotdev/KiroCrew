"""Tests for the MCP Apps render switch (``mcp_gateway.apps_enabled``).

The switch stands alone: the broker starts whenever EITHER pooling or MCP Apps
is on, and the stub that carries the app-call relay is emitted for every server
regardless of poolability. So ``apps_enabled`` is the whole decision, and these
tests pin that plus the env-override precedence rules around it.
"""

import json

import pytest

from kiro_crew.mcp_gateway.backend import MCP_APPS_ENV_FLAG, _mcp_apps_enabled


def _pin_config(monkeypatch, *, enabled: bool, apps_enabled: bool) -> None:
    import kiro_crew.config.loader as loader

    real = loader.KiroCrewConfig.load()
    monkeypatch.setattr(real.mcp_gateway, "enabled", enabled, raising=False)
    monkeypatch.setattr(real.mcp_gateway, "apps_enabled", apps_enabled, raising=False)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: real))


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    """Clear the env flag so the config path is what is under test."""
    monkeypatch.delenv(MCP_APPS_ENV_FLAG, raising=False)


@pytest.mark.parametrize(
    "enabled,apps_enabled,expected",
    [
        (True, True, True),
        (True, False, False),
        # Pooling off, apps on: the stub layer is still emitted (each connection
        # simply gets its own backend), so the render path exists.
        (False, True, True),
        (False, False, False),
    ],
)
def test_gate_follows_apps_enabled_alone(monkeypatch, enabled, apps_enabled, expected):
    _pin_config(monkeypatch, enabled=enabled, apps_enabled=apps_enabled)
    assert _mcp_apps_enabled() is expected


def test_config_opt_out_beats_env_on(monkeypatch):
    """The load-bearing precedence rule, and the reason it exists.

    A daemon that outlives its gateway keeps its own environment, so if the env
    flag outranked config an ADOPTED daemon would carry on rendering after the
    user switched the feature off in the dashboard — and the dashboard cannot
    restart a daemon it did not spawn. Deciding it here, in the process that
    renders, closes that without probing anyone's environment.
    """
    _pin_config(monkeypatch, enabled=True, apps_enabled=False)
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
    assert _mcp_apps_enabled() is False


def test_env_on_still_forces_the_feature_when_config_does_not_opt_out(monkeypatch):
    """The e2e harness path: env on wins over a broker that is off."""
    _pin_config(monkeypatch, enabled=False, apps_enabled=True)
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
    assert _mcp_apps_enabled() is True


def test_apps_enabled_alone_is_enough(monkeypatch):
    """The (pooling off, apps on) cell — the whole point of the decoupling.

    Pooling off no longer means no broker and no stub: the broker starts for
    MCP Apps too, and every server still gets a stub, so a tool result can be
    intercepted and rendered. Only the backend behind the stub changes — one per
    connection instead of shared.
    """
    _pin_config(monkeypatch, enabled=False, apps_enabled=True)
    assert _mcp_apps_enabled() is True


def test_env_kill_switch_still_wins_over_config(monkeypatch):
    _pin_config(monkeypatch, enabled=True, apps_enabled=True)
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "0")
    assert _mcp_apps_enabled() is False


def test_env_on_does_not_resurrect_an_explicit_opt_out(monkeypatch):
    """Inverted deliberately: an explicit off is no longer overridable by env on.

    An earlier form of this gate let ``KIROCREW_MCP_APPS=1`` win over config in
    every case, which is what allowed an adopted daemon to keep rendering after
    the dashboard opt-out. ``env=on`` still forces the feature when config has
    not opted out — covered by
    ``test_env_on_still_forces_the_feature_when_config_does_not_opt_out``.
    """
    _pin_config(monkeypatch, enabled=False, apps_enabled=False)
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
    assert _mcp_apps_enabled() is False


class TestConfigParsing:
    @staticmethod
    def _load_from(tmp_path, monkeypatch, payload):
        """Load a real config from ``tmp_path`` through the production path."""
        from kiro_crew.config import loader

        tmp_path.mkdir(parents=True, exist_ok=True)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
        # Isolate from host config.local.json which could deep-merge unexpected
        # values on macOS developer hosts.
        monkeypatch.setattr(loader, "config_local_path", lambda: tmp_path / "config.local.json")
        loader._invalidate_config_cache()
        return loader.KiroCrewConfig.load()

    def test_defaults_to_true_when_absent(self):
        from kiro_crew.config.loader import McpGatewayConfig

        assert McpGatewayConfig().apps_enabled is True

    def test_defaults_to_true_when_section_present_but_key_absent(self, tmp_path, monkeypatch):
        loaded = self._load_from(tmp_path, monkeypatch, {"mcp_gateway": {"enabled": True}})
        assert loaded.mcp_gateway.apps_enabled is True

    def test_malformed_value_is_stripped_by_the_validator(self, tmp_path, monkeypatch, caplog):
        """A non-boolean is REMOVED upstream, so it arrives as absent.

        Pins the mechanism rather than a wished-for outcome: the schema validator
        drops an invalid value (config/validation.py ``_apply_field_default``), so
        the loader cannot tell malformed from absent and resolves to the absent
        default. The user's signal is the logged warning naming the field.
        """
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            loaded = self._load_from(
                tmp_path, monkeypatch, {"mcp_gateway": {"apps_enabled": "false"}}
            )
        assert loaded.mcp_gateway.apps_enabled is True
        assert "mcp_gateway.apps_enabled" in caplog.text

    def test_truthy_string_does_not_sneak_through_as_enabled(self, tmp_path, monkeypatch):
        """``bool("false")`` is True, so a raw cast would be wrong.

        The validator is what prevents that, which is why this asserts on the
        loaded value and not on ``_safe_bool`` in isolation.
        """
        loaded = self._load_from(
            tmp_path, monkeypatch, {"mcp_gateway": {"apps_enabled": "false", "enabled": True}}
        )
        assert isinstance(loaded.mcp_gateway.apps_enabled, bool)

    def test_explicit_false_is_honoured(self, tmp_path, monkeypatch):
        loaded = self._load_from(tmp_path, monkeypatch, {"mcp_gateway": {"apps_enabled": False}})
        assert loaded.mcp_gateway.apps_enabled is False
