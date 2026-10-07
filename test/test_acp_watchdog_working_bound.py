"""The tool watchdog bounds an opaque MCP tool's WORKING reading.

An opaque MCP tool reads WORKING on any movement in the runtime's tree, which
a warm sibling MCP server produces too. So that one reading is bounded by
``watchdog.tool_stall_hard_cap_secs``; a matched shell child, the declared
``wait`` verdict and a kirocrew-core tool that pings the session keepalive
keep deferring. Recovery is the session-scoped ``session/cancel``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_acp_stale_recovery import _SilentQueue

from kiro_crew.acp.liveness import (
    EVIDENCE_MCP_SUBTREE_ACTIVE,
    VERDICT_WORKING,
    ToolCallState,
)
from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.types import STOP_REASON_TOOL_STALL
from kiro_crew.session_directive import CORE_MCP_SERVER

_MCP_ACTIVE = (VERDICT_WORKING, f"{EVIDENCE_MCP_SUBTREE_ACTIVE} (io +512B cpu +3t)")


def _settings(hard_cap: float) -> WatchdogSettings:
    return WatchdogSettings(
        check_after_secs=0.01,
        tool_stall_suspect_secs=999.0,
        tool_stall_hard_cap_secs=hard_cap,
    )


class _Oracle:
    """Fixed verdict that counts consults made after the hard cap elapsed.

    The deferral tests wait on this count instead of on a deadline, so they
    only pass once the watchdog really judged the reading past the cap.
    """

    def __init__(self, verdict, hard_cap: float, wanted: int = 3) -> None:
        self._verdict = verdict
        self._hard_cap = hard_cap
        # Started at the first consult, which comes after the turn's own idle
        # clock starts, so "past the cap" here is never earlier than the
        # handle's own reading of it.
        self._cap_at: float | None = None
        self._wanted = wanted
        self.past_cap = 0
        self.done = threading.Event()

    def __call__(self, pid, tool):
        now = time.monotonic()
        if self._cap_at is None:
            self._cap_at = now + self._hard_cap
        if now > self._cap_at:
            self.past_cap += 1
            if self.past_cap >= self._wanted:
                self.done.set()
        return self._verdict


def _handle(wd: WatchdogSettings, verdict, tool: ToolCallState) -> AcpSessionHandle:
    rt = MagicMock()
    rt._last_activity = time.monotonic()
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=wd)
    handle._turn_done.clear()
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._inflight_tool = tool
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_tool = verdict if callable(verdict) else (lambda pid, t: verdict)
    return handle


async def _drain(handle: AcpSessionHandle, timeout: float) -> list:
    return [ev async for ev in handle._dispatch_events(1, timeout)]


async def _assert_deferred_past_cap(handle: AcpSessionHandle, oracle: _Oracle) -> None:
    """Run the loop until the oracle was consulted past the cap, then stop it."""
    events: list = []

    async def _run() -> None:
        async for ev in handle._dispatch_events(1, 60.0):
            events.append(ev)

    task = asyncio.create_task(_run())
    try:
        for _ in range(1000):
            if oracle.done.is_set() or task.done():
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert oracle.done.is_set(), f"only {oracle.past_cap} consult(s) past the cap"
    handle._runtime.send_notification.assert_not_awaited()
    assert all(ev.stop_reason != STOP_REASON_TOOL_STALL for ev in events)


def _opaque_tool() -> ToolCallState:
    return ToolCallState(title="ReadInternalWebsites", command="{}")


@pytest.mark.asyncio
async def test_opaque_mcp_working_is_cancelled_past_the_hard_cap():
    handle = _handle(_settings(hard_cap=0.05), _MCP_ACTIVE, _opaque_tool())
    metrics: list[tuple] = []
    handle._emit_watchdog_metric = lambda action, verdict, evidence, idle, **kw: metrics.append(  # type: ignore[method-assign]
        (action, verdict, kw.get("window"))
    )

    events = await _drain(handle, timeout=5.0)

    assert handle._runtime.send_notification.await_args.args[0] == "session/cancel"
    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert "verdict=working" in events[-1].text
    assert ("cancel", VERDICT_WORKING, "working_cap") in metrics


@pytest.mark.asyncio
async def test_opaque_mcp_working_defers_inside_the_hard_cap():
    # hard_cap 0 for the counter only: every consult counts, the handle's own
    # cap stays far away, so these are consults made inside the cap.
    oracle = _Oracle(_MCP_ACTIVE, hard_cap=0.0)
    handle = _handle(_settings(hard_cap=999.0), oracle, _opaque_tool())

    await _assert_deferred_past_cap(handle, oracle)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence",
    [
        "shell child 4242 matched command",
        "shell child 4242 alive (io +10B cpu +1t)",
        "wait tool declared 600s (30s elapsed)",
    ],
)
async def test_other_working_readings_are_never_bounded(evidence):
    oracle = _Oracle((VERDICT_WORKING, evidence), hard_cap=0.05)
    handle = _handle(_settings(hard_cap=0.05), oracle, _opaque_tool())

    await _assert_deferred_past_cap(handle, oracle)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["spawn_sub_agents", "kirocrew-core___spawn_sub_agents"])
async def test_keepalive_tool_is_not_cut_off(tool_name):
    tool = ToolCallState(
        title="spawn_sub_agents", tool_name=tool_name, mcp_server_name=CORE_MCP_SERVER
    )
    oracle = _Oracle(_MCP_ACTIVE, hard_cap=0.05)
    handle = _handle(_settings(hard_cap=0.05), oracle, tool)

    await _assert_deferred_past_cap(handle, oracle)


@pytest.mark.parametrize(
    ("tool", "trusted"),
    [
        (ToolCallState(tool_name="spawn_sub_agents", mcp_server_name=CORE_MCP_SERVER), True),
        # wait never reads mcp subtree active (its declared verdict answers first).
        (ToolCallState(tool_name="wait", mcp_server_name=CORE_MCP_SERVER), False),
        # The title is model-authored and never selects the exemption.
        (ToolCallState(title="spawn_sub_agents"), False),
        # Another server's tool of the same name is not the core tool.
        (ToolCallState(tool_name="spawn_sub_agents", mcp_server_name="other"), False),
        (ToolCallState(tool_name="spawn_run", mcp_server_name=CORE_MCP_SERVER), False),
        (
            ToolCallState(
                tool_name="spawn_sub_agents", mcp_server_name=CORE_MCP_SERVER, is_shell=True
            ),
            False,
        ),
    ],
)
def test_keepalive_identity_reads_only_the_adapter_identity(tool, trusted):
    assert tool.is_trusted_keepalive_tool() is trusted
