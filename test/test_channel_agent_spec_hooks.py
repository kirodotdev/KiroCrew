"""An agent spec's own PreToolUse hooks on an agent-channel member's permission requests.

On a backend that never receives the spec's ``hooks`` (KAS here), Crew fires them
itself, and the projection turns every call such a hook covers into a permission
request so the hook can run first. A channel member answers that request in
``channel._stream_task``: the spec's guard must refuse it before channel trust,
YOLO, a command grant or the approval card can let it through.

These drive the real ``_stream_task`` with the spec on disk and the real hook
store, on a provider double that streams one shell call. The hook process
(``run_script_hook``), the hooks' governance check and the user's own hook files
are stubbed out.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.config.paths as paths_mod
import kiro_crew.hooks as hooks_mod
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_GOOSE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.channel import _stream_task
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHookResult, ScriptHookStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.tool_permission import SPEC_HOOKS_UNREADABLE

_AGENT = "kas-channel-member"


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


@pytest.fixture
def runs(monkeypatch) -> list[tuple[str, bool]]:
    """``(command, from the spec)`` of every hook run; ``guard`` exits 2."""
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": None)

    async def run(hook, context="", hook_event=None, **_kwargs):
        seen.append((hook.command, hook.id.startswith("spec:")))
        code = 2 if hook.command == "guard" else 0
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            exit_code=code,
            stderr="denied by the guard" if code == 2 else "",
        )

    monkeypatch.setattr(hooks_mod, "run_script_hook", run)
    monkeypatch.setattr(spec_hooks, "_user_hooks_dir", lambda: None)
    return seen


@pytest.fixture
def sel_rows(monkeypatch) -> MagicMock:
    sel_mock = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
    return sel_mock


def _write_spec(command: str, *, raw: str | None = None, matcher: str = "execute_bash") -> None:
    agents = paths_mod.kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    hook = {"matcher": matcher, "command": command} if matcher else {"command": command}
    spec = {"name": _AGENT, "prompt": "p", "tools": ["*"], "hooks": {"preToolUse": [hook]}}
    (agents / f"{_AGENT}.json").write_text(raw or json.dumps(spec), encoding="utf-8")


class _Client:
    def __init__(self, tmp_path, backend: str) -> None:
        self.capabilities = capabilities_for(backend)
        self.cwd = str(tmp_path)
        # Only a KAS session records the agent it runs; every runtime keeps the
        # one it started a session with when that session named none.
        self.kas_projected_agent = _AGENT if backend == ACP_BACKEND_KAS else ""
        self._agent = _AGENT
        self.approve_tool = AsyncMock()
        self.reject_tool = AsyncMock()

    async def stream(self, message):
        yield LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Say hi",
            tool_kind="execute",
            tool_input=json.dumps({"command": "echo hi"}),
            request_id="r1",
            tool_call_id="t1",
            is_shell=True,
            harness_tool_id="run_command",
        )
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")


async def _run(tmp_path, backend: str = ACP_BACKEND_KAS, agent_name: str = _AGENT) -> _Client:
    """One member turn on a trusted channel, which auto-approves what reaches its tier."""
    client = _Client(tmp_path, backend)
    agent = SimpleNamespace(
        id="a1",
        role="dev",
        agent_name=agent_name,
        session_key="channel:c1:a1",
        _approval_future=None,
        _trusted_commands=set(),
        _trusted_bases=set(),
        _pending_approval_command="",
    )
    channel = SimpleNamespace(id="c1", trusted=True, members={}, _broadcast=MagicMock())
    channel.post = AsyncMock()
    await _stream_task(agent, channel, client, "run echo hi")
    return client


@pytest.mark.asyncio
async def test_a_spec_pre_tool_use_deny_refuses_before_channel_trust(tmp_path, runs, sel_rows):
    """The repro: a trusted channel must not auto-approve past the spec's exit-2 guard."""
    _write_spec("guard")
    client = await _run(tmp_path)
    assert runs == [("guard", True)]
    client.reject_tool.assert_awaited_once_with("r1")
    client.approve_tool.assert_not_awaited()
    outcomes = [kw.get("outcome") for _, kw in sel_rows.log_tool_invocation.call_args_list]
    assert "rejected_spec_hook" in outcomes


@pytest.mark.asyncio
async def test_a_call_the_spec_allows_still_reaches_channel_trust(tmp_path, runs, sel_rows):
    _write_spec("allow")
    client = await _run(tmp_path)
    assert runs == [("allow", True)]
    client.approve_tool.assert_awaited_once_with("r1")
    client.reject_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_kiro_cli_runs_the_spec_itself_so_crew_fires_none(tmp_path, runs, sel_rows):
    _write_spec("guard")
    client = await _run(tmp_path, ACP_BACKEND_KIRO)
    assert runs == []
    client.approve_tool.assert_awaited_once_with("r1")


@pytest.mark.asyncio
async def test_an_unreadable_spec_refuses_every_request(tmp_path, runs, sel_rows):
    _write_spec("guard", raw="{not json")
    client = await _run(tmp_path)
    client.reject_tool.assert_awaited_once_with("r1")
    client.approve_tool.assert_not_awaited()
    refusals = [
        kw.get("metadata")
        for _, kw in sel_rows.log_tool_invocation.call_args_list
        if kw.get("outcome") == "rejected_spec_hook"
    ]
    assert refusals == [{"reason": SPEC_HOOKS_UNREADABLE}]


@pytest.mark.asyncio
@pytest.mark.parametrize("command,approved", [("allow", True), ("guard", False)])
async def test_an_unnamed_member_on_goose_is_gated_by_its_sessions_agent(
    tmp_path, runs, sel_rows, command, approved
):
    """A preset member names no agent, and a goose session records none: the gate
    reads the agent the session started with, rather than refusing every call."""
    _write_spec(command, matcher="")
    client = await _run(tmp_path, ACP_BACKEND_GOOSE, agent_name="")
    assert runs == [(command, True)]
    assert client.approve_tool.await_count == int(approved)
    assert client.reject_tool.await_count == int(not approved)


@pytest.mark.asyncio
async def test_a_hooks_page_pre_tool_use_hook_does_not_gate_an_agent_channel(
    tmp_path, runs, sel_rows, monkeypatch
):
    """Only the spec's own hooks join this gate; the Hooks page's are unchanged here."""
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    store = ScriptHookStore(hooks_dir)
    store.create({"name": "page-guard", "event": HOOK_EVENT_PRE_TOOL_USE, "command": "guard"})
    monkeypatch.setattr(hooks_mod, "_global_script_hook_store", store)
    _write_spec("allow")
    client = await _run(tmp_path)
    assert runs == [("allow", True)]
    client.approve_tool.assert_awaited_once_with("r1")
