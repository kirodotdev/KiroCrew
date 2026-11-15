"""The managed-readiness barrier waits for a server that is queued, not stuck.

KAS connects one process's MCP servers a few at a time and reports a queued server
as ``connecting``; Crew's managed servers are merged last, so on an agent with many
servers they can still be queued when the plain budget runs out. These tests pin
that the barrier keeps waiting while the host's connect queue visibly moves (any
session's snapshot settles more servers), up to a bounded allowance, and that every
other path keeps failing exactly where it failed before: no movement, a pending
server that is not ``connecting``, a runtime with no progress tracker.

The clock is scripted: the handle's queue replays frames at set times and raises
the wait's timeout at a set time, so no test sleeps or races a real deadline.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.acp import session_handle
from kiro_crew.acp.mcp_connect_progress import McpConnectProgress
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_handle_mcp import QUEUE_EXTENSION_SECS
from kiro_crew.acp.session_handle_runtime_contract import AcpRequestTimeout
from kiro_crew.acp.types import METHOD_KAS_MCP_STATUS, JsonRpcMessage

_SID = "sess-queue"
_OTHER = "sess-other"
_CLIENT = {"kiro": {"resource": {"source": {"origin": "client"}}}}
_PROFILE = {"kiro": {"resource": {"source": {"origin": "profile"}}}}
_BUDGET = 90.0
_CORE = "kirocrew-core"


def _status(*servers: dict, session: str = _SID) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS, params={"sessionId": session, "servers": list(servers)}
    )


def _crew(status: str) -> dict:
    entry: dict = {"name": _CORE, "status": status, "_meta": _CLIENT}
    if status == "connected":
        entry["tools"] = [{"name": "ping", "description": "probe", "disabled": False}]
    return entry


def _agent_servers(settled: int, total: int = 8) -> list[dict]:
    """An agent's own servers, the first *settled* of them connected."""
    return [
        {
            "name": f"agent-{i}",
            "status": "connected" if i < settled else "connecting",
            "_meta": _PROFILE,
        }
        for i in range(total)
    ]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _ScriptedQueue:
    """The handle's queue, replaying ``(at, frame)`` steps on a scripted clock.

    A frame step moves the clock to *at* and returns the frame, after feeding it to
    the runtime's progress tracker the way the reader loop does. A ``None`` frame
    moves the clock to *at* and raises the wait's timeout. Running out of steps is
    a test bug, reported instead of hanging.
    """

    def __init__(self, clock: _Clock, progress: McpConnectProgress | None, steps) -> None:
        self._clock = clock
        self._progress = progress
        self._steps = list(steps)

    async def get(self) -> JsonRpcMessage:
        while True:
            if not self._steps:
                raise AssertionError("the barrier read past the scripted frames")
            at, frame = self._steps.pop(0)
            self._clock.now = 1000.0 + at
            if frame is None:
                raise asyncio.TimeoutError
            if self._progress is not None:
                self._progress.observe(frame)
            # Another session's frame reaches the tracker only, as on the reader.
            if (frame.params or {}).get("sessionId") == _SID:
                return frame

    def put_nowait(self, _item) -> None:  # pragma: no cover - runtime death path only
        raise AssertionError("unexpected runtime death")


def _handle(monkeypatch, steps, *, tracked: bool = True) -> AcpSessionHandle:
    clock = _Clock()
    # Through the facade: its mirror forwards the patch to every owner that binds
    # ``time``, and its undo restores them all.
    monkeypatch.setattr(session_handle, "time", SimpleNamespace(monotonic=clock.monotonic))
    progress = McpConnectProgress() if tracked else None
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = _SID
    handle._mcp_report = McpSessionReport()
    handle._mcp_report.begin_session([])
    handle._queue = _ScriptedQueue(clock, progress, steps)  # type: ignore[assignment]
    handle._apply_init_notification = lambda *_a, **_k: None  # type: ignore[method-assign]
    if progress is not None:
        handle._runtime = SimpleNamespace(mcp_connect_progress=progress)
    return handle


async def _wait(handle: AcpSessionHandle) -> None:
    await handle.wait_mcp_ready((_CORE,), _BUDGET)


# ── the tracker ───────────────────────────────────────────────────────────────


def test_only_a_rise_in_settled_servers_moves_the_epoch():
    progress = McpConnectProgress()
    progress.observe(_status(_crew("connecting"), *_agent_servers(0)))
    assert progress.epoch == 0
    progress.observe(_status(_crew("connecting"), *_agent_servers(2)))
    assert progress.epoch == 1
    # The same snapshot again, and a reconnect that drops the count, are no movement.
    progress.observe(_status(_crew("connecting"), *_agent_servers(2)))
    progress.observe(_status(_crew("connecting"), *_agent_servers(1)))
    assert progress.epoch == 1
    progress.observe(_status(_crew("connecting"), *_agent_servers(1)))
    assert progress.epoch == 1
    progress.observe(_status(_crew("connecting"), *_agent_servers(3)))
    assert progress.epoch == 2


