"""A running workflow step reports the tool call it last started as ``agent_progress``.

Every executor path tells the runner each tool call a step starts
(``agent_exec.step_on_event``); the runner sends the first at once and a newer one
at most every ``PROGRESS_EVERY_SECS``, on a timer, and drops a pending one when
the call settles. The run tree shows the tool on a running row.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import kiro_crew.workflows.agent_exec as agent_exec
import kiro_crew.workflows.agent_pool as agent_pool
import kiro_crew.workflows.runner as runner_mod
from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE, EVENT_TOOL_RESULT, AcpEvent
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.workflows.runner import WorkflowRunner

pytestmark = pytest.mark.asyncio

_ONE_STEP = 'META = {"name": "p"}\nasync def workflow(ctx):\n    return await ctx.agent("do it")\n'


class _Provider:
    def __init__(self, key: str) -> None:
        self.key = key

    async def new_conversation(self) -> None:
        return None

    def is_process_alive(self) -> bool:
        return True

    def set_delete_delay(self, seconds: float) -> None:
        return None


class _Sessions:
    async def get_or_create(self, key: str, **_kw: Any) -> tuple[_Provider, bool, bool]:
        return _Provider(key), False, False

    def release(self, key: str, *, cleanup: bool = False) -> None:
        return None

    async def reset(self, key: str, *, ends_conversation: bool = False) -> None:
        return None

    async def end_children_for(self, key: str) -> None:
        return None

    async def run_identity_check(self) -> None:
        return None

    def is_retiring(self, key: str) -> bool:
        return False

    async def recycle_if_sandbox_tightened(self, key: str) -> bool:
        return False

    async def destroy(self, key: str) -> None:
        return None


async def _two_tool_calls(_provider: Any, _msg: str, *, on_event: Any = None, **_k: Any) -> str:
    """A turn that starts ``Read a.txt``, updates it, then starts a call with no title."""
    for event in (
        AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", title="Read a.txt"),
        AcpEvent(kind=EVENT_TOOL_CALL_UPDATE, tool_call_id="tc-1", title="Read a.txt (2 KB)"),
        AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", title="Read a.txt"),
        AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", tool_final=True),
        AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-2", tool_name="shell"),
    ):
        await on_event(event)
    return "done"


@pytest.mark.parametrize("executor", ["cold", "pooled", "unpooled"])
async def test_every_executor_reports_each_tool_call_the_step_starts_once(
    executor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reported: list[str] = []
    opts: dict[str, Any] = {"_on_progress": reported.append}
    if executor == "cold":
        monkeypatch.setattr(agent_exec, "stream_and_collect", _two_tool_calls)
        fn = agent_exec.build_agent_fn(_Sessions(), run_id="wf_x")
        assert await fn("do it", opts) == "done"
    else:
        monkeypatch.setattr(agent_pool, "stream_and_collect", _two_tool_calls)
        fn, pool = agent_pool.build_pooled_agent_fn(_Sessions(), run_id="wf_x", max_workers=1)
        if executor == "unpooled":
            opts["session"] = "chain-A"
        try:
            assert await fn("do it", opts) == "done"
        finally:
            await pool.shutdown()

    assert reported == ["Read a.txt", "shell"]


async def test_a_step_reports_its_first_tool_call_on_the_run_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_exec, "stream_and_collect", _two_tool_calls)
    fn = agent_exec.build_agent_fn(_Sessions(), run_id="wf_r")

    res = await WorkflowRunner(agent_fn=fn, audit=lambda *a, **k: None).run(
        _ONE_STEP, run_id="wf_r", now="2026-11-25T00:00:00Z"
    )

    assert res.ok and res.result == "done", res.error
    types = [e.type for e in res.events]
    progress = [e for e in res.events if e.type == "agent_progress"]
    assert [(p.data["agent_id"], p.data["turns"], p.data["last_tool"]) for p in progress] == [
        ("a0", 1, "Read a.txt")
    ]
    assert progress[0].data["elapsed_s"] >= 0
    assert (
        types.index("agent_started") < types.index("agent_progress") < types.index("agent_finished")
    )


def _progress_timers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, Any, Any]]:
    """Every ``(delay, callback, handle)`` the runner's progress reporter schedules."""
    loop = asyncio.get_running_loop()
    timers: list[tuple[float, Any, Any]] = []
    real_call_later = loop.call_later

    def call_later(delay: float, callback: Any, *args: Any) -> asyncio.TimerHandle:
        handle = real_call_later(delay, callback, *args)
        if getattr(callback, "__qualname__", "").startswith("_RunContext._progress_reporter"):
            timers.append((delay, callback, handle))
        return handle

    monkeypatch.setattr(loop, "call_later", call_later)
    return timers


