"""A moved Bot's limits its worker cannot carry exactly (``mate_held_rules``).

Pins that such a limit is never written in a form that stops the worker (it is
held instead, with a KAS ``ask`` rule in its place); that a held limit is found
for the calls it covers and only those; and that no gate approves such a call
without a person: an ``auto`` cron's approver asks, a surface with no approver
refuses, and a trusting dashboard thread shows the card. A call no held limit
covers is approved as before (each case has that control).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.config.paths as paths_mod
import kiro_crew.slack.gateway  # noqa: F401  (binds sel before any patch; see deny_notice)
from kiro_crew import mate_held_rules, mate_worker
from kiro_crew.cron_service.fields import auto_job_approver
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

pytestmark = pytest.mark.usefixtures("ample_host_free_memory")

BASE = {
    "name": "kirocrew-mate-worker",
    "prompt": "worker base prompt",
    "tools": ["fs_read", "@kirocrew-core"],
    "allowedTools": ["fs_read"],
    "mcpServers": {"kirocrew-core": {"command": "kc", "args": ["core"]}},
}
_FILE = {"name": "zxc", "mcpServers": {}, "tools": [], "allowedTools": []}
#: A ``toolsSettings`` key outside what the worker carries.
_ODD = {"toolsSettings": {"execute_bash": {"denyByDefault": True}}}
_DENY = {"permissions": {"rules": [{"capability": "web_fetch", "effect": "deny"}]}}


@pytest.fixture
def ledger(monkeypatch, tmp_path) -> Path:
    mates = tmp_path / "home" / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: tmp_path / "home")
    return mates.parent / "migrated.json"


def _hold(ledger: Path, capability: str) -> None:
    rules = [{"text": "its permissions rule to deny shell", "capability": capability}]
    ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "", "rules": rules}}}))


def _event(kind: str, title: str = "a call") -> LLMEvent:
    return LLMEvent(kind=EVENT_PERMISSION_REQUEST, title=title, request_id="r1", tool_kind=kind)


def test_an_uncarriable_limit_is_held_and_the_worker_still_starts():
    out, held = mate_held_rules.split_policy(True, BASE, {**_ODD, **_DENY}, _FILE)

    assert [h["capability"] for h in held] == ["shell"]
    merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, out))
    rules = merged["permissions"]["rules"]
    assert {"capability": "web_fetch", "effect": "deny"} in rules
    assert {"capability": "shell", "effect": "ask"} in rules
    assert "toolsSettings" not in out


def test_a_worker_off_kas_gets_no_restriction_written_and_every_one_held():
    out, held = mate_held_rules.split_policy(False, BASE, _DENY, _FILE)

    assert out == _FILE
    assert [h["capability"] for h in held] == ["web_fetch"]


def test_a_held_limit_covers_its_capability_and_nothing_else(ledger):
    _hold(ledger, "shell")

    assert mate_held_rules.held_for(_event("execute"), agent="zxc")
    assert mate_held_rules.held_for(_event("read"), agent="zxc") == []
    assert mate_held_rules.held_for(_event("execute"), agent="other") == []


def test_a_limit_whose_capability_is_unknown_covers_every_call(ledger):
    _hold(ledger, mate_held_rules.ANY)

    assert mate_held_rules.held_for(_event("read"), agent="zxc")


class _Provider:
    supports_steer = True
    supports_refusal_steer = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def steer(self, message: str) -> bool:
        return True

    async def approve_tool(self, request_id) -> None:
        self.calls.append("approve")

    async def reject_tool(self, request_id) -> None:
        self.calls.append("reject")


async def _resolve(kind: str, approver=None) -> list[str]:
    provider = _Provider()
    with patch("kiro_crew.permission_floor.refusal_for", return_value=None):
        await _resolve_permission(
            provider,
            _event(kind),
            ToolApprovalPolicy.AUTO_APPROVE,
            None,
            on_tool_approval=approver,
            agent="zxc",
        )
    return provider.calls


@pytest.mark.asyncio
async def test_an_auto_cron_asks_a_person_for_a_held_call_and_runs_the_rest(ledger):
    _hold(ledger, "shell")
    card = AsyncMock(return_value=False)
    approver = auto_job_approver(card)

    assert await _resolve("execute", approver) == ["reject"]
    card.assert_awaited_once()
    card.reset_mock()
    assert await _resolve("read", approver) == ["approve"]
    card.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_surface_with_no_approver_refuses_a_held_call(ledger):
    _hold(ledger, "shell")

    assert await _resolve("execute") == ["reject"]
    assert await _resolve("read") == ["approve"]


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [True, False])
async def test_a_trusting_dashboard_thread_shows_the_card_for_a_held_call(tmp_path, held):
    from chat_runner_coverage_helpers import (
        _complete,
        _drive,
        _permission,
        _runner_state,
        _set_stream,
        _slot,
    )

    from kiro_crew.dashboard import chat_runner

    state, client = _runner_state(tmp_path)
    slot = _slot()
    slot._trust = True
    _set_stream(client, [_permission(tool_input=json.dumps({"command": "ls"})), _complete()])
    phrases = ["its permissions rule to deny shell"] if held else []
    with (
        patch.object(mate_held_rules, "held_for", return_value=phrases),
        patch.object(chat_runner, "sel", return_value=MagicMock()),
        patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        patch.object(chat_runner, "_name_grant_refusal_off_loop", new=AsyncMock(return_value=None)),
        patch.object(chat_runner, "_name_grant_refusal_for", new=AsyncMock(return_value=None)),
    ):
        await _drive(state, slot)

    if held:
        client.approve_tool.assert_not_awaited()
        assert [m for m in slot.messages if m.get("role") == "permission"]
    else:
        client.approve_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_hook_grant_does_not_approve_a_held_call(ledger):
    """The operator's auto-approve globs approve by name; a held call still asks."""
    from kiro_crew.hooks import TOOL_AUTO_APPROVE, ToolHookResult

    _hold(ledger, "shell")
    hooks = MagicMock()
    hooks.effective_denied_regexes = MagicMock(return_value=[])
    hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=TOOL_AUTO_APPROVE))

    async def resolve(kind: str, card: AsyncMock) -> list[str]:
        provider = _Provider()
        with patch("kiro_crew.name_grant.refusal_for_event", new=AsyncMock(return_value=None)):
            await _resolve_permission(
                provider,
                _event(kind),
                ToolApprovalPolicy.HOOK_BASED,
                hooks,
                on_tool_approval=card,
                agent="zxc",
            )
        return provider.calls

    card = AsyncMock(return_value=False)
    assert await resolve("execute", card) == ["reject"]
    card.assert_awaited_once()
    card = AsyncMock(return_value=False)
    assert await resolve("read", card) == ["approve"]
    card.assert_not_awaited()


