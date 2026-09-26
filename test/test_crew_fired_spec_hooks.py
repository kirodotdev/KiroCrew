"""An agent spec's own ``hooks`` run from Crew's turn loop on KAS, and only there.

KAS takes the agent over a wire schema with no slot for ``hooks``, so the turn loop
fires them through the hook store; kiro-cli runs the field itself, so a kiro-cli
session must not get them from Crew too. ``toolsSettings`` and ``slashCommand``
still reach no KAS session, and the user is told once per session.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.config.paths as paths_mod
import kiro_crew.hooks as hooks_mod
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner
from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    ScriptHook,
    ScriptHookResult,
    ScriptHookStore,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


def _client(backend: str, cwd: str = "") -> SimpleNamespace:
    return SimpleNamespace(capabilities=capabilities_for(backend), cwd=cwd)


def _write_spec(agents_dir: Path, name: str, **fields) -> None:
    spec = {"name": name, "prompt": "p", **fields}
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


@pytest.fixture
def agents_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: d)
    return d


def _prepare(client, agent, *, is_new=False):
    return asyncio.run(
        chat_runner._prepare_spec_hooks(
            SimpleNamespace(), SimpleNamespace(), client, agent, is_new=is_new
        )
    )


@pytest.fixture
def notices(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(
        chat_runner, "append_and_surface", lambda state, slot, role, text, cls: seen.append(text)
    )
    return seen


_OBJECT_HOOKS = {"preToolUse": [{"matcher": "shell", "command": "guard.sh"}]}


def test_membership_is_kas_only():
    assert capabilities_for(ACP_BACKEND_KAS).crew_fires_spec_hooks is True
    assert capabilities_for(ACP_BACKEND_KIRO).crew_fires_spec_hooks is False


def test_a_kiro_cli_session_gets_no_spec_hooks_from_crew(agents_dir, notices):
    _write_spec(agents_dir, "a1", hooks=_OBJECT_HOOKS, toolsSettings={"x": 1})
    assert _prepare(_client(ACP_BACKEND_KIRO), "a1", is_new=True) == ([], False, None)
    assert notices == []


def test_a_kas_session_gets_the_spec_hooks(agents_dir, notices):
    _write_spec(agents_dir, "a1", hooks=_OBJECT_HOOKS)
    hooks, unreadable, _ = _prepare(_client(ACP_BACKEND_KAS), "a1")
    assert unreadable is False
    assert [(h.event, h.matcher, h.command) for h in hooks] == [
        (HOOK_EVENT_PRE_TOOL_USE, "shell", "guard.sh")
    ]


def test_a_kas_session_runs_spec_hooks_in_its_workspace(agents_dir, notices):
    _write_spec(agents_dir, "a1", hooks=_OBJECT_HOOKS)
    assert _prepare(_client(ACP_BACKEND_KAS, "/w"), "a1")[2] == "/w"


def test_an_unreadable_spec_fails_closed(agents_dir, notices):
    (agents_dir / "a1.json").write_text("{not json", encoding="utf-8")
    assert _prepare(_client(ACP_BACKEND_KAS), "a1") == ([], True, None)


def test_tools_settings_notice_fires_once_per_session(agents_dir, notices):
    _write_spec(agents_dir, "a1", hooks=_OBJECT_HOOKS, toolsSettings={"shell": {}})
    _prepare(_client(ACP_BACKEND_KAS), "a1", is_new=True)
    _prepare(_client(ACP_BACKEND_KAS), "a1", is_new=False)
    assert len(notices) == 1
    assert "toolsSettings" in notices[0]
    assert "hooks" not in notices[0]


def test_disabled_and_confirm_documents_do_not_run():
    docs = [
        {"name": "on", "trigger": "PreToolUse", "action": {"type": "command", "command": "a"}},
        {
            "name": "off",
            "trigger": "PreToolUse",
            "enabled": False,
            "action": {"type": "command", "command": "b"},
        },
        {
            "name": "ask",
            "trigger": "PreToolUse",
            "confirm": True,
            "action": {"type": "command", "command": "c"},
        },
    ]
    hooks = spec_hooks.spec_script_hooks("a1", {"hooks": docs})
    assert [h.command for h in hooks] == ["a"]


def test_every_skipped_spec_hook_is_audited_once(monkeypatch):
    import kiro_crew.agent as agent_mod

    audited: list = []
    monkeypatch.setattr(
        agent_mod, "_sel_hook_rejected", lambda event, value, reason: audited.append(reason)
    )
    docs = [
        {
            "name": "off",
            "trigger": "PreToolUse",
            "enabled": False,
            "action": {"type": "command", "command": "b"},
        },
        {
            "name": "ask",
            "trigger": "PreToolUse",
            "confirm": True,
            "action": {"type": "command", "command": "c"},
        },
        {"name": "agent", "trigger": "PreToolUse", "action": {"type": "agent", "prompt": "p"}},
    ]
    spec = {"hooks": docs}
    assert spec_hooks.spec_script_hooks("a1", spec) == []
    assert spec_hooks.spec_script_hooks("a1", spec) == []
    assert len(audited) == 3
    assert any("confirm" in r for r in audited)


def test_object_form_maps_events_timeout_and_drops_matcher_off_tool_events():
    hooks = spec_hooks.spec_script_hooks(
        "a1",
        {"hooks": {"stop": [{"matcher": "x", "command": "s", "timeout_ms": 2500}]}},
    )
    assert [(h.event, h.matcher, h.timeout) for h in hooks] == [(HOOK_EVENT_STOP, "", 3)]


def test_an_oversized_or_unsafe_matcher_drops_the_hook_and_retains_nothing():
    long = "a" * 10_000
    hooks = spec_hooks.spec_script_hooks(
        "a1",
        {
            "hooks": {
                "preToolUse": [
                    {"matcher": long, "command": "x"},
                    {"matcher": "sh;rm", "command": "y"},
                    {"matcher": "sh*", "command": "z"},
                ]
            }
        },
    )
    assert [(h.command, h.matcher) for h in hooks] == [("z", "sh*")]


def test_conversion_stops_at_the_hook_cap():
    entries = [{"command": f"c{i}"} for i in range(500)]
    hooks = spec_hooks.spec_script_hooks("a1", {"hooks": {"stop": entries}})
    assert len(hooks) == spec_hooks._MAX_SPEC_HOOKS


def test_extra_hooks_fire_but_are_never_persisted(tmp_path, monkeypatch):
    store = ScriptHookStore(tmp_path)
    ran: list = []

    async def fake_run(hook, context="", hook_event=None):
        ran.append((hook.id, hook_event.get("tool_name")))
        return ScriptHookResult(hook_id=hook.id, hook_name=hook.name, event=hook.event)

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    extra = [ScriptHook(id="spec:a1:x", event=HOOK_EVENT_PRE_TOOL_USE, matcher="sh*", command="c")]
    asyncio.run(store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="shell", extra_hooks=extra))
    asyncio.run(store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="read", extra_hooks=extra))
    assert ran == [("spec:a1:x", "shell")]
    persisted = tmp_path / "hooks.json"
    assert not persisted.exists() or "spec:a1:x" not in persisted.read_text(encoding="utf-8")


def _passthrough_sandbox(monkeypatch):
    monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))


def test_a_spec_pre_tool_use_hook_exit_2_blocks(tmp_path, agents_dir, monkeypatch, notices):
    _passthrough_sandbox(monkeypatch)
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": None)
    script = tmp_path / "deny.py"
    script.write_text(
        "import os, sys\nsys.stderr.write(os.getcwd())\nsys.exit(2)\n", encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = f'"{sys.executable}" "{script}"'
    _write_spec(agents_dir, "a1", hooks={"preToolUse": [{"command": command}]})
    hooks, _, _ = _prepare(_client(ACP_BACKEND_KAS), "a1")
    results = asyncio.run(
        ScriptHookStore(tmp_path).fire(
            HOOK_EVENT_PRE_TOOL_USE,
            tool_name="shell",
            tool_input={},
            extra_hooks=hooks,
            extra_hooks_cwd=str(workspace),
        )
    )
    # Blocks, and ran in the session workspace rather than the gateway's cwd.
    assert [(r.exit_code, r.blocked) for r in results] == [(2, True)]
    assert Path(results[0].stderr).resolve() == workspace.resolve()


def test_a_governance_denied_spec_hook_does_not_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": "off")

    async def no_spawn(*a, **k):
        raise AssertionError("a denied hook must not spawn")

    monkeypatch.setattr("kiro_crew.sandbox.create_subprocess_limited", no_spawn)
    hooks = spec_hooks.spec_script_hooks("a1", {"hooks": _OBJECT_HOOKS})
    results = asyncio.run(
        ScriptHookStore(tmp_path).fire(
            HOOK_EVENT_PRE_TOOL_USE,
            tool_name="shell",
            parent_session_key="s1",
            extra_hooks=hooks,
        )
    )
    assert [r.blocked for r in results] == [True]
    assert "governance" in results[0].error


@pytest.mark.asyncio
async def test_an_in_turn_agent_switch_reloads_the_new_agents_spec_hooks(tmp_path, monkeypatch):
    """After a provider-side switch, the turn's remaining tool calls meet the NEW
    agent's spec hooks: the runner re-prepares them for the agent it switched to."""
    from unittest.mock import AsyncMock, MagicMock

    from chat_test_helpers import _make_state

    from kiro_crew.agent_discovery import clear_list_agents_cache
    from kiro_crew.config.loader import refresh_materialized_agents
    from kiro_crew.config.paths import kiro_agents_dir
    from kiro_crew.providers.base import EVENT_AGENT_SWITCHED, EVENT_COMPLETE, LLMEvent

    d = kiro_agents_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name in ("helper", "other"):
        (d / f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    clear_list_agents_cache()
    refresh_materialized_agents()

    seen: list[str] = []

    async def spy(state, slot, client, agent, *, is_new):
        seen.append(agent)
        return [], False, None

    monkeypatch.setattr(chat_runner, "_prepare_spec_hooks", spy)
    state = _make_state(tmp_path)
    client = MagicMock()
    client.context_usage_pct = MagicMock(return_value=50.0)
    client.shutdown = AsyncMock()
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_success = MagicMock()
    state.sessions.record_failure = AsyncMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot("spec-hooks-switch-slot")
    slot.agent = "helper"

    async def _stream(msg):
        yield LLMEvent(kind=EVENT_AGENT_SWITCHED, text="other")
        yield LLMEvent(kind=EVENT_COMPLETE)

    client.stream = _stream
    client.stream_command = _stream
    try:
        await chat_runner._run_chat(state, slot, "/agent other")
    finally:
        tasks = list(state._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    assert seen == ["helper", "other"]
