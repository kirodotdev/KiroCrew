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
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
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
_ODD = {"toolsSettings": {"execute_bash": {"fallbackAction": "deny"}}}
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


def test_a_deny_beside_a_named_bundle_is_carried_not_held_on_every_call():
    """A ``policies`` bundle only grants, so the deny beside it lands as a rule alone."""
    deny = {"capability": "subagent", "effect": "deny", "match": ["*"]}
    block = {"permissions": {"policies": ["read-only-shell"], "rules": [deny]}}

    out, held = mate_held_rules.split_policy(True, BASE, block, _FILE)
    assert held == []
    assert out["permissions"] == {"rules": [deny]}

    _, held_off_kas = mate_held_rules.split_policy(False, BASE, block, _FILE)
    assert [h["capability"] for h in held_off_kas] == ["subagent"]


def test_a_block_with_an_unknown_key_is_still_held_on_every_call():
    block = {"permissions": {"rules": [], "mystery": True}}

    _, held = mate_held_rules.split_policy(True, BASE, block, _FILE)

    assert [h["capability"] for h in held] == [mate_held_rules.ANY]


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

    async def steer(self, message: str, *, origin: str = "user") -> bool:
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

    assert "since the upgrade" not in fresh and "Permissions" not in fresh
    assert "since the upgrade" in repaired
    # Nothing clears a hold, so neither notice points at a setting as the fix.
    for prompt in (fresh, repaired):
        assert "does not lift it" in prompt


def test_an_unreadable_ledger_holds_every_call_of_the_bots_it_still_names(ledger, monkeypatch):
    """A damaged ``migrated.json`` cannot say which limits a Bot has, so every call of
    a Bot it still names is held; a Bot its text does not name runs as before."""
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    ledger.write_text('{"held_rules": {"zxc": {"member_id": "zxc", "rul', encoding="utf-8")
    cfg = KiroCrewConfig()
    cfg.agents = {
        "zxc": KiroCrewAgentConfig(member_id="zxc", kiro_agent="zxc"),
        "abc": KiroCrewAgentConfig(member_id="abc", kiro_agent="abc"),
        "kirocrew": KiroCrewAgentConfig(member_id="", kiro_agent="kirocrew"),
    }
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))

    assert mate_held_rules.held_for(_event("read"), agent="zxc") == [mate_held_rules.UNREADABLE]
    assert mate_held_rules.held_for(_event("read"), agent="abc") == []
    assert mate_held_rules.held_for(_event("read"), agent="kirocrew") == []

    # An entry recorded with no member id is matched by its Bot's name.
    ledger.write_text('{"held_rules": {"kirocrew": {"member_id": "", "rul', encoding="utf-8")
    assert mate_held_rules.held_for(_event("read"), agent="kirocrew") == [
        mate_held_rules.UNREADABLE
    ]
    assert mate_held_rules.held_for(_event("read"), agent="zxc") == []


def test_a_call_that_cannot_be_matched_against_read_rules_is_held(ledger, monkeypatch):
    """Once a Bot's rules are read, an error matching the call holds it."""
    _hold(ledger, "shell")

    def broken(event):
        raise RuntimeError("no capability")

    monkeypatch.setattr(mate_held_rules, "event_capabilities", broken)

    assert mate_held_rules.held_for(_event("read"), agent="zxc") == [
        "its permissions rule to deny shell"
    ]
    assert mate_held_rules.held_for(_event("read"), agent="other") == []


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
            backend=ACP_BACKEND_CLAUDE,
            memory_mode="persistent",
            _session_key="k",
            _session_mcp_cache=[{"name": "gh"}, {"name": "dot.probe"}],
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
    # Claude spells a server's tools with `.` as `_`; the ask must match them.
    assert "mcp__dot_probe" in every

    # A named helper the Bot spawns runs under the Bot's identity but is not its
    # worker session: it carries the same asks.
    monkeypatch.setattr(members, "is_crewmate_worker_session", lambda key: False)
    rules[0]["capability"] = "fs_read"
    ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "m1", "rules": rules}}}))
    assert options("m1")["settings"]["permissions"]["ask"] == ["Read", "Grep", "Glob"]
    assert "settings" not in options("other")
    # A read limit leaves the helper the user's own MCP servers.
    assert "strictMcpConfig" not in options("m1")

    # A held mcp limit names only Crew's servers in its asks, so the helper must
    # not also start the user's own (possibly pre-approved) claude servers.
    rules[0]["capability"] = "mcp"
    ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "m1", "rules": rules}}}))
    held_mcp = options("m1")
    assert held_mcp["strictMcpConfig"] is True
    assert "mcp__gh" in held_mcp["settings"]["permissions"]["ask"]
    assert "strictMcpConfig" not in options("other")


