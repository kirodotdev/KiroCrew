"""A spec hook's tool matcher names kiro-cli tools, and meets KAS's names for them.

On KAS the call's title is its command line or a prose sentence, so a matcher such as
``execute_bash`` never met it. The turn loop now matches the spec's hooks against the
id KAS states for the call (``_meta.kiro.toolId``), translated through one table.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

import kiro_crew.config.paths as paths_mod
import kiro_crew.hooks as hooks_mod
from kiro_crew.acp._dispatch import build_permission_event
from kiro_crew.acp.kas_permissions import KAS_TOOL_IDS_BY_KIRO_TOOL, kas_tool_match_names
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    ScriptHook,
    ScriptHookResult,
    ScriptHookStore,
    ToolHookResult,
)
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, LLMEvent


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


# ── The table ──


def test_the_translation_table_serves_only_kas():
    # A second backend that Crew fires spec hooks for needs its own vocabulary.
    assert ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS == frozenset({ACP_BACKEND_KAS})


def test_a_kas_shell_call_answers_to_its_kiro_cli_name():
    assert kas_tool_match_names("run_command") == ("execute_bash", "run_command", "shell")
    assert kas_tool_match_names("str_replace") == ("fs_write", "str_replace", "write")
    assert kas_tool_match_names("fs_write") == ("fs_write", "write")
    assert kas_tool_match_names("") == ()


def test_an_id_the_table_does_not_know_keeps_only_its_own_name():
    assert kas_tool_match_names("some_new_tool") == ("some_new_tool",)


def test_no_kas_id_is_reached_from_two_kiro_cli_names():
    # One KAS tool, one kiro-cli meaning: overlapping rows would let a matcher on
    # one kiro-cli tool fire for another's calls.
    seen: dict[str, str] = {}
    for name, ids in KAS_TOOL_IDS_BY_KIRO_TOOL.items():
        for tool_id in ids:
            assert tool_id not in seen, (tool_id, seen.get(tool_id), name)
            seen[tool_id] = name


# ── The id Crew reads off the wire ──


def _permission_msg(meta: object) -> SimpleNamespace:
    params = {
        "sessionId": "s",
        "toolCall": {"toolCallId": "run_command_1", "status": "pending", "title": "touch /x"},
        "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
    }
    if meta is not None:
        params["_meta"] = meta
    return SimpleNamespace(id=1, params=params)


def test_the_permission_event_carries_kas_s_tool_id():
    # The frame shape a live kiro-cli 2.24 KAS session sends for a shell call.
    meta = {"kiro": {"toolId": "run_command", "command": "touch /x"}}
    event, _ = build_permission_event(_permission_msg(meta))
    assert event.harness_tool_id == "run_command"
    assert event.title == "touch /x"


@pytest.mark.parametrize(
    "meta",
    [
        None,
        {"kiro": {}},
        {"kiro": {"toolId": 7}},
        {"kiro": {"toolId": "a b"}},
        {"kiro": {"toolId": "run_*"}},
        {"kiro": {"toolId": "x" * 129}},
    ],
    ids=["no-meta", "no-tool-id", "not-a-string", "whitespace", "glob", "too-long"],
)
def test_a_missing_or_malformed_tool_id_reads_as_none(meta):
    event, _ = build_permission_event(_permission_msg(meta))
    assert event.harness_tool_id == ""


def test_an_mcp_shaped_tool_id_is_kept():
    event, _ = build_permission_event(_permission_msg({"kiro": {"toolId": "@srv/tool:v1"}}))
    assert event.harness_tool_id == "@srv/tool:v1"


def test_a_tool_id_with_a_trailing_newline_is_refused():
    event, _ = build_permission_event(_permission_msg({"kiro": {"toolId": "run_command\n"}}))
    assert event.harness_tool_id == ""


# ── Conversion ──


def _pre_tool_hooks(matcher: str) -> list[ScriptHook]:
    return spec_hooks.spec_script_hooks(
        "a1", {"hooks": {"preToolUse": [{"matcher": matcher, "command": "guard.sh"}]}}
    )


@pytest.mark.parametrize("matcher", ["execute_bash", "shell", "run_command", "fs_*", "*"])
def test_a_matcher_naming_a_tool_kas_runs_is_kept(matcher):
    assert [h.matcher for h in _pre_tool_hooks(matcher)] == [matcher]


def test_a_matcher_the_table_does_not_know_is_kept_and_warned(caplog):
    with caplog.at_level("WARNING", logger="kiro_crew.agent_sdk.spec_hooks"):
        hooks = _pre_tool_hooks("use_aws")
    assert [h.matcher for h in hooks] == ["use_aws"]
    assert any("use_aws" in r.getMessage() for r in caplog.records)


def test_a_kept_matcher_meets_the_kas_id_it_names(tmp_path, monkeypatch):
    # A KAS built-in with no kiro-cli row is still guarded by a matcher naming its id.
    ran = _recording_runner(monkeypatch)
    hooks = _pre_tool_hooks("disclose_context")
    _fire(ScriptHookStore(tmp_path), hooks, "disclose_context")
    assert ran == [hooks[0].id]


def test_a_post_tool_use_title_matcher_is_kept_and_warned(caplog):
    # PostToolUse still matches the call's title, which the author is told once.
    with caplog.at_level("WARNING", logger="kiro_crew.agent_sdk.spec_hooks"):
        hooks = spec_hooks.spec_script_hooks(
            "a1", {"hooks": {"postToolUse": [{"matcher": "touch*", "command": "log.sh"}]}}
        )
    assert [h.matcher for h in hooks] == ["touch*"]
    assert any("postToolUse" in r.getMessage() for r in caplog.records)


# ── Matching in the hook store ──


def _recording_runner(monkeypatch) -> list:
    ran: list = []

    async def fake_run(hook, context="", hook_event=None):
        ran.append(hook.id)
        return ScriptHookResult(hook_id=hook.id, hook_name=hook.name, event=hook.event)

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    return ran


def _fire(store, hooks, tool_id):
    import asyncio

    return asyncio.run(
        store.fire(
            HOOK_EVENT_PRE_TOOL_USE,
            tool_name="touch /tmp/x",
            extra_hooks=hooks,
            extra_hooks_tool_names=kas_tool_match_names(tool_id) if tool_id else None,
        )
    )


def test_an_execute_bash_matcher_meets_a_kas_shell_call(tmp_path, monkeypatch):
    ran = _recording_runner(monkeypatch)
    hooks = _pre_tool_hooks("execute_bash")
    _fire(ScriptHookStore(tmp_path), hooks, "run_command")
    assert ran == [hooks[0].id]


def test_a_spec_hook_is_told_the_tool_s_kiro_cli_name(tmp_path, monkeypatch):
    # A script that branches on tool_name reads the vocabulary its matcher is in;
    # a Hooks-page hook in the same fire still gets the call's title.
    seen: dict = {}

    async def fake_run(hook, context="", hook_event=None):
        seen[hook.id] = hook_event.get("tool_name")
        return ScriptHookResult(hook_id=hook.id, hook_name=hook.name, event=hook.event)

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    store = ScriptHookStore(tmp_path)
    stored = ScriptHook(id="page", event=HOOK_EVENT_PRE_TOOL_USE, matcher="*", command="c")
    store._hooks[stored.id] = stored
    hooks = _pre_tool_hooks("execute_bash")
    _fire(store, hooks, "run_command")
    assert seen == {"page": "touch /tmp/x", hooks[0].id: "execute_bash"}


def test_an_unrelated_matcher_stays_silent(tmp_path, monkeypatch):
    ran = _recording_runner(monkeypatch)
    _fire(ScriptHookStore(tmp_path), _pre_tool_hooks("fs_write"), "run_command")
    assert ran == []


def test_a_call_kas_did_not_name_is_matched_on_its_title(tmp_path, monkeypatch):
    # No id: the hook is matched as a Hooks-page hook is, not skipped as not applying.
    ran = _recording_runner(monkeypatch)
    hooks = _pre_tool_hooks("*touch*")
    _fire(ScriptHookStore(tmp_path), hooks, "")
    assert ran == [hooks[0].id]


def test_a_matcher_is_never_compared_with_the_title(tmp_path, monkeypatch):
    # The title of a KAS shell call is its command line, which the model wrote.
    ran = _recording_runner(monkeypatch)
    _fire(ScriptHookStore(tmp_path), _pre_tool_hooks("*touch*"), "run_command")
    assert ran == []


def test_a_shell_alias_matcher_meets_a_kas_shell_call(tmp_path, monkeypatch):
    ran = _recording_runner(monkeypatch)
    hooks = _pre_tool_hooks("shell")
    _fire(ScriptHookStore(tmp_path), hooks, "run_command")
    assert ran == [hooks[0].id]


# ── Through the turn loop ──


def _write_spec(agents_dir: Path, matcher: str) -> None:
    spec = {
        "name": "a1",
        "prompt": "p",
        "hooks": {"preToolUse": [{"matcher": matcher, "command": "deny.sh"}]},
    }
    (agents_dir / "a1.json").write_text(json.dumps(spec), encoding="utf-8")
    # The turn resolves the slot's agent through discovery before it prepares hooks.
    from kiro_crew.agent_discovery import clear_list_agents_cache
    from kiro_crew.config.loader import refresh_materialized_agents

    clear_list_agents_cache()
    refresh_materialized_agents()


@pytest.fixture
def agents_dir() -> Path:
    d = paths_mod.kiro_agents_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _blocking_runner(monkeypatch) -> list:
    """Every spec hook exits 2; the list records each one that ran."""
    ran: list = []

    async def fake_run(hook, context="", hook_event=None):
        ran.append(hook.id)
        return ScriptHookResult(
            hook_id=hook.id, hook_name=hook.name, event=hook.event, exit_code=2, stderr="no"
        )

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": None)
    return ran


def _turn_state(tmp_path, backend: str):
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    client = AsyncMock()
    client.capabilities = capabilities_for(backend)
    client.cwd = ""
    sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    sessions.record_failure = AsyncMock()
    sessions.check_context_usage = MagicMock()
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    # Auto-approve, so a call no hook blocks is approved instead of waiting on a prompt.
    cb = MagicMock()
    cb.hooks.on_tool_call.return_value = ToolHookResult.auto_approve()
    cb.build_message.return_value = ("hello", None)
    state.context_builder = cb
    state._hook_store = ScriptHookStore(tmp_path / "hooks")
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    return state, client


async def _one_shell_call(state, client) -> None:
    async def stream(*a, **k):
        yield LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="touch /tmp/x",
            tool_kind="execute",
            request_id="req-1",
            harness_tool_id="run_command",
        )
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    calls = {"n": 0}

    def _stream(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return stream()

        async def done():
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        return done()

    client.stream = MagicMock(side_effect=_stream)
    client.context_usage_pct = MagicMock(return_value=0.0)
    slot = _ChatSlot("chat-1-spec-kas")
    slot.agent = "a1"
    with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
        mock_sel.return_value = MagicMock()
        await _run_chat(state, slot, "hello")
        if slot.task:
            await slot.task


@pytest.mark.asyncio
async def test_kas_an_execute_bash_spec_hook_blocks_the_shell_call(
    tmp_path, agents_dir, monkeypatch
):
    ran = _blocking_runner(monkeypatch)
    _write_spec(agents_dir, "execute_bash")
    state, client = _turn_state(tmp_path, ACP_BACKEND_KAS)
    await _one_shell_call(state, client)
    assert len(ran) == 1
    client.reject_tool.assert_called_once()
    client.approve_tool.assert_not_called()


@pytest.mark.asyncio
async def test_kas_an_unrelated_spec_hook_does_not_fire(tmp_path, agents_dir, monkeypatch):
    ran = _blocking_runner(monkeypatch)
    _write_spec(agents_dir, "fs_write")
    state, client = _turn_state(tmp_path, ACP_BACKEND_KAS)
    await _one_shell_call(state, client)
    assert ran == []
    client.approve_tool.assert_called_once()
    client.reject_tool.assert_not_called()


@pytest.mark.asyncio
async def test_kiro_cli_crew_fires_no_spec_hook(tmp_path, agents_dir, monkeypatch):
    ran = _blocking_runner(monkeypatch)
    _write_spec(agents_dir, "execute_bash")
    state, client = _turn_state(tmp_path, ACP_BACKEND_KIRO)
    await _one_shell_call(state, client)
    assert ran == []
    client.approve_tool.assert_called_once()