async def test_a_newer_tool_call_is_sent_on_a_timer_when_the_step_then_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timers = _progress_timers(monkeypatch)

    async def fn(_prompt: str, opts: dict) -> str:
        for title in ("A", "B", "C"):
            opts["_on_progress"](title)
        # The step starts nothing more: only the timer can send C. Fire it as the loop would.
        for _delay, callback, handle in timers:
            handle.cancel()
            callback()
        return "done"

    res = await WorkflowRunner(agent_fn=fn, audit=lambda *a, **k: None).run(
        _ONE_STEP, run_id="wf_t", now="2026-11-25T00:00:00Z"
    )

    assert res.ok, res.error
    assert [
        (e.data["turns"], e.data["last_tool"]) for e in res.events if e.type == "agent_progress"
    ] == [(1, "A"), (3, "C")]
    assert len(timers) == 1 and 0 < timers[0][0] <= runner_mod.PROGRESS_EVERY_SECS


async def test_a_pending_progress_is_cancelled_when_the_call_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timers = _progress_timers(monkeypatch)

    async def fn(_prompt: str, opts: dict) -> str:
        opts["_on_progress"]("A")
        opts["_on_progress"]("B")
        return "done"

    res = await WorkflowRunner(agent_fn=fn, audit=lambda *a, **k: None).run(
        _ONE_STEP, run_id="wf_c", now="2026-11-25T00:00:00Z"
    )

    assert res.ok, res.error
    assert [e.data["last_tool"] for e in res.events if e.type == "agent_progress"] == ["A"]
    assert len(timers) == 1 and timers[0][2].cancelled()


async def test_a_reported_tool_title_is_redacted_then_cut() -> None:
    token = github_classic(seed=42127)
    title = f"Running: curl -H 'Authorization: token {token}' " + "x" * 200

    async def fn(_prompt: str, opts: dict) -> str:
        opts["_on_progress"](title)
        return "done"

    res = await WorkflowRunner(agent_fn=fn, audit=lambda *a, **k: None).run(
        _ONE_STEP, run_id="wf_s", now="2026-11-25T00:00:00Z"
    )

    assert res.ok, res.error
    shown = [e.data["last_tool"] for e in res.events if e.type == "agent_progress"]
    assert len(shown) == 1
    assert token not in shown[0]
    assert shown[0].startswith("Running: curl") and shown[0].endswith("…")
    assert len(shown[0]) == runner_mod.PROGRESS_MAX_TOOL_CHARS + 1


class _RefiningProvider(_Provider):
    """A provider on claude, whose ``tool_call_update`` refines a call's first title."""

    capabilities = capabilities_for(ACP_BACKEND_CLAUDE)


async def test_a_refining_harness_renames_its_newest_call_without_counting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timers = _progress_timers(monkeypatch)
    provider = _RefiningProvider("wf:wf_n:0")

    def fire() -> None:
        for _delay, callback, handle in timers:
            if not handle.cancelled():
                handle.cancel()
                callback()

    async def fn(_prompt: str, opts: dict) -> str:
        on_event = agent_exec.step_on_event(provider.key, [], opts, provider)
        for event in (
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", title="Terminal"),
            AcpEvent(kind=EVENT_TOOL_CALL_UPDATE, tool_call_id="tc-1", title="ls -la"),
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-2", title="Read File"),
            AcpEvent(kind=EVENT_TOOL_CALL_UPDATE, tool_call_id="tc-1", title="ls -la /"),
            AcpEvent(kind=EVENT_TOOL_CALL_UPDATE, tool_call_id="tc-2", title="Read b.txt"),
        ):
            await on_event(event)
        fire()
        # The same title again changes nothing, so no further send is scheduled.
        await on_event(
            AcpEvent(kind=EVENT_TOOL_CALL_UPDATE, tool_call_id="tc-2", title="Read b.txt")
        )
        return "done"

    res = await WorkflowRunner(agent_fn=fn, audit=lambda *a, **k: None).run(
        _ONE_STEP, run_id="wf_n", now="2026-11-25T00:00:00Z"
    )

    assert res.ok, res.error
    assert [
        (e.data["turns"], e.data["last_tool"]) for e in res.events if e.type == "agent_progress"
    ] == [(1, "Terminal"), (2, "Read b.txt")]
    assert len(timers) == 1