def test_a_child_ask_that_has_no_room_leaves_the_authored_denies_whole(ledger, monkeypatch):
    """KAS refuses a ``permissions`` block over its rule cap whole, denies included,
    so ``held_child_spec`` adds an ask only where it fits; the pre-approval still goes."""
    from types import SimpleNamespace

    from kiro_crew import execution_context
    from kiro_crew.acp.kas_permissions import _MAX_RULES

    monkeypatch.setattr(
        execution_context, "read_session_execution", lambda key: SimpleNamespace(member_id="m1")
    )
    rules = [{"text": "deny fetching", "capability": "web_fetch"}]
    ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "m1", "rules": rules}}}))
    ask = {"capability": "web_fetch", "effect": "ask"}

    def child(denies: int) -> dict:
        deny = [
            {"capability": "mcp", "effect": "deny", "match": [f"s{i}/*"]} for i in range(denies)
        ]
        spec = {"allowedTools": ["web_fetch"], "permissions": {"rules": deny}}
        return mate_held_rules.held_child_spec(spec, "k")

    full = child(_MAX_RULES)
    assert len(full["permissions"]["rules"]) == _MAX_RULES
    assert ask not in full["permissions"]["rules"]
    assert "web_fetch" not in full.get("allowedTools", [])

    room = child(_MAX_RULES - 1)
    assert room["permissions"]["rules"][-1] == ask
    assert len(room["permissions"]["rules"]) == _MAX_RULES


def _held_child(ledger, monkeypatch, capability: str, spec: dict) -> dict:
    from types import SimpleNamespace

    from kiro_crew import execution_context

    monkeypatch.setattr(
        execution_context, "read_session_execution", lambda key: SimpleNamespace(member_id="m1")
    )
    rules = [{"text": "a held limit", "capability": capability}]
    ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "m1", "rules": rules}}}))
    return mate_held_rules.held_child_spec(spec, "k")


def test_a_child_ask_counts_the_rules_a_long_match_is_split_into(ledger, monkeypatch):
    """199 rules as written are 200 once a 65-pattern ``match`` is split, so one more
    ask would make KAS refuse the block whole and drop every deny."""
    from kiro_crew.acp.kas_permissions import _MAX_RULES, projection_accepts

    long_deny = {
        "capability": "shell",
        "effect": "deny",
        "match": [f"cmd{i} *" for i in range(65)],
    }
    deny = [
        {"capability": "mcp", "effect": "deny", "match": [f"s{i}/*"]} for i in range(_MAX_RULES - 2)
    ]
    authored = {"permissions": {"rules": [*deny, long_deny]}}
    assert projection_accepts(authored["permissions"])

    out = _held_child(ledger, monkeypatch, "web_fetch", authored)

    assert out["permissions"]["rules"] == [*deny, long_deny]
    assert projection_accepts(out["permissions"])


def test_a_held_subagent_limit_drops_the_childs_trusted_agents(ledger, monkeypatch):
    spec = {"toolsSettings": {"subagent": {"trustedAgents": ["helper"], "availableAgents": ["x"]}}}

    out = _held_child(ledger, monkeypatch, "subagent", spec)

    assert out["toolsSettings"]["subagent"] == {"availableAgents": ["x"]}


