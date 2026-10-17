"""A timed-out session start names the unanswered requests it was sent behind.

kiro-cli answers ``session/new``, ``session/load`` and ``session/set_mode`` one at
a time per process, so a start sent while an earlier one is still unanswered
spends its budget waiting for it. The timeout text has to say so. Without it,
a start whose injected servers all reported reads "the stall is later in
session startup", and the operator chases a stage the start never reached.

These tests drive the REAL ``_send_and_await`` and reader loop against a faked
subprocess, so the "unanswered" verdict is the one production computes from
``_pending_requests``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_update_provider import _UNALLOCATABLE_PID

import kiro_crew.acp.runtime as runtime_mod
from kiro_crew.acp.runtime import (
    AcpRequestTimeout,
    AcpRuntime,
    AcpSessionStartTimeout,
    _queued_behind_note,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_SERIAL_SESSION_STARTS,
    MCP_ROSTER_COMPLETE_NOTE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_SESSION_NEW,
    METHOD_SESSION_TERMINATE,
    METHOD_SET_MODE,
    METHOD_SET_MODEL,
    JsonRpcMessage,
)

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# Bounds a wait that is otherwise pinned to a real signal, so a hang fails here.
_BACKSTOP = 30.0


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch):
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 0.05, raising=False)
    runtime_mod._session_start_gates.clear()
    monkeypatch.setattr(runtime_mod, "_resolve_session_start_concurrency", lambda: 2)
    yield
    runtime_mod._session_start_gates.clear()


def _runtime() -> tuple[AcpRuntime, asyncio.StreamReader, MagicMock]:
    rt = AcpRuntime(work_dir="/tmp")
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = _UNALLOCATABLE_PID
    rt._initialized = True
    rt._expect_mcp_reports = False
    rt._session_start_timeout = 0.05
    # Long enough that the first start's collector still owns its request when
    # the second start goes out; each test answers it before finishing.
    rt._start_collect_timeout = _BACKSTOP
    return rt, reader, proc


def _sent_ids(proc: MagicMock, method: str) -> list[int]:
    ids = []
    for call in proc.stdin.write.call_args_list:
        frame = json.loads(call.args[0].decode())
        if frame.get("method") == method:
            ids.append(frame["id"])
    return ids


def _feed(reader: asyncio.StreamReader, obj: dict) -> None:
    reader.feed_data((json.dumps(obj) + "\n").encode())


def _roster(*names: str) -> list[dict]:
    return [{"name": n, "command": "/bin/true", "args": [], "env": []} for n in names]


async def _settle(rt: AcpRuntime, reader: asyncio.StreamReader, proc: MagicMock) -> None:
    """Answer every session/new still owned by a collector, so none outlives the test.

    Nobody adopts the late session, so the collector terminates it; that request
    is answered too, or the teardown waits out its own timeout.
    """
    answered: set[int] = set()

    async def _answer_terminates() -> None:
        while True:
            for req_id in _sent_ids(proc, METHOD_SESSION_TERMINATE):
                if req_id not in answered:
                    answered.add(req_id)
                    _feed(reader, {"jsonrpc": "2.0", "id": req_id, "result": {}})
            await asyncio.sleep(0.01)

    responder = asyncio.ensure_future(_answer_terminates())
    try:
        for req_id in _sent_ids(proc, METHOD_SESSION_NEW):
            collector = rt._start_collectors.get(req_id)
            if collector is None:
                continue
            _feed(
                reader, {"jsonrpc": "2.0", "id": req_id, "result": {"sessionId": f"late-{req_id}"}}
            )
            await asyncio.wait_for(collector.settled.wait(), timeout=_BACKSTOP)
    finally:
        responder.cancel()
        await asyncio.gather(responder, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_start_sent_behind_an_unanswered_start_names_it():
    """The field shape: the first start times out and kiro-cli is still working on
    it, so the retry is queued behind it and times out too, with every injected
    server reported. The retry's text must name the start ahead of it and must
    not claim the stall is later in its own startup."""
    rt, reader, proc = _runtime()
    reader_task = asyncio.ensure_future(rt._reader_loop())
    try:
        with pytest.raises(AcpSessionStartTimeout) as first:
            await rt.create_session(mcp_servers=_roster("alpha"))
        assert "queued behind" not in str(first.value), "nothing was ahead of the first start"

        # The second start's only injected server reported READY.
        rt._pending_init_notifications.append(
            JsonRpcMessage(method=METHOD_MCP_SERVER_INITIALIZED, params={"serverName": "alpha"})
        )
        with pytest.raises(AcpSessionStartTimeout) as second:
            await rt.create_session(mcp_servers=_roster("alpha"))

        text = str(second.value)
        assert "timed out after" in text
        assert "1/1 session-injected MCP server(s) reported" in text
        assert (
            "queued behind 1 earlier request(s) this agent process had not answered "
            "by the deadline (1 session/new; oldest sent" in text
        )
        assert MCP_ROSTER_COMPLETE_NOTE not in text
        await _settle(rt, reader, proc)
    finally:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_start_after_an_answered_one_names_nothing():
    """An answered start is not ahead of anything: the next timeout keeps the
    plain MCP verdict, roster-complete note included."""
    rt, reader, proc = _runtime()
    reader_task = asyncio.ensure_future(rt._reader_loop())
    try:
        with pytest.raises(AcpSessionStartTimeout):
            await rt.create_session(mcp_servers=_roster("alpha"))
        await _settle(rt, reader, proc)

        rt._pending_init_notifications.append(
            JsonRpcMessage(method=METHOD_MCP_SERVER_INITIALIZED, params={"serverName": "alpha"})
        )
        with pytest.raises(AcpSessionStartTimeout) as second:
            await rt.create_session(mcp_servers=_roster("alpha"))

        text = str(second.value)
        assert "queued behind" not in text
        assert MCP_ROSTER_COMPLETE_NOTE in text
        await _settle(rt, reader, proc)
    finally:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_mode_switch_counts_and_other_requests_do_not():
    """A set_mode still unanswered is ahead of a start; set_model is not
    serialized by the agent and never counts. Driven through the real
    ``_send_and_await`` with no reader, so nothing is ever answered."""
    rt, _reader, _proc = _runtime()

    for method in (METHOD_SET_MODE, METHOD_SET_MODEL):
        task = asyncio.ensure_future(rt._send_and_await(method, {}, timeout=_BACKSTOP))
        await asyncio.sleep(0)
    with pytest.raises(AcpRequestTimeout) as caught:
        await rt._send_and_await(METHOD_SESSION_NEW, {}, timeout=0.05)

    queued = getattr(caught.value, "queued_behind", None)
    assert queued is not None and [m for m, _age in queued] == [METHOD_SET_MODE]
    for pending in list(rt._pending_requests.values()):
        pending.cancel()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _answer(rt: AcpRuntime, req_id: int) -> None:
    """Resolve an awaited request the way the reader loop does on its response."""
    future = rt._pending_requests.pop(req_id, None)
    assert future is not None and not future.done()
    future.set_result({})


@pytest.mark.asyncio
async def test_a_request_answered_before_the_deadline_is_not_blamed():
    """The earlier request was outstanding when the start went out but answered
    a moment later: it cost the start little or nothing, so the timeout must not
    claim the start queued behind it, and the MCP verdict keeps its note."""
    rt, _reader, proc = _runtime()
    switch = asyncio.ensure_future(rt._send_and_await(METHOD_SET_MODE, {}, timeout=_BACKSTOP))
    await asyncio.sleep(0)
    (switch_id,) = _sent_ids(proc, METHOD_SET_MODE)

    async def _answer_soon() -> None:
        await asyncio.sleep(0.02)
        _answer(rt, switch_id)

    answering = asyncio.ensure_future(_answer_soon())
    with pytest.raises(AcpRequestTimeout) as caught:
        await rt._send_and_await(METHOD_SESSION_NEW, {}, timeout=0.2)
    await answering
    await switch

    assert getattr(caught.value, "queued_behind", None) is None


@pytest.mark.asyncio
async def test_a_request_answered_while_the_start_waits_for_the_write_lock_is_not_ahead():
    """A request answered while this start was still waiting for the stdin write
    lock never shared the wire with it, and it is answered before the start's
    deadline, so the deadline rule leaves it out."""
    rt, _reader, proc = _runtime()
    switch = asyncio.ensure_future(rt._send_and_await(METHOD_SET_MODE, {}, timeout=_BACKSTOP))
    await asyncio.sleep(0)
    (switch_id,) = _sent_ids(proc, METHOD_SET_MODE)

    lock = rt._stdin_write_lock()
    await lock.acquire()
    start = asyncio.ensure_future(rt._send_and_await(METHOD_SESSION_NEW, {}, timeout=0.2))
    await asyncio.sleep(0.02)
    assert not _sent_ids(proc, METHOD_SESSION_NEW), "the start is still waiting for the lock"
    _answer(rt, switch_id)
    await switch
    lock.release()

    with pytest.raises(AcpRequestTimeout) as caught:
        await start
    assert getattr(caught.value, "queued_behind", None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, ACP_BACKEND_CODEX])
async def test_a_backend_not_measured_to_serialize_is_never_said_to(backend):
    """H6: the claim is granted by membership in ACP_BACKENDS_SERIAL_SESSION_STARTS.
    On a harness outside it, a start that overlapped an unanswered request is not
    described as having waited for it, and nothing is recorded."""
    assert backend not in ACP_BACKENDS_SERIAL_SESSION_STARTS
    rt, _reader, _proc = _runtime()
    rt._acp_backend = backend

    task = asyncio.ensure_future(rt._send_and_await(METHOD_SET_MODE, {}, timeout=_BACKSTOP))
    await asyncio.sleep(0)
    with pytest.raises(AcpRequestTimeout) as caught:
        await rt._send_and_await(METHOD_SESSION_NEW, {}, timeout=0.05)

    assert getattr(caught.value, "queued_behind", None) is None
    assert rt._one_at_a_time_sent == {}
    for pending in list(rt._pending_requests.values()):
        pending.cancel()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_kiro_is_the_measured_member():
    assert ACP_BACKEND_KIRO in ACP_BACKENDS_SERIAL_SESSION_STARTS


def test_the_bookkeeping_never_outgrows_pending_requests():
    """An id that left ``_pending_requests`` (answered, abandoned, or the runtime
    died) is dropped at the next send, so the map cannot accumulate."""
    rt = AcpRuntime(work_dir="/tmp")
    for req_id in range(1, 6):
        rt._pending_requests[req_id] = MagicMock()
        rt._one_at_a_time_ahead(METHOD_SESSION_NEW, req_id)
    assert set(rt._one_at_a_time_sent) == {1, 2, 3, 4, 5}

    rt._pending_requests.clear()
    rt._pending_requests[6] = MagicMock()
    assert rt._one_at_a_time_ahead(METHOD_SESSION_NEW, 6) == []
    assert set(rt._one_at_a_time_sent) == {6}


def test_the_note_summarizes_kinds_and_the_oldest_age():
    note = _queued_behind_note(
        [(METHOD_SESSION_NEW, 85.4), (METHOD_SESSION_NEW, 12.0), (METHOD_SET_MODE, 3.0)]
    )
    assert note.startswith(
        "queued behind 3 earlier request(s) this agent process had not answered by the deadline"
    )
    assert "(1 session/new" not in note and "2 session/new, 1 session/set_mode" in note
    assert "oldest sent 85s earlier" in note
    assert _queued_behind_note([]) == ""