@pytest.mark.parametrize(
    "matcher, capability",
    [
        ("@builtin", mate_held_rules.ANY),
        ("@builtin/execute_bash", "shell"),
        ("@builtin/fs_write", "fs_write"),
        ("@jira", "mcp"),
        ("execute_bash", "shell"),
    ],
)
def test_a_held_hook_is_recorded_under_the_tools_its_matcher_covers(matcher, capability):
    hooks = {"preToolUse": [{"matcher": matcher, "command": "x"}]}

    _out, held = mate_held_rules.split_policy(False, BASE, {"hooks": hooks}, _FILE)

    assert [h["capability"] for h in held] == [capability]


def test_a_bot_moved_now_is_not_told_earlier_work_ran_without_its_limits():
    from kiro_crew import mate_upgrade_notice as notice

    fresh = notice.policy_notice_prompt("x", fresh=True)
    repaired = notice.policy_notice_prompt("x")

    assert "since the upgrade" not in fresh and "Permissions" in fresh
    assert "since the upgrade" in repaired


def test_an_unreadable_ledger_holds_every_call_of_a_bot_and_no_other(ledger, monkeypatch):
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    ledger.write_text("{not json", encoding="utf-8")
    cfg = KiroCrewConfig()
    cfg.agents = {
        "zxc": KiroCrewAgentConfig(member_id="zxc", kiro_agent="zxc"),
        "kirocrew": KiroCrewAgentConfig(member_id="", kiro_agent="kirocrew"),
    }
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))

    assert mate_held_rules.held_for(_event("read"), agent="zxc") == [mate_held_rules.UNREADABLE]
    assert mate_held_rules.held_for(_event("read"), agent="kirocrew") == []


def test_a_limit_whose_capability_is_unknown_makes_kas_ask_on_every_capability():
    hooks = [{"event": "preToolUse", "matcher": "*", "command": "x"}]

    out, held = mate_held_rules.split_policy(True, BASE, {"hooks": hooks}, _FILE)

    assert [h["capability"] for h in held] == [mate_held_rules.ANY]
    asks = {r["capability"] for r in out["permissions"]["rules"] if r["effect"] == "ask"}
    assert {"mcp", "shell", "fs_write"} <= asks


def test_a_bot_made_again_under_a_deleted_bots_name_is_not_held(ledger, monkeypatch):
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    rules = [{"text": "deny shell", "capability": "shell"}]
    ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "old", "rules": rules}}}))
    cfg = KiroCrewConfig()
    cfg.agents = {"zxc": KiroCrewAgentConfig(member_id="new", kiro_agent="zxc")}
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))

    assert mate_held_rules.held_for(_event("execute"), agent="zxc") == []
    cfg.agents["zxc"].member_id = "old"
    assert mate_held_rules.held_for(_event("execute"), agent="zxc") == ["deny shell"]