def test_a_bot_off_kas_whose_old_agent_only_granted_tools_gets_no_hold():
    from kiro_crew.mate_config_migration import carried_policy

    grants = {
        "fs_write": {"allowedPaths": ["/work/**"]},
        "execute_bash": {"allowedCommands": ["ls"], "autoAllowReadonly": True},
    }
    source = {**_FILE, "toolsSettings": grants}

    _, held = mate_held_rules.split_policy(False, BASE, carried_policy(BASE, source), _FILE)
    assert held == []

    source["toolsSettings"]["fs_write"]["deniedPaths"] = ["/etc/**"]
    _, held = mate_held_rules.split_policy(False, BASE, carried_policy(BASE, source), _FILE)
    assert [h["capability"] for h in held] == ["fs_write"]


def test_a_limit_keeps_the_grants_in_its_own_entry_as_its_exceptions():
    """``denyByDefault`` with ``allowedCommands`` is one rule: the commands are what
    it still allows, so dropping them would refuse calls the old agent ran."""
    from kiro_crew.mate_config_migration import carried_policy

    shell = {"denyByDefault": True, "allowedCommands": ["git status"], "autoAllowReadonly": True}
    source = {**_FILE, "toolsSettings": {"execute_bash": shell}}

    assert carried_policy(BASE, source)["toolsSettings"] == {"execute_bash": shell}


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


def test_a_held_hook_that_sees_no_call_is_named_as_not_running_not_as_asking(monkeypatch):
    """No call ever asks for such a hook, so the notice must not say one does."""
    from kiro_crew import mate_upgrade_notice

    owed: list[tuple] = []
    monkeypatch.setattr(
        mate_upgrade_notice, "mark_policy_owed", lambda *a, **k: owed.append((a, k))
    )
    hook = {"text": "its postToolUse hook", "capability": mate_held_rules.NO_CALL}
    shell = {"text": "its permissions rule to deny shell", "capability": "shell"}

    mate_held_rules.owe_held_notice("zxc", [hook])
    (_name, change), kwargs = owed[-1]
    assert kwargs["held"] is False
    assert "asks you" not in change and "its postToolUse hook" in change

    mate_held_rules.owe_held_notice("zxc", [hook, shell])
    (_name, change), kwargs = owed[-1]
    assert kwargs["held"] is True
    asks, _sep, stopped = change.partition("; and ")
    assert "deny shell" in asks and "postToolUse" not in asks
    assert "postToolUse" in stopped and "do not run" in stopped


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


def test_undoing_a_hold_clears_only_the_identity_it_was_recorded_for(ledger):
    theirs = {"member_id": "theirs", "rules": [{"text": "deny shell", "capability": "shell"}]}
    ledger.write_text(json.dumps({"held_rules": {"zxc": theirs}}))

    mate_held_rules.drop_held("zxc", "ours")
    assert json.loads(ledger.read_text())["held_rules"]["zxc"] == theirs
    mate_held_rules.drop_held("zxc", "theirs")
    assert "zxc" not in json.loads(ledger.read_text())["held_rules"]


def test_a_repair_records_no_hold_for_a_bot_file_another_gateway_keeps(ledger):
    """The other gateway's Bot of the same name keeps the hold it has."""
    from kiro_crew import mate_restriction_carry

    theirs = {"member_id": "theirs", "rules": [{"text": "deny fs_read", "capability": "fs_read"}]}
    ledger.write_text(json.dumps({"held_rules": {"zxc": theirs}}))
    (ledger.parent / "agents" / "zxc.json").write_text("{}")
    (ledger.parent / "owners.json").write_text(json.dumps({"zxc": "/other/data/home"}))

    held = [{"text": "deny shell", "capability": "shell"}]
    assert mate_restriction_carry._hold("zxc", held) is False
    assert json.loads(ledger.read_text())["held_rules"]["zxc"] == theirs


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


def _hold_b() -> None:
    mate_held_rules.record_held("b", "", [{"text": "deny shell", "capability": "shell"}])


def _check_b() -> None:
    from kiro_crew.mate_policy_repair import mark_checked

    mark_checked("b")


def _move_b() -> None:
    from kiro_crew.mate_config_migration import _record_migrated

    _record_migrated("b")