def test_another_sessions_settling_counts_and_other_frames_do_not():
    progress = McpConnectProgress()
    progress.observe(_status(*_agent_servers(1), session=_OTHER))
    assert progress.epoch == 1
    for frame in (
        JsonRpcMessage(id=7, method=METHOD_KAS_MCP_STATUS, params={"sessionId": _SID}),
        JsonRpcMessage(method="_kiro/tools/didChange", params={"sessionId": _SID, "tags": []}),
        JsonRpcMessage(method=METHOD_KAS_MCP_STATUS, params={"servers": _agent_servers(5)}),
        JsonRpcMessage(method=METHOD_KAS_MCP_STATUS, params={"sessionId": _SID, "servers": 3}),
        JsonRpcMessage(method=METHOD_KAS_MCP_STATUS, params=None),
    ):
        progress.observe(frame)
    assert progress.epoch == 1


def test_the_tracker_forgets_the_oldest_session_past_its_cap():
    progress = McpConnectProgress()
    for i in range(1100):
        progress.observe(_status(*_agent_servers(1), session=f"s-{i}"))
    assert progress.epoch == 1100
    assert len(progress._last) == 1024


def test_the_tracker_keeps_a_fixed_size_key_for_any_session_id():
    progress = McpConnectProgress()
    progress.observe(_status(*_agent_servers(1), session="x" * 1_000_000))
    assert progress.epoch == 1
    assert [len(key) for key in progress._last] == [16]


def test_a_reload_of_the_same_session_counts_its_servers_settling_again():
    # A live MCP reload or a mode switch starts the same session id over from none
    # settled; its rises are queue movement even below the earlier activation's count.
    progress = McpConnectProgress()
    progress.observe(_status(*_agent_servers(9, total=9)))
    assert progress.epoch == 1
    progress.observe(_status(_crew("connecting"), *_agent_servers(0, total=9)))
    progress.observe(_status(_crew("connecting"), *_agent_servers(4, total=9)))
    assert progress.epoch == 2


# ── the barrier ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_queued_server_starts_after_the_budget_while_the_queue_moves(monkeypatch):
    handle = _handle(
        monkeypatch,
        [
            (1, _status(_crew("connecting"), *_agent_servers(0))),
            (60, _status(_crew("connecting"), *_agent_servers(4))),
            # The budget ends with the queue still ahead of kirocrew-core.
            (_BUDGET, None),
            (120, _status(_crew("connecting"), *_agent_servers(8))),
            (130, _status(_crew("connected"), *_agent_servers(8))),
        ],
    )
    await _wait(handle)


@pytest.mark.asyncio
async def test_with_no_movement_the_start_fails_at_the_budget_as_before(monkeypatch):
    handle = _handle(
        monkeypatch,
        [
            (1, _status(_crew("connecting"), *_agent_servers(0))),
            (_BUDGET, None),
        ],
    )
    with pytest.raises(AcpRequestTimeout) as caught:
        await _wait(handle)
    assert str(caught.value) == (
        "KAS managed MCP readiness timed out after 90s: kirocrew-core: connecting"
    )


@pytest.mark.asyncio
async def test_movement_does_not_extend_a_wait_for_a_server_that_is_not_connecting(
    monkeypatch,
):
    # Connected without its tools: no queue stands between it and readiness.
    hidden = {"name": _CORE, "status": "connected", "_meta": _CLIENT}
    handle = _handle(
        monkeypatch,
        [
            (1, _status(hidden, *_agent_servers(0))),
            (60, _status(hidden, *_agent_servers(4))),
            (_BUDGET, None),
        ],
    )
    with pytest.raises(AcpRequestTimeout) as caught:
        await _wait(handle)
    assert "timed out after 90s: kirocrew-core: tools not advertised" in str(caught.value)


@pytest.mark.asyncio
async def test_an_extension_needs_new_movement_since_the_last_one(monkeypatch):
    handle = _handle(
        monkeypatch,
        [
            (1, _status(_crew("connecting"), *_agent_servers(0))),
            (60, _status(_crew("connecting"), *_agent_servers(4))),
            (_BUDGET, None),
            # Extended to +180; nothing settles in that window.
            (_BUDGET * 2, None),
        ],
    )
    with pytest.raises(AcpRequestTimeout) as caught:
        await _wait(handle)
    message = str(caught.value)
    assert message.startswith("KAS managed MCP readiness timed out after 180s: kirocrew-core")
    assert "agent.session_start_timeout_secs" in message


