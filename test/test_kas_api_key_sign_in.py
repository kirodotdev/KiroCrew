"""``agent.kas_api_key_sign_in``: Kiro Crew's own engine bundle signs in with ``KIRO_API_KEY``.

Off by default. On, only an empty vault plus a set key move the bundle plan to
cli-owned, so the launch hands the engine the key, Crew answers no credential
callback for that process, and the identity sweep counts the key.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from kiro_crew.acp import kas_bundle
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.auth import bridge
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.sections_agent import AgentConfig
from kiro_crew.kiro_prerequisite_identity import _combine_identity_fingerprints
from kiro_crew.kiro_prerequisite_spawn_identity import spawned_under

_KEY = "kas-api-key-under-test"


@pytest.fixture
def inputs(monkeypatch, tmp_path):
    """The predicate's three inputs: the opt-in, the vault, and the data home's ``.env``."""
    home = tmp_path / "crew"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    state = {"on": True, "vault": False}

    def load(*_args, **_kwargs):
        return SimpleNamespace(agent=SimpleNamespace(kas_api_key_sign_in=state["on"]))

    monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", load)
    monkeypatch.setattr(bridge, "vault_holds_identity", lambda **_kw: state["vault"])
    state["env_file"] = home / ".env"
    return state


def test_off_by_default_and_a_stored_true_is_loaded(monkeypatch, tmp_path):
    assert AgentConfig().kas_api_key_sign_in is False
    home = tmp_path / "crew"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"agent": {"kas_api_key_sign_in": True}}), encoding="utf-8"
    )
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    assert loader_mod.KiroCrewConfig.load().agent.kas_api_key_sign_in is True


def test_on_with_an_empty_vault_and_an_inherited_key(inputs):
    assert bridge.api_key_sign_in_applies({"KIRO_API_KEY": _KEY}) is True


def test_on_with_the_key_only_in_the_data_home_env_file(inputs):
    inputs["env_file"].write_text(f"KIRO_API_KEY={_KEY}\n", encoding="utf-8")
    assert bridge.api_key_sign_in_applies({}) is True


def test_off_an_ambient_key_has_no_effect(inputs):
    inputs["on"] = False
    assert bridge.api_key_sign_in_applies({"KIRO_API_KEY": _KEY}) is False


def test_a_stored_sign_in_wins_over_the_key(inputs):
    inputs["vault"] = True
    assert bridge.api_key_sign_in_applies({"KIRO_API_KEY": _KEY}) is False


def test_machine_mode_wins_over_the_key(inputs, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.agent_sdk.kas_machine.configured_machine_auth", lambda: SimpleNamespace()
    )
    assert bridge.api_key_sign_in_applies({"KIRO_API_KEY": _KEY}) is False


def test_on_with_no_key_anywhere_keeps_the_vault(inputs):
    assert bridge.api_key_sign_in_applies({}) is False


def _bundle(tmp_path):
    return SimpleNamespace(
        version="0.66.104", node_version="22", node_bin=tmp_path / "node", acp_server=tmp_path / "s"
    )


async def _bundle_plan(monkeypatch, tmp_path, applies):
    async def answer(environ):
        assert environ == {"KIRO_API_KEY": _KEY}
        return applies

    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: True)
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: _bundle(tmp_path))
    monkeypatch.setattr(kas_harness, "api_key_sign_in_applies_off_loop", answer)
    ctx = SpawnContext(
        agent="a",
        work_dir=str(tmp_path),
        model=None,
        environ={"KIRO_API_KEY": _KEY},
        home=tmp_path,
    )
    return await kas_harness.KasHarness().resolve_spawn(ctx)


@pytest.mark.asyncio
async def test_the_bundle_plan_is_cli_owned_when_the_key_signs_in(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        plan = await _bundle_plan(monkeypatch, tmp_path, applies=True)
    assert plan.engine_bundle is True
    assert plan.host_auth is False
    assert "KAS auth owner=KIRO_API_KEY" in caplog.text
    assert _KEY not in caplog.text


@pytest.mark.asyncio
async def test_the_bundle_plan_stays_crew_owned_otherwise(monkeypatch, tmp_path):
    plan = await _bundle_plan(monkeypatch, tmp_path, applies=False)
    assert plan.engine_bundle is True
    assert plan.host_auth is True


@pytest.mark.parametrize("host_auth, spared", [(True, True), (False, False)])
def test_a_key_rotation_retires_only_a_key_signed_in_bundle_process(host_auth, spared):
    """Pins the seam the plan relies on: a ``host_auth=False`` runtime's key component counts."""
    stamp = _combine_identity_fingerprints("store-digest", "", api_key="old-key-digest")
    live = _combine_identity_fingerprints("store-digest", "", api_key="new-key-digest")
    runtime = SimpleNamespace(
        acp_backend=ACP_BACKEND_KAS, _kas_host_auth=host_auth, spawn_identity=stamp
    )
    assert spawned_under(SimpleNamespace(_runtime=runtime), live) is spared
