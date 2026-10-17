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
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TOOL_CALL,
    LLMEvent,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


# ── The table ──


def test_every_backend_crew_fires_spec_hooks_for_has_a_translation_table():
    # A backend that Crew fires spec hooks for needs its own vocabulary: KAS's is
    # this module's table, every other member's is in harness_tool_names.
    from kiro_crew.acp.harness_tool_names import HARNESS_TOOL_TABLES

    assert ACP_BACKEND_KAS in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS
    assert ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS - {ACP_BACKEND_KAS} == frozenset(HARNESS_TOOL_TABLES)


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


def test_the_hook_rows_are_the_gate_s_policy_table_inverted():
    # ONE kiro-cli <-> KAS table: every policy-table pair appears here as a row
    # entry, and every row entry outside the hook-only tools is a policy-table
    # pair, so a built-in added or renamed in one place cannot go missing in
    # the other.
    from kiro_crew.acp.kas_permissions import _HOOK_ONLY_ROWS
    from kiro_crew.platform.tool_names import KIRO_CLI_NAME_BY_KAS_TOOL

    pairs = {(name, tool_id) for name, ids in KAS_TOOL_IDS_BY_KIRO_TOOL.items() for tool_id in ids}
    hook_only = {(name, tool_id) for name, ids in _HOOK_ONLY_ROWS.items() for tool_id in ids}
    policy = {(name, kas_id) for kas_id, name in KIRO_CLI_NAME_BY_KAS_TOOL.items()}
    assert policy <= pairs
    assert pairs - hook_only - policy == {("fs_write", "fs_write")}


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


@pytest.mark.parametrize(
    "matcher", ["@kirocrew-core/monitor_start", "@github/create_issue", "@my.srv-2/*"]
)
def test_an_mcp_server_tool_matcher_is_kept_without_a_warning(matcher, caplog):
    # The form the gate builds for an MCP call, the KAS projection reads as MCP and
    # the agent-host contract documents; the object form's character set alone
    # dropped it as "invalid matcher" and the guard never ran.
    with caplog.at_level("WARNING", logger="kiro_crew.agent_sdk.spec_hooks"):
        hooks = _pre_tool_hooks(matcher)
    assert [h.matcher for h in hooks] == [matcher]
    assert not caplog.records


@pytest.mark.parametrize(
    "matcher",
    [
        "@*/create_issue",  # a server glob would widen the guard across servers
        "@gith?b/create_issue",
        "@github",  # no tool part
        "@/create_issue",
        "@github/",
        "@github/create/issue",
        "@github/create_issue\n",  # $-before-newline must not admit this
        "@github/create;issue",
        "@git hub/create_issue",
        "@" + "s" * 200 + "/t",  # over the matcher length cap
    ],
)
def test_a_malformed_mcp_matcher_still_drops_the_hook(matcher):
    assert _pre_tool_hooks(matcher) == []


def test_an_mcp_server_tool_spec_hook_withholds_mcp_auto_approval(tmp_path, monkeypatch):
    # Without this, KAS auto-approves the MCP call and no permission request reaches
    # the gate, so even an accepted matcher would never see the call.
    from kiro_crew.acp import kas_agents
    from kiro_crew.acp.kas_permissions import hook_gated_capabilities

    monkeypatch.setattr(kas_agents, "get_global_hook_store", lambda: ScriptHookStore(tmp_path))
    spec = {"hooks": {"preToolUse": [{"matcher": "@github/create_issue", "command": "g.sh"}]}}
    matchers = kas_agents.pre_tool_hook_matchers("a1", spec)
    assert matchers == ("@github/create_issue",)
    assert hook_gated_capabilities(matchers) == {"mcp"}


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
    # A real client records no projected batch until a KAS session registers one.
    client.kas_auto_approved_capabilities = None
    client.kas_projected_agent = ""
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


async def _one_turn(state, client, events) -> None:
    """One chat turn whose stream yields *events*, then completes."""

    async def stream(*a, **k):
        for event in events:
            yield event
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


def _page_hook(state, matcher: str) -> None:
    state._hook_store._hooks = {
        "page": ScriptHook(id="page", event=HOOK_EVENT_PRE_TOOL_USE, matcher=matcher, command="p")
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("matcher", ["@github/create_issue", "mcp__github__create_issue"])
async def test_kas_a_hooks_page_mcp_hook_meets_the_call_by_its_server_identity(
    tmp_path, agents_dir, monkeypatch, matcher
):
    ran = _blocking_runner(monkeypatch)
    _write_spec(agents_dir, "fs_write")
    state, client = _turn_state(tmp_path, ACP_BACKEND_KAS)
    _page_hook(state, matcher)
    await _one_turn(
        state,
        client,
        [
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                title="Create issue",
                request_id="req-1",
                tool_name="create_issue",
                mcp_server_name="github",
                harness_tool_id="create_issue",
            )
        ],
    )
    assert ran == ["page"]
    client.reject_tool.assert_called_once()
    client.approve_tool.assert_not_called()


@pytest.mark.asyncio
async def test_kas_an_mcp_server_tool_spec_hook_blocks_the_mcp_call(
    tmp_path, agents_dir, monkeypatch
):
    ran = _blocking_runner(monkeypatch)
    _write_spec(agents_dir, "@github/create_issue")
    state, client = _turn_state(tmp_path, ACP_BACKEND_KAS)
    await _one_turn(
        state,
        client,
        [
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                title="Create issue",
                request_id="req-1",
                tool_name="create_issue",
                mcp_server_name="github",
                harness_tool_id="create_issue",
            )
        ],
    )
    assert len(ran) == 1 and ran[0].startswith("spec:")
    client.reject_tool.assert_called_once()
    client.approve_tool.assert_not_called()


def _fetch_frames() -> list:
    # KAS sends the tool-call frame BEFORE the permission request.
    return [
        LLMEvent(kind=EVENT_TOOL_CALL, title="Fetch URL", tool_call_id="t-1"),
        LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Fetch URL",
            request_id="req-1",
            tool_call_id="t-1",
            harness_tool_id="web_fetch",
        ),
    ]


@pytest.mark.asyncio
async def test_kas_a_gated_chat_call_runs_its_hook_once_on_the_permission_request(
    tmp_path, agents_dir, monkeypatch
):
    ran = _blocking_runner(monkeypatch)
    _write_spec(agents_dir, "fs_write")
    state, client = _turn_state(tmp_path, ACP_BACKEND_KAS)
    _page_hook(state, "web_fetch")
    informational = AsyncMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.fire_tool_hooks", informational)
    await _one_turn(state, client, _fetch_frames())
    assert ran == ["page"]
    informational.assert_not_awaited()
    client.reject_tool.assert_called_once()


@pytest.mark.asyncio
async def test_kiro_cli_a_chat_tool_call_still_fires_its_informational_hook(
    tmp_path, agents_dir, monkeypatch
):
    _write_spec(agents_dir, "fs_write")
    state, client = _turn_state(tmp_path, ACP_BACKEND_KIRO)
    informational = AsyncMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.fire_tool_hooks", informational)
    await _one_turn(
        state, client, [LLMEvent(kind=EVENT_TOOL_CALL, title="Fetch URL", tool_call_id="t-1")]
    )
    informational.assert_awaited_once()
    assert informational.await_args.args[1] == "Fetch URL"


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
