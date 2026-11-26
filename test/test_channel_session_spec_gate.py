"""An agent spec's own PreToolUse guard on the unattended turns of a live session.

On a backend where Kiro Crew, not the harness, fires the agent spec's ``hooks`` (KAS),
a sub-agent's report injected into its parent's session (a channel or a cron run) and a
plain Slack auto-nudge both run through ``stream_and_collect``. Each asks the spec's PreToolUse
hooks first (``channel_spec_hooks.SessionSpecGate``), read as a native Slack turn reads
them, so a guard that exits 2 stops the call before any approval.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from slack_gateway_coverage_helpers import _loop, _slack_nudge_orchestrator

import kiro_crew.hooks as hooks_mod
import kiro_crew.sel as sel_mod
import kiro_crew.slack.gateway as gw
from kiro_crew.acp.types import AcpEvent
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHook, ScriptHookResult
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST

_KEY = "slack:111.222"


class _Session:
    """A live session on *backend* whose agent asks to run one shell command."""

    def __init__(self, backend: str) -> None:
        self.capabilities = capabilities_for(backend)
        self._agent = "a1"
        self.session_id = "acp-1"
        self.cwd = "/w"
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def stream(self, message, **_kw):
        yield AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq1", title="Running: echo hi")
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    async def approve_tool(self, request_id, **_kw):
        self.approved.append(request_id)
        return True

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)


@pytest.fixture
def guard(monkeypatch) -> dict:
    """Agent ``a1``'s spec: one PreToolUse guard on every tool; ``exit`` is its exit code."""
    record: dict = {"exit": 2, "runs": [], "agents": [], "rows": []}

    async def fake_turn_spec_hooks(provider, agent, **_kw):
        record["agents"].append(agent)
        hook = ScriptHook(
            id="spec:a1:preToolUse:0",
            name="guard.sh",
            event=HOOK_EVENT_PRE_TOOL_USE,
            matcher="*",
            command="guard.sh",
            spec_origin="user",
        )
        return spec_hooks.TurnSpecHooks([hook], "/w", False, True)

    async def fake_run(hook, context="", hook_event=None, cwd=None, **_kw):
        record["runs"].append((hook.command, (hook_event or {}).get("session_key")))
        return ScriptHookResult(
            hook_id=hook.id, hook_name=hook.name, event=hook.event, exit_code=record["exit"]
        )

    class _Sel:
        """Keeps each tool row; any other audit call a late ``sel()`` makes is dropped."""

        def log_tool_invocation(self, **row):
            record["rows"].append(row)

        def __getattr__(self, _name):
            return lambda *a, **k: None

    monkeypatch.setattr(spec_hooks, "turn_spec_hooks", fake_turn_spec_hooks)
    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    # No Hooks-page store in this process: only the spec's own guard may run.
    monkeypatch.setattr(hooks_mod, "get_global_hook_store", lambda: None)
    monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
    return record


def _spec_hook_denials(record: dict) -> list[dict]:
    return [r for r in record["rows"] if (r.get("metadata") or {}).get("mechanism") == "spec_hook"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "exit_code", "parent_key", "refused", "runs"),
    [
        (ACP_BACKEND_KAS, 2, _KEY, True, 1),
        (ACP_BACKEND_KAS, 2, "cron:j1", True, 1),
        (ACP_BACKEND_KAS, 0, _KEY, False, 1),
        (ACP_BACKEND_KIRO, 2, _KEY, False, 0),
    ],
)
async def test_a_subagent_report_turn_asks_the_parent_agents_spec_guard(
    guard, backend, exit_code, parent_key, refused, runs
):
    guard["exit"] = exit_code
    parent = _Session(backend)
    orch = _slack_nudge_orchestrator()
    label = parent_key.split(":", 1)[0]
    await orch._inject_with_retry(SimpleNamespace(id="sa-1"), parent, "report", parent_key, label)
    # Exit 2 refuses the call before the report turn's auto-approve, in a channel or
    # a cron parent; exit 0 lets it run as before; kiro-cli runs the spec's hooks
    # itself, so Crew never does.
    assert (parent.rejected, parent.approved) == ((["rq1"], []) if refused else ([], ["rq1"]))
    assert guard["runs"] == [("guard.sh", parent_key)] * runs
    assert guard["agents"] == ["a1"] * runs
    assert len(_spec_hook_denials(guard)) == int(refused)


@pytest.mark.asyncio
async def test_a_plain_slack_nudge_asks_the_thread_agents_spec_guard(guard, monkeypatch):
    thread = _Session(ACP_BACKEND_KAS)
    orch = _slack_nudge_orchestrator()
    orch.sessions.get_or_create = AsyncMock(return_value=(thread, False, False))
    approver = AsyncMock(return_value=True)
    orch._interactive_approval = lambda *a, **k: approver

    async def built(_fn, *_a, **_k):
        return "MSG", None

    monkeypatch.setattr(gw, "run_in_embed_pool", built)
    await orch._fire_slack_nudge(_loop(key=_KEY), None)
    # The guard ran once, under the thread's key, and refused the call before
    # anyone was asked to approve it.
    assert guard["runs"] == [("guard.sh", _KEY)]
    assert (thread.rejected, thread.approved) == (["rq1"], [])
    approver.assert_not_awaited()
    assert len(_spec_hook_denials(guard)) == 1
