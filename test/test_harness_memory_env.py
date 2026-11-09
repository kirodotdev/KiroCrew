"""``agent.harness_memory`` reaches the KAS engine as its memory override variable.

On by default, on both routes; an operator's own value wins; an unreadable config
leaves the engine's default alone.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import kiro_cli
from kiro_crew.acp import harness_memory_env as mod
from kiro_crew.acp import kas_bundle
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.sections_agent import AgentConfig

_VAR = mod.KAS_MEMORY_ENV


@pytest.fixture(autouse=True)
def quiet_host(tmp_path, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep credentials and engine probes independent of the host."""
    mp = _floor_monkeypatch
    mp.setattr("kiro_crew.config.loader.inject_kiro_cli_api_key", lambda _env: None)
    mp.setattr("kiro_crew.config.loader.strip_kiro_cli_api_key", lambda _env: None)
    mp.setattr(kiro_cli, "mandatory_mcps_drop_allowed", lambda _binary: False)
    mp.setattr(kas_bundle, "kas_engines_root", lambda home=None: tmp_path / "kas")
    mp.setattr(kas_bundle, "bundled_kas_root", lambda: None)


def _config(mp: pytest.MonkeyPatch, value: object) -> None:
    mp.setattr(
        KiroCrewConfig,
        "load",
        classmethod(lambda cls: SimpleNamespace(agent=SimpleNamespace(harness_memory=value))),
    )


def _bundle_node(tmp_path) -> str:
    return str(tmp_path / "kas" / "0.66.33" / "node" / "bin" / "node")


def test_setting_defaults_on() -> None:
    assert AgentConfig().harness_memory is True


@pytest.mark.parametrize("relay", [False, True])
@pytest.mark.parametrize(("value", "expected"), [(True, "true"), (False, "false")])
def test_spawn_env_follows_setting(relay, value, expected, tmp_path, monkeypatch) -> None:
    _config(monkeypatch, value)
    binary = str(tmp_path / "bin" / "kiro-cli") if relay else _bundle_node(tmp_path)
    env: dict[str, str] = {}
    KasHarness().apply_spawn_env(env, spawned_binary=binary)
    assert env[_VAR] == expected


def test_non_bool_reads_as_off(monkeypatch) -> None:
    _config(monkeypatch, "true")
    env: dict[str, str] = {}
    mod.apply_harness_memory_env(env)
    assert env[_VAR] == "false"


@pytest.mark.parametrize("operator", ["false", "true", ""])
def test_operator_value_wins(operator, monkeypatch) -> None:
    _config(monkeypatch, True)
    env = {_VAR: operator}
    mod.apply_harness_memory_env(env)
    assert env[_VAR] == operator


def test_unreadable_config_leaves_engine_default(monkeypatch) -> None:
    def _boom(cls):
        raise OSError("unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    env: dict[str, str] = {}
    mod.apply_harness_memory_env(env)
    assert _VAR not in env


def test_loader_reads_stored_false() -> None:
    from kiro_crew.config.loader_builders import _agent_section_fields

    assert _agent_section_fields({"harness_memory": False}).harness_memory is False
    assert _agent_section_fields({}).harness_memory is True


def test_agent_env_scrub_keeps_the_variable() -> None:
    """The scrub runs after ``apply_spawn_env``; stripping the name would undo the setting."""
    from kiro_crew.sandbox_spawn import scrub_agent_subprocess_env

    scrubbed = scrub_agent_subprocess_env({_VAR: "true"}, forward_ssh_auth_sock=False)
    assert scrubbed.get(_VAR) == "true"