@pytest.mark.parametrize(
    "other_write", [_hold_b, _check_b, _move_b], ids=["a-hold", "a-check", "a-move"]
)
def test_clearing_one_bots_hold_does_not_erase_a_write_made_meanwhile(ledger, other_write):
    """The move clears Bot a's hold after a failed repoint while another thread
    writes the same ledger: that write survives the clear."""
    import threading

    from kiro_crew import mate_config_migration as migration

    rules = [{"text": "deny shell", "capability": "shell"}]
    ledger.write_text(json.dumps({"held_rules": {"a": {"member_id": "", "rules": rules}}}))
    real_read = migration._read_ledger
    lift_read, go = threading.Event(), threading.Event()

    def read_then_wait():
        data = real_read()
        if threading.current_thread().name == "lift":
            lift_read.set()
            assert go.wait(10)
        return data

    with patch.object(migration, "_read_ledger", read_then_wait):
        lift = threading.Thread(target=mate_held_rules.drop_held, args=("a", ""), name="lift")
        lift.start()
        assert lift_read.wait(10)
        other = threading.Thread(target=other_write)
        other.start()
        # With the lock this write waits for the lift; without it, it lands now.
        other.join(0.5)  # flake-ok: bounded; a slow host can only make this pass
        go.set()
        lift.join(10)
        other.join(10)
    assert not lift.is_alive() and not other.is_alive()

    data = json.loads(ledger.read_text())
    assert "a" not in data.get("held_rules", {})
    assert "b" in [
        *data.get("held_rules", {}),
        *data.get("policy_checked", []),
        *data.get("migrated", []),
    ]


@pytest.mark.parametrize(
    "write", [_hold_b, _check_b, _move_b, lambda: mate_held_rules.drop_held("a", "")]
)
def test_every_ledger_write_holds_the_file_lock_another_gateway_takes(ledger, monkeypatch, write):
    """Two gateways with their own data homes can share one Kiro home and its ledger,
    so a thread lock alone would let one gateway's write erase the other's."""
    import contextlib

    from kiro_crew import mate_config_migration as migration
    from kiro_crew import platform_compat

    rules = [{"text": "deny shell", "capability": "shell"}]
    ledger.write_text(json.dumps({"held_rules": {"a": {"member_id": "", "rules": rules}}}))
    locked: list[str] = []
    writes: list[list[str]] = []
    real_lock, real_write = platform_compat.flock_exclusive, migration._write_ledger

    @contextlib.contextmanager
    def lock(fd):
        with real_lock(fd):
            locked.append("ledger")
            try:
                yield
            finally:
                locked.pop()

    def write_ledger(data):
        writes.append(list(locked))
        real_write(data)

    monkeypatch.setattr(platform_compat, "flock_exclusive", lock)
    monkeypatch.setattr(migration, "_write_ledger", write_ledger)

    write()

    assert writes and all(w == ["ledger"] for w in writes)
    assert (ledger.parent / "migrated.json.lock").exists()


