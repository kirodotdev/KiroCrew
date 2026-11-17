"""An agent spec's own hooks on a cron turn, on a backend that never runs them (P0-11),
and the spec keys nothing carries named off the dashboard (P1-9).

kiro-cli runs a spec's ``hooks`` on every session. On KAS Crew fires them in its
place; a cron turn does through :class:`kiro_crew.cron_service.turn_hooks.CronTurnHooks`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import kiro_crew.hooks as hooks_mod
from kiro_crew import llm_helpers
from kiro_crew.acp.types import AcpEvent
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.cron_service.turn_hooks import CronTurnHooks
from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHook,
    ScriptHookResult,
)
from kiro_crew.messaging import script_hooks
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST


def _hook(event: str, command: str, matcher: str = "") -> ScriptHook:
    return ScriptHook(
        id=f"spec:a1:{event}:0",
        name=command,
        event=event,
        matcher=matcher,
        command=command,
        spec_origin="user",
    )


class _Provider:
    def __init__(self) -> None:
        self.approved: list = []
        self.rejected: list = []
        self.session_id = "acp-1"
        self.cwd = "/w"
        self.capabilities = capabilities_for(ACP_BACKEND_KAS)

    async def stream(self, message, **_kw):
        yield AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq1", title="Running: ls")
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    async def approve_tool(self, request_id, **_kw):
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)


@pytest.fixture
def ran(monkeypatch) -> dict:
    """Every hook that ran (event, command, session_key), and the exit each gives."""
    record: dict = {"runs": [], "exit": {}}

    async def fake_run(hook, context="", hook_event=None, cwd=None, **_kw):
        record["runs"].append((hook.event, hook.command, (hook_event or {}).get("session_key")))
        stdout = f"from {hook.command}" if hook.event != HOOK_EVENT_PRE_TOOL_USE else ""
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            exit_code=record["exit"].get(hook.command, 0),
            stdout=stdout,
        )

    # The store's methods run on the facade's globals (hook_runtime/__init__).
    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    # No Hooks-page store in this process: only the spec's own hooks may run.
    monkeypatch.setattr(hooks_mod, "get_global_hook_store", lambda: None)
    monkeypatch.setattr(script_hooks, "get_global_hook_store", lambda: None)
    return record


def _spec(monkeypatch, hooks, *, lost=(), stale=False):
    async def fake_turn_spec_hooks(provider, agent, **_kw):
        return spec_hooks.TurnSpecHooks(list(hooks), "/w", False, True, tuple(lost))

    async def fake_stale(provider, agent, **_kw):
        return stale

    monkeypatch.setattr(spec_hooks, "turn_spec_hooks", fake_turn_spec_hooks)
    monkeypatch.setattr(spec_hooks, "hook_projection_stale", fake_stale)


def _prepare(provider, *, fresh=False) -> CronTurnHooks:
    return asyncio.run(CronTurnHooks.prepare(provider, "a1", "cron:j1", "go", fresh_session=fresh))


def _collect(provider, hooks: CronTurnHooks) -> list:
    gate: list = []
    asyncio.run(
        llm_helpers.stream_and_collect(
            provider,
            "hello",
            approval_policy=llm_helpers.ToolApprovalPolicy.AUTO_APPROVE,
            on_tool_gate=lambda title, ok, security, why: gate.append((ok, security)),
            spec_hooks=hooks,
        )
    )
    return gate


@pytest.mark.parametrize(("exit_code", "blocked"), [(2, True), (0, False), (-1, False)])
def test_a_cron_turn_gates_its_permission_requests_on_the_specs_pre_tool_hooks(
    monkeypatch, ran, exit_code, blocked
):
    ran["exit"]["guard.sh"] = exit_code
    _spec(monkeypatch, [_hook(HOOK_EVENT_PRE_TOOL_USE, "guard.sh", "*")])
    provider = _Provider()
    gate = _collect(provider, _prepare(provider))
    # Exit 2 blocks, as under kiro-cli; a hook that crashed lets the call run.
    assert (provider.rejected, provider.approved) == ((["rq1"], []) if blocked else ([], ["rq1"]))
    # A spec-hook refusal says nothing about the job: it does not move auto-pause.
    assert gate == [(not blocked, False)]
    # The run is governed under the cron session's own key.
    assert (HOOK_EVENT_PRE_TOOL_USE, "guard.sh", "cron:j1") in ran["runs"]


def test_an_unreadable_spec_blocks_a_cron_turns_tools_as_in_a_dashboard_chat(monkeypatch, ran):
    async def unreadable(provider, agent, **_kw):
        return spec_hooks.TurnSpecHooks([], "/w", True, True)

    monkeypatch.setattr(spec_hooks, "turn_spec_hooks", unreadable)
    provider = _Provider()
    _collect(provider, _prepare(provider))
    assert provider.rejected == ["rq1"] and provider.approved == []


def test_the_specs_prompt_and_stop_hooks_fire_on_a_cron_turn(monkeypatch, ran):
    _spec(
        monkeypatch,
        [
            _hook(HOOK_EVENT_AGENT_SPAWN, "spawn.sh"),
            _hook(HOOK_EVENT_USER_PROMPT_SUBMIT, "submit.sh"),
            _hook(HOOK_EVENT_STOP, "stop.sh"),
        ],
    )
    hooks = _prepare(_Provider(), fresh=True)
    assert asyncio.run(hooks.prompt_context()) == ["from spawn.sh", "from submit.sh"]
    asyncio.run(hooks.stop("done"))
    assert [r[:2] for r in ran["runs"]] == [
        (HOOK_EVENT_AGENT_SPAWN, "spawn.sh"),
        (HOOK_EVENT_USER_PROMPT_SUBMIT, "submit.sh"),
        (HOOK_EVENT_STOP, "stop.sh"),
    ]


def test_a_stale_batch_runs_no_spec_hooks_and_says_so(monkeypatch, ran):
    ran["exit"]["guard.sh"] = 2
    _spec(monkeypatch, [_hook(HOOK_EVENT_PRE_TOOL_USE, "guard.sh", "*")], stale=True)
    provider = _Provider()
    hooks = _prepare(provider)
    _collect(provider, hooks)
    # Main's behaviour: no spec hook ran, so the call went through.
    assert ran["runs"] == [] and provider.approved == ["rq1"]
    assert "none of the agent's hooks ran" in hooks.notices()


def test_a_mid_turn_switch_rereads_the_spec_for_the_new_agent(monkeypatch, ran):
    seen: list = []

    async def fake_turn_spec_hooks(provider, agent, **_kw):
        seen.append(agent)
        return spec_hooks.TurnSpecHooks([], "/w", False, True)

    monkeypatch.setattr(spec_hooks, "turn_spec_hooks", fake_turn_spec_hooks)
    hooks = _prepare(_Provider())
    asyncio.run(hooks.observe(AcpEvent(kind="agent_switched", text="a2")))
    assert seen == ["a1", "a2"] and hooks.agent == "a2"


# ── P1-9: the spec keys nothing carries ───────────────────────────────────────


def test_turn_spec_hooks_returns_the_keys_nothing_carries(monkeypatch):
    seen: dict = {}

    def fake_fired(agent, project_dir=None, **kw):
        seen.update(kw)
        return [], ["toolsSettings"], 0

    monkeypatch.setattr(spec_hooks, "crew_fired_spec_hooks", fake_fired)
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd="/w")
    out = asyncio.run(spec_hooks.turn_spec_hooks(provider, "a1"))
    assert out.lost == ("toolsSettings",)
    # Read as the dashboard chat reads it: KAS translates toolsSettings.
    assert seen["translates_tools_settings"] is True


def test_a_cron_turn_names_the_lost_keys_as_the_dashboard_does(monkeypatch, ran):
    from kiro_crew.dashboard.chat_turn.tool_approval import _spec_keys_notice

    _spec(monkeypatch, [], lost=("permissions",))
    hooks = _prepare(_Provider())
    assert hooks.notices() == _spec_keys_notice("a1", ["permissions"])


def test_a_hooks_page_pre_tool_hook_stays_informational_on_a_cron_turn(monkeypatch, ran):
    """Only the spec's PreToolUse hooks gate; a failing Hooks-page one does not refuse."""
    from kiro_crew.hooks import ScriptHookStore

    page = ScriptHookStore(load=False)
    page._hooks = {"audit": ScriptHook(id="audit", event=HOOK_EVENT_PRE_TOOL_USE, command="audit")}
    ran["exit"]["audit"] = 1
    monkeypatch.setattr(hooks_mod, "get_global_hook_store", lambda: page)
    # A spec with only a Stop hook does not gate the turn at all.
    _spec(monkeypatch, [_hook(HOOK_EVENT_STOP, "stop.sh")])
    provider = _Provider()
    hooks = _prepare(provider)
    assert hooks.gated is False
    _collect(provider, hooks)
    assert provider.approved == ["rq1"] and provider.rejected == []
    # With a spec PreToolUse hook that allows, the failing page hook still does not refuse.
    _spec(monkeypatch, [_hook(HOOK_EVENT_PRE_TOOL_USE, "guard.sh", "*")])
    hooks = _prepare(provider)
    event = AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq2", title="Running: ls")
    before = len(ran["runs"])
    assert hooks.gated is True and asyncio.run(hooks.refusal(event)) is None
    # The gate ran the spec's hook alone, never the Hooks-page one.
    assert [r[1] for r in ran["runs"][before:]] == ["guard.sh"]