@pytest.mark.asyncio
async def test_after_an_extension_a_server_missing_its_tools_gets_no_queue_advice(monkeypatch):
    hidden = {"name": _CORE, "status": "connected", "_meta": _CLIENT}
    handle = _handle(
        monkeypatch,
        [
            (1, _status(_crew("connecting"), *_agent_servers(0))),
            (60, _status(_crew("connecting"), *_agent_servers(4))),
            (_BUDGET, None),
            (150, _status(hidden, *_agent_servers(8))),
            (_BUDGET * 2, None),
        ],
    )
    with pytest.raises(AcpRequestTimeout) as caught:
        await _wait(handle)
    message = str(caught.value)
    assert message == (
        "KAS managed MCP readiness timed out after 180s: kirocrew-core: tools not advertised"
    )


@pytest.mark.asyncio
async def test_a_queue_that_keeps_moving_still_fails_at_the_allowance(monkeypatch):
    ceiling = _BUDGET + QUEUE_EXTENSION_SECS
    steps: list = [(1, _status(_crew("connecting"), *_agent_servers(0, total=40)))]
    settled = 0
    at = 1.0
    while at < ceiling:
        at += 30
        settled += 1
        steps.append((min(at, ceiling), _status(_crew("connecting"), *_agent_servers(settled, 40))))
    steps.append((ceiling, None))
    handle = _handle(monkeypatch, steps)
    with pytest.raises(AcpRequestTimeout) as caught:
        await _wait(handle)
    assert f"timed out after {ceiling:g}s" in str(caught.value)


@pytest.mark.asyncio
async def test_another_sessions_settling_extends_the_wait(monkeypatch):
    handle = _handle(
        monkeypatch,
        [
            (1, _status(_crew("connecting"), *_agent_servers(0))),
            # An earlier session's servers, ahead in the same process queue, settle.
            (50, _status(*_agent_servers(3), session=_OTHER)),
            (_BUDGET, None),
            (100, _status(_crew("connected"), *_agent_servers(8))),
        ],
    )
    await _wait(handle)


@pytest.mark.asyncio
async def test_a_runtime_without_a_tracker_never_extends(monkeypatch):
    handle = _handle(
        monkeypatch,
        [
            (1, _status(_crew("connecting"), *_agent_servers(0))),
            (60, _status(_crew("connecting"), *_agent_servers(4))),
            (_BUDGET, None),
        ],
        tracked=False,
    )
    with pytest.raises(AcpRequestTimeout, match="timed out after 90s"):
        await _wait(handle)


# ── the bound ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("budget", [90, 900])
def test_the_subagent_startup_watchdog_outlives_session_new_plus_the_longest_wait(
    budget, monkeypatch
):
    from kiro_crew.config import live
    from kiro_crew.config.sections_agent import AgentConfig
    from kiro_crew.subagent import SubagentManager

    manager = SubagentManager.__new__(SubagentManager)
    manager._startup_timeout_override = None
    snapshot = SimpleNamespace(agent=AgentConfig(session_start_timeout_secs=budget))
    monkeypatch.setattr(live, "snapshot", lambda: snapshot)
    # session/new may spend its whole budget, then the barrier its budget plus
    # the queue allowance; the watchdog must not reap that start first.
    assert manager._startup_deadline > budget + budget + QUEUE_EXTENSION_SECS


# ── the reader feeds the tracker ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_reader_counts_a_status_frame_for_a_session_it_does_not_route():
    from acp_runtime_helpers import (
        _drain,
        _feed,
        _make_runtime,
        _register,
        _start_reader,
        _stop_reader,
    )

    def raw(msg: JsonRpcMessage) -> dict:
        return {"jsonrpc": "2.0", "method": msg.method, "params": msg.params}

    rt, reader, _proc = _make_runtime()
    queues = _register(rt, _SID)
    task = await _start_reader(rt)
    try:
        _feed(reader, raw(_status(*_agent_servers(2), session=_OTHER)))
        _feed(reader, raw(_status(_crew("connecting"), *_agent_servers(1))))
        await _drain(reader)
    finally:
        await _stop_reader(task)
    assert rt.mcp_connect_progress.epoch == 2
    # Routing is unchanged: only the registered session's frame reached a queue.
    assert queues[_SID].qsize() == 1


@pytest.mark.asyncio
async def test_a_malformed_status_entry_does_not_stop_the_reader():
    from acp_runtime_helpers import (
        _drain,
        _feed,
        _make_runtime,
        _register,
        _start_reader,
        _stop_reader,
    )

    rt, reader, _proc = _make_runtime()
    queues = _register(rt, _SID)
    task = await _start_reader(rt)
    try:
        for bad in ([], {"x": 1}, 7, None):
            _feed(
                reader,
                {
                    "jsonrpc": "2.0",
                    "method": METHOD_KAS_MCP_STATUS,
                    "params": {"sessionId": _OTHER, "servers": [{"name": "ext", "status": bad}]},
                },
            )
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "method": METHOD_KAS_MCP_STATUS,
                "params": {"sessionId": _SID, "servers": _agent_servers(1)},
            },
        )
        await _drain(reader)
        assert not task.done()
    finally:
        await _stop_reader(task)
    assert rt.mcp_connect_progress.epoch == 1
    assert queues[_SID].qsize() == 1
