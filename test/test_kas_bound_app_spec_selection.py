"""A KAS session bound to an app's agent serves the app's registered file everywhere.

The projection reads the bound ``<app>--<agent>.json`` by its path
(``apps.agent_launch.read_bound_app_spec``), so the spec hooks and the broker
overlay (stubs injected and subtracted) must read that file too, even when a
newer, differing spec at the user level claims the same name. An unbound
session of the same agent still takes the newest claimant.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp import runtime_session_setup
from kiro_crew.acp.harness import kas
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.apps import agent_launch, bridges
from kiro_crew.config import paths
from kiro_crew.dashboard import chat_runner
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE
from kiro_crew.mcp_gateway import session_servers
from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER

BOUND_KEY = "dashboard:app-slot"
#: The registered file is OLDER, so a newest pick alone would take the user's file.
_CLAIMANTS = (("app--deploy", "app", 1_000_000_000), ("user-deploy", "user", 2_000_000_000))


def _write(path: Path, value: dict, stamp: int) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    os.utime(path, ns=(stamp, stamp))


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


@pytest.fixture
def bound(tmp_path, monkeypatch):
    """Two differing ``deploy`` specs, their overlays, and a session bound to the app's."""
    agents = tmp_path / "agents"
    overlay = tmp_path / "overlays"
    agents.mkdir()
    overlay.mkdir()
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr(agent_launch, "_KAS_BINDINGS", {})
    for stem, label, stamp in _CLAIMANTS:
        spec = {
            "name": "deploy",
            "prompt": label,
            "hooks": {"preToolUse": [{"matcher": "execute_bash", "command": f"{label}.sh"}]},
            "mcpServers": {f"{label}-srv": {"command": label}},
        }
        _write(agents / f"{stem}.json", spec, stamp)
        rewritten = {
            **spec,
            "mcpServers": {
                name: {**entry, _WRAPPER_MARKER: True, "args": [], "env": {}}
                for name, entry in spec["mcpServers"].items()
            },
        }
        # Overlay timestamps rank opposite to the sources'; they never decide.
        _write(overlay / f"{stem}.json", rewritten, 3_000_000_000 - stamp)
    agent_launch.bind_session_app_agent(BOUND_KEY, "app", "deploy")
    return agents, overlay


def _pre_tool_commands(hooks) -> list[str]:
    return [h.command for h in hooks if h.event == HOOK_EVENT_PRE_TOOL_USE]


def _chat_turn_hooks(session_key: str):
    client = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd="")
    slot = SimpleNamespace(_active_turn_session_key=session_key)
    return asyncio.run(
        chat_runner._prepare_spec_hooks(SimpleNamespace(), slot, client, "deploy", is_new=False)
    )


def test_a_bound_chat_turn_fires_the_registered_files_hooks(bound):
    hooks, unreadable, _ = _chat_turn_hooks(BOUND_KEY)
    assert unreadable is False
    assert _pre_tool_commands(hooks) == ["app.sh"]


def test_a_bound_subagent_turn_fires_the_registered_files_hooks(bound):
    provider = SimpleNamespace(
        capabilities=capabilities_for(ACP_BACKEND_KAS), cwd="", _session_key=BOUND_KEY
    )
    out = asyncio.run(spec_hooks.turn_spec_hooks(provider, "deploy"))
    assert out.unreadable is False
    assert _pre_tool_commands(out.hooks) == ["app.sh"]


def test_an_unbound_session_of_the_agent_fires_the_newest(bound):
    hooks, unreadable, _ = _chat_turn_hooks("dashboard:other")
    assert unreadable is False
    assert _pre_tool_commands(hooks) == ["user.sh"]


def test_a_bound_session_whose_registered_file_is_gone_fails_closed(bound):
    agents, _overlay = bound
    (agents / "app--deploy.json").unlink()
    assert _chat_turn_hooks(BOUND_KEY) == ([], True, None)


@pytest.fixture
def projection(bound, monkeypatch):
    monkeypatch.setattr(runtime_session_setup, "_ref_spec_snapshot", lambda *_a: None)
    monkeypatch.setattr(session_servers, "_registry_ceiling_exemptions", lambda: None)
    monkeypatch.setattr(kas.agent_mod, "ensure_agent_materialized", lambda *_a: None)
    monkeypatch.setattr(kas.agent_mod, "require_fork_governance", lambda *_a: None)
    monkeypatch.setattr(kas.agent_mod, "require_fresh_derived_spec", lambda *_a: None)
    monkeypatch.setattr(kas, "with_default_crew_servers", lambda _a, spec: spec)
    monkeypatch.setattr(kas, "_skill_scope_rules", lambda *_a: ())
    monkeypatch.setattr("kiro_crew.template_mcp_additions.merge_at_spawn", lambda _a, spec: spec)
    monkeypatch.setattr("kiro_crew.agent_sdk.kas_model_catalog.vault_identity", lambda: None)
    captured: list = []

    def build(_dir, _agent, spec, *, stub_server_names, **_kwargs):
        captured.append((spec["prompt"], stub_server_names))
        return []

    monkeypatch.setattr(kas.kas_agents_mod, "build_kas_custom_agents", build)
    return bound, captured


@pytest.mark.parametrize(
    ("session_key", "label"), [(BOUND_KEY, "app"), ("dashboard:other", "user")]
)
@pytest.mark.asyncio
async def test_pooled_stubs_and_subtraction_follow_the_projected_file(
    projection, session_key, label
):
    (agents, overlay), captured = projection
    pooled, _ = runtime_session_setup._pooled_session_servers_and_ref_spec(
        overlay, "deploy", "kas", agents.parent, session_key=session_key
    )
    assert {entry["name"] for entry in pooled} == {f"{label}-srv"}
    await kas.KasHarness().session_extras(
        "deploy", work_dir=None, mcp_gateway_overlay=str(overlay), session_key=session_key
    )
    assert captured == [(label, frozenset({f"{label}-srv"}))]


def test_a_bound_session_with_no_registered_overlay_pools_nothing(projection):
    (agents, overlay), _captured = projection
    (overlay / "app--deploy.json").unlink()
    pooled, _ = runtime_session_setup._pooled_session_servers_and_ref_spec(
        overlay, "deploy", "kas", agents.parent, session_key=BOUND_KEY
    )
    assert pooled == []
