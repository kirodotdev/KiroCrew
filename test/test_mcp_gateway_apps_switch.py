"""The MCP Apps gate after the stub became the per-server opt-in.

There is no ``mcp_gateway.apps_enabled`` switch any more. Capability follows
THE STUB: the gate only ever runs inside a backend, and a backend exists only
because a stub reached the broker for a server the operator stubbed — so the
opt-in already happened upstream, and a preference could neither grant the
feature (with no stub there is no render or callback path) nor honestly withdraw
it.

What remains is ``KIROCREW_MCP_APPS`` as an absolute kill switch. These tests pin
that, and pin that a released config still carrying ``apps_enabled`` changes
nothing in either direction.
"""

import pytest

from kiro_crew.mcp_gateway.backend import MCP_APPS_ENV_FLAG, _mcp_apps_enabled


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    """Clear the env flag so each test states its own precondition."""
    monkeypatch.delenv(MCP_APPS_ENV_FLAG, raising=False)


def test_enabled_by_default_because_the_stub_already_proves_intent() -> None:
    """Reaching this gate means a stub reached the broker, which means the
    operator stubbed the server. There is nothing further to ask."""
    assert _mcp_apps_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", "Off"])
def test_env_kill_switch_disables(monkeypatch, value: str) -> None:
    """The one way to run a stubbed server's backend without its UI."""
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, value)
    assert _mcp_apps_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_env_on_is_accepted(monkeypatch, value: str) -> None:
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, value)
    assert _mcp_apps_enabled() is True


def test_unparseable_env_value_leaves_the_feature_on(monkeypatch) -> None:
    """Only a value that reads as OFF disables. Garbage is not an opt-out: it
    would silently withdraw UI from a server the operator did route."""
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "banana")
    assert _mcp_apps_enabled() is True


@pytest.mark.parametrize("apps_enabled", [True, False])
def test_stored_config_key_is_honoured(monkeypatch, apps_enabled: bool) -> None:
    """``apps_enabled`` is retired as a forward control but still honoured when
    STORED. A released version treated ``false`` as a trustworthy opt-out, so a
    config already carrying it keeps its opt-out; absent defaults True, so only a
    value an operator actually wrote can withhold the feature.
    """
    import kiro_crew.config.loader as loader

    real = loader.KiroCrewConfig.load()
    monkeypatch.setattr(real.mcp_gateway, "apps_enabled", apps_enabled, raising=False)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: real))

    assert _mcp_apps_enabled() is apps_enabled


def test_stored_opt_out_beats_env_on(monkeypatch) -> None:
    """Tightest-wins: a stored ``false`` disables EVEN with the env flag on.

    This ordering is what made the opt-out trustworthy across process boundaries
    in the released version — a daemon that outlives its gateway keeps its own
    environment, so env-first precedence let an adopted daemon carry on rendering
    after the operator had switched the feature off.
    """
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
    import kiro_crew.config.loader as loader

    real = loader.KiroCrewConfig.load()
    monkeypatch.setattr(real.mcp_gateway, "apps_enabled", False, raising=False)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: real))

    assert _mcp_apps_enabled() is False


def test_unreadable_config_fails_closed(monkeypatch) -> None:
    """An unreadable config disables the feature rather than rendering on a
    guess: gatewayd cannot confirm the stored preference, and withholding an
    optional rendering feature is the low-harm outcome."""
    import kiro_crew.config.loader as loader

    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(_boom))

    assert _mcp_apps_enabled() is False
