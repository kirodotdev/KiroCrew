"""Crew turns off the KAS engine's own session-title request on both routes.

At a session's first prompt the engine spends one extra model request titling the
session for kiro-cli's session list; Crew never reads that title. The engine skips
it when ``KIRO_DISABLE_SESSION_TITLE_LLM`` is ``true``, and a value the engine
already inherits (``false`` included) is kept.
"""

from __future__ import annotations

import pytest

from kiro_crew import kiro_cli
from kiro_crew.acp import kas_bundle
from kiro_crew.acp.harness.kas import KasHarness

_TITLE = "KIRO_DISABLE_SESSION_TITLE_LLM"


@pytest.fixture(autouse=True)
def quiet_host(tmp_path, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep credentials and engine probes independent of the host."""
    mp = _floor_monkeypatch
    mp.setattr("kiro_crew.config.loader.inject_kiro_cli_api_key", lambda _env: None)
    mp.setattr("kiro_crew.config.loader.strip_kiro_cli_api_key", lambda _env: None)
    mp.setattr(kiro_cli, "mandatory_mcps_drop_allowed", lambda _binary: False)
    mp.setattr(kas_bundle, "kas_engines_root", lambda home=None: tmp_path / "kas")
    mp.setattr(kas_bundle, "bundled_kas_root", lambda: None)


def _binary(route: str, tmp_path) -> str | None:
    if route == "bundle":
        return str(tmp_path / "kas" / "0.66.33" / "node" / "bin" / "node")
    if route == "relay-kiro-cli":
        return str(tmp_path / "bin" / "kiro-cli")
    return None


_ROUTES = ["bundle", "relay-default", "relay-kiro-cli"]


@pytest.mark.parametrize("route", _ROUTES)
def test_every_route_turns_the_title_request_off(route, tmp_path) -> None:
    env: dict[str, str] = {}
    KasHarness().apply_spawn_env(env, spawned_binary=_binary(route, tmp_path))
    assert env.get(_TITLE) == "true"


@pytest.mark.parametrize("route", _ROUTES)
def test_an_inherited_value_wins(route, tmp_path) -> None:
    env = {_TITLE: "false"}
    KasHarness().apply_spawn_env(env, spawned_binary=_binary(route, tmp_path))
    assert env.get(_TITLE) == "false"
