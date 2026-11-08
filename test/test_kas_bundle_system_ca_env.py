"""Crew's own KAS bundle trusts the OS certificate store, as kiro-cli's KAS spawn does.

Node trusts only its built-in roots unless ``NODE_USE_SYSTEM_CA`` is set, so a
corporate CA that lives only in the OS store fails every TLS call on the bundle
route. kiro-cli sets the variable itself when it spawns KAS, so the relay is left alone.
"""

from __future__ import annotations

import pytest

from kiro_crew import kiro_cli
from kiro_crew.acp import kas_bundle
from kiro_crew.acp.harness.kas import KasHarness

_CA = "NODE_USE_SYSTEM_CA"


@pytest.fixture(autouse=True)
def quiet_host(tmp_path, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep credentials and engine probes independent of the host."""
    mp = _floor_monkeypatch
    mp.setattr("kiro_crew.config.loader.inject_kiro_cli_api_key", lambda _env: None)
    mp.setattr("kiro_crew.config.loader.strip_kiro_cli_api_key", lambda _env: None)
    mp.setattr(kiro_cli, "mandatory_mcps_drop_allowed", lambda _binary: False)
    mp.setattr(kas_bundle, "kas_engines_root", lambda home=None: tmp_path / "kas")
    mp.setattr(kas_bundle, "bundled_kas_root", lambda: None)


def _bundle_node(tmp_path) -> str:
    return str(tmp_path / "kas" / "0.66.33" / "node" / "bin" / "node")


def test_bundle_spawn_trusts_the_os_store(tmp_path) -> None:
    env: dict[str, str] = {}
    KasHarness().apply_spawn_env(env, spawned_binary=_bundle_node(tmp_path))
    assert env[_CA] == "1"


@pytest.mark.parametrize("operator", ["0", "1"])
def test_operator_value_wins_on_the_bundle(operator, tmp_path) -> None:
    env = {_CA: operator}
    KasHarness().apply_spawn_env(env, spawned_binary=_bundle_node(tmp_path))
    assert env[_CA] == operator


@pytest.mark.parametrize("spawned_binary", [None, "relay"])
def test_relay_spawn_is_left_to_kiro_cli(spawned_binary, tmp_path) -> None:
    binary = None if spawned_binary is None else str(tmp_path / "bin" / "kiro-cli")
    env: dict[str, str] = {}
    KasHarness().apply_spawn_env(env, spawned_binary=binary)
    assert _CA not in env