def test_a_claude_worker_asks_for_the_built_ins_a_held_limit_covers(ledger, monkeypatch):
    """Claude's ``default`` mode runs in-project Read, Grep and Glob unasked, so the
    worker session of a Bot with a held read limit carries an ``ask`` rule for each."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew import execution_context, members
    from kiro_crew.acp.client_claude_session import (
        _claude_session_meta,
        _prepare_claude_held_asks,
    )
    from kiro_crew.connections import crew_oauth_proxy

    def options(member_id: str) -> dict:
        monkeypatch.setattr(
            execution_context,
            "read_session_execution",
            lambda key: SimpleNamespace(member_id=member_id),
        )
        client = SimpleNamespace(
            memory_mode="persistent", _session_key="k", _session_mcp_cache=[{"name": "gh"}]
        )
        # The ledger is read off the loop before the envelope is built.
        asyncio.run(_prepare_claude_held_asks(client))
        return _claude_session_meta(client)["claudeCode"]["options"]

    monkeypatch.setattr(members, "is_crewmate_worker_session", lambda key: True)
    monkeypatch.setattr(crew_oauth_proxy, "is_connection_server", lambda key, name: False)
    rules = [{"text": "deny reading", "capability": "fs_read"}]
    ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "m1", "rules": rules}}}))

    held = options("m1")
    assert held["settings"]["permissions"]["ask"] == ["Read", "Grep", "Glob"]
    assert held["allowDangerouslySkipPermissions"] is False
    assert "settings" not in options("other")

    rules[0]["capability"] = mate_held_rules.ANY
    ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "m1", "rules": rules}}}))
    every = options("m1")["settings"]["permissions"]["ask"]
    assert {"Bash", "Read", "Write", "WebFetch", "Task", "mcp__gh"} <= set(every)


@pytest.mark.parametrize(
    "hooks",
    [
        {"agentSpawn": [{"command": "x"}], "userPromptSubmit": [{"command": "y"}]},
        [{"event": "stop", "command": "x"}, {"event": "postToolUse", "command": "y"}],
    ],
)
def test_a_held_hook_that_sees_no_call_before_it_runs_holds_no_call(ledger, hooks):
    _out, held = mate_held_rules.split_policy(False, BASE, {"hooks": hooks}, _FILE)
    assert held and {h["capability"] for h in held} == {mate_held_rules.NO_CALL}
    ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "", "rules": held}}}))

    for kind in ("execute", "edit", "read", ""):
        assert mate_held_rules.held_for(_event(kind), agent="zxc") == []
    # A preToolUse hook beside them still holds every call.
    gate = {"preToolUse": [{"command": "z"}]}
    _out, held = mate_held_rules.split_policy(False, BASE, {"hooks": gate}, _FILE)
    ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "", "rules": held}}}))
    assert mate_held_rules.held_for(_event("read"), agent="zxc")


@pytest.mark.parametrize(
    "fields, held",
    [
        ({"tool_kind": "search", "tool_name": "web_search"}, True),
        ({"tool_kind": "fetch", "harness_builtin_tool": "web_search"}, True),
        ({"tool_kind": "fetch", "harness_builtin_tool": "web_fetch"}, False),
        ({"tool_kind": "fetch", "harness_tool_id": "claude#WebFetch"}, False),
        ({"tool_kind": "fetch", "harness_tool_id": "claude#WebSearch"}, True),
        ({"tool_kind": "fetch"}, True),
        ({"tool_kind": "fetch", "tool_name": "web_fetch"}, False),
        ({"tool_kind": "read", "tool_name": "fs_read"}, False),
    ],
)
def test_a_held_web_search_limit_covers_a_web_search(ledger, fields, held):
    _hold(ledger, "web_search")
    event = LLMEvent(kind=EVENT_PERMISSION_REQUEST, title="a call", request_id="r1", **fields)

    assert bool(mate_held_rules.held_for(event, agent="zxc")) is held


def test_an_array_form_pre_tool_hook_named_by_trigger_is_a_gate():
    """The array form names its event under ``trigger``; a PreToolUse one still gates."""
    hooks = [
        {"trigger": "PreToolUse", "matcher": "execute_bash", "action": {"type": "command"}},
        {"trigger": "SessionStart", "action": {"type": "command"}},
    ]

    _out, held = mate_held_rules.split_policy(False, BASE, {"hooks": hooks}, _FILE)

    assert [h["capability"] for h in held] == ["shell", mate_held_rules.NO_CALL]


def test_a_rewritten_transcript_identity_does_not_hide_the_vouched_one(ledger, monkeypatch):
    """The transcript record is written by the session it names, so it can add an
    identity but never hide the one this process vouched for."""
    from types import SimpleNamespace

    from kiro_crew import execution_context

    rules = [{"text": "deny shell", "capability": "shell"}]
    ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "m1", "rules": rules}}}))
    monkeypatch.setattr(
        execution_context, "read_session_execution", lambda k: SimpleNamespace(member_id="forged")
    )
    monkeypatch.setattr(
        execution_context,
        "read_vouched_session_execution",
        lambda k: SimpleNamespace(member_id="m1"),
    )

    assert mate_held_rules.held_for(_event("execute"), session_key="k") == ["deny shell"]


def test_the_asks_a_held_limit_needs_are_written_even_when_carried_rules_fill_the_policy():
    """A carried rule never uses up the room a required ``ask`` needs: it is held instead."""
    from kiro_crew.acp.kas_permissions import _MAX_RULES

    denies = [
        {"capability": "web_fetch", "match": [f"https://h{i}.example/*"], "effect": "deny"}
        for i in range(_MAX_RULES)
    ]
    policy = {"permissions": {"rules": denies}, **_ODD}

    out, held = mate_held_rules.split_policy(True, BASE, policy, _FILE)

    merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, out))
    assert {"capability": "shell", "effect": "ask"} in merged["permissions"]["rules"]
    assert {"capability": "web_fetch", "effect": "ask"} in merged["permissions"]["rules"]
    assert {"shell", "web_fetch"} <= {h["capability"] for h in held}


@pytest.mark.asyncio
async def test_a_read_only_turn_refuses_a_held_read_it_would_approve(ledger):
    """A READ_ONLY turn (a Side Chat) has no one to ask, so a held read is refused."""
    from types import SimpleNamespace

    from kiro_crew.hooks import TOOL_AUTO_APPROVE

    _hold(ledger, "fs_read")
    hooks = MagicMock()
    hooks.on_tool_call.return_value = SimpleNamespace(
        action=TOOL_AUTO_APPROVE, reason="", read_only=True, security_deny=False
    )

    async def resolve(kind: str) -> list[str]:
        provider = _Provider()
        with patch("kiro_crew.llm_helpers._steer_host_deny", new=AsyncMock()):
            await _resolve_permission(
                provider, _event(kind), ToolApprovalPolicy.READ_ONLY, hooks, agent="zxc"
            )
        return provider.calls

    assert await resolve("read") == ["reject"]
    _hold(ledger, "shell")
    assert await resolve("read") == ["approve"]


def test_held_limits_that_cannot_be_recorded_are_reported_not_raised(ledger, monkeypatch):
    """The move keeps a Bot where it is when its held limits have no record."""
    from kiro_crew import mate_restriction_carry

    held = [{"text": "deny shell", "capability": "shell"}]
    assert mate_restriction_carry._hold("zxc", held) is True
    assert json.loads(ledger.read_text())["held_rules"]["zxc"]["rules"] == held

    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mate_held_rules, "record_held", refuse)
    assert mate_restriction_carry._hold("zxc", held) is False


def test_a_worker_backend_that_cannot_be_told_applies_no_restriction(monkeypatch):
    """Unknown backend: nothing is written that could stop the worker, so all is held."""
    from kiro_crew import mate_restriction_carry

    def unknown(cfg, name=None):
        raise RuntimeError("no backend")

    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", unknown)
    assert mate_restriction_carry.applies_restrictions(object(), "zxc") is False
    out, held = mate_restriction_carry.carry_restrictions(object(), BASE, "zxc", _DENY, _FILE)
    assert "permissions" not in out and [h["capability"] for h in held] == ["web_fetch"]


def test_a_held_capability_in_a_full_policy_keeps_no_allow_and_still_asks():
    """A Bot file whose policy is full: its allows for the held capability go, which
    makes room for the ask, and the worker still merges (starts)."""
    from kiro_crew.acp.kas_permissions import _MAX_RULES

    allows = [
        {"capability": "web_fetch", "match": [f"https://h{i}.example/*"], "effect": "allow"}
        for i in range(_MAX_RULES)
    ]
    mate_file = {**_FILE, "permissions": {"rules": allows}}
    odd = {"toolsSettings": {"web_fetch": {"denyByDefault": True}}}

    out, held = mate_held_rules.split_policy(True, BASE, odd, mate_file)

    assert [h["capability"] for h in held] == ["web_fetch"]
    rules = out["permissions"]["rules"]
    assert not [r for r in rules if r.get("capability") == "web_fetch" and r["effect"] == "allow"]
    assert {"capability": "web_fetch", "effect": "ask"} in rules
    mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, out))