def test_a_kiro_cli_helper_of_a_bot_with_held_limits_is_refused_and_nothing_else(
    ledger, monkeypatch
):
    """Kiro CLI reads a helper's spec from its own file, so its pre-approvals cannot
    be narrowed: a helper whose spec pre-approves a held capability, or cannot be
    read, is refused. A helper that pre-approves nothing held, the Bot's worker,
    its other sessions and every other Bot's helpers still start."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew import execution_context
    from kiro_crew.acp.harness.kiro import KiroHarness
    from kiro_crew.acp.session_handle import AcpAgentUnavailable

    monkeypatch.setattr(
        execution_context,
        "read_session_execution",
        lambda key: SimpleNamespace(member_id="other" if key.endswith("theirs") else "m1"),
    )
    rules = [{"text": "deny fetching", "capability": "web_fetch"}]
    ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "m1", "rules": rules}}}))
    agents = ledger.parent.parent / "agents"
    agents.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents)
    for name, allowed in {"helper": ["web_fetch"], "reader": ["fs_read"]}.items():
        spec = {"name": name, "tools": ["*"], "allowedTools": allowed}
        (agents / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")

    def start(agent: str, key: str):
        return asyncio.run(KiroHarness().session_extras(agent, work_dir=None, session_key=key))

    # A helper that pre-approves nothing a held limit covers starts as before.
    start("reader", "subagent:abc")
    with pytest.raises(AcpAgentUnavailable):
        start("no-such-spec", "subagent:abc")
    with pytest.raises(AcpAgentUnavailable) as refused:
        start("helper", "subagent:abc")
    text = str(refused.value)
    assert "helper" in text and "Kiro CLI" in text and "asking you" in text
    assert "config" not in text.lower() and "kirocrew " not in text

    start(mate_worker.MATE_WORKER_AGENT, "subagent:abc")
    start("helper", "dashboard:chat")
    start("helper", "subagent:theirs")

    # A limit no call falls under never makes a helper ask, so it refuses nothing.
    rules[0]["capability"] = mate_held_rules.NO_CALL
    ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "m1", "rules": rules}}}))
    start("helper", "subagent:abc")

    # Kiro CLI reads inside the session folder unasked for every agent, so a held
    # read limit (or one of unknown capability) refuses even a helper that grants nothing.
    (agents / "plain.json").write_text(
        json.dumps({"name": "plain", "tools": ["*"], "allowedTools": []}), encoding="utf-8"
    )
    for capability in ("fs_read", mate_held_rules.ANY):
        rules[0]["capability"] = capability
        ledger.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "m1", "rules": rules}}}))
        with pytest.raises(AcpAgentUnavailable):
            start("plain", "subagent:abc")


def test_an_unreadable_workspace_helper_spec_is_not_replaced_by_the_users(
    ledger, monkeypatch, tmp_path
):
    """Kiro CLI loads the workspace's spec first, so one the reader refuses counts as
    pre-approving; the user-level spec of the same name is not read in its place."""
    from kiro_crew.agent_sdk import agent_specs

    user = tmp_path / "user-agents"
    user.mkdir()
    (user / "helper.json").write_text(json.dumps({"name": "helper"}), encoding="utf-8")
    workspace = tmp_path / "proj"
    (workspace / ".kiro" / "agents").mkdir(parents=True)
    (workspace / ".kiro" / "agents" / "helper.json").write_text(
        json.dumps({"name": "helper", "allowedTools": ["web_fetch"]}), encoding="utf-8"
    )
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    real = agent_specs.load_agent_spec

    def refuse_workspace(agents_dir, agent):
        if agents_dir != user:
            raise agent_specs.AgentSpecUnreadable("refused")
        return real(agents_dir, agent)

    monkeypatch.setattr(agent_specs, "load_agent_spec", refuse_workspace)

    assert mate_held_rules._kiro_cli_spec("helper", workspace) is None
    # A workspace with no spec of that name falls through to the user's.
    assert mate_held_rules._kiro_cli_spec("helper", tmp_path / "empty") == {"name": "helper"}


def test_the_kiro_cli_helper_check_reads_disk_off_the_event_loop(monkeypatch):
    """The check reads the ledger and the helper's spec, so it never runs on the loop."""
    import asyncio
    import threading

    from kiro_crew.acp.harness.kiro import KiroHarness

    seen: list[bool] = []

    def refusal(session_key, agent, *, cwd=None):
        seen.append(threading.current_thread() is threading.main_thread())
        return ""

    monkeypatch.setattr(mate_held_rules, "kiro_cli_helper_refusal", refusal)

    asyncio.run(KiroHarness().session_extras("helper", work_dir=None, session_key="subagent:a"))

    assert seen == [False]


def _web_event() -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Fetching https://example.com",
        request_id="r1",
        tool_kind="fetch",
        harness_tool_id="web_fetch",
        kas_builtin_ids=True,
        kas_managed_ask=True,
    )


async def _resolve_web(approver=None) -> list[str]:
    provider = _Provider()
    with patch("kiro_crew.permission_floor.refusal_for", return_value=None):
        await _resolve_permission(
            provider,
            _web_event(),
            ToolApprovalPolicy.AUTO_APPROVE,
            None,
            on_tool_approval=approver,
            agent="zxc",
        )
    return provider.calls


@pytest.fixture
def web_ask_edition(monkeypatch):
    """Set which edition ``web_ask_rule`` reads; the rest of the turn stays ``oss``."""
    from types import SimpleNamespace

    from kiro_crew import web_ask_rule

    def set_edition(edition: str) -> None:
        monkeypatch.setattr(
            web_ask_rule, "edition", SimpleNamespace(is_lts=lambda: edition == "lts")
        )

    return set_edition


@pytest.mark.asyncio
@pytest.mark.parametrize("edition", ["lts", "oss"])
async def test_a_surface_with_no_approver_refuses_an_org_web_ask(ledger, web_ask_edition, edition):
    """``web_ask_rule``: an ``AUTO_APPROVE`` turn with nobody to ask refuses KAS's ask
    on an organisation's web rule in LTS, and approves it in ``oss`` as before."""
    web_ask_edition(edition)
    assert await _resolve_web() == (["reject"] if edition == "lts" else ["approve"])


@pytest.mark.asyncio
@pytest.mark.parametrize("edition", ["lts", "oss"])
async def test_an_auto_cron_asks_a_person_for_an_org_web_ask(ledger, web_ask_edition, edition):
    """``web_ask_rule``: an ``auto`` cron job's approver does not answer KAS's ask on an
    organisation's web rule in LTS; its interactive fallback is asked instead."""
    web_ask_edition(edition)
    card = AsyncMock(return_value=False)
    calls = await _resolve_web(auto_job_approver(card))
    if edition == "lts":
        assert calls == ["reject"]
        card.assert_awaited_once()
    else:
        assert calls == ["approve"]
        card.assert_not_awaited()


def _deep_ledger(ledger: Path, depth: int = 200_000) -> None:
    """A ``migrated.json`` naming Bot ``m1``, nested too deep for the JSON parser."""
    head = '{"held_rules": {"zxc": {"member_id": "m1", "rules": [], "x": '
    ledger.write_text(head + "[" * depth + "]" * depth + "}}}", encoding="utf-8")


def test_a_ledger_nested_too_deep_to_parse_holds_every_call_of_the_bot_it_names(
    ledger, monkeypatch
):
    from types import SimpleNamespace

    from kiro_crew import execution_context

    _deep_ledger(ledger)
    who = {"member": "m1"}
    monkeypatch.setattr(
        execution_context,
        "read_session_execution",
        lambda key: SimpleNamespace(member_id=who["member"]),
    )
    spec = {"allowedTools": ["fs_read", "execute_bash"]}

    assert mate_held_rules.held_for(_event("read"), session_key="k") == [mate_held_rules.UNREADABLE]
    child = mate_held_rules.held_child_spec(spec, "k")
    assert "fs_read" not in child.get("allowedTools", [])
    assert "execute_bash" not in child.get("allowedTools", [])

    # Control: a session of no member is held by nothing and its spec is unchanged.
    who["member"] = ""
    assert mate_held_rules.held_for(_event("read"), session_key="k") == []
    assert mate_held_rules.held_child_spec(spec, "k") is spec


def test_a_held_bots_child_spec_that_cannot_be_narrowed_is_refused(ledger, monkeypatch):
    from types import SimpleNamespace

    from kiro_crew import execution_context
    from kiro_crew.agent_sdk import kas_tool_view

    def _boom(_block):
        raise RuntimeError("narrowing failed")

    monkeypatch.setattr(kas_tool_view, "permissions_block_readable", _boom)
    spec = {"allowedTools": ["fs_read"]}
    with pytest.raises(mate_held_rules.HeldLimitsNotApplied):
        _held_child(ledger, monkeypatch, "fs_read", spec)

    # Control: the same fault on a session whose Bot holds nothing changes nothing.
    monkeypatch.setattr(
        execution_context, "read_session_execution", lambda key: SimpleNamespace(member_id="other")
    )
    assert mate_held_rules.held_child_spec(spec, "k") is spec


def test_a_record_read_error_leaves_the_child_spec_as_the_gate_reads_it(ledger, monkeypatch):
    """An error reading the record reads as nothing held, in the child and the gate alike."""

    def _boom(*_a, **_k):
        raise RuntimeError("record read failed")

    _hold(ledger, "fs_read")
    monkeypatch.setattr(mate_held_rules, "_bot_rules", _boom)
    spec = {"allowedTools": ["fs_read"]}
    assert mate_held_rules.held_child_spec(spec, "k") is spec
    assert mate_held_rules.held_for(_event("read"), session_key="k") == []
