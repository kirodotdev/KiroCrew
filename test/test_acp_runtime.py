"""Unit tests for the AcpRuntime single-reader demux (Phase 1 multiplexing).

These exercise the routing logic that lets ONE kiro-cli acp process host
multiple concurrent sessions: the single _reader_loop owns stdout and routes
each frame to the right destination —

  - JSON-RPC response whose id is in _pending_requests  → resolve that Future
  - JSON-RPC response whose id is in _routed_requests   → that session's queue
  - notification carrying params.sessionId              → that session's queue
  - request (method + id) with no sessionId             → answered ONCE at
                                                           connection level (-32601)
  - notification with no sessionId                       → broadcast to all
  - empty read (process exit)                            → _mark_dead: fail all
                                                           futures + poison queues

The headline test (`test_multiple_sessions_routed_independently`) proves the
end-to-end claim: two AcpSessionHandle turns run concurrently on one runtime and
each receives only its own session's text + completion.

The reader is driven with a REAL asyncio.StreamReader fed crafted JSON-RPC
lines; the subprocess and stdin are mocked (no kiro-cli is launched).
"""

import asyncio
import gc
import json
import os
import time
import weakref
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from spawn_test_helpers import strip_spawn_shim

from kiro_crew.acp.client import _OVERSIZE_DRAIN_MAX_BYTES
from kiro_crew.acp.runtime import (
    _REQUEST_TIMEOUT,
    _SESSION_NEW_TIMEOUT,
    _TERMINATE_TIMEOUT,
    AcpRuntime,
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpSessionHandle,
    _ColdStartAdmission,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    JSONRPC_METHOD_NOT_FOUND,
    METHOD_COMMANDS_EXECUTE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_TERMINATE,
    METHOD_SESSION_UPDATE,
    METHOD_SET_CONFIG_OPTION,
    METHOD_SET_MODE,
    JsonRpcMessage,
)

# ── Harness ──


@pytest.fixture(autouse=True)
def _fast_no_report_ceiling(monkeypatch):
    """Shrink drain_init()'s no-report ceiling for every test in this module.

    Many tests drive the real create_session()/load_session() path against a
    fake backend that never emits MCP registration frames; at the production
    ceiling each would stall drain_init() for seconds. drain_init() resolves
    the module constant at call time precisely so this patch takes effect.
    Tests that exercise the ceiling itself pass an explicit value instead.
    """
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 0.05, raising=False)


def _make_runtime():
    """An initialized AcpRuntime wired to a fake subprocess.

    stdout is a real StreamReader we feed lines into; stdin is a mock that
    records writes; the reader loop can run against it without a real process.
    """
    rt = AcpRuntime(work_dir="/tmp")
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = 4242
    rt._process = proc
    rt._pid = 4242
    rt._initialized = True
    return rt, reader, proc


def _feed(reader: asyncio.StreamReader, obj: dict) -> None:
    reader.feed_data((json.dumps(obj) + "\n").encode())


def _register(rt: AcpRuntime, *session_ids: str) -> dict[str, asyncio.Queue]:
    queues = {sid: asyncio.Queue() for sid in session_ids}
    rt._session_queues.update(queues)
    return queues


async def _start_reader(rt: AcpRuntime) -> asyncio.Task:
    task = asyncio.ensure_future(rt._reader_loop())
    await asyncio.sleep(0)  # let the loop reach its first readline
    return task


def _permission_msg(request_id: int) -> JsonRpcMessage:
    """A server→client permission REQUEST, the shape the answerer is given."""
    return JsonRpcMessage(
        id=request_id,
        method=METHOD_REQUEST_PERMISSION,
        params={"sessionId": "sA", "options": []},
    )


async def _stop_reader(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _await_routed(rt: AcpRuntime, *session_ids: str, timeout: float = 5.0) -> dict[str, int]:
    """Wait until the runtime has an in-flight request for each session, and
    return the ``{session_id: request_id}`` map.

    This replaces the ``await asyncio.sleep(0.05); req_id = rt._next_id - 1``
    idiom, which was wrong in two independent ways.

    The **timing** problem: 50ms is a guess at how long a driver task takes to
    reach ``send_request``. It holds on an idle machine and fails on a loaded
    Windows CI runner, where the driver may not have run yet. The test then reads
    an id belonging to no request, feeds a response nothing is waiting for, and
    fails much later as an opaque ``TimeoutError`` in ``wait_for`` rather than at
    the line that guessed wrong.

    The **correctness** problem: ``_next_id - 1`` assumes the most recently
    allocated id belongs to *this* prompt. That is only true when nothing else
    allocated an id in between, which no test actually enforces.
    ``_routed_requests`` maps request id to session id, so looking a session up
    there is exact regardless of what else is in flight.

    Waiting on ``_routed_requests`` is the right signal: ``send_request``
    populates it in the same synchronous block that allocates the id
    (``runtime.py``), so the entry is visible as soon as the request exists.
    """
    deadline = time.monotonic() + timeout
    wanted = set(session_ids)
    while True:
        routed = {sid: rid for rid, sid in rt._routed_requests.items()}
        if wanted <= routed.keys():
            return routed
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"timed out after {timeout}s waiting for in-flight requests for "
                f"{sorted(wanted)}; currently routed: {routed}"
            )
        # Yield rather than spin: the driver task needs the loop to progress.
        await asyncio.sleep(0.001)


async def _await_pending(
    rt: AcpRuntime, *, exclude: set[int] | None = None, timeout: float = 5.0
) -> int:
    """Wait for an in-flight control-plane request and return its id.

    The ``_pending_requests`` counterpart to :func:`_await_routed`, and it exists
    for the same reason. It replaces the
    ``await asyncio.sleep(0); next(iter(rt._pending_requests))`` idiom, which
    assumes one loop iteration is enough for the caller to reach
    ``_send_and_await``. ``create_session`` first awaits ``asyncio.to_thread`` to
    resolve the MCP-gateway overlay off the loop, so a single yield leaves
    ``_pending_requests`` empty — and ``next()`` on an empty iterator raises
    ``StopIteration``, which PEP 479 converts into
    ``RuntimeError("coroutine raised StopIteration")`` on its way out of a
    coroutine. That names neither the stale assumption nor the line that made it.

    ``_send_and_await`` registers the future in the same synchronous block that
    allocates the id (``runtime.py``), so the entry is visible as soon as the
    request exists. ``exclude`` drops ids the caller already consumed, so a test
    driving a second request cannot pick up a leftover entry from the first.
    """
    seen = exclude or set()
    deadline = time.monotonic() + timeout
    while True:
        fresh = [rid for rid in rt._pending_requests if rid not in seen]
        if fresh:
            # These tests keep exactly one control-plane request in flight, so
            # more than one means the id being returned is a coin flip.
            assert len(fresh) == 1, f"expected one in-flight request, got {fresh}"
            return fresh[0]
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"timed out after {timeout}s waiting for an in-flight request; "
                f"currently pending: {sorted(rt._pending_requests)}"
            )
        # Yield rather than spin: the caller needs the loop to progress.
        await asyncio.sleep(0.001)


# ── The _await_routed helper itself ──


@pytest.mark.asyncio
async def test_await_routed_tolerates_a_driver_that_has_not_run_yet():
    """The helper must not depend on the driver having been scheduled.

    This is the exact condition that made the old
    ``await asyncio.sleep(0.05); req_id = rt._next_id - 1`` idiom flake on loaded
    Windows runners: the sleep expires, but the driver task has not yet reached
    ``send_request``, so ``_next_id`` has not advanced and the computed id
    belongs to no request. The test then feeds a response nothing is waiting for
    and fails later as an opaque ``TimeoutError``.

    Here the driver is deliberately never given a chance to run before the read,
    which is the worst case of that race.
    """
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        # No yield: the driver has definitely not sent anything yet, so the old
        # arithmetic would compute an id for a request that does not exist.
        assert rt._routed_requests == {}
        stale_id = rt._next_id - 1

        routed = await _await_routed(rt, "sA")
        assert routed["sA"] != stale_id, "the old idiom would have used a wrong id"
        assert rt._routed_requests[routed["sA"]] == "sA"

        _feed(reader, {"id": routed["sA"], "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_await_routed_reports_which_sessions_were_missing_on_timeout():
    """A timeout must name the sessions it waited for, not just time out.

    The old idiom failed indirectly, in an unrelated ``wait_for``; this keeps the
    diagnosis at the line that actually waited.
    """
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    with pytest.raises(AssertionError) as exc:
        await _await_routed(rt, "sA", timeout=0.05)
    assert "sA" in str(exc.value)
    assert "currently routed" in str(exc.value)


# ── Notification routing by sessionId ──


@pytest.mark.asyncio
async def test_notification_routed_to_named_session():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA", "x": 1}})
        msg = await asyncio.wait_for(q["sA"].get(), timeout=1.0)
        assert msg.params["sessionId"] == "sA"
        # The other session's queue must NOT have received it.
        assert q["sB"].empty()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_notification_for_unknown_session_is_dropped_not_broadcast():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        # sessionId present but not registered → routed-by-id path misses; it
        # has a sessionId so it is NOT broadcast either.
        _feed(reader, {"method": "session/update", "params": {"sessionId": "ghost"}})
        await asyncio.sleep(0.05)
        assert q["sA"].empty()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_null_session_notification_broadcasts_to_all():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": "some/global", "params": {}})  # no sessionId
        a = await asyncio.wait_for(q["sA"].get(), timeout=1.0)
        b = await asyncio.wait_for(q["sB"].get(), timeout=1.0)
        assert a.method == "some/global"
        assert b.method == "some/global"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_ownerless_request_answered_once_not_broadcast():
    """A server→client REQUEST with no sessionId gets exactly ONE -32601 reply.

    Before the fix it took the broadcast branch: every registered session's
    dispatch loop classified it as server_request_unknown and each replied
    -32601 on the shared stdin — one request id, N responses (issue #4864).
    The runtime now answers it once at connection level and never enqueues it.
    """
    rt, reader, proc = _make_runtime()
    q = _register(rt, "sA", "sB")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 4864, "method": "unknown/ownerless", "params": {}})
        # The answer task runs off the reader loop; give it ticks to complete.
        for _ in range(20):
            await asyncio.sleep(0)
        replies = [json.loads(call.args[0].decode()) for call in proc.stdin.write.call_args_list]
        errors = [r for r in replies if r.get("id") == 4864 and "error" in r]
        assert len(errors) == 1, f"expected exactly one reply, got {replies}"
        assert errors[0]["error"]["code"] == -32601
        # Not enqueued to ANY session — no dispatch loop ever sees it.
        assert q["sA"].empty()
        assert q["sB"].empty()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_permission_answer_waits_for_shared_answer_capacity_then_answers():
    """A temporary full cap delays, rather than drops, the next auto-answer.

    Drives the unroutable-permission answerer, the one caller of the shared
    admission wait. (It was written against KAS's credential callback, which was
    the second caller until kiro-cli's ACP relay took ownership of auth.)
    """
    rt, _reader, _ = _make_runtime()
    rt._max_answer_tasks = 1
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    second_capacity_check = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    capacity_checks = 0

    async def blocked_answer(msg, session_id, *, reason="x") -> None:
        del session_id, reason
        if msg.id == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
            await release_second.wait()

    wait_for_capacity = rt._wait_for_answer_capacity

    async def observed_capacity(*args, **kwargs) -> bool:
        nonlocal capacity_checks
        capacity_checks += 1
        if capacity_checks == 2:
            second_capacity_check.set()
        return await wait_for_capacity(*args, **kwargs)

    rt._answer_unroutable_permission = blocked_answer  # type: ignore[method-assign]
    rt._wait_for_answer_capacity = observed_capacity  # type: ignore[method-assign]
    try:
        await rt._spawn_answer_task(_permission_msg(1), "sA")
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        assert len(rt._answer_tasks) == 1

        second = asyncio.ensure_future(rt._spawn_answer_task(_permission_msg(2), "sA"))
        await asyncio.wait_for(second_capacity_check.wait(), timeout=1.0)
        assert not second_started.is_set()

        release_first.set()
        await asyncio.wait_for(second_started.wait(), timeout=1.0)
        await second
        assert len(rt._answer_tasks) == 1
        assert sum(rt._dropped_frames.values()) == 0

        retained = next(iter(rt._answer_tasks))
        discarded = asyncio.Event()
        retained.add_done_callback(lambda _task: discarded.set())
        release_second.set()
        await asyncio.wait_for(discarded.wait(), timeout=1.0)
        assert rt._answer_tasks == set()
    finally:
        release_first.set()
        release_second.set()
        await asyncio.gather(*rt._answer_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_ownerless_response_with_null_result_is_not_answered():
    """An id-carrying frame with NO method is a response, not a request.

    A response whose result is null slips past the result/error routing check;
    it must not be mistaken for an ownerless request and answered -32601 —
    that would inject a spurious error reply for an id the backend owns.
    """
    rt, reader, proc = _make_runtime()
    _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 77, "result": None})  # response shape, no method
        for _ in range(20):
            await asyncio.sleep(0)
        replies = [json.loads(call.args[0].decode()) for call in proc.stdin.write.call_args_list]
        assert not [r for r in replies if r.get("id") == 77]
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_answer_cap_timeout_marks_runtime_dead_without_growth():
    """A wedged shared cap fails the runtime instead of losing a request."""
    rt, _reader, _ = _make_runtime()
    rt._max_answer_tasks = 1
    rt._answer_cap_wait_secs = 0.0
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    marked_dead = asyncio.Event()
    dead_reasons: list[str] = []

    async def blocked_answer(msg, session_id, *, reason="x") -> None:
        del session_id, reason
        if msg.id == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()

    def mark_dead(reason: str) -> None:
        dead_reasons.append(reason)
        rt._dead = True
        marked_dead.set()

    rt._answer_unroutable_permission = blocked_answer  # type: ignore[method-assign]
    rt._mark_dead = mark_dead  # type: ignore[method-assign]
    try:
        await rt._spawn_answer_task(_permission_msg(1), "sA")
        await asyncio.wait_for(first_started.wait(), timeout=1.0)

        await rt._spawn_answer_task(_permission_msg(2), "sA")
        await asyncio.wait_for(marked_dead.wait(), timeout=1.0)

        assert not second_started.is_set()
        assert len(rt._answer_tasks) == 1
        assert sum(rt._dropped_frames.values()) == 0
        assert dead_reasons and "permission" in dead_reasons[0]
    finally:
        release_first.set()
        await asyncio.gather(*rt._answer_tasks, return_exceptions=True)


# ── Response routing by id ──


@pytest.mark.asyncio
async def test_awaited_response_resolves_pending_future():
    rt, reader, _ = _make_runtime()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 7, "result": {"sessionId": "new1"}})
        result = await asyncio.wait_for(fut, timeout=1.0)
        assert result == {"sessionId": "new1"}
        assert 7 not in rt._pending_requests
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_missing_agent_spec_error_reaches_caller_actionable(tmp_path):
    """A missing agent spec must not reach the caller as a raw -32603 dict.

    kiro-cli answers ``session/set_mode`` for an agent it cannot resolve with a
    bare "Internal error" whose data is ``Mode '<name>' not found``. Routed raw,
    the caller — and the dashboard chat bubble behind it — got the JSON-RPC dict
    verbatim: an internal ACP concept, no mention of the missing file, and no
    remedy, on a condition that fails every subsequent turn too.

    This pins the formatting AT THE CALL SITE rather than only unit-testing the
    helper: the awaited-request branch of the reader is the single path every
    handshake error (initialize / session/new / session/set_mode) takes, so a
    regression that unwires the helper is invisible to a helper-only test.
    """
    rt, reader, _ = _make_runtime()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut
    task = await _start_reader(rt)
    try:
        with patch("kiro_crew.acp.runtime.kiro_agents_dir", return_value=tmp_path):
            _feed(
                reader,
                {
                    "id": 7,
                    "error": {
                        "code": -32603,
                        "message": "Internal error",
                        "data": "Mode 'kirocrew' not found",
                    },
                },
            )
            with pytest.raises(AcpRuntimeError) as excinfo:
                await asyncio.wait_for(fut, timeout=1.0)
    finally:
        await _stop_reader(task)

    text = str(excinfo.value)
    assert "'kirocrew.json'" in text  # the file that is missing
    assert str(tmp_path) in text  # where it was looked for
    assert "kirocrew setup --agent-only --clean" in text  # the repair
    assert "-32603" not in text  # no raw protocol frame


@pytest.mark.asyncio
async def test_non_numeric_response_id_dropped_without_killing_demux():
    """The id in a response frame is agent-controlled. int("req-1") raised
    ValueError, which the reader's catch-all turned into _mark_dead — poisoning
    EVERY multiplexed session over one unmatched frame. The frame must be
    dropped and the reader must keep routing."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut
    task = await _start_reader(rt)
    try:
        # String / list / overflow ids: none int() coercible. json parses
        # 1e9999 to float("inf"), which raises OverflowError (not ValueError).
        _feed(reader, {"id": "req-1", "result": {"ok": True}})
        _feed(reader, {"id": [1], "error": {"code": -1}})
        reader.feed_data(b'{"id": 1e9999, "result": {}}\n')
        # The reader must still be alive: a valid response and a routed
        # notification must both be delivered after the bad frames.
        _feed(reader, {"id": 7, "result": {"sessionId": "new1"}})
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        result = await asyncio.wait_for(fut, timeout=1.0)
        assert result == {"sessionId": "new1"}
        msg = await asyncio.wait_for(q["sA"].get(), timeout=1.0)
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_numeric_string_response_id_still_coerced():
    """A digit-string id ("7") keeps working — it was int()-coerced before and
    must keep matching the pending int key after the guard."""
    rt, reader, _ = _make_runtime()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": "7", "result": {"ok": 1}})
        result = await asyncio.wait_for(fut, timeout=1.0)
        assert result == {"ok": 1}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_error_response_sets_exception_on_future():
    rt, reader, _ = _make_runtime()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[9] = fut
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 9, "error": {"code": -1, "message": "boom"}})
        with pytest.raises(AcpRuntimeError):
            await asyncio.wait_for(fut, timeout=1.0)
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_routed_response_goes_to_session_queue():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    rt._routed_requests[11] = "sA"
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 11, "result": {"stopReason": "end_turn"}})
        msg = await asyncio.wait_for(q["sA"].get(), timeout=1.0)
        assert msg.id == 11
        assert 11 not in rt._routed_requests
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unmatched_response_is_ignored():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 999, "result": {}})  # no pending/routed entry
        await asyncio.sleep(0.05)
        assert q["sA"].empty()
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_non_json_line_is_skipped():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        reader.feed_data(b"not json at all\n")
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await asyncio.wait_for(q["sA"].get(), timeout=1.0)
        assert msg.params["sessionId"] == "sA"  # loop survived the bad line
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_non_object_json_line_does_not_crash_reader():
    """A valid-JSON but non-object line (bare scalar / array) must be skipped,
    not fed to JsonRpcMessage.from_dict (which would raise AttributeError and
    tear down EVERY multiplexed session on the shared runtime)."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        for bad in (b"123\n", b'"a string"\n', b"[1, 2, 3]\n", b"true\n", b"null\n"):
            reader.feed_data(bad)
        # A well-formed frame after the bad lines must still route → reader alive.
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await asyncio.wait_for(q["sA"].get(), timeout=1.0)
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead  # reader never marked the runtime dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_oversize_stdout_frame_is_dropped_not_fatal():
    """A single JSON-RPC line over the stdout buffer must cost ONE frame, not
    the whole runtime.

    Regression: the reader used to _mark_dead on overrun, which poisons every
    multiplexed session's queue and fails every pending future — users saw
    "process exited / chat failure" mid-turn after one huge tool result.

    Driven through a REAL StreamReader so this asserts asyncio's actual
    behaviour, not a mock's.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        reader.feed_data(b"X" * 1024 + b"\n")  # oversize, newline present
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await asyncio.wait_for(q["sA"].get(), timeout=5.0)
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unterminated_oversize_stdout_recovers_at_next_frame():
    """The shape actually observed in the field: an oversize line whose newline
    has NOT arrived yet, so the reader drains prefix after prefix before the
    stream is back in sync. It must ride through every step and route the next
    real frame.

    Asserts the outcome (recovery), not the step count: how many buffer-fulls
    the reader sees depends on how the feeds interleave with its task.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        for _ in range(4):
            reader.feed_data(b"Y" * 512)  # no newline anywhere
            await asyncio.sleep(0)
        reader.feed_data(b"TAIL-OF-OVERSIZE-LINE\n")  # line finally terminates
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await asyncio.wait_for(q["sA"].get(), timeout=5.0)
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_oversize_frame_split_mid_multibyte_does_not_kill_demux():
    """The drained remainder must never reach json.loads.

    Regression for a defect in the second cut of this fix: the drain consumed only
    the buffered prefix and let the recovered tail through as a line. That tail is
    a byte-slice cut at an arbitrary offset, so an oversize frame carrying
    multibyte UTF-8 (CJK, emoji — ordinary in tool output) splits a character;
    `json.loads` then raises UnicodeDecodeError, which is NOT a
    json.JSONDecodeError, so it escaped the non-JSON guard into the loop's crash
    handler and killed EVERY multiplexed session.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        # Two conditions make the tail reach the parser, and both are ordinary:
        #  - the discard boundary must fall mid-character, which the UNTERMINATED
        #    branch does by construction (it reports `consumed = len(buffer)`, an
        #    arbitrary byte offset; a newline-terminated overrun instead reports
        #    the newline's offset, already a character boundary), and
        #  - the remainder after the last discard must be UNDER the reader limit,
        #    so readuntil returns it as a normal-looking line instead of
        #    overrunning again.
        # Dense CJK, fed in 500-byte slices that are not multiples of 3.
        blob = ("苹" * 400).encode() + b"\n"  # 1201 bytes
        assert len(blob) % 3 != 0
        for off in range(0, 1000, 500):
            reader.feed_data(blob[off : off + 500])
            await asyncio.sleep(0)
        reader.feed_data(blob[1000:])  # 201 bytes < limit → returned as a line
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await asyncio.wait_for(q["sA"].get(), timeout=5.0)
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_many_terminated_oversize_frames_never_exhaust_the_budget():
    """A run of oversize-but-properly-terminated frames must stay survivable.

    Regression for a defect in the first cut of this fix: the guard counted
    oversize *frames* rather than bytes-without-a-boundary, so a replay of N
    newline-terminated >limit frames walked straight into runtime death even
    though every one of them recovered a frame boundary. The budget is now scoped
    to a single drain call, each of which provably ends on a boundary.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    rounds = 40
    try:
        for i in range(rounds):
            reader.feed_data(b"X" * 4096 + b"\n")
            _feed(reader, {"method": "session/update", "params": {"sessionId": "sA", "n": i}})
        for i in range(rounds):
            msg = await asyncio.wait_for(q["sA"].get(), timeout=5.0)
            assert msg.params["n"] == i
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unterminated_blob_past_the_byte_budget_marks_runtime_dead():
    """The escape hatch: a stream that never yields a frame boundary would have
    the reader draining forever, so exceeding the byte budget must still reach the
    terminal state.

    The liveness oracle cannot cover this case — it reads CPU/IO movement, and a
    garbage-spewing stream moves both, so it would be judged WORKING.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        fed = 0
        while fed <= _OVERSIZE_DRAIN_MAX_BYTES and not rt._dead:
            reader.feed_data(b"Z" * 65536)  # never a newline
            fed += 65536
            await asyncio.sleep(0)
        await asyncio.wait_for(task, timeout=5.0)
    except Exception:
        pass
    finally:
        await _stop_reader(task)
    assert rt._dead


def test_runtime_reuses_clients_oversize_drain_helper():
    """The consume-prefix-and-retry drain must have ONE definition. A second copy
    is how two read paths drift apart (they already disagreed once, when only one
    of them killed the process)."""
    import kiro_crew.acp.client as client_mod
    import kiro_crew.acp.runtime as runtime_mod

    assert runtime_mod._drain_oversize_line is client_mod._drain_oversize_line
    assert runtime_mod.OversizeLineUnrecoverable is client_mod.OversizeLineUnrecoverable


def test_runtime_uses_clients_augmented_kiro_bin_resolver():
    """spawn() must resolve kiro-cli via the SAME augmented-PATH resolver as
    AcpClient (honours KIROCREW_KIRO_BIN + augmented_path so a non-login gateway
    finds a ~/.local/bin install). A bare shutil.which(PATH) duplicate regressed
    the kiro/_bg path to 'kiro-cli not found in PATH'. Assert single-source."""
    import kiro_crew.acp.client as client_mod
    import kiro_crew.acp.runtime as runtime_mod

    assert runtime_mod._resolve_kiro_bin_for_spawn is client_mod._resolve_kiro_bin_for_spawn


@pytest.mark.parametrize("backend", [None, ACP_BACKEND_KAS])
@pytest.mark.asyncio
async def test_runtime_missing_kiro_bin_reports_the_directories_it_searched(backend):
    """The runtime spawn path must DIAGNOSE a missing Kiro CLI, not just name it.

    ``AcpClient._spawn`` already separates "this binary is not installed" from
    "its install directory is outside the search" by naming every directory it
    walked (see ``test_spawn_kiro_missing_bin_reports_only_resolver_search_dirs``
    and ``env.describe_search_path``). The runtime sibling raised a bare
    ``kiro-cli not found in PATH``, which is both less information and factually
    wrong: resolution also walks ``%LOCALAPPDATA%\\Kiro-Cli``,
    ``%ProgramFiles%\\Kiro-Cli`` and the ``KIROCREW_KIRO_BIN`` override, none of
    which is PATH.

    That gap is what produced #6497. A Windows reporter read "not found in PATH",
    ran ``where kiro-cli``, got nothing, saw the gateway's OWN ``kirocrew.exe``
    in the app bundle, and concluded the agent CLI had been renamed and this
    lookup left stale. It had not been: ``kirocrew`` is Kiro Crew's own console
    script and ``kiro-cli`` is a separate prerequisite the message never said it
    was looking for anywhere but PATH.

    Both spawn branches are covered because both resolve the same binary and both
    have to answer the same question.
    """
    import kiro_crew.acp.client as client_mod
    import kiro_crew.acp.runtime as runtime_mod

    searched = [os.path.join(os.sep, "managed-bin"), os.path.join(os.sep, "path-bin")]
    unsearched = os.path.join(os.sep, "never-checked")
    rt = AcpRuntime(work_dir="/tmp")
    if backend is not None:
        rt._acp_backend = backend

    async def _no_bin(*, environ=None, home=None):
        return None

    with (
        patch.object(runtime_mod, "_resolve_kiro_bin_for_spawn", _no_bin),
        patch.object(client_mod, "known_kiro_cli_dirs", return_value=searched),
    ):
        with pytest.raises(AcpRuntimeError) as raised:
            await rt._resolve_spawn_argv()

    message = str(raised.value)
    assert "searched 2 directories" in message
    assert searched[0] in message
    assert searched[1] in message
    assert unsearched not in message
    # The remedy travels with the diagnosis, the rule env.MCP_PATH_HINT exists
    # for: a reader who learns the search missed their install needs the one
    # knob that covers it named here, not only in the source.
    assert "KIROCREW_KIRO_BIN" in message


async def _wait_for_queued(admission: _ColdStartAdmission, expected: int) -> None:
    """Yield to scheduled starters until the coordinator exposes the queue."""
    for _ in range(100):
        if admission.queued == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"cold-start queue did not reach {expected}, active={admission.active}, "
        f"queued={admission.queued}"
    )


@pytest.mark.asyncio
async def test_cold_start_admission_caps_simultaneous_runtime_spawns(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    admission = _ColdStartAdmission(limit=2)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)
    release = asyncio.Event()
    first_two_entered = asyncio.Event()
    running = 0
    peak = 0

    async def controlled_spawn(self):
        nonlocal peak, running
        running += 1
        peak = max(peak, running)
        if running == 2:
            first_two_entered.set()
        try:
            await release.wait()
        finally:
            running -= 1

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", controlled_spawn)
    tasks = [asyncio.create_task(AcpRuntime().spawn()) for _ in range(3)]
    try:
        await asyncio.wait_for(first_two_entered.wait(), timeout=1.0)
        await _wait_for_queued(admission, 1)

        assert peak == 2
        assert admission.active == 2
        assert admission.queued == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert peak == 2
    assert admission.active == 0
    assert admission.queued == 0


@pytest.mark.asyncio
async def test_cold_start_cancellation_releases_active_admission_slot(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    admission = _ColdStartAdmission(limit=1)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)
    entered = asyncio.Event()
    block = asyncio.Event()

    async def blocked_spawn(self):
        entered.set()
        await block.wait()

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", blocked_spawn)
    task = asyncio.create_task(AcpRuntime().spawn())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert admission.active == 0
    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", AsyncMock(return_value=None))
    await AcpRuntime().spawn()
    assert admission.active == 0


@pytest.mark.asyncio
async def test_cold_start_queued_cancellation_does_not_consume_slot(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    admission = _ColdStartAdmission(limit=1)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_spawn(self):
        entered.set()
        await release.wait()

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", blocked_spawn)
    active = asyncio.create_task(AcpRuntime().spawn())
    queued = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        queued = asyncio.create_task(AcpRuntime().spawn())
        await _wait_for_queued(admission, 1)

        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert admission.active == 1
        assert admission.queued == 0
    finally:
        release.set()
        if queued is not None and not queued.done():
            queued.cancel()
        cleanup_tasks = [active] + ([] if queued is None else [queued])
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    assert admission.active == 0


@pytest.mark.asyncio
async def test_cold_start_exception_releases_admission_slot(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    admission = _ColdStartAdmission(limit=1)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)

    async def fail_spawn(self):
        raise RuntimeError("initialize failed")

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", fail_spawn)
    with pytest.raises(RuntimeError, match="initialize failed"):
        await AcpRuntime().spawn()
    assert admission.active == 0

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", AsyncMock(return_value=None))
    await AcpRuntime().spawn()
    assert admission.active == 0


def test_cold_start_admission_registry_releases_contended_closed_loop(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    monkeypatch.setattr(
        runtime_mod,
        "_cold_start_admissions",
        runtime_mod.weakref.WeakKeyDictionary(),
    )
    monkeypatch.setattr(runtime_mod, "_COLD_START_MAX_CONCURRENT", 1)

    loop = asyncio.new_event_loop()
    loop_ref = weakref.ref(loop)
    asyncio.set_event_loop(loop)

    async def contend_and_drain():
        admission = runtime_mod._cold_start_admission()
        assert runtime_mod._cold_start_admission() is admission
        await admission.acquire()
        queued = asyncio.create_task(admission.acquire())
        await _wait_for_queued(admission, 1)
        admission_ref = weakref.ref(admission)
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        admission.release()
        assert admission.active == 0
        assert admission.queued == 0
        return admission_ref

    try:
        admission_ref = loop.run_until_complete(contend_and_drain())
    finally:
        asyncio.set_event_loop(None)
        loop.close()
        del loop

    gc.collect()
    assert admission_ref() is None
    assert loop_ref() is None


@pytest.mark.asyncio
async def test_runtime_spawn_passes_installed_path_through_exact_wrappers(
    tmp_path,
    monkeypatch,
):
    import kiro_crew.acp.runtime as runtime_mod

    macos_dir = tmp_path / "Kiro CLI.app" / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True)
    executable = macos_dir / "kiro-cli"
    executable.write_bytes(b"#!/bin/sh\n")
    executable.chmod(0o755)
    (macos_dir / "kiro-cli-chat").write_bytes(b"sibling")
    launch_path = str(executable)
    wrapped: dict[str, object] = {}

    class _StopSpawn(Exception):
        pass

    def capture_wrap(argv, mode, **kwargs):
        wrapped.update(argv=list(argv), mode=mode, kwargs=kwargs)
        return ["/usr/bin/sandbox-wrapper", *argv], None

    async def stop_spawn(*args, **kwargs):
        wrapped["spawn_args"] = args
        wrapped["spawn_kwargs"] = kwargs
        raise _StopSpawn()

    async def resolve_installed(*, environ=None, home=None):
        return launch_path

    monkeypatch.setattr(
        runtime_mod,
        "_resolve_kiro_bin_for_spawn",
        resolve_installed,
    )
    monkeypatch.setattr(runtime_mod, "wrap_argv", capture_wrap)
    voice_guard = MagicMock()
    monkeypatch.setattr(runtime_mod, "assert_voice_runtime_outside_agent_workspace", voice_guard)
    monkeypatch.setattr(
        runtime_mod,
        "cgroup_scope_argv",
        lambda argv: ["/usr/bin/cgroup-wrapper", *argv],
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", stop_spawn)

    runtime = AcpRuntime(work_dir=tmp_path / "workspace")
    with pytest.raises(_StopSpawn):
        await runtime.spawn()

    assert wrapped["argv"] == [launch_path, "acp", "--agent", runtime._agent]
    assert wrapped["mode"] == "auto"
    assert wrapped["kwargs"] == {
        "strip_python_env": True,
        "is_kiro_cli": True,
    }
    voice_guard.assert_called_once_with(runtime._work_dir)
    assert strip_spawn_shim(wrapped["spawn_args"]) == (
        "/usr/bin/cgroup-wrapper",
        "/usr/bin/sandbox-wrapper",
        launch_path,
        "acp",
        "--agent",
        runtime._agent,
    )
    spawn_kwargs = wrapped["spawn_kwargs"]
    assert isinstance(spawn_kwargs, dict)
    # The installed binary is exec'd in place: no inherited snapshot descriptor,
    # and the sibling subcommand binary a multi-call CLI dispatches to is still
    # reachable beside the launch path.
    assert "pass_fds" not in spawn_kwargs
    assert (Path(launch_path).parent / "kiro-cli-chat").exists()


@pytest.mark.asyncio
async def test_bound_macos_runtime_hands_sessions_the_verified_pathname(monkeypatch, tmp_path):
    """The ACP peer resolves this name itself, so it must be a name it can resolve.

    It is returned only after the descriptor proves it still names the bound
    identity. The earlier "/dev/fd/<n>" spelling is not resolvable on macOS -- the
    only platform that binds -- so it reached the peer as an unusable cwd.
    """
    import kiro_crew.acp.runtime as runtime_mod

    runtime = AcpRuntime(work_dir=tmp_path)
    runtime._bound_workspace_fd = 71
    runtime._spawn_work_dir = str(tmp_path)
    target = AsyncMock(return_value="/canonical/workspace")
    monkeypatch.setattr(runtime_mod, "resolve_bound_session_workspace", target)

    # The DESCRIPTOR's own name, not the pathname the caller asked with: the peer
    # re-resolves what it is handed, so the caller's spelling would leave the
    # retarget window open.
    assert await runtime._session_work_dir(tmp_path) == "/canonical/workspace"
    target.assert_called_once_with(71, tmp_path)


@pytest.mark.asyncio
async def test_bound_macos_runtime_rejects_descendant_session_workspace(monkeypatch, tmp_path):
    import kiro_crew.acp.runtime as runtime_mod

    runtime = AcpRuntime(work_dir=tmp_path)
    runtime._bound_workspace_fd = 71
    runtime._spawn_work_dir = str(tmp_path)
    descendant = tmp_path / "packages" / "app"
    target = AsyncMock(side_effect=runtime_mod.BoundWorkspaceMismatch(str(descendant)))
    monkeypatch.setattr(runtime_mod, "resolve_bound_session_workspace", target)

    with pytest.raises(AcpRuntimeError, match="exact workspace"):
        await runtime._session_work_dir(descendant)
    target.assert_awaited_once_with(71, descendant)


@pytest.mark.asyncio
async def test_bound_macos_runtime_rejects_different_session_workspace(monkeypatch, tmp_path):
    import kiro_crew.acp.runtime as runtime_mod

    runtime = AcpRuntime(work_dir=tmp_path)
    runtime._bound_workspace_fd = 72
    runtime._spawn_work_dir = str(tmp_path)
    monkeypatch.setattr(
        runtime_mod,
        "resolve_bound_session_workspace",
        AsyncMock(side_effect=runtime_mod.BoundWorkspaceMismatch("retargeted")),
    )

    with pytest.raises(AcpRuntimeError, match="exact workspace"):
        await runtime._session_work_dir(tmp_path / "retargeted")


@pytest.mark.asyncio
async def test_kill_cancellation_still_releases_bound_workspace(monkeypatch, tmp_path):
    import kiro_crew.acp.runtime as runtime_mod

    runtime = AcpRuntime(work_dir=tmp_path)
    runtime._bound_workspace_fd = 73
    runtime._spawn_work_dir = str(tmp_path)
    entered = asyncio.Event()
    closed: list[int] = []

    async def stalled_teardown(*, expected=False):
        entered.set()
        await asyncio.Event().wait()

    async def record_release(descriptor):
        closed.append(descriptor)

    monkeypatch.setattr(runtime, "_kill_inner", stalled_teardown)
    monkeypatch.setattr(runtime_mod, "release_bound_agent_workspace", record_release)

    task = asyncio.create_task(runtime.kill())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed == [73]
    assert runtime._bound_workspace_fd is None
    assert runtime._spawn_work_dir == str(tmp_path)


# ── Process death propagation ──


@pytest.mark.asyncio
async def test_process_exit_marks_dead_and_poisons_queues():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[3] = fut
    task = await _start_reader(rt)
    try:
        reader.feed_eof()  # empty readline → process exited
        # Pending future fails, every session queue gets a None poison sentinel.
        with pytest.raises(AcpRuntimeDead):
            await asyncio.wait_for(fut, timeout=1.0)
        assert await asyncio.wait_for(q["sA"].get(), timeout=1.0) is None
        assert await asyncio.wait_for(q["sB"].get(), timeout=1.0) is None
        assert rt._dead is True
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_mark_dead_is_idempotent():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    rt._mark_dead("first")
    rt._mark_dead("second")  # no-op, must not double-poison or raise
    assert await asyncio.wait_for(q["sA"].get(), timeout=1.0) is None
    assert q["sA"].empty()


# ── Death-log severity: deliberate teardown vs genuine death (#4052) ──
#
# A warm-pool TTL recycle tears runtimes down via kill() on a schedule; logging
# that at the same severity and shape as a crash made `kirocrew logs` misreport
# routine recycling as process death. These tests pin the split: kill() → INFO,
# every genuine death path → WARNING, and the state transitions identical.


def _death_records(caplog):
    """The 'AcpRuntime dead' records, selected by the raw log template so the
    assertions can check levelname (severity) separately from message shape."""
    return [r for r in caplog.records if str(r.msg).startswith("AcpRuntime dead")]


def _neuter_kill_side_effects(monkeypatch, proc):
    """Keep kill() away from the host: never signal the fake PID (4242 could be
    a real process), never touch the PID-tracking files."""
    import kiro_crew.acp.runtime as rt_mod

    proc.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(rt_mod.platform_compat, "kill_process_tree", lambda *a, **k: None)
    monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt_mod, "_untrack_pid", lambda p: None)
    monkeypatch.setattr(rt_mod, "_untrack_session_pid", lambda p: None)


@pytest.mark.asyncio
async def test_deliberate_kill_logs_info_and_still_fails_pending_futures(caplog, monkeypatch):
    """A deliberate kill(expected=True) of a LIVE runtime (pool recycle /
    session shutdown) must log the death at INFO — no WARNING — while
    everything non-log stays identical: pending futures still fail with
    AcpRuntimeDead and session queues are poisoned."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    q = _register(rt, "sA")
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=True)

    records = _death_records(caplog)
    assert [r.levelname for r in records] == ["INFO"]
    assert "killed" in records[0].getMessage()
    # Severity-only change: waiters still learn the runtime died.
    with pytest.raises(AcpRuntimeDead):
        await asyncio.wait_for(fut, timeout=1.0)
    assert await asyncio.wait_for(q["sA"].get(), timeout=1.0) is None


@pytest.mark.asyncio
async def test_kill_default_is_unexpected_and_warns(caplog, monkeypatch):
    """A bare kill() keeps the WARNING: the default is fail-safe so every
    cleanup kill on a failure path — initialize()'s failed-spawn cleanup, a
    failed session setup — and any future call site stays a WARNING without
    opting in."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill()

    assert [r.levelname for r in _death_records(caplog)] == ["WARNING"]


@pytest.mark.asyncio
async def test_kill_refuses_info_downgrade_when_process_already_exited(caplog, monkeypatch):
    """A replacement path can observe is_alive() == False (returncode set by
    the child watcher) and kill() before the reader loop marks the death.
    That is a genuine death being reaped, not a teardown this caller started:
    expected=True must be refused and the WARNING kept."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    proc.returncode = 1  # process already exited on its own; _dead still False

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=True)

    records = _death_records(caplog)
    assert [r.levelname for r in records] == ["WARNING"]
    assert "returncode=1" in records[0].getMessage()


@pytest.mark.asyncio
async def test_unexpected_process_exit_still_warns_with_diagnostic_shape(caplog):
    """A genuine death (process exited) keeps today's WARNING and its full
    diagnostic shape — reason with rc, returncode=, stderr_tail: — unchanged."""
    import logging

    rt, reader, proc = _make_runtime()
    proc.returncode = 1
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[3] = fut
    task = await _start_reader(rt)
    try:
        with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
            reader.feed_eof()  # empty readline → process exited
            with pytest.raises(AcpRuntimeDead):
                await asyncio.wait_for(fut, timeout=1.0)
    finally:
        await _stop_reader(task)

    records = _death_records(caplog)
    assert [r.levelname for r in records] == ["WARNING"]
    msg = records[0].getMessage()
    assert "process exited (rc=1)" in msg
    assert "returncode=1" in msg
    assert "stderr_tail: <none>" in msg


# ── Send paths ──


@pytest.mark.asyncio
async def test_send_request_registers_routing_and_increments_id():
    rt, _, proc = _make_runtime()
    _register(rt, "sA")
    rid = await rt.send_request("session/prompt", {"sessionId": "sA", "prompt": []})
    assert rt._routed_requests[rid] == "sA"
    # The next id advances.
    rid2 = await rt.send_request("session/prompt", {"sessionId": "sA"})
    assert rid2 != rid
    # Wire payload carries the id + method.
    sent = proc.stdin.write.call_args_list[0].args[0].decode()
    frame = json.loads(sent)
    assert frame["id"] == rid and frame["method"] == "session/prompt"
    proc.stdin.drain.assert_awaited()


@pytest.mark.asyncio
async def test_send_request_without_session_does_not_register_routing():
    rt, _, _ = _make_runtime()
    rid = await rt.send_request("initialize", {})  # no sessionId
    assert rid not in rt._routed_requests


@pytest.mark.asyncio
async def test_send_notification_has_no_id_and_no_routing():
    rt, _, proc = _make_runtime()
    _register(rt, "sA")
    before_id = rt._next_id
    await rt.send_notification("session/cancel", {"sessionId": "sA"})
    sent = proc.stdin.write.call_args_list[0].args[0].decode()
    frame = json.loads(sent)
    assert frame["method"] == "session/cancel"
    assert "id" not in frame  # notification: no id allocated
    assert rt._next_id == before_id  # id space untouched
    assert not rt._routed_requests  # nothing to leak


@pytest.mark.asyncio
async def test_send_request_on_dead_runtime_raises():
    rt, _, _ = _make_runtime()
    rt._dead = True
    with pytest.raises(AcpRuntimeDead):
        await rt.send_request("session/prompt", {"sessionId": "sA"})


# ── AcpSessionHandle behaviour ──


@pytest.mark.asyncio
async def test_handle_destroy_terminates_and_unregisters_session():
    """destroy() must evict the session on kiro-cli via _kiro.dev/session/terminate
    (freeing its transcript/context in the shared multiplexed process) AND
    unregister the local queue. A local-only unregister would leak the session
    in kiro-cli's in-memory map for the process's whole lifetime — the
    background-runtime unbounded-RSS bug this fix closes."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    rt._send_and_await = AsyncMock(return_value={})  # type: ignore[method-assign]
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.destroy()
    # kiro-cli was told to terminate exactly this session.
    rt._send_and_await.assert_awaited_once()
    assert rt._send_and_await.call_args.args[0] == METHOD_SESSION_TERMINATE
    assert rt._send_and_await.call_args.args[1] == {"sessionId": "sA"}
    # Local queue also unregistered.
    assert "sA" not in rt._session_queues


@pytest.mark.asyncio
async def test_terminate_session_sends_bounded_terminate_for_target_only():
    """terminate_session issues _kiro.dev/session/terminate for exactly the
    target sessionId with a bounded timeout (teardown can't stall on an
    unresponsive runtime), and unregisters ONLY that session — a co-tenant
    session on the shared runtime is untouched (unlike kill())."""
    rt, _, _ = _make_runtime()
    _register(rt, "sA", "sB")
    rt._send_and_await = AsyncMock(return_value={})  # type: ignore[method-assign]
    await rt.terminate_session("sA")
    rt._send_and_await.assert_awaited_once()
    assert rt._send_and_await.call_args.args[0] == METHOD_SESSION_TERMINATE
    assert rt._send_and_await.call_args.args[1] == {"sessionId": "sA"}
    assert rt._send_and_await.call_args.kwargs["timeout"] == _TERMINATE_TIMEOUT
    assert "sA" not in rt._session_queues
    assert "sB" in rt._session_queues  # co-tenant survives


@pytest.mark.asyncio
async def test_terminate_session_is_best_effort_when_send_fails():
    """If the terminate request fails (runtime slow/dead), teardown must NOT
    raise and MUST still unregister locally (incl. routed-request cleanup) so
    the reader stops routing to an abandoned queue."""
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    rt._routed_requests[5] = "sA"
    rt._send_and_await = AsyncMock(side_effect=AcpRuntimeError("timed out"))  # type: ignore[method-assign]
    await rt.terminate_session("sA")  # must not raise
    assert "sA" not in rt._session_queues
    assert 5 not in rt._routed_requests


@pytest.mark.asyncio
async def test_terminate_session_skips_roundtrip_when_dead():
    """A dead runtime already freed the session's memory with the process, so
    terminate skips the doomed round-trip but still unregisters locally."""
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    rt._dead = True
    rt._send_and_await = AsyncMock()  # type: ignore[method-assign]
    await rt.terminate_session("sA")
    rt._send_and_await.assert_not_awaited()
    assert "sA" not in rt._session_queues


@pytest.mark.asyncio
async def test_terminate_session_unregisters_even_on_cancellation():
    """If the terminate await is cancelled, the local unregister MUST still run.
    asyncio.CancelledError is a BaseException (not Exception in 3.9+), so it slips
    past the inner `except Exception`; the `finally` guarantees local cleanup so
    the reader loop stops routing to an abandoned queue. The cancellation itself
    still propagates (finally does not swallow it)."""
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    rt._send_and_await = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await rt.terminate_session("sA")
    assert "sA" not in rt._session_queues


# ── _is_stale / has_active_sessions ──


@pytest.mark.asyncio
async def test_is_stale_none_when_fresh_and_small(monkeypatch):
    """A freshly-spawned runtime is not stale; the RSS probe is skipped
    entirely because it is younger than the age band."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic()  # just spawned
    rt._max_rss_mb = 500.0
    called = {"n": 0}

    def _boom(pid):
        called["n"] += 1
        return 999999.0  # would be "stale" if ever consulted

    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", _boom)
    assert await rt._is_stale() is None
    assert called["n"] == 0  # young runtime never probes RSS


@pytest.mark.asyncio
async def test_is_stale_none_when_old_but_small_rss(monkeypatch):
    """Past the age band but below the RSS threshold → not stale. Exercises the
    small-RSS branch with a concrete value (not the None lookup-failure path)."""
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0  # older than the probe band
    rt._max_rss_mb = 500.0
    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", lambda pid: 10.0)
    assert await rt._is_stale() is None


@pytest.mark.asyncio
async def test_is_stale_age_when_past_max_age():
    """A runtime older than _max_age_secs is stale with reason 'age'."""
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 10.0
    rt._spawn_monotonic = time.monotonic() - 20.0
    assert await rt._is_stale() == "age"


@pytest.mark.asyncio
async def test_is_stale_rss_when_tree_over_threshold(monkeypatch):
    """Past the age band and RSS tree over threshold → stale with reason 'rss'."""
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0  # old enough to probe
    rt._max_rss_mb = 100.0
    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", lambda pid: 250.0)
    assert await rt._is_stale() == "rss"


@pytest.mark.asyncio
async def test_is_stale_none_when_no_pid():
    rt, _, _ = _make_runtime()
    rt._pid = None
    assert await rt._is_stale() is None


def test_stale_by_age_cheap_check():
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 10.0
    rt._spawn_monotonic = time.monotonic() - 20.0
    assert rt._stale_by_age() is True
    rt._spawn_monotonic = time.monotonic()
    assert rt._stale_by_age() is False
    rt._pid = None
    assert rt._stale_by_age() is False


def test_get_rss_mb_real_process():
    """_get_rss_mb parses a real process (this test process) and returns a
    positive MiB value; a nonexistent PID returns None. Skips where the
    platform can't introspect RSS (no /proc AND ps blocked, e.g. a locked-down
    macOS sandbox) — _get_rss_mb returns None there by design."""
    from kiro_crew.acp.runtime import _get_rss_mb

    rss = _get_rss_mb(os.getpid())
    if rss is None:
        pytest.skip("RSS introspection unavailable in this environment")
    assert rss > 0.0
    assert _get_rss_mb(2**31 - 1) is None  # nonexistent pid


def test_get_rss_tree_mb_real_process():
    """The real tree probe returns a positive sample for this process and None
    for a nonexistent PID.

    Do not compare this sample with a separate single-process RSS read: resident
    sets are live values and Windows may trim the working set between the two
    calls.  The deterministic root-plus-descendants arithmetic is covered with
    fixed values in ``test_platform_compat_coverage.py``.
    """
    from kiro_crew.acp.runtime import _get_rss_tree_mb

    tree = _get_rss_tree_mb(os.getpid())
    if tree is None:
        pytest.skip("RSS introspection unavailable in this environment")
    assert tree > 0.0
    assert _get_rss_tree_mb(2**31 - 1) is None


@pytest.mark.asyncio
async def test_has_active_sessions_false_when_empty():
    rt, _, _ = _make_runtime()
    assert rt.has_active_sessions() is False


@pytest.mark.asyncio
async def test_has_active_sessions_true_when_registered():
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    assert rt.has_active_sessions() is True


@pytest.mark.asyncio
async def test_handle_cancel_uses_notification():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    rt.send_notification = AsyncMock()  # type: ignore[method-assign]
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.cancel()
    rt.send_notification.assert_awaited_once()
    assert rt.send_notification.call_args.args[0] == "session/cancel"
    assert handle._cancelled is True


@pytest.mark.asyncio
async def test_concurrent_prompt_on_same_handle_rejected():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        # First turn is in-flight (no completion fed) — _turn_done stays clear.
        first = asyncio.ensure_future(handle.prompt("hello").__anext__())
        await asyncio.sleep(0.05)
        # A second prompt on the same handle must refuse rather than corrupt state.
        with pytest.raises(AcpRuntimeError):
            await handle.prompt("again").__anext__()
        first.cancel()
        try:
            await first
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        await _stop_reader(task)


# ── Headline: one runtime, many sessions, correct routing ──


@pytest.mark.asyncio
async def test_multiple_sessions_routed_independently():
    """Two concurrent prompt turns on ONE runtime each receive only their own
    session's text chunk and completion — proving sessionId demux isolates them.
    """
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    handle_a = AcpSessionHandle("sA", q["sA"], rt)
    handle_b = AcpSessionHandle("sB", q["sB"], rt)
    task = await _start_reader(rt)

    out_a: list = []
    out_b: list = []

    async def drive(handle, out):
        async for ev in handle.prompt("go", timeout=5.0):
            out.append(ev)

    da = asyncio.ensure_future(drive(handle_a, out_a))
    db = asyncio.ensure_future(drive(handle_b, out_b))
    try:
        # Let both turns issue their session/prompt requests and register routing.
        sid_to_req = await _await_routed(rt, "sA", "sB")
        assert set(sid_to_req) == {"sA", "sB"}, "both prompts must be in flight"

        # Interleave text chunks for the two sessions (out of order on purpose).
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sB",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "Bravo"},
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "Alpha"},
                },
            },
        )
        # Complete each turn via its own prompt response (routed by id).
        _feed(reader, {"id": sid_to_req["sA"], "result": {"stopReason": "end_turn"}})
        _feed(reader, {"id": sid_to_req["sB"], "result": {"stopReason": "end_turn"}})

        await asyncio.wait_for(asyncio.gather(da, db), timeout=5.0)

        text_a = "".join(e.text for e in out_a if e.kind == EVENT_TEXT_CHUNK)
        text_b = "".join(e.text for e in out_b if e.kind == EVENT_TEXT_CHUNK)
        assert text_a == "Alpha"
        assert text_b == "Bravo"
        # Cross-talk check: neither session saw the other's text.
        assert "Bravo" not in text_a
        assert "Alpha" not in text_b
        # Each turn ended with its own EVENT_COMPLETE.
        assert any(e.kind == EVENT_COMPLETE for e in out_a)
        assert any(e.kind == EVENT_COMPLETE for e in out_b)
    finally:
        for t in (da, db):
            if not t.done():
                t.cancel()
        await _stop_reader(task)


# ── AcpSessionHandle API method tests ──


@pytest.mark.asyncio
async def test_handle_session_id_property():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    assert handle.session_id == "sA"


@pytest.mark.asyncio
async def test_handle_is_turn_active():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # Freshly created — no active turn
    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_prompt_resets_turn_done_when_send_request_fails():
    """If send_request raises after _turn_done is cleared (e.g. AcpRuntimeDead on
    a broken pipe), the handle must NOT stay stuck as turn-active — otherwise
    every subsequent prompt() is permanently rejected with 'turn already active'."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(side_effect=AcpRuntimeDead("broken pipe"))

    gen = handle.prompt("hi", timeout=3.0)
    with pytest.raises(AcpRuntimeDead):
        await gen.__anext__()  # send_request fires on first iteration

    # Recovered: turn no longer active, so the handle is reusable.
    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_prompt_resets_turn_done_when_cancelled():
    """Same guard, but for cancellation — which is NOT an ``Exception``.

    ``asyncio.CancelledError`` derives from ``BaseException``, so an
    ``except Exception`` guard lets it through and leaves ``_turn_done`` cleared
    forever: ``is_turn_active`` reports True permanently and every later
    ``prompt()`` on the handle is rejected as already active. A turn timing out
    or being cancelled is routine, so this must recover.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(side_effect=asyncio.CancelledError())

    gen = handle.prompt("hi", timeout=3.0)
    with pytest.raises(asyncio.CancelledError):
        await gen.__anext__()

    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_prompt_resets_turn_done_when_cancelled_while_building_blocks():
    """Cancellation at the prompt-ASSEMBLY await, not the send await.

    Image reads are offloaded with ``asyncio.to_thread``, which adds a second
    cancellation point inside the turn-state guard — and a longer-lived one,
    since it does file I/O. Cancelling there must not wedge the handle either.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=1)

    with patch(
        "kiro_crew.acp.session_handle.build_prompt_blocks",
        side_effect=asyncio.CancelledError(),
    ):
        gen = handle.prompt("hi", timeout=3.0)
        with pytest.raises(asyncio.CancelledError):
            await gen.__anext__()

    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_handle_wait_turn_done_immediate():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # Already done — returns True immediately
    result = await handle.wait_turn_done(timeout=0.1)
    assert result is True


@pytest.mark.asyncio
async def test_handle_wait_turn_done_timeout():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._turn_done.clear()  # simulate active turn
    result = await handle.wait_turn_done(timeout=0.05)
    assert result is False


@pytest.mark.asyncio
async def test_handle_approve_tool():
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.approve_tool("req-7", option_id="allow_always")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["id"] == "req-7"
    assert sent["result"]["outcome"]["outcome"] == "selected"
    assert sent["result"]["outcome"]["optionId"] == "allow_always"


@pytest.mark.asyncio
async def test_handle_reject_tool():
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.reject_tool("req-8")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["id"] == "req-8"
    assert sent["result"]["outcome"]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_handle_set_mode():
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.set_mode("kirocrew-lite")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["method"] == "session/set_mode"
    assert sent["params"]["modeId"] == "kirocrew-lite"


@pytest.mark.asyncio
async def test_handle_set_model():
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.set_model("claude-sonnet-4")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["method"] == "session/set_model"
    assert sent["params"]["modelId"] == "claude-sonnet-4"


# ── send_response / send_error ──


@pytest.mark.asyncio
async def test_send_response_writes_json():
    rt, _, proc = _make_runtime()
    await rt.send_response("req-42", {"ok": True})
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["id"] == "req-42"
    assert sent["result"] == {"ok": True}
    assert "error" not in sent


@pytest.mark.asyncio
async def test_send_error_writes_json():
    rt, _, proc = _make_runtime()
    await rt.send_error("req-99", -32601, "Method not found")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["id"] == "req-99"
    assert sent["error"]["code"] == -32601
    assert sent["error"]["message"] == "Method not found"


@pytest.mark.asyncio
async def test_send_response_on_dead_runtime_raises():
    rt, _, _ = _make_runtime()
    rt._dead = True
    with pytest.raises(AcpRuntimeDead):
        await rt.send_response("x", {})


@pytest.mark.asyncio
async def test_send_error_on_dead_runtime_raises():
    rt, _, _ = _make_runtime()
    rt._dead = True
    with pytest.raises(AcpRuntimeDead):
        await rt.send_error("x", -1, "err")


# ── unregister_session cleans routed_requests ──


@pytest.mark.asyncio
async def test_unregister_session_cleans_routed_requests():
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    rt._routed_requests[10] = "sA"
    rt._routed_requests[11] = "sA"
    rt._routed_requests[12] = "sB"  # different session
    rt.unregister_session("sA")
    assert "sA" not in rt._session_queues
    assert 10 not in rt._routed_requests
    assert 11 not in rt._routed_requests
    assert 12 in rt._routed_requests  # sB untouched


# ── is_alive ──


@pytest.mark.asyncio
async def test_is_alive_true():
    rt, _, proc = _make_runtime()
    proc.returncode = None
    assert rt.is_alive() is True


@pytest.mark.asyncio
async def test_is_alive_false_when_dead():
    rt, _, _ = _make_runtime()
    rt._dead = True
    assert rt.is_alive() is False


@pytest.mark.asyncio
async def test_is_alive_false_when_no_process():
    rt, _, _ = _make_runtime()
    rt._process = None
    assert rt.is_alive() is False


# ── _dispatch_events: notification kind branches ──


@pytest.mark.asyncio
async def test_dispatch_permission_request():
    """Permission request notification yields EVENT_PERMISSION_REQUEST.

    Uses kiro-cli's REAL payload shape: the tool info is nested under
    ``params["toolCall"]`` (title/kind/toolCallId), NOT flat under ``params``.
    A prior ``tool_call`` update (kind="execute") seeds the trusted shell cache
    so the permission event resolves ``is_shell=True`` — the signal chat_runner's
    trust-mode gate needs to waive the tool-name length cap on shell commands.
    """
    from kiro_crew.acp.types import (
        EVENT_PERMISSION_REQUEST,
        METHOD_REQUEST_PERMISSION,
        METHOD_SESSION_UPDATE,
    )

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        # First a tool_call update (seeds the trusted is_shell cache).
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tcP",
                        "title": "git status",
                        "kind": "execute",
                    },
                },
            },
        )
        # Then the permission request in kiro's real toolCall-nested shape.
        _feed(
            reader,
            {
                "id": 5001,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "sessionId": "sA",
                    "toolCall": {"title": "git status", "kind": "execute", "toolCallId": "tcP"},
                    "options": [
                        {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                        {
                            "optionId": "allow_always",
                            "name": "Allow always",
                            "kind": "allow_always",
                        },
                    ],
                },
            },
        )
        # Then complete the turn
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        perm = [e for e in events if e.kind == EVENT_PERMISSION_REQUEST]
        assert len(perm) == 1
        assert perm[0].title == "git status"
        assert perm[0].request_id == 5001
        assert perm[0].tool_kind == "execute"
        assert perm[0].tool_call_id == "tcP"
        # The critical regression guard: is_shell must be True so the trust-mode
        # gate does not reject the long shell command title on the length cap.
        assert perm[0].is_shell is True
        # Advertised optionIds recorded so approve/reject echo the exact ids.
        assert handle._permission_options[5001] == {"once": "allow_once", "always": "allow_always"}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_approve_tool_echoes_recorded_option():
    """approve_tool echoes the advertised optionId recorded from the request."""
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # Simulate build_permission_event having recorded claude-agent-acp ids.
    handle._permission_options[42] = {"once": "allow", "always": "allow_always"}
    await handle.approve_tool(42)  # no explicit id → resolves the "once" variant
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["result"]["outcome"]["optionId"] == "allow"
    assert 42 not in handle._permission_options  # consumed on use


@pytest.mark.asyncio
async def test_reject_tool_prefers_recorded_reject_option():
    """reject_tool sends a clean 'selected' reject when one was advertised."""
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._permission_options[7] = {"once": "allow", "reject": "reject"}
    await handle.reject_tool(7)
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["result"]["outcome"]["outcome"] == "selected"
    assert sent["result"]["outcome"]["optionId"] == "reject"


@pytest.mark.asyncio
async def test_dispatch_tool_call_and_result():
    """Tool call + tool result notifications yield correct events."""
    from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_RESULT

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        # Tool call
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc1",
                        "title": "bash",
                        "kind": "shell",
                    },
                },
            },
        )
        # Tool result (real kiro 2.10.0 shape: nested block.content.text)
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc1",
                        "content": [{"content": {"type": "text", "text": "output here"}}],
                    },
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)

        tc = [e for e in events if e.kind == EVENT_TOOL_CALL]
        tr = [e for e in events if e.kind == EVENT_TOOL_RESULT]
        assert len(tc) == 1 and tc[0].tool_call_id == "tc1" and tc[0].title == "bash"
        assert len(tr) == 1 and tr[0].tool_output == "output here"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_tool_stall_cancels_session_not_runtime(monkeypatch):
    """A dispatched tool that goes silent must be recovered by a session-scoped
    session/cancel (so co-tenant sessions on the shared runtime survive), NOT by
    killing the runtime process. The turn ends with stop_reason 'tool_stall'."""
    from kiro_crew.acp.session_handle import WatchdogSettings
    from kiro_crew.acp.types import EVENT_COMPLETE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    rt.send_notification = AsyncMock()  # type: ignore[method-assign]
    handle = AcpSessionHandle(
        "sA",
        q["sA"],
        rt,
        watchdog=WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=0.05),
    )
    handle._tool_dispatched = True  # a tool was dispatched this turn
    handle._stale_eligible = False  # the stale-turn check must NOT be what fires

    # Queue that always times out (empty forever), advancing the wall clock a
    # little each poll so the stall idle window is crossed deterministically.
    class _SilentQueue:
        async def get(self):
            await asyncio.sleep(0.06)
            raise asyncio.TimeoutError

        def qsize(self) -> int:
            return 0  # always empty; TOCTOU guard sees no new frames

    handle._queue = _SilentQueue()  # type: ignore[assignment]

    events = []
    async for ev in handle._dispatch_events(req_id=1, timeout=30.0):
        events.append(ev)

    # Recovery was a session-scoped session/cancel for THIS sessionId — the
    # runtime process is never killed (no killpg/SIGKILL on the stall path).
    rt.send_notification.assert_awaited_once()
    assert rt.send_notification.call_args.args[0] == "session/cancel"
    assert rt.send_notification.call_args.args[1]["sessionId"] == "sA"
    # Turn ends cleanly, flagged as a stall.
    assert events and events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "error: tool stall"


@pytest.mark.asyncio
async def test_tool_stall_recovery_completes_even_if_cancel_fails(monkeypatch):
    """If session/cancel raises or times out (an unresponsive runtime is likely
    right after a stall), the watchdog must still complete the turn — the
    bounded wait_for + except must not let recovery hang or bubble."""
    from kiro_crew.acp.session_handle import WatchdogSettings
    from kiro_crew.acp.types import EVENT_COMPLETE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle(
        "sA",
        q["sA"],
        rt,
        watchdog=WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=0.05),
    )
    handle._tool_dispatched = True
    handle._stale_eligible = False
    # cancel() fails (stands in for the wait_for timeout path — both raise into
    # the same except Exception).
    handle.cancel = AsyncMock(side_effect=RuntimeError("runtime unresponsive"))  # type: ignore[method-assign]

    class _SilentQueue:
        async def get(self):
            await asyncio.sleep(0.06)
            raise asyncio.TimeoutError

        def qsize(self) -> int:
            return 0  # always empty; TOCTOU guard sees no new frames

    handle._queue = _SilentQueue()  # type: ignore[assignment]

    events = []
    async for ev in handle._dispatch_events(req_id=1, timeout=30.0):
        events.append(ev)

    handle.cancel.assert_awaited_once()
    assert events and events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "error: tool stall"


@pytest.mark.asyncio
async def test_dispatch_thinking_chunk():
    """agent_thought_chunk yields EVENT_THINKING_CHUNK."""
    from kiro_crew.acp.types import EVENT_THINKING_CHUNK

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "agent_thought_chunk", "text": "thinking..."},
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        think = [e for e in events if e.kind == EVENT_THINKING_CHUNK]
        assert len(think) == 1 and think[0].text == "thinking..."
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_compaction_and_clear():
    """Compaction and clear notifications yield appropriate events."""
    from kiro_crew.acp.types import (
        EVENT_CLEAR_STATUS,
        EVENT_COMPACTION_STATUS,
        METHOD_CLEAR_STATUS,
        METHOD_COMPACTION_STATUS,
    )

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_COMPACTION_STATUS,
                "params": {
                    "sessionId": "sA",
                    "status": {"type": "compacting"},
                    "summary": "50%",
                },
            },
        )
        _feed(reader, {"method": METHOD_CLEAR_STATUS, "params": {"sessionId": "sA"}})
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        comp = [e for e in events if e.kind == EVENT_COMPACTION_STATUS]
        clr = [e for e in events if e.kind == EVENT_CLEAR_STATUS]
        assert len(comp) == 1 and comp[0].text == "compacting"
        assert len(clr) == 1
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_compaction_completed_resets_context_stats():
    """A completed compaction in the prompt dispatch loop must drop the stale
    context-usage counts (regression: the meter froze at the pre-compaction
    value because context_tokens_from_usage=True blocked fresh metadata)."""
    from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, AcpPromptStats

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=75.0,
        context_used_tokens=150_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ev in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_COMPACTION_STATUS,
                "params": {
                    "sessionId": "sA",
                    "status": {"type": "completed"},
                    "summary": "squeezed",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        stats = handle.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_tokens_from_usage is False
        assert stats.context_window_tokens == 200_000  # model unchanged
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_wait_for_compaction_drain_path_resets_context_stats():
    """The async-after-end_turn drain path in wait_for_compaction bypasses the
    prompt dispatch loop, so it must drop the stale counts itself."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=75.0,
        context_used_tokens=150_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    # Poison the queue behind the status so the post-compaction metadata grace
    # drain exits immediately instead of sleeping out its window.
    q["sA"].put_nowait(None)

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    stats = handle.last_prompt_stats
    assert stats.context_pct == 0.0
    assert stats.context_used_tokens == 0
    assert stats.context_tokens_from_usage is False
    assert stats.context_window_tokens == 200_000


@pytest.mark.asyncio
async def test_wait_for_compaction_drain_applies_post_compaction_metadata():
    """kiro emits the real post-compaction pct ~1s after the completed status;
    the drain path must capture it and derive against the KEPT served window."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=90.0,
        context_used_tokens=900_000,
        context_window_tokens=1_000_000,  # served window (differs from registry)
        context_tokens_from_usage=True,
    )
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    q["sA"].put_nowait(
        JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"sessionId": "sA", "contextUsagePercentage": 5.0},
        )
    )

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    stats = handle.last_prompt_stats
    assert stats.context_pct == 5.0
    assert stats.context_window_tokens == 1_000_000
    assert stats.context_used_tokens == 50_000


@pytest.mark.asyncio
async def test_post_compaction_drain_requeues_frames_before_poison():
    """Death during the grace drain: buffered frames must be re-queued BEFORE
    the poison sentinel, or recovery would see death first and strand them."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=75.0,
        context_used_tokens=150_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    stray = JsonRpcMessage(method="session/update", params={"sessionId": "sA", "update": {}})
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    q["sA"].put_nowait(stray)
    q["sA"].put_nowait(None)

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    # Order restored: the stray frame first, the poison sentinel last.
    assert q["sA"].get_nowait() is stray
    assert q["sA"].get_nowait() is None


@pytest.mark.asyncio
async def test_outer_buffered_frame_restored_before_poison_from_nested_drain():
    """A frame buffered by wait_for_compaction ITSELF (before the completed
    status) must also be restored ahead of a poison consumed by the NESTED
    grace drain — separate buffers restored at different times would park the
    frame behind the death sentinel and its consumer would see AcpProcessDied
    despite a completed command."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=75.0,
        context_used_tokens=150_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    stray = JsonRpcMessage(method="session/update", params={"sessionId": "sA", "update": {}})
    q["sA"].put_nowait(stray)  # buffered by the OUTER wait loop
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    q["sA"].put_nowait(None)  # death consumed by the NESTED drain

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    assert q["sA"].get_nowait() is stray
    assert q["sA"].get_nowait() is None


@pytest.mark.asyncio
async def test_drain_passes_metering_frames_through_for_next_turn_billing():
    """A late meteringUsage frame must NOT be consumed by the grace drain —
    on the between-turns auto-compact path the credits would land in a stats
    window nothing reads and be wiped by the next prompt's re-init. The frame
    is re-queued untouched so the next turn's dispatch loop bills it."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=90.0,
        context_used_tokens=900_000,
        context_window_tokens=1_000_000,
        context_tokens_from_usage=True,
    )
    metering = JsonRpcMessage(
        method="_kiro.dev/metadata",
        params={"sessionId": "sA", "meteringUsage": [{"unit": "credit", "amount": 0.5}]},
    )
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    q["sA"].put_nowait(metering)
    q["sA"].put_nowait(
        JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"sessionId": "sA", "contextUsagePercentage": 5.0},
        )
    )

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    stats = handle.last_prompt_stats
    # The pct frame WAS applied...
    assert stats.context_pct == 5.0
    # ...but the metering frame was neither billed to the dead window nor lost:
    assert stats.credits == 0.0
    assert q["sA"].get_nowait() is metering


@pytest.mark.asyncio
async def test_wait_for_compaction_cached_result_applies_post_compaction_metadata():
    """The mid-turn cached path (compact() captured the completed status while
    draining its own prompt) must also grace-drain for the metadata."""
    from kiro_crew.acp.types import AcpPromptStats, JsonRpcMessage

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # The dispatch loop already reset the stats when it captured the result.
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=0.0,
        context_used_tokens=0,
        context_window_tokens=1_000_000,
        context_tokens_from_usage=False,
    )
    handle._compact_result = {"type": "completed", "summary": "ok"}
    q["sA"].put_nowait(
        JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"sessionId": "sA", "contextUsagePercentage": 5.0},
        )
    )

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    stats = handle.last_prompt_stats
    assert stats.context_pct == 5.0
    assert stats.context_used_tokens == 50_000


@pytest.mark.asyncio
async def test_dispatch_agent_switched():
    """Agent switched notification yields EVENT_AGENT_SWITCHED."""
    from kiro_crew.acp.types import EVENT_AGENT_SWITCHED, METHOD_AGENT_SWITCHED

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_AGENT_SWITCHED,
                "params": {
                    "sessionId": "sA",
                    "agentName": "kirocrew-lite",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        sw = [e for e in events if e.kind == EVENT_AGENT_SWITCHED]
        assert len(sw) == 1 and sw[0].text == "kirocrew-lite"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_mcp_oauth_request():
    """MCP OAuth request notification yields EVENT_MCP_OAUTH_REQUEST."""
    from kiro_crew.acp.types import EVENT_MCP_OAUTH_REQUEST, METHOD_MCP_OAUTH_REQUEST

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {
                    "sessionId": "sA",
                    "serverName": "github-mcp",
                    "oauthUrl": "https://auth.example.com",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        oauth = [e for e in events if e.kind == EVENT_MCP_OAUTH_REQUEST]
        assert len(oauth) == 1
        assert oauth[0].server_name == "github-mcp"
        assert oauth[0].oauth_url == "https://auth.example.com"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_mcp_server_initialized():
    """MCP server initialized yields EVENT_MCP_SERVER_INITIALIZED."""
    from kiro_crew.acp.types import EVENT_MCP_SERVER_INITIALIZED, METHOD_MCP_SERVER_INITIALIZED

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_MCP_SERVER_INITIALIZED,
                "params": {
                    "sessionId": "sA",
                    "serverName": "builder-mcp",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        init = [e for e in events if e.kind == EVENT_MCP_SERVER_INITIALIZED]
        assert len(init) == 1 and init[0].server_name == "builder-mcp"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_mcp_server_init_failure():
    """MCP server init failure yields EVENT_MCP_SERVER_INIT_FAILURE."""
    from kiro_crew.acp.types import EVENT_MCP_SERVER_INIT_FAILURE, METHOD_MCP_SERVER_INIT_FAILURE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_MCP_SERVER_INIT_FAILURE,
                "params": {
                    "sessionId": "sA",
                    "serverName": "bad-mcp",
                    "error": "timeout",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        fail = [e for e in events if e.kind == EVENT_MCP_SERVER_INIT_FAILURE]
        assert len(fail) == 1 and fail[0].server_name == "bad-mcp" and fail[0].text == "timeout"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_unknown_server_request_gets_error_response():
    """Unknown server→client request gets a -32601 error response."""
    rt, reader, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        # Unknown method WITH an id (server request, not notification)
        _feed(reader, {"id": 9999, "method": "unknown/method", "params": {"sessionId": "sA"}})
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        # Check that an error response was sent back
        calls = proc.stdin.write.call_args_list
        error_sent = False
        for call in calls:
            data = json.loads(call.args[0].decode())
            if data.get("id") == 9999 and "error" in data:
                assert data["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
                error_sent = True
        assert error_sent, "Expected -32601 error response for unknown server request"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_tool_call_update_raw_output():
    """tool_call_update with rawOutput yields EVENT_TOOL_RESULT."""
    from kiro_crew.acp.types import EVENT_TOOL_RESULT

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc2",
                        "rawOutput": {"items": [{"Text": "raw stuff"}]},
                    },
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        tr = [e for e in events if e.kind == EVENT_TOOL_RESULT]
        assert len(tr) == 1 and tr[0].tool_output == "raw stuff"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_tool_call_update_refinement():
    """tool_call_update with title but no content yields EVENT_TOOL_CALL_UPDATE."""
    from kiro_crew.acp.types import EVENT_TOOL_CALL_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc3",
                        "title": "Reading file",
                        "kind": "fs",
                        "rawInput": "/etc/hosts",
                    },
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        tu = [e for e in events if e.kind == EVENT_TOOL_CALL_UPDATE]
        assert len(tu) == 1
        assert tu[0].title == "Reading file"
        assert tu[0].tool_input == "/etc/hosts"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_usage_update():
    """usage_update sets context stats on last_prompt_stats."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "usage_update",
                        "usage": {"used": 5000, "size": 10000},
                    },
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        assert handle.last_prompt_stats.context_pct == 50.0
        assert handle.last_prompt_stats.context_used_tokens == 5000
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_cost_and_prompt_tokens_reach_event_complete():
    """claude seam billing: a session-cumulative usage_update cost and the
    PromptResponse token counts are delta'd/folded into last_prompt_stats and
    surfaced on EVENT_COMPLETE.usage — the wiring issue #6750 adds. Two turns
    prove the delta: turn 2 is billed only its own movement of the cumulative
    counter, and its own token counts."""
    from kiro_crew.acp.types import EVENT_COMPLETE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def run_turn(cost_amount, input_tokens, output_tokens):
            events: list = []

            async def drive():
                async for ev in handle.prompt("hi", timeout=3.0):
                    events.append(ev)

            driver = asyncio.ensure_future(drive())
            req_id = (await _await_routed(rt, "sA"))["sA"]
            _feed(
                reader,
                {
                    "method": METHOD_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sA",
                        "update": {
                            "sessionUpdate": "usage_update",
                            "used": 5000,
                            "size": 10000,
                            "cost": {"amount": cost_amount, "currency": "USD"},
                        },
                    },
                },
            )
            _feed(
                reader,
                {
                    "id": req_id,
                    "result": {
                        "stopReason": "end_turn",
                        "inputTokens": input_tokens,
                        "outputTokens": output_tokens,
                        "cachedReadTokens": 7,
                        "cachedWriteTokens": 3,
                    },
                },
            )
            await asyncio.wait_for(driver, timeout=3.0)
            (complete,) = [ev for ev in events if ev.kind == EVENT_COMPLETE]
            return complete

        first = await run_turn(0.30, 100, 50)
        assert first.usage.cost_usd == pytest.approx(0.30)
        assert first.usage.input_tokens == 100
        assert first.usage.output_tokens == 50
        assert first.usage.cache_read_tokens == 7
        assert first.usage.cache_creation_tokens == 3

        # Turn 2: the cumulative counter moved 0.30 -> 0.50, so this turn's
        # cost is the 0.20 delta, and the token counts are its own, not a sum.
        second = await run_turn(0.50, 40, 20)
        assert second.usage.cost_usd == pytest.approx(0.20)
        assert second.usage.input_tokens == 40
        assert second.usage.output_tokens == 20
    finally:
        await _stop_reader(task)


@pytest.mark.parametrize(
    "cost",
    [
        "0.5",  # cost not dict-shaped
        {"amount": "0.5"},
        {"amount": True},
        {"amount": float("nan")},
        {"amount": -0.01},
        {"amount": 10**400},
    ],
)
def test_handle_update_malformed_cost_is_noop(cost):
    """A malformed agent-supplied cost must degrade to absent at the
    parse_usage_cost chokepoint — no exception inside the prompt-turn dispatch
    path, and no stats movement."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    msg = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sA",
            "update": {"sessionUpdate": "usage_update", "used": 1, "size": 2, "cost": cost},
        },
    )
    events = handle._handle_update(msg)  # must not raise
    assert events == []
    assert handle.last_prompt_stats.cost_usd == 0.0
    assert handle.last_prompt_stats.cost_session_usd == 0.0


@pytest.mark.parametrize(
    "used,size",
    [
        ("5000", "10000"),  # numeric strings
        (float("inf"), 10000),
        (float("nan"), float("nan")),
        (10**400, 10000),  # bignum: math.isfinite itself raises OverflowError
        ([5000], {"n": 1}),
        (True, True),
    ],
)
def test_handle_update_malformed_usage_is_noop(used, size):
    """The session-handle path consumes the same agent-supplied usage_update as
    AcpClient. parse_usage_update validates at the shared chokepoint, so a
    malformed used/size must be a no-op here too — not a TypeError/
    OverflowError inside the prompt-turn dispatch."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    msg = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sA",
            "update": {"sessionUpdate": "usage_update", "used": used, "size": size},
        },
    )
    events = handle._handle_update(msg)  # must not raise
    assert events == []
    assert handle.last_prompt_stats.context_pct == 0.0
    assert handle.last_prompt_stats.context_used_tokens == 0


@pytest.mark.asyncio
async def test_dispatch_metadata_credits():
    """_kiro.dev/metadata meteringUsage(unit=credit) accumulates into last_prompt_stats
    and is propagated onto EVENT_COMPLETE; non-credit units are ignored."""
    from kiro_crew.acp.types import EVENT_COMPLETE, METHOD_METADATA

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events: list = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_METADATA,
                "params": {
                    "sessionId": "sA",
                    "contextUsagePercentage": 12.5,
                    "meteringUsage": [
                        {"unit": "credit", "value": 1.0},
                        {"unit": "token", "value": 999},  # not a credit — ignored
                        {"unit": "credit", "value": 0.23},
                    ],
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        assert handle.last_prompt_stats.credits == pytest.approx(1.23)
        assert handle.last_prompt_stats.context_pct == 12.5
        complete = [e for e in events if e.kind == EVENT_COMPLETE]
        assert complete and complete[-1].usage.credits == pytest.approx(1.23)
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_metadata_credits_robust():
    """Non-numeric / missing meteringUsage values and metadata with no meteringUsage
    are handled without raising; credits stays 0."""
    from kiro_crew.acp.types import METHOD_METADATA

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_METADATA,
                "params": {
                    "sessionId": "sA",
                    "meteringUsage": [{"unit": "credit", "value": "oops"}, {"unit": "credit"}],
                },
            },
        )
        _feed(
            reader, {"method": METHOD_METADATA, "params": {"sessionId": "sA"}}
        )  # no meteringUsage
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        assert handle.last_prompt_stats.credits == 0.0
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_metadata_credits_routed_per_session():
    """Concurrent sessions on one runtime each accrue only their own kiro credits —
    metadata notifications are demuxed by sessionId, no cross-talk."""
    from kiro_crew.acp.types import METHOD_METADATA

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    handle_a = AcpSessionHandle("sA", q["sA"], rt)
    handle_b = AcpSessionHandle("sB", q["sB"], rt)
    task = await _start_reader(rt)

    async def drive(handle):
        async for _ in handle.prompt("go", timeout=5.0):
            pass

    da = asyncio.ensure_future(drive(handle_a))
    db = asyncio.ensure_future(drive(handle_b))
    try:
        sid_to_req = await _await_routed(rt, "sA", "sB")
        assert set(sid_to_req) == {"sA", "sB"}, "both prompts must be in flight"

        _feed(
            reader,
            {
                "method": METHOD_METADATA,
                "params": {
                    "sessionId": "sA",
                    "meteringUsage": [{"unit": "credit", "value": 2.0}],
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_METADATA,
                "params": {
                    "sessionId": "sB",
                    "meteringUsage": [{"unit": "credit", "value": 0.5}],
                },
            },
        )
        _feed(reader, {"id": sid_to_req["sA"], "result": {"stopReason": "end_turn"}})
        _feed(reader, {"id": sid_to_req["sB"], "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(asyncio.gather(da, db), timeout=5.0)

        assert handle_a.last_prompt_stats.credits == pytest.approx(2.0)
        assert handle_b.last_prompt_stats.credits == pytest.approx(0.5)
    finally:
        for t in (da, db):
            if not t.done():
                t.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_list():
    """Subagent list notification yields EVENT_SUBAGENT_LIST."""
    from kiro_crew.acp.types import EVENT_SUBAGENT_LIST, METHOD_SUBAGENT_LIST_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SUBAGENT_LIST_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "subagents": [{"id": "sub1"}],
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        sl = [e for e in events if e.kind == EVENT_SUBAGENT_LIST]
        assert len(sl) == 1 and sl[0].subagents == [{"id": "sub1"}]
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_activity_tool():
    """Subagent activity with toolCallId yields EVENT_SUBAGENT_ACTIVITY.

    The kiro frame's params.sessionId is the SUB-session id (not the parent's
    registered session), so the reader would correctly drop it. We inject it
    straight into the parent's queue to exercise the dispatch branch.
    """
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, METHOD_KIRO_SESSION_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sub-1",
                        "update": {"toolCallId": "tc5", "title": "read file"},
                    },
                }
            )
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        sa = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert len(sa) == 1
        assert sa[0].sub_session_id == "sub-1"
        assert sa[0].tool_call_id == "tc5"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_activity_text():
    """Subagent activity with agent_message_chunk yields text event."""
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, METHOD_KIRO_SESSION_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sub-2",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "text": "hello from sub",
                        },
                    },
                }
            )
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        sa = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert len(sa) == 1
        assert sa[0].text == "hello from sub"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_activity_text_is_redacted():
    """Sub-agent streamed text is LLM output surfaced on the dashboard, so it
    MUST be scrubbed (credentials + exfil URLs) before being yielded."""
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, METHOD_KIRO_SESSION_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sub-3",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "text": "leaked AKIAIOSFODNN7EXAMPLE key",
                        },
                    },
                }
            )
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)
        sa = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert len(sa) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in sa[0].text
        assert "[REDACTED: credential]" in sa[0].text
    finally:
        await _stop_reader(task)


# ── Error during prompt turn ──


@pytest.mark.asyncio
async def test_prompt_error_response_raises():
    """An error response for the prompt request raises AcpError."""
    from kiro_crew.acp.client import AcpError

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(reader, {"id": req_id, "error": {"code": -1, "message": "throttled"}})
        with pytest.raises(AcpError):
            await asyncio.wait_for(driver, timeout=3.0)
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_prompt_transient_error_sets_transient_flag():
    """A transient backend 5xx error response (a mid-stream InternalServerError
    surfaced as JSON-RPC -32603) raises AcpError with transient=True, so the
    chat_runner / llm_helpers retry ladder fires instead of a bare error card.
    Regression for the kiro raise site that previously lacked the flag."""
    from kiro_crew.acp.client import AcpError

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": (
                        "Encountered an error in the response stream: "
                        "CodewhispererChatResponseStream(ServiceError(InternalServerError "
                        '{ message: "...please try again." }))'
                    ),
                },
            },
        )
        with pytest.raises(AcpError) as excinfo:
            await asyncio.wait_for(driver, timeout=3.0)
        assert excinfo.value.transient is True
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_prompt_auth_error_not_transient():
    """An auth error response raises AcpError with transient=False so it fails
    fast — a retry cannot fix an expired/denied credential."""
    from kiro_crew.acp.client import AcpError

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": "ExpiredTokenException: signature expired",
                },
            },
        )
        with pytest.raises(AcpError) as excinfo:
            await asyncio.wait_for(driver, timeout=3.0)
        assert excinfo.value.transient is False
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_wait_for_response_transient_error_sets_flag():
    """The non-streaming _wait_for_response path also classifies a transient
    backend 5xx (a -32603 InternalServerError) as transient=True, so
    request/response turns (session/new, set_mode, cancel, …) share the same
    retry eligibility. Covers the second kiro raise site."""
    from kiro_crew.acp.client import AcpError

    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "id": 7,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": (
                        "Encountered an error in the response stream: "
                        "InternalServerError ... please try again."
                    ),
                },
            }
        )
    )
    with pytest.raises(AcpError) as excinfo:
        await handle._wait_for_response(7, timeout=3.0)
    assert excinfo.value.transient is True


# ── Runtime properties ──


@pytest.mark.asyncio
async def test_runtime_pid():
    rt, _, _ = _make_runtime()
    assert rt.pid == 4242


# ── Multi-session routing: stronger guarantees ──


@pytest.mark.asyncio
async def test_n_sessions_routed_independently():
    """Five concurrent prompt turns on ONE runtime each receive exactly their
    own session's text + completion — no cross-talk at higher fan-out.
    """
    n = 5
    sids = [f"s{i}" for i in range(n)]
    rt, reader, _ = _make_runtime()
    q = _register(rt, *sids)
    handles = {sid: AcpSessionHandle(sid, q[sid], rt) for sid in sids}
    task = await _start_reader(rt)

    out: dict[str, list] = {sid: [] for sid in sids}

    async def drive(sid):
        async for ev in handles[sid].prompt("go", timeout=5.0):
            out[sid].append(ev)

    drivers = [asyncio.ensure_future(drive(sid)) for sid in sids]
    try:
        sid_to_req = await _await_routed(rt, *sids)
        assert set(sid_to_req) == set(sids), "all prompts must be in flight"

        # Feed each session a uniquely-identifying text chunk, reverse order.
        for sid in reversed(sids):
            _feed(
                reader,
                {
                    "method": METHOD_SESSION_UPDATE,
                    "params": {
                        "sessionId": sid,
                        "update": {"sessionUpdate": "agent_message_chunk", "text": f"text-{sid}"},
                    },
                },
            )
        # Complete every turn (responses routed by id).
        for sid in sids:
            _feed(reader, {"id": sid_to_req[sid], "result": {"stopReason": "end_turn"}})

        await asyncio.wait_for(asyncio.gather(*drivers), timeout=5.0)

        for sid in sids:
            text = "".join(e.text for e in out[sid] if e.kind == EVENT_TEXT_CHUNK)
            assert text == f"text-{sid}", f"session {sid} got wrong/cross text: {text!r}"
            assert any(e.kind == EVENT_COMPLETE for e in out[sid])
    finally:
        for t in drivers:
            if not t.done():
                t.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_one_session_errors_others_unaffected():
    """When one session's turn errors, the other concurrent session still
    completes normally — failures are isolated per session.
    """
    from kiro_crew.acp.client import AcpError

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sOk", "sErr")
    h_ok = AcpSessionHandle("sOk", q["sOk"], rt)
    h_err = AcpSessionHandle("sErr", q["sErr"], rt)
    task = await _start_reader(rt)

    ok_out: list = []
    err_exc: list = []

    async def drive_ok():
        async for ev in h_ok.prompt("go", timeout=5.0):
            ok_out.append(ev)

    async def drive_err():
        try:
            async for _ in h_err.prompt("go", timeout=5.0):
                pass
        except AcpError as exc:  # noqa: BLE001
            err_exc.append(exc)

    d_ok = asyncio.ensure_future(drive_ok())
    d_err = asyncio.ensure_future(drive_err())
    try:
        sid_to_req = await _await_routed(rt, "sOk", "sErr")
        # sErr gets an error response; sOk gets text + normal completion.
        _feed(reader, {"id": sid_to_req["sErr"], "error": {"code": -1, "message": "boom"}})
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sOk",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "fine"},
                },
            },
        )
        _feed(reader, {"id": sid_to_req["sOk"], "result": {"stopReason": "end_turn"}})

        await asyncio.wait_for(asyncio.gather(d_ok, d_err), timeout=5.0)

        assert len(err_exc) == 1, "errored session should raise AcpError"
        ok_text = "".join(e.text for e in ok_out if e.kind == EVENT_TEXT_CHUNK)
        assert ok_text == "fine"
        assert any(e.kind == EVENT_COMPLETE for e in ok_out)
        # The errored session's turn is marked done (does not wedge the runtime).
        assert not h_err.is_turn_active
    finally:
        for t in (d_ok, d_err):
            if not t.done():
                t.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_interleaved_tool_calls_routed_per_session():
    """tool_call frames for two concurrent sessions are each delivered only to
    the originating session's stream.
    """
    from kiro_crew.acp.types import EVENT_TOOL_CALL

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    h_a = AcpSessionHandle("sA", q["sA"], rt)
    h_b = AcpSessionHandle("sB", q["sB"], rt)
    task = await _start_reader(rt)

    out_a: list = []
    out_b: list = []

    async def drive(handle, out):
        async for ev in handle.prompt("go", timeout=5.0):
            out.append(ev)

    da = asyncio.ensure_future(drive(h_a, out_a))
    db = asyncio.ensure_future(drive(h_b, out_b))
    try:
        sid_to_req = await _await_routed(rt, "sA", "sB")
        # Interleave tool calls for each session.
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "a1",
                        "title": "toolA",
                        "kind": "shell",
                    },
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sB",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "b1",
                        "title": "toolB",
                        "kind": "fs",
                    },
                },
            },
        )
        _feed(reader, {"id": sid_to_req["sA"], "result": {"stopReason": "end_turn"}})
        _feed(reader, {"id": sid_to_req["sB"], "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(asyncio.gather(da, db), timeout=5.0)

        tc_a = [e for e in out_a if e.kind == EVENT_TOOL_CALL]
        tc_b = [e for e in out_b if e.kind == EVENT_TOOL_CALL]
        assert len(tc_a) == 1 and tc_a[0].tool_call_id == "a1" and tc_a[0].title == "toolA"
        assert len(tc_b) == 1 and tc_b[0].tool_call_id == "b1" and tc_b[0].title == "toolB"
        # No cross-talk: session A never saw session B's tool call and vice versa.
        assert all(e.tool_call_id != "b1" for e in out_a)
        assert all(e.tool_call_id != "a1" for e in out_b)
    finally:
        for t in (da, db):
            if not t.done():
                t.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_destroyed_session_stops_receiving_frames():
    """After a session is destroyed, frames tagged with its id are dropped and
    do NOT leak into a sibling session that is still active.
    """
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    # destroy() now round-trips _kiro.dev/session/terminate; no reader is running
    # yet here, so ack it instantly to avoid the bounded terminate timeout.
    rt._send_and_await = AsyncMock(return_value={})  # type: ignore[method-assign]
    h_a = AcpSessionHandle("sA", q["sA"], rt)
    await h_a.destroy()  # sA terminated + unregistered
    task = await _start_reader(rt)
    try:
        # Frame for the destroyed session must be dropped (not broadcast to sB).
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "ghost"},
                },
            },
        )
        # A legitimate frame for sB still routes.
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sB",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "live"},
                },
            },
        )
        msg = await asyncio.wait_for(q["sB"].get(), timeout=1.0)
        assert msg.params["sessionId"] == "sB"
        # sB's queue must not contain the ghost frame.
        assert q["sB"].empty()
    finally:
        await _stop_reader(task)


# ── Tests for Phase 3 unification: AcpSessionHandle gap-fill methods ──


class TestAcpSessionHandleCommands:
    """Tests for send_command and set_config_option."""

    @pytest.mark.asyncio
    async def test_send_command_plain(self):
        """send_command with no args sends plain string command."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        # Mock send_request to capture what's sent and return a fake req_id
        sent_payloads = []
        req_counter = [100]

        async def capture_send(method, params):
            sent_payloads.append((method, params))
            req_id = req_counter[0]
            req_counter[0] += 1
            # Put a fake response in the queue so _wait_for_response resolves
            resp_msg = JsonRpcMessage.from_dict({"id": req_id, "result": {"text": "compacted"}})
            await q["s1"].put(resp_msg)
            return req_id

        rt.send_request = capture_send
        result = await handle.send_command("/compact")
        assert result == "compacted"
        assert sent_payloads[0][0] == METHOD_COMMANDS_EXECUTE
        assert sent_payloads[0][1]["command"] == "/compact"
        assert sent_payloads[0][1]["sessionId"] == "s1"

    @pytest.mark.asyncio
    async def test_send_command_with_args(self):
        """send_command with args sends TuiCommand object form."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        sent_payloads = []
        req_counter = [200]

        async def capture_send(method, params):
            sent_payloads.append((method, params))
            req_id = req_counter[0]
            req_counter[0] += 1
            resp_msg = JsonRpcMessage.from_dict({"id": req_id, "result": {"text": "ok"}})
            await q["s1"].put(resp_msg)
            return req_id

        rt.send_request = capture_send
        result = await handle.send_command("/effort", args={"level": "high"})
        assert result == "ok"
        cmd = sent_payloads[0][1]["command"]
        assert isinstance(cmd, dict)
        assert cmd["command"] == "effort"
        assert cmd["args"] == {"level": "high"}

    @pytest.mark.asyncio
    async def test_set_config_option(self):
        """set_config_option sends correct JSON-RPC request."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        sent_payloads = []
        req_counter = [300]

        async def capture_send(method, params):
            sent_payloads.append((method, params))
            req_id = req_counter[0]
            req_counter[0] += 1
            resp_msg = JsonRpcMessage.from_dict({"id": req_id, "result": {}})
            await q["s1"].put(resp_msg)
            return req_id

        rt.send_request = capture_send
        await handle.set_config_option("effort", "high")
        assert sent_payloads[0][0] == METHOD_SET_CONFIG_OPTION
        assert sent_payloads[0][1] == {
            "sessionId": "s1",
            "configId": "effort",
            "value": "high",
        }


class TestAcpSessionHandleState:
    """Tests for state tracking properties."""

    def test_initial_state(self):
        """New handle has empty state."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        assert handle.model == ""
        assert handle.config_options == []
        assert handle.available_models == []

    def test_store_session_config(self):
        """store_session_config populates configOptions and available models."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        resp = {
            "sessionId": "s1",
            "configOptions": [
                {"id": "effort", "options": [{"value": "low"}, {"value": "high"}]},
            ],
            "models": {
                "availableModels": [
                    {"modelId": "opus-4", "name": "Claude Opus 4"},
                    {"modelId": "sonnet-4", "name": "Claude Sonnet 4"},
                ],
            },
        }
        handle.store_session_config(resp)
        assert len(handle.config_options) == 1
        assert handle.config_options[0]["id"] == "effort"
        assert len(handle.available_models) == 2
        assert handle.available_models[0]["modelId"] == "opus-4"

    def test_supports_config_option(self):
        """supports_config_option checks for matching id."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        # No options yet — returns True (lazy backend assumption)
        assert handle.supports_config_option("effort") is True

        handle._config_options = [{"id": "effort", "options": []}]
        assert handle.supports_config_option("effort") is True
        assert handle.supports_config_option("mode") is False

    def test_get_valid_effort_levels(self):
        """get_valid_effort_levels extracts from config options."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        handle._config_options = [
            {
                "id": "effort",
                "options": [
                    {"value": "low", "label": "Low"},
                    {"value": "medium", "label": "Medium"},
                    {"value": "high", "label": "High"},
                ],
            },
        ]
        assert handle.get_valid_effort_levels() == ["low", "medium", "high"]

    def test_set_model_updates_state(self):
        """set_model updates the _model field."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        # Directly set to test the state (set_model is async and would need send_request)
        handle._model = "opus-4"
        assert handle.model == "opus-4"

    def test_config_option_update_in_handle_update(self):
        """_handle_update processes config_option_update by updating state."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        msg = JsonRpcMessage.from_dict(
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "s1",
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": [
                            {"id": "effort", "options": [{"value": "extreme"}]},
                        ],
                    },
                },
            }
        )
        events = handle._handle_update(msg)
        assert events == []  # No event emitted
        assert len(handle._config_options) == 1
        assert handle._config_options[0]["id"] == "effort"


class TestAcpSessionHandleResponsiveness:
    """Tests for is_responsive."""

    def test_responsive_when_alive_and_recent(self):
        """is_responsive returns True when runtime is alive with recent activity."""
        rt, _, _ = _make_runtime()
        rt._last_activity = time.monotonic()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        assert handle.is_responsive() is True

    def test_not_responsive_when_stale(self):
        """is_responsive returns False when activity is old."""
        rt, _, _ = _make_runtime()
        rt._last_activity = time.monotonic() - 700  # 700s ago, threshold is 600
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        assert handle.is_responsive(stale_threshold=600.0) is False

    def test_not_responsive_when_dead(self):
        """is_responsive returns False when runtime is dead."""
        rt, _, _ = _make_runtime()
        rt._dead = True
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        assert handle.is_responsive() is False


class TestAcpRuntimePidTracking:
    """kill() must untrack the runtime PID from the orphan-sweep files so a
    dead entry isn't chased (mirrors AcpClient._reset_state). Spawn-side
    tracking is covered indirectly — it uses the same session_pid helpers."""

    @pytest.mark.asyncio
    async def test_kill_untracks_pid(self, monkeypatch):
        rt, _, proc = _make_runtime()
        proc.wait = AsyncMock(return_value=0)

        calls: dict[str, list[int]] = {"pid": [], "session": []}
        import kiro_crew.acp.runtime as rt_mod

        # runtime.py imports these at module top (from kiro_crew.session_pid
        # import _untrack_pid, _untrack_session_pid), so kill() resolves them in
        # the runtime namespace — patch WHERE USED, not the source module.
        monkeypatch.setattr(rt_mod, "_untrack_pid", lambda p: calls["pid"].append(p))
        monkeypatch.setattr(rt_mod, "_untrack_session_pid", lambda p: calls["session"].append(p))
        # os.killpg / getpgid on the fake PID would raise — the kill() body
        # already guards those with OSError/ProcessLookupError, so let them fire.
        #
        # kill() only untracks once pid_exists() confirms the process is GONE, so
        # stub that decision instead of betting the fake PID is absent from the
        # host's process table. It is not a safe bet: Windows recycles PIDs from a
        # small space, and on a CI runner spawning subprocesses across xdist
        # workers 4242 was intermittently a REAL live process -- kill() then took
        # the survivor branch and this asserted `[] == [4242]`.
        monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda pid: False)

        await rt.kill()

        assert calls["pid"] == [4242]
        assert calls["session"] == [4242]

    @pytest.mark.asyncio
    async def test_kill_keeps_pid_tracked_when_the_process_survives(self, monkeypatch):
        """A survivor must STAY tracked so the orphan sweeps can still reach it.

        The counterpart to the test above, and the reason that one has to stub
        `pid_exists` rather than rely on the ambient process table: untracking a
        process that outlived SIGTERM/SIGKILL escalation would leak it until
        reboot, because the sweep would no longer have a handle on it.
        """
        rt, _, proc = _make_runtime()
        proc.wait = AsyncMock(return_value=0)

        calls: dict[str, list[int]] = {"pid": [], "session": []}
        import kiro_crew.acp.runtime as rt_mod

        monkeypatch.setattr(rt_mod, "_untrack_pid", lambda p: calls["pid"].append(p))
        monkeypatch.setattr(rt_mod, "_untrack_session_pid", lambda p: calls["session"].append(p))
        monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda pid: True)

        await rt.kill()

        assert calls["pid"] == []
        assert calls["session"] == []


class TestAcpRuntimeLoadSession:
    """load_session() must mirror AcpClient._initialize_session's resume path:
    issue session/load DIRECTLY (no session/new first) under the ORIGINAL sid,
    with the same cwd + mcpServers (pooled broker stubs re-declared; [] when no
    overlay is configured) + _kiro.dev/session_file _meta. The double-session
    drift it replaces produced stopReason='refusal'."""

    @pytest.mark.asyncio
    async def test_load_session_sends_direct_session_load_params(self, monkeypatch):
        rt, _, _ = _make_runtime()
        rt._can_load_session = True

        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            # session/load echoes "modes"; set_mode echoes nothing meaningful.
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

        handle = await rt.load_session(
            "/home/u/.kiro/sessions/cli/sid-123.json",
            "sid-123",
            cwd="/work",
            agent="kirocrew",
        )

        # No session/new was issued — the first RPC is session/load itself.
        methods = [m for m, _ in sent]
        assert METHOD_SESSION_NEW not in methods
        assert methods[0] == METHOD_SESSION_LOAD

        load_params = sent[0][1]
        assert load_params == {
            "sessionId": "sid-123",
            "cwd": "/work",
            # [] because _make_runtime configures no MCP-gateway overlay — the
            # non-pooled path is unchanged by the #3528 stub re-declaration.
            "mcpServers": [],
            "_meta": {"_kiro.dev/session_file": "/home/u/.kiro/sessions/cli/sid-123.json"},
        }
        # Handle adopts the ORIGINAL sid and its queue is registered.
        assert handle.session_id == "sid-123"
        assert "sid-123" in rt._session_queues
        # set_mode ran for the resumed session (mirrors AcpClient step 4).
        assert METHOD_SET_MODE in methods

    @pytest.mark.asyncio
    async def test_load_session_raises_when_capability_absent(self):
        rt, _, _ = _make_runtime()
        rt._can_load_session = False
        with pytest.raises(AcpRuntimeError):
            await rt.load_session("/f.json", "sid-x")
        # No queue leaked on the guard path.
        assert "sid-x" not in rt._session_queues

    @pytest.mark.asyncio
    async def test_load_session_without_modes_raises_and_unregisters(self, monkeypatch):
        rt, _, _ = _make_runtime()
        rt._can_load_session = True

        async def _fake_send(method, params, timeout=None):
            return {}  # no "modes" → load did not actually restore state

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

        with pytest.raises(AcpRuntimeError):
            await rt.load_session("/f.json", "sid-y", agent="kirocrew")
        # The queue registered before the send must be cleaned up on failure.
        assert "sid-y" not in rt._session_queues

    @pytest.mark.asyncio
    async def test_load_session_reinjects_the_kas_agent_definition(self, monkeypatch):
        """Resume must re-send the agent, for the same reason session/new sends it.

        KAS registers client agents PER SESSION and has no ``--agent`` flag, so a
        resumed session that is not handed them again advertises only the modes it
        can find on disk — and that set is not a superset of what session/new had,
        because KAS skips an agent profile written for kiro-cli. Omitting this made
        the requested mode genuinely absent on resume, and the mode guard then
        refused the load rather than silently running the backend default.
        """
        from kiro_crew.acp._dispatch import build_session_new_params

        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        async def _fake_agents(agent):
            return [{"id": agent, "prompt": "p", "tools": []}]

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
        rt._acp_backend = ACP_BACKEND_KAS

        await rt.load_session("", "sid-kas", cwd="/work", agent="kirocrew")

        load_params = sent[0][1]
        assert load_params["_meta"]["kiro"]["customAgents"] == [
            {"id": "kirocrew", "prompt": "p", "tools": []}
        ]
        # Same envelope as session/new, because both go through one builder. Two
        # hand-built copies of this nesting would be free to drift, and a resumed
        # session that got a subtly different shape would fail the same way the
        # missing injection did: mode absent, load refused.
        assert (
            load_params["_meta"]["kiro"]
            == build_session_new_params(
                "/work", kas_custom_agents=[{"id": "kirocrew", "prompt": "p", "tools": []}]
            )["_meta"]["kiro"]
        )

    @pytest.mark.asyncio
    async def test_load_session_keeps_the_transcript_path_alongside_the_agents(self, monkeypatch):
        """Merged, not assigned: a third _meta writer must not drop an earlier one.

        The two envelopes belong to different backends today (a transcript path is
        kiro-cli-only), so in practice they do not collide — which is exactly why a
        plain assignment would survive review and then lose a field later.
        """
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        async def _fake_agents(agent):
            return [{"id": agent, "prompt": "p", "tools": []}]

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
        rt._acp_backend = ACP_BACKEND_KAS

        await rt.load_session("/t.json", "sid-both", cwd="/work", agent="kirocrew")

        meta = sent[0][1]["_meta"]
        assert meta["_kiro.dev/session_file"] == "/t.json"
        assert "kiro" in meta

    @pytest.mark.asyncio
    async def test_the_kiro_resume_path_never_reaches_the_adapter(self, monkeypatch):
        """harness-parity H13: the kiro construction path must not change at all.

        Relying on ``_kas_custom_agents`` to answer ``None`` would leave the kiro
        resume awaiting an adapter coroutine — working, but changed, and free to
        grow a failure mode later. The backend guard is what makes the kiro path
        reach a comparison and stop, so this asserts the adapter is never called.
        """
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []
        calls: list[str] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        async def _fake_agents(agent):
            calls.append(agent)
            return [{"id": agent, "prompt": "p", "tools": []}]

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)

        await rt.load_session("/t.json", "sid-kiro", cwd="/work", agent="kirocrew")

        assert calls == []
        assert sent[0][1]["_meta"] == {"_kiro.dev/session_file": "/t.json"}

    @pytest.mark.asyncio
    async def test_load_session_params_match_acp_client(self, monkeypatch):
        """Drift guard: the kiro (non-claude) session/load payload built here
        must equal the one AcpClient._initialize_session builds, so the two
        resume paths never diverge. Compares the field set explicitly."""
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        captured: dict = {}

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_LOAD:
                captured.update(params)
                return {"modes": {}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt.load_session("/k/sid.json", "sid", cwd="/w", agent="kirocrew")

        # Mirror of AcpClient's kiro-branch load_params (client.py step 2).
        # mcpServers is [] on BOTH paths here because no overlay is configured;
        # the pooled case is covered by test_load_session_redeclares_pooled_stubs.
        expected = {
            "sessionId": "sid",
            "cwd": "/w",
            "mcpServers": [],
            "_meta": {"_kiro.dev/session_file": "/k/sid.json"},
        }
        assert captured == expected

    @pytest.mark.asyncio
    async def test_load_session_unregisters_queue_when_set_mode_fails(self, monkeypatch):
        """A set_mode failure AFTER the queue is registered must TERMINATE the
        resumed session on kiro-cli (session/load already restored it there, so
        a plain unregister leaks it) and drop the local queue, so the caller's
        create_session() fallback doesn't leave the reader routing late
        transcript-replay frames to an abandoned resume_sid queue."""
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        methods: list[str] = []

        async def _fake_send(method, params, timeout=None):
            methods.append(method)
            if method == METHOD_SESSION_LOAD:
                return {"modes": {}, "models": []}  # load succeeds, queue registers
            if method == METHOD_SET_MODE:
                raise AcpRuntimeError("set_mode boom")
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

        with pytest.raises(AcpRuntimeError):
            await rt.load_session("/k/sid.json", "sid-z", cwd="/w", agent="kirocrew")
        assert METHOD_SESSION_TERMINATE in methods
        assert "sid-z" not in rt._session_queues

    @pytest.mark.asyncio
    async def test_load_session_redeclares_pooled_stubs(self, tmp_path, monkeypatch):
        """#3528 regression: a resumed session must re-declare the pooled broker
        stubs. session/load re-initializes the session's MCP servers, so the []
        this path used to send was APPLIED — the stubs stopped shadowing the
        agent spec's same-named entries and kiro-cli spawned its own copy of
        every pooled server, silently un-pooling the session for life.

        Asserts on the EMITTED mcpServers of both requests: load_session must
        carry exactly the entries create_session injects for the same agent +
        overlay. Mutating the fix back to [] fails the first assert."""
        from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER

        overlay = tmp_path / "agents"
        overlay.mkdir()
        (overlay / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "mcpServers": {
                        "builder-mcp": {
                            _WRAPPER_MARKER: True,
                            "command": "/data/mcp-gateway/stubs/mc-mcp-stub-wrapper.sh",
                            "args": ["--target-command=builder-mcp"],
                            "env": {},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        rt._mcp_gateway_overlay = str(overlay)

        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {}, "models": []}
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-new"}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

        await rt.load_session("/k/sid.json", "sid-r", cwd="/w", agent="kirocrew")
        load_params = next(p for m, p in sent if m == METHOD_SESSION_LOAD)
        assert [e["name"] for e in load_params["mcpServers"]] == ["builder-mcp"]

        # Parity with create_session for the same agent + overlay: the two
        # injection paths must never diverge.
        await rt.create_session(cwd="/w", agent="kirocrew")
        new_params = next(p for m, p in sent if m == METHOD_SESSION_NEW)
        assert load_params["mcpServers"] == new_params["mcpServers"]

    @pytest.mark.asyncio
    async def test_load_session_resolves_stubs_off_the_event_loop(self, monkeypatch):
        """The overlay lookup stats and reads files; like create_session it must
        run via asyncio.to_thread, not on the loop thread."""
        import threading

        import kiro_crew.acp.runtime as rt_mod

        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        rt._mcp_gateway_overlay = "/nonexistent-overlay"

        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []

        def _recording_pooled(overlay_dir, agent, channel_id=None):
            seen.append(threading.current_thread())
            return []

        monkeypatch.setattr(rt_mod, "pooled_session_servers", _recording_pooled)

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_LOAD:
                return {"modes": {}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt.load_session("/k/sid.json", "sid-t", cwd="/w", agent="kirocrew")

        assert seen, "load_session never consulted pooled_session_servers"
        assert all(t is not loop_thread for t in seen)

    def test_every_session_request_builder_consults_pooled_servers(self):
        """#3528 guard: the stub injection now lives at multiple call sites in
        two files, and this bug was exactly one of them silently sending [].
        Enumerate every function that issues session/new or session/load and
        assert each one consults the pooled-stub resolution (either
        pooled_session_servers directly or the _pooled_mcp_servers hook), so a
        fourth builder — or a regression in an existing one — fails here
        instead of shipping another silent un-pooling path."""
        import ast
        import inspect

        import kiro_crew.acp.client as client_mod
        import kiro_crew.acp.runtime as rt_mod

        _SEND_FUNCS = {"_send_request", "_send_and_await"}
        _SESSION_METHODS = {"METHOD_SESSION_NEW", "METHOD_SESSION_LOAD"}

        def _builders(module) -> dict[str, str]:
            src = inspect.getsource(module)
            out: dict[str, str] = {}
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for call in ast.walk(node):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr in _SEND_FUNCS
                        and call.args
                        and isinstance(call.args[0], ast.Name)
                        and call.args[0].id in _SESSION_METHODS
                    ):
                        out[node.name] = ast.get_source_segment(src, node) or ""
                        break
            return out

        builders = {**_builders(rt_mod), **_builders(client_mod)}
        # Exempt: builders whose session exists only to read the session/new
        # response and is terminated before any prompt. Such a session never
        # calls a tool, so pooled broker stubs would add per-probe MCP boot
        # churn without pooling anything. Everything a REAL conversation runs
        # through must stay in the ratchet.
        _NEVER_PROMPTS = {"probe_advertised_models"}
        for name in _NEVER_PROMPTS:
            assert name in builders, f"{name} no longer issues session/new — remove its exemption"
            builders.pop(name)
        # The four known builders; a new one is included automatically.
        assert {
            "create_session",
            "load_session",
            "_new_session_following_substitution",
            "_initialize_session",
        } <= builders.keys(), f"expected builders missing from scan: {sorted(builders)}"
        for name, body in builders.items():
            assert "pooled_session_servers" in body or "_pooled_mcp_servers" in body, (
                f"{name} issues session/new or session/load but never consults "
                "the pooled broker stubs — it would un-pool its sessions (#3528)"
            )


@pytest.mark.asyncio
async def test_create_session_terminates_session_when_set_mode_fails(monkeypatch):
    """A set_mode failure AFTER session/new succeeded must TERMINATE the session
    on kiro-cli — session/new already created it in the shared process, so a
    plain local unregister would leak it there (RSS growth). terminate_session
    also unregisters the local queue, so the abandoned-queue routing is closed
    too. Mirrors the same cleanup load_session() performs."""
    rt, _, _ = _make_runtime()
    methods: list[str] = []

    async def _fake_send(method, params, timeout=None):
        methods.append(method)
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-new"}  # session/new succeeds → queue registers
        if method == METHOD_SET_MODE:
            raise AcpRuntimeError("set_mode boom")
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    with pytest.raises(AcpRuntimeError):
        await rt.create_session(cwd="/w", agent="kirocrew")
    # kiro-cli was told to evict the just-created session, and the local queue
    # registered before set_mode is cleaned up on failure.
    assert METHOD_SESSION_TERMINATE in methods
    assert "sid-new" not in rt._session_queues


@pytest.mark.asyncio
async def test_create_session_registers_queue_on_success(monkeypatch):
    """Happy path: a successful create_session keeps the session queue
    registered so the returned handle receives its frames."""
    rt, _, _ = _make_runtime()

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-ok"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    handle = await rt.create_session(cwd="/w", agent="kirocrew")
    assert handle.session_id == "sid-ok"
    assert "sid-ok" in rt._session_queues


@pytest.mark.asyncio
async def test_create_session_buffers_oauth_emitted_before_response():
    """OAuth emitted during session/new survives until the provider can drain it."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    rt, reader, _ = _make_runtime()
    reader_task = await _start_reader(rt)
    create_task = asyncio.create_task(rt.create_session(cwd="/w"))
    try:
        request_id = await _await_pending(rt)
        oauth_url = "https://mcp.linear.app/authorize?client_id=shared"
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {
                    "sessionId": "sid-new",
                    "serverName": "linear",
                    "oauthUrl": oauth_url,
                },
            },
        )
        _feed(reader, {"id": request_id, "result": {"sessionId": "sid-new"}})

        handle = await asyncio.wait_for(create_task, timeout=3.0)
        provider = AcpSessionProvider(handle, rt)
        assert provider.pop_pending_oauth_requests() == [
            {"serverName": "linear", "oauthUrl": oauth_url}
        ]
        assert provider.pop_pending_oauth_requests() == []
    finally:
        if not create_task.done():
            create_task.cancel()
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_failed_session_init_oauth_does_not_leak_to_reused_id():
    """A failed init cannot leave an approval URL for a later shared session."""
    rt, reader, _ = _make_runtime()
    reader_task = await _start_reader(rt)
    failed_task = asyncio.create_task(rt.create_session(cwd="/w"))
    fresh_task = None
    try:
        failed_request_id = await _await_pending(rt)
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {
                    "sessionId": "sid-reused",
                    "serverName": "linear",
                    "oauthUrl": "https://mcp.linear.app/authorize?client_id=stale",
                },
            },
        )
        _feed(
            reader,
            {
                "id": failed_request_id,
                "error": {"code": -32603, "message": "session init failed"},
            },
        )
        with pytest.raises(AcpRuntimeError, match="session init failed"):
            await asyncio.wait_for(failed_task, timeout=3.0)
        assert not rt._pending_init_notifications

        fresh_task = asyncio.create_task(rt.create_session(cwd="/w"))
        fresh_request_id = await _await_pending(rt, exclude={failed_request_id})
        _feed(reader, {"id": fresh_request_id, "result": {"sessionId": "sid-reused"}})
        handle = await asyncio.wait_for(fresh_task, timeout=3.0)
        assert handle.pop_pending_oauth_requests() == []
    finally:
        for task in (failed_task, fresh_task):
            if task is not None and not task.done():
                task.cancel()
        await _stop_reader(reader_task)


# ── Drift-parity fixes (AcpRuntime ↔ AcpClient): #1-#4 + #5b ──


@pytest.mark.asyncio
async def test_steer_notifications_yield_steer_events():
    """#4: steering_* session/update frames classify as "steer" and yield the
    EVENT_STEER_* events (previously dropped — classify_notification had no steer
    branch, so the shared demux path never surfaced mid-turn steer)."""
    from kiro_crew.acp.types import (
        EVENT_STEER_CLEARED,
        EVENT_STEER_CONSUMED,
        EVENT_STEER_QUEUED,
    )

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events: list = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "steering_queued", "content": "please focus on X"},
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "steering_consumed", "content": "focus on X"},
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "steering_cleared"},
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)

        kinds = [e.kind for e in events]
        assert EVENT_STEER_QUEUED in kinds
        assert EVENT_STEER_CONSUMED in kinds
        assert EVENT_STEER_CLEARED in kinds
        queued = next(e for e in events if e.kind == EVENT_STEER_QUEUED)
        assert queued.text == "please focus on X"
        consumed = next(e for e in events if e.kind == EVENT_STEER_CONSUMED)
        assert consumed.text == "focus on X"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_tool_interrupted_marker_synthesizes_complete(monkeypatch):
    """#2: kiro-cli's security-filter marker (text-only, no `complete` response)
    must synthesize EVENT_COMPLETE so the turn does not hang until the 2h prompt
    timeout, and must emit the SEL audit. No prompt response is fed here — the
    turn MUST still terminate."""
    import kiro_crew.acp.session_handle as sh

    sel_mock = MagicMock()
    monkeypatch.setattr(sh, "sel", lambda: sel_mock)
    marker = "Tool uses were interrupted, waiting for the next user prompt"

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events: list = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=5.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        await asyncio.sleep(0.05)
        # Only the marker text chunk — NO {"id": req_id, "result": ...} response.
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": marker},
                    },
                },
            },
        )
        # Must finish WITHOUT the turn response (the synthesized complete ends it).
        await asyncio.wait_for(driver, timeout=3.0)

        assert events[-1].kind == EVENT_COMPLETE
        assert handle._turn_done.is_set()
        sel_mock.log_tool_invocation.assert_called_once()
        _kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert _kwargs["outcome"] == "denied"
        assert _kwargs["tool_name"] == "kiro_cli_security_filter"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unresponsive_cancel_unblocks_without_killing_runtime():
    """#3: after cancel(), if kiro-cli never acks (no cancelled stopReason) within
    the grace budget, the dispatch loop synthesizes a terminal EVENT_COMPLETE so
    the caller unblocks — WITHOUT killing the shared runtime (send_notification is
    the only runtime call; no kill)."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    rt.kill = MagicMock()  # type: ignore[method-assign]  # must NOT be called
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events: list = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=5.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        await asyncio.sleep(0.05)
        await handle.cancel()
        # Backdate the cancel so the grace window has already elapsed.
        handle._cancel_ts = time.monotonic() - (handle._cancel_grace_secs + 1)
        # Wake the loop so it re-checks the cancel guard at the top of the while.
        _feed(reader, {"method": "_kiro.dev/metadata", "params": {"sessionId": "sA"}})
        await asyncio.wait_for(driver, timeout=3.0)

        assert events[-1].kind == EVENT_COMPLETE
        assert events[-1].stop_reason == "error: cancel unacked"
        assert handle._turn_done.is_set()
        rt.kill.assert_not_called()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_drain_init_consumes_init_frames_and_captures_config():
    """#1: drain_init() pulls MCP-init/config frames off the session queue after
    set_mode so they don't race into the first prompt, and captures
    config_option_update into cached configOptions."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    cfg = [{"id": "effort", "options": ["low", "high"]}]
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "config_option_update", "configOptions": cfg},
                },
            }
        )
    )
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/mcp/server_initialized",
                "params": {"sessionId": "sA", "serverName": "builder-mcp"},
            }
        )
    )
    await handle.drain_init(duration=0.5, idle_exit=0.05)
    assert q["sA"].empty()  # frames drained, not left for the first prompt
    assert handle._config_options == cfg


@pytest.mark.asyncio
async def test_drain_init_repoisons_on_dead_runtime():
    """#1: a None sentinel (runtime died during init) is re-queued so the next
    consumer still sees the death, and drain stops."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(None)
    await handle.drain_init(duration=0.5, idle_exit=0.05)
    assert q["sA"].get_nowait() is None  # sentinel preserved


def _mcp_initialized_frame(session_id: str, server: str) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "method": "_kiro.dev/mcp/server_initialized",
            "params": {"sessionId": session_id, "serverName": server},
        }
    )


def _metadata_frame(session_id: str) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "method": "_kiro.dev/metadata",
            "params": {"sessionId": session_id, "contextUsagePercentage": 1.0},
        }
    )


def _mcp_failure_frame(session_id: str, server: str, error: str) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "method": "_kiro.dev/mcp/server_init_failure",
            "params": {"sessionId": session_id, "serverName": server, "error": error},
        }
    )


@pytest.mark.asyncio
async def test_drain_init_records_the_session_mcp_report():
    """Registration frames the drain consumes become this session's report.

    Parity with AcpClient._drain_notifications: without this the frames are
    consumed and the session has no way to say which servers started.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "kirocrew-core"))
    q["sA"].put_nowait(_mcp_failure_frame("sA", "slack-mcp", "spawn ENOENT"))
    q["sA"].put_nowait(_metadata_frame("sA"))

    await handle.drain_init(duration=0.5, idle_exit=0.05)

    payload = handle.mcp_session_report().payload()
    assert payload is not None
    assert payload["ready"] == ["kirocrew-core"]
    assert payload["failed"] == ["slack-mcp"]
    assert payload["failures"] == {"slack-mcp": "spawn ENOENT"}


@pytest.mark.asyncio
async def test_drain_init_skips_a_stale_backlog_report():
    """A pre-switch backlog frame is drained but NOT credited to this report.

    ``stale_report_frames`` names how many already-queued frames are the PRE-switch
    agent's roster. Recording one would show a server the current agent does not
    have as mounted here; leaving it out only understates, which the report
    renders as "no report" rather than "not mounted".
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "previous-agent-server"))

    await handle.drain_init(
        duration=0.2, idle_exit=0.01, no_report_ceiling=0.05, stale_report_frames=1
    )

    assert q["sA"].empty()  # still drained, as the arming docstring promises
    assert handle.mcp_session_report().payload() is None


@pytest.mark.asyncio
async def test_drain_init_refuses_a_sessionless_frame_on_a_lone_runtime():
    """A frame naming no session is not this session's, even when unmarked.

    The runtime sets ``fanout_no_owner`` only once MORE THAN ONE queue is
    registered — right for the idle-stall clock, which may treat a lone session
    as the sole owner of whatever arrives, but not for a view that publishes
    server names as "what THIS session mounted". A co-tenant that emits a
    sessionless frame before registering its own queue would otherwise have its
    server attributed here.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    frame = _mcp_initialized_frame("sA", "a-co-tenants-server")
    frame.params.pop("sessionId", None)
    assert frame.fanout_no_owner is False, "the lone-queue case leaves it clear"
    q["sA"].put_nowait(frame)

    await handle.drain_init(duration=0.2, idle_exit=0.01, no_report_ceiling=0.05)

    assert handle.mcp_session_report().payload() is None


@pytest.mark.asyncio
async def test_drain_init_credits_the_active_agent_when_the_queue_never_empties():
    """A refilled queue must not extend the stale backlog past its own depth.

    The original defect: exhaustion was detected by the queue going EMPTY, so an
    active agent that refilled it before the backlog drained kept the flag set
    for the rest of the drain and had every one of its reports skipped — a
    session stuck at "no report" for as long as its servers kept talking.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "previous-agent-server"))

    async def _refill_before_the_backlog_drains() -> None:
        # Lands while the drain is awaiting its first get(), so the queue is
        # already non-empty again when the second iteration begins.
        await asyncio.sleep(0)
        q["sA"].put_nowait(_mcp_initialized_frame("sA", "current-agent-server"))

    feeder = asyncio.create_task(_refill_before_the_backlog_drains())
    try:
        await handle.drain_init(
            duration=0.3, idle_exit=0.05, no_report_ceiling=0.5, stale_report_frames=1
        )
    finally:
        await feeder

    payload = handle.mcp_session_report().payload()
    assert payload is not None, "the active agent's report was swallowed as stale"
    assert payload["ready"] == ["current-agent-server"]


@pytest.mark.asyncio
async def test_drain_init_credits_a_report_that_landed_before_set_mode_answered():
    """A count measured at drain time would swallow the active agent's own report.

    The original defect: the pre-switch depth was read inside ``drain_init``,
    i.e. AFTER set_mode returned. kiro-cli can emit the switched-to agent's
    registrations before it answers, so those frames were already queued by then
    and got counted as pre-switch — consumed without being recorded, leaving a
    false "no report" panel. The caller measures before it sends instead, so a
    frame that arrives during the request is credited.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "previous-agent-server"))

    # What the caller reads BEFORE sending set_mode: one staged frame.
    staged = handle.queued_frame_count()
    assert staged == 1
    # The switched-to agent registers while set_mode is still in flight.
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "current-agent-server"))

    await handle.drain_init(
        duration=0.2, idle_exit=0.01, no_report_ceiling=0.05, stale_report_frames=staged
    )

    payload = handle.mcp_session_report().payload()
    assert payload is not None, "the active agent's report was swallowed as pre-switch"
    assert payload["ready"] == ["current-agent-server"]


@pytest.mark.asyncio
async def test_drain_init_records_a_post_backlog_report_after_a_switch():
    """After the stale backlog is exhausted, the active agent's own report counts."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "previous-agent-server"))

    async def _after_switch() -> None:
        await asyncio.sleep(0.05)
        q["sA"].put_nowait(_mcp_initialized_frame("sA", "current-agent-server"))

    feeder = asyncio.create_task(_after_switch())
    try:
        await handle.drain_init(
            duration=0.3, idle_exit=0.05, no_report_ceiling=0.5, stale_report_frames=1
        )
    finally:
        await feeder

    payload = handle.mcp_session_report().payload()
    assert payload is not None
    assert payload["ready"] == ["current-agent-server"]


@pytest.mark.asyncio
async def test_create_session_records_the_wire_roster(monkeypatch):
    """The report carries the roster session/new actually sent.

    A different fact from the agent spec on disk, which is what the dashboard's
    other MCP views read.
    """
    rt, _, _ = _make_runtime()

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-roster"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    servers = [{"name": "kirocrew-core", "command": "x"}, {"name": "kirocrew-cron"}]

    handle = await rt.create_session(cwd="/w", agent="kirocrew", mcp_servers=servers)

    assert handle.mcp_session_report().configured == ("kirocrew-core", "kirocrew-cron")


@pytest.mark.asyncio
async def test_drain_init_waits_past_idle_window_for_first_mcp_report(monkeypatch):
    """#2627: the idle shortcut is not eligible before the first MCP
    registration frame. A server that stays silent past the idle window and
    THEN reports is still observed — non-MCP frames (metadata) that arrive
    immediately after set_mode must not arm the shortcut either."""
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 5.0, raising=False)
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # A non-MCP frame is already queued (kiro-cli emits metadata right after
    # set_mode); it must be consumed without arming the idle exit.
    q["sA"].put_nowait(_metadata_frame("sA"))

    async def _late_report() -> None:
        # Many idle windows of silence before the server finally reports.
        await asyncio.sleep(0.15)
        q["sA"].put_nowait(_mcp_initialized_frame("sA", "slow-npx"))

    feeder = asyncio.create_task(_late_report())
    try:
        await handle.drain_init(duration=0.2, idle_exit=0.01)
    finally:
        await feeder
    # The late report was drained rather than left to race into the first turn.
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_drain_init_no_reports_returns_at_ceiling():
    """#2627: a drain that never sees an MCP report returns at the no-report
    ceiling instead of hanging (bounded even when servers are dead or absent)."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_metadata_frame("sA"))  # non-MCP traffic doesn't extend it
    # The outer wait_for is the hang guard: generous vs the 0.2s ceiling so a
    # loaded shard can't flake it, tiny vs a genuine unbounded wait.
    await asyncio.wait_for(
        handle.drain_init(duration=0.05, idle_exit=0.01, no_report_ceiling=0.2),
        timeout=5.0,
    )
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_drain_init_idle_exit_stays_prompt_after_first_report():
    """#2627: once a report has been seen, a subsequent idle gap still exits
    promptly — the warm path must not degrade into full-ceiling waits. The
    ceilings are deliberately huge relative to the outer bound, so completing
    inside it proves the idle shortcut (not a ceiling) ended the drain."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "fast-server"))
    await asyncio.wait_for(
        handle.drain_init(duration=30.0, idle_exit=0.02, no_report_ceiling=30.0),
        timeout=5.0,
    )
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_drain_init_zero_ceiling_keeps_idle_exit_active_from_start():
    """#2627: no_report_ceiling=0.0 (MCP-free runtime opt-out) restores the
    pre-fix behavior — idle exit is active before any report, so an empty
    queue exits after one idle window instead of holding for a first report."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_metadata_frame("sA"))
    # duration is deliberately huge relative to the outer bound: completing
    # inside it proves the idle shortcut ended the drain despite zero reports.
    await asyncio.wait_for(
        handle.drain_init(duration=30.0, idle_exit=0.02, no_report_ceiling=0.0),
        timeout=5.0,
    )
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_mcp_free_runtime_skips_no_report_ceiling(monkeypatch):
    """#2627: a runtime constructed with expect_mcp_reports=False passes the
    zero ceiling to drain_init, so its sessions never hold for a report."""
    rt = AcpRuntime(work_dir="/tmp", expect_mcp_reports=False)
    rt._initialized = True
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 4242
    rt._process = proc
    rt._pid = 4242

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-lite"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    seen: dict = {}
    orig = AcpSessionHandle.drain_init

    async def _spy(self, *args, **kwargs):
        seen.update(kwargs)
        await orig(self, *args, **kwargs)

    with patch.object(AcpSessionHandle, "drain_init", _spy):
        await rt.create_session(cwd="/w", agent="kirocrew-lite", mcp_servers=[])
    assert seen.get("no_report_ceiling") == 0.0


@pytest.mark.asyncio
async def test_drain_init_ignores_pre_switch_reports_still_waits_for_new_agent(monkeypatch):
    """#2627 (review): on a shared runtime, session/new initializes the
    PARENT mode's servers; their staged registration frames must not arm the
    idle shortcut for a session that was then mode-SWITCHED — the switched-to
    agent's own slow server, reporting after set_mode, must still be observed."""
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 5.0, raising=False)
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # Staged during session/new: the pre-switch agent's roster.
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "parent-mode-server"))
    q["sA"].put_nowait(_metadata_frame("sA"))

    async def _late_report() -> None:
        # The switched-to agent's server reports well past the idle window.
        await asyncio.sleep(0.15)
        q["sA"].put_nowait(_mcp_initialized_frame("sA", "subagent-slow-npx"))

    feeder = asyncio.create_task(_late_report())
    try:
        await handle.drain_init(duration=0.2, idle_exit=0.01, stale_report_frames=1)
    finally:
        await feeder
    # Without the stale-backlog gate, the staged parent report arms the idle
    # exit and the drain returns before the late report — leaving it queued.
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_reader_retains_mcp_registration_frames_during_init():
    """#2627: server_initialized / init_failure emitted before the session/new
    response are staged (like OAuth) and handed to the new session's queue, so
    drain_init() sees warm servers' reports and arms its idle shortcut."""
    rt, reader, _ = _make_runtime()
    task = asyncio.create_task(rt._reader_loop())
    try:

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_NEW:
                # Frames arrive while session/new is in flight — before the
                # queue can be registered under the not-yet-known session id.
                _feed(
                    reader,
                    {
                        "method": "_kiro.dev/mcp/server_initialized",
                        "params": {"sessionId": "sid-warm", "serverName": "core"},
                    },
                )
                _feed(
                    reader,
                    {
                        "method": "_kiro.dev/mcp/server_init_failure",
                        "params": {"sessionId": "sid-warm", "serverName": "broken"},
                    },
                )
                await asyncio.sleep(0.05)  # let the reader route them
                return {"sessionId": "sid-warm"}
            return {}

        with patch.object(rt, "_send_and_await", _fake_send):
            with patch.object(AcpSessionHandle, "drain_init", AsyncMock()) as mock_drain:
                handle = await rt.create_session(cwd="/w", agent="kirocrew", mcp_servers=[])
        assert handle.session_id == "sid-warm"
        mock_drain.assert_awaited_once()
        # Both registration frames were transferred into the session queue
        # (this is what drain_init would consume to arm its idle shortcut).
        methods = []
        while not handle._queue.empty():
            frame = handle._queue.get_nowait()
            assert frame is not None
            methods.append(frame.method)
        assert methods == [
            "_kiro.dev/mcp/server_initialized",
            "_kiro.dev/mcp/server_init_failure",
        ]
        # Staging area was emptied by the transfer.
        assert not rt._pending_init_notifications
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_backfill_context_window_from_pct(monkeypatch):
    """#5b: pct-only metadata (kiro 2.10+) backfills window/used tokens from the
    model registry; no-op once a real usage_update set the window."""
    import kiro_crew.acp.session_handle as sh

    # The backfill only fires for a KNOWN window (has_known_window) and resolves
    # via the central model_window authority, so mock both for the fake model.
    monkeypatch.setattr(sh.model_registry, "has_known_window", lambda mid: True)
    monkeypatch.setattr(sh.model_registry, "model_window", lambda mid, **kw: 200000)
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._model = "some-model"
    handle._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 25},
            }
        )
    )
    assert handle.last_prompt_stats.context_pct == 25.0
    assert handle.last_prompt_stats.context_window_tokens == 200000
    assert handle.last_prompt_stats.context_used_tokens == 50000

    # A prior real usage_update wins — metadata must override neither the
    # window NOR the token-derived pct (else the headline % desyncs from the
    # "used / total" token text shown in the dashboard popover).
    handle2 = AcpSessionHandle("sA", q["sA"], rt)
    handle2._model = "some-model"
    handle2.last_prompt_stats.context_pct = 40.8
    handle2.last_prompt_stats.context_used_tokens = 408000
    handle2.last_prompt_stats.context_window_tokens = 999
    handle2.last_prompt_stats.context_tokens_from_usage = True
    handle2._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 80},
            }
        )
    )
    assert handle2.last_prompt_stats.context_window_tokens == 999
    assert handle2.last_prompt_stats.context_pct == 40.8
    assert handle2.last_prompt_stats.context_used_tokens == 408000


def test_session_handle_usage_update_sets_flag_and_metadata_cannot_clobber():
    """SessionHandle parity with AcpClient: a real usage_update through
    _handle_update sets context_tokens_from_usage, and a later metadata
    contextUsagePercentage must not clobber the token-derived pct (the
    408K/1000K-vs-73% desync on the shared-runtime path)."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._handle_update(
        JsonRpcMessage.from_dict(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "usage_update",
                        "used": 408000,
                        "size": 1000000,
                    }
                },
            }
        )
    )
    assert handle.last_prompt_stats.context_tokens_from_usage is True
    assert handle.last_prompt_stats.context_pct == 40.8
    assert handle.last_prompt_stats.context_used_tokens == 408000
    assert handle.last_prompt_stats.context_window_tokens == 1000000

    handle._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 73},
            }
        )
    )
    assert handle.last_prompt_stats.context_pct == 40.8  # NOT clobbered to 73
    assert handle.last_prompt_stats.context_used_tokens == 408000


def test_backfill_context_window_clamps_malformed_pct(monkeypatch):
    """A degenerate metadata percentage (huge finite / inf / NaN) must not
    overflow round() and abort the turn on the shared-runtime path; derived
    used stays in [0, window]."""
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh.model_registry, "has_known_window", lambda mid: True)
    monkeypatch.setattr(sh.model_registry, "model_window", lambda mid, **kw: 200000)
    for bad in (1e308, float("inf"), float("nan")):
        rt, _, _ = _make_runtime()
        q = _register(rt, "sA")
        handle = AcpSessionHandle("sA", q["sA"], rt)
        handle._model = "some-model"
        # Must not raise OverflowError/ValueError.
        handle._track_metadata(
            JsonRpcMessage.from_dict(
                {
                    "method": "_kiro.dev/metadata",
                    "params": {"contextUsagePercentage": bad},
                }
            )
        )
        used = handle.last_prompt_stats.context_used_tokens
        assert 0 <= used <= 200000
        # context_pct is sanitized at the source, never left non-finite.
        pct = handle.last_prompt_stats.context_pct
        assert 0.0 <= pct <= 100.0


def test_backfill_context_window_no_model_is_safe(monkeypatch):
    """#5b: no _model set → records pct only, no crash, no token backfill."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 30},
            }
        )
    )
    assert handle.last_prompt_stats.context_pct == 30.0
    assert handle.last_prompt_stats.context_window_tokens == 0


# ── Round-1 follow-up fixes: #5b currentModelId backfill + send_command redaction ──


def test_backfill_uses_resolved_model_id_from_session_config(monkeypatch):
    """#5b (parity): store_session_config captures currentModelId into
    _resolved_model_id, so context-window backfill works even when the user
    never called set_model — and _model stays empty (no pinning)."""
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh.model_registry, "has_known_window", lambda mid: True)
    monkeypatch.setattr(sh.model_registry, "model_window", lambda mid, **kw: 300000)
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config(
        {"models": {"currentModelId": "resolved-model", "availableModels": []}}
    )
    assert handle._resolved_model_id == "resolved-model"
    assert handle._model == ""  # must NOT pollute the user-picked model field
    handle._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 40},
            }
        )
    )
    assert handle.last_prompt_stats.context_window_tokens == 300000
    assert handle.last_prompt_stats.context_used_tokens == 120000


@pytest.mark.asyncio
async def test_send_command_redacts_output(monkeypatch):
    """#send_command (parity): the command response text is redacted before
    return, matching AcpClient.send_command."""
    import kiro_crew.acp.session_handle as sh

    # send_command now applies the explicit two-pass redactors (parity with
    # AcpClient.send_command), not the redact_text helper.
    monkeypatch.setattr(sh, "redact_exfiltration_urls", lambda s: (s, []))
    monkeypatch.setattr(sh, "redact_credentials", lambda s: ("REDACTED", []))
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")

    async def _fake_send_request(method, params):
        return 1

    rt.send_request = _fake_send_request  # type: ignore[method-assign]
    handle = AcpSessionHandle("sA", q["sA"], rt)

    async def _fake_wait(req_id, timeout=60.0):
        return JsonRpcMessage.from_dict({"id": 1, "result": {"text": "secret token xyz"}})

    handle._wait_for_response = _fake_wait  # type: ignore[assignment]
    out = await handle.send_command("/compact")
    assert out == "REDACTED"


# ── Round-2 parity fixes: auth detection, exception translation, steer ──


@pytest.mark.asyncio
async def test_saw_not_logged_in_detects_auth_failure():
    """#1: AcpRuntime.saw_not_logged_in reports kiro-cli's auth-failure signal on
    stderr so a death can be surfaced as AcpAuthRequired.

    Drives the real ``_drain_stderr`` rather than assigning ``_stderr_lines``
    directly. The observation is now latched as each line arrives, because the
    buffer is a 20-line ring and nothing asks about auth until a request has
    already timed out -- by which point a chatty startup can have evicted the
    line. ``_drain_stderr`` is the only production writer of that buffer, so
    driving it is strictly closer to the real path than the previous assignment
    was; the two assertions below are unchanged in intent.
    """

    class _Stderr:
        def __init__(self, lines):
            self._lines = [f"{ln}\n".encode() for ln in lines]

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

    async def _drain(lines):
        rt, _, proc = _make_runtime()
        proc.stderr = _Stderr(lines)
        await rt._drain_stderr()
        return rt

    rt = await _drain(["startup noise", "error: You are not logged in, please log in"])
    assert rt.saw_not_logged_in() is True
    rt = await _drain(["ordinary stderr", "mcp server ready"])
    assert rt.saw_not_logged_in() is False


@pytest.mark.asyncio
async def test_stream_translates_runtime_dead_to_process_died():
    """#2: AcpSessionProvider.stream translates AcpRuntimeDead (an
    AcpRuntimeError, which chat_runner does NOT catch) into AcpProcessDied so
    the caller's AcpProcessDied handler fires (parity with AcpClient)."""
    from kiro_crew.acp.client import AcpProcessDied
    from kiro_crew.acp.session_provider import AcpSessionProvider

    rt = MagicMock()
    rt.saw_not_logged_in = MagicMock(return_value=False)
    handle = MagicMock()

    async def _boom(msg):
        raise AcpRuntimeDead("pipe broken")
        yield  # noqa: mark as async generator

    handle.prompt = _boom
    prov = AcpSessionProvider.__new__(AcpSessionProvider)
    prov._handle = handle
    prov._runtime = rt
    with pytest.raises(AcpProcessDied):
        async for _ in prov.stream("hi"):
            pass


@pytest.mark.asyncio
async def test_stream_translates_auth_failure_to_auth_required():
    """#1: when stderr shows 'not logged in', a runtime death surfaces as
    AcpAuthRequired (non-retryable login prompt) rather than AcpProcessDied."""
    from kiro_crew.acp.client import AcpAuthRequired
    from kiro_crew.acp.session_provider import AcpSessionProvider

    rt = MagicMock()
    rt.saw_not_logged_in = MagicMock(return_value=True)
    handle = MagicMock()

    async def _boom(msg):
        raise AcpRuntimeDead("pipe broken")
        yield

    handle.prompt = _boom
    prov = AcpSessionProvider.__new__(AcpSessionProvider)
    prov._handle = handle
    prov._runtime = rt
    with pytest.raises(AcpAuthRequired):
        async for _ in prov.stream("hi"):
            pass


@pytest.mark.asyncio
async def test_handle_steer_sends_session_steer():
    """#3: outbound steer() wraps the message and sends _session/steer; empty
    message or no session returns False without sending."""
    sent = {}

    async def _send_request(method, params):
        sent["method"] = method
        sent["params"] = params
        return 1

    rt = MagicMock()
    rt.send_request = _send_request
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt)
    assert handle.supports_steer is True
    assert handle.last_steer_monotonic == 0.0  # never steered
    ok = await handle.steer("please focus on X")
    assert ok is True
    assert sent["method"] == "_session/steer"
    assert "please focus on X" in sent["params"]["message"]
    assert await handle.steer("   ") is False


@pytest.mark.asyncio
async def test_handle_steer_stamps_write_time_and_provider_passes_it_through():
    """The stamp lives at the innermost write because that is the one point
    every steer funnels through — the dashboard steers the client directly
    while the IM transports steer the provider wrapper. The dashboard's
    keepalive route reads it to decide whether a sleeping `wait` should return
    early, so a refused steer must not move it.
    """
    rt = MagicMock()

    async def _send_request(method, params):
        return 1

    rt.send_request = _send_request
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt)
    from kiro_crew.acp.session_provider import AcpSessionProvider

    prov = AcpSessionProvider.__new__(AcpSessionProvider)
    prov._handle = handle
    prov._runtime = rt

    assert prov.last_steer_monotonic == 0.0
    before = time.monotonic()
    assert await handle.steer("focus on X") is True
    after = time.monotonic()
    stamped = handle.last_steer_monotonic
    assert before <= stamped <= after
    # The wrapper the IM transports hold must report the same fact.
    assert prov.last_steer_monotonic == stamped

    # A refused steer (empty text) never reached the wire, so it must not
    # look newer than the sleep it would otherwise cut short.
    assert await handle.steer("  ") is False
    assert handle.last_steer_monotonic == stamped


# ── Round-3 fixes: cancel_session grace + idempotent cancel ──


@pytest.mark.asyncio
async def test_provider_cancel_session_accepts_and_forwards_grace():
    """Blocker #1: AcpSessionProvider.cancel_session must accept grace_secs
    (AcpProvider.cancel calls it with grace_secs=) and forward it to the
    handle — otherwise a kiro-path cancel raises TypeError."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    rt, _, _ = _make_runtime()
    rt.send_notification = AsyncMock()  # type: ignore[method-assign]
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    prov = AcpSessionProvider.__new__(AcpSessionProvider)
    prov._handle = handle
    prov._runtime = rt
    await prov.cancel_session(grace_secs=25.0)  # must NOT raise TypeError
    assert handle._cancel_grace_secs == 25.0


def test_is_turn_active_factors_cancelled():
    """is_turn_active is False once cancel() has fired (parity with
    AcpClient.has_active_turn) so a repeat cancel is a no-op early-return."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._turn_done.clear()
    handle._cancelled = False
    assert handle.is_turn_active is True
    handle._cancelled = True
    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_dispatch_mcp_oauth_guard_and_dedup():
    """Shared-path mcp_oauth_request mirrors AcpClient (R5 fix): unsafe-scheme
    URLs and empty serverName are dropped; duplicates deduped; a matching
    server_initialized discards the dedupe entry so a later retry re-emits."""
    from kiro_crew.acp.types import (
        EVENT_MCP_OAUTH_REQUEST,
        METHOD_MCP_OAUTH_REQUEST,
        METHOD_MCP_SERVER_INITIALIZED,
    )

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        base = {"sessionId": "sA"}
        # unsafe scheme -> dropped
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {**base, "serverName": "evil", "oauthUrl": "javascript:alert(1)"},
            },
        )
        # empty serverName -> dropped
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {**base, "serverName": "", "oauthUrl": "https://ok.example.com"},
            },
        )
        # safe -> emitted
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {**base, "serverName": "gh", "oauthUrl": "https://auth.example.com"},
            },
        )
        # duplicate same server -> deduped
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {**base, "serverName": "gh", "oauthUrl": "https://auth.example.com"},
            },
        )
        # server_initialized -> discard dedupe entry
        _feed(
            reader,
            {"method": METHOD_MCP_SERVER_INITIALIZED, "params": {**base, "serverName": "gh"}},
        )
        # safe again after discard -> re-emitted
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {**base, "serverName": "gh", "oauthUrl": "https://auth.example.com"},
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=3.0)

        oauth = [e for e in events if e.kind == EVENT_MCP_OAUTH_REQUEST]
        # evil (unsafe) + empty-name dropped; gh emitted, deduped, then re-emitted = 2
        assert [e.server_name for e in oauth] == ["gh", "gh"], [e.server_name for e in oauth]
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_is_turn_active_requires_alive_runtime():
    """Contract parity: a turn on a DEAD runtime reads inactive (mirrors
    AcpClient.has_active_turn's process-alive condition)."""
    rt = MagicMock()
    rt.is_alive.return_value = True
    h = AcpSessionHandle("sA", asyncio.Queue(), rt)
    h._turn_done.clear()
    h._cancelled = False
    assert h.is_turn_active is True
    rt.is_alive.return_value = False
    assert h.is_turn_active is False


@pytest.mark.asyncio
async def test_set_model_syncs_resolved_model_id():
    """Contract parity: set_model updates BOTH _model and _resolved_model_id
    (else context-window backfill uses the stale session/new model)."""
    rt = MagicMock()
    rt.is_alive.return_value = True
    rt.send_request = AsyncMock()
    h = AcpSessionHandle("sA", asyncio.Queue(), rt)
    h._turn_done.set()
    await h.set_model("new-model")
    assert h._model == "new-model"
    assert h._resolved_model_id == "new-model"


@pytest.mark.asyncio
async def test_set_model_rebases_context_stats(monkeypatch):
    """Contract parity with AcpClient.set_model: a mid-session switch re-anchors
    last_prompt_stats to the new model's window and clears the authoritative
    usage flag, so the next metadata pct backfills against the NEW model
    instead of being gated forever by the old model's usage_update."""
    from kiro_crew import model_registry

    monkeypatch.setattr(model_registry, "has_known_window", lambda mid: True)
    monkeypatch.setattr(model_registry, "model_window", lambda mid, **kw: 272_000)
    rt = MagicMock()
    rt.is_alive.return_value = True
    rt.send_request = AsyncMock()
    h = AcpSessionHandle("sA", asyncio.Queue(), rt)
    h._turn_done.set()
    h.last_prompt_stats.context_used_tokens = 100_000
    h.last_prompt_stats.context_window_tokens = 1_000_000
    h.last_prompt_stats.context_pct = 10.0
    h.last_prompt_stats.context_tokens_from_usage = True

    await h.set_model("new-model")

    stats = h.last_prompt_stats
    assert stats.context_window_tokens == 272_000
    assert stats.context_used_tokens == 100_000
    assert stats.context_pct == round(100_000 / 272_000 * 100, 1)
    assert stats.context_tokens_from_usage is False


def test_normalize_models_shape():
    """Contract parity: available_models normalized to {modelId,name,description}
    with guaranteed keys (mirrors AcpClient._capture_available_models)."""
    out = AcpSessionHandle._normalize_models(
        [
            {"modelId": "m1", "name": "Model One", "description": "d"},
            {"value": "m2"},  # value fallback; name defaults to id
            {"name": "no-id"},  # dropped: no id
            "garbage",  # dropped: not a dict
        ]
    )
    assert out == [
        {"modelId": "m1", "name": "Model One", "description": "d"},
        {"modelId": "m2", "name": "m2", "description": ""},
    ]


def test_store_session_config_syncs_effort_levels(monkeypatch):
    """Contract parity: store_session_config pushes effort levels to the global
    validation set (mirrors AcpClient._sync_effort_levels)."""
    import sys
    import types

    calls = []
    fake = types.ModuleType("kiro_crew.dashboard.chat_persistence")
    fake.update_reasoning_effort_values = lambda levels: calls.append(levels)
    monkeypatch.setitem(sys.modules, "kiro_crew.dashboard.chat_persistence", fake)
    rt = MagicMock()
    rt.is_alive.return_value = True
    h = AcpSessionHandle("sA", asyncio.Queue(), rt)
    h.store_session_config(
        {"configOptions": [{"id": "effort", "options": [{"value": "low"}, {"value": "high"}]}]}
    )
    assert calls == [["low", "high"]]


@pytest.mark.asyncio
async def test_stale_turn_probes_then_signals_recovery():
    """A stale turn probed via session/cancel that never acks within the grace
    window is a confirmed wedge → the shared-runtime handle yields
    EVENT_COMPLETE(STOP_REASON_STALE_RECOVER) so the dashboard auto-recovers
    (reset+resume+continue-nudge). Replaces the former stale->end_turn behavior,
    which orphaned the wedged turn until the user's next message collided with
    'prompt already in progress'. (Stale DETECTION → probe is covered by
    test_acp_stale_recovery.py::test_genuine_stale_probes_via_cancel.)"""
    from kiro_crew.acp.types import EVENT_COMPLETE, STOP_REASON_STALE_RECOVER

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._turn_done.clear()  # a turn is in flight (cleared by prompt() in prod)
    # A genuine stale turn was probed via session/cancel; the grace window has
    # elapsed with no ack (confirmed wedge). The unresponsive-cancel branch runs
    # at the loop top, before any queue read, so this is deterministic.
    handle._stale_probe = True
    handle._cancelled = True
    handle._cancel_ts = time.monotonic() - 1.0
    handle._cancel_grace_secs = 0.05

    events = [ev async for ev in handle._dispatch_events(req_id=1, timeout=5.0)]

    assert events and events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == STOP_REASON_STALE_RECOVER
    assert handle._turn_done.is_set()


@pytest.mark.asyncio
async def test_mark_dead_clears_routed_requests():
    """R7 fix: _mark_dead clears _routed_requests (not just _pending_requests) so
    a routed-request correlation can't linger past runtime death."""
    rt, _, _ = _make_runtime()
    rt._routed_requests[42] = "sA"
    rt._pending_requests[7] = asyncio.get_event_loop().create_future()
    rt._mark_dead("test")
    assert rt._routed_requests == {}
    assert rt._pending_requests == {}


def test_build_permission_event_sets_raw_tool_params():
    """Regression (PR #21 HIGH): the shared build_permission_event must carry
    raw_tool_params (the structured dict cached from the preceding tool_call) so
    the governance keystone (hooks.on_tool_call sensitive-path / write-protected
    checks) enforces on the shared-runtime path even when the display title
    hides the path."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    raw_cache = {"tc-1": {"path": "/home/u/.ssh/id_rsa", "content": "x"}}
    msg = JsonRpcMessage.from_dict(
        {
            "id": 5,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "toolCall": {"toolCallId": "tc-1", "title": "Editing"},
                "options": [],
            },
        }
    )
    event, _recorded = build_permission_event(msg, raw_params_cache=raw_cache)
    assert event.raw_tool_params == {"path": "/home/u/.ssh/id_rsa", "content": "x"}
    # RETAINED on use (.get, matching the sibling caches): a second permission
    # frame for the same toolCallId (re-ask after reject_once, re-prompt after
    # a mode change) must still find the params — the per-turn dispatch
    # .clear() handles cleanup.
    assert "tc-1" in raw_cache
    event2, _ = build_permission_event(msg, raw_params_cache=raw_cache)
    assert event2.raw_params_trusted is True


def test_build_permission_event_raw_params_none_without_cache():
    """No cache entry + no inline dict → raw_tool_params stays None (no crash)."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    msg = JsonRpcMessage.from_dict(
        {
            "id": 6,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {"toolCall": {"toolCallId": "tc-x", "title": "Editing"}, "options": []},
        }
    )
    event, _ = build_permission_event(msg, raw_params_cache={})
    assert event.raw_tool_params is None


@pytest.mark.parametrize("redaction_cache", [None, {}])
def test_cached_input_without_redaction_provenance_fails_closed(redaction_cache):
    """Unknown cached-input provenance may display, but cannot grant trust."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    msg = JsonRpcMessage.from_dict(
        {
            "id": 61,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "toolCall": {"toolCallId": "tc-legacy", "title": "Legacy cached tool"},
                "options": [],
            },
        }
    )
    cached = '{"command": "echo [REDACTED: credential]"}'

    event, _ = build_permission_event(
        msg,
        tool_input_cache={"tc-legacy": cached},
        tool_input_redacted_cache=redaction_cache,
    )

    assert event.tool_input == cached
    assert event.tool_input_redacted is True


def test_build_permission_event_recovers_mcp_server_name_from_cache():
    """Regression: build_permission_event must carry mcp_server_name recovered
    from the preceding tool_call (the permission payload has no _meta), so
    hooks.on_tool_call's app-own-server auto-approve can fire on the dashboard
    permission path. Without this the event's mcp_server_name is always "" and
    the feature is inert."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    mcp_cache = {"tc-1": "mochi:mochi"}
    msg = JsonRpcMessage.from_dict(
        {
            "id": 7,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "toolCall": {"toolCallId": "tc-1", "title": "perform_pet_action"},
                "options": [],
            },
        }
    )
    event, _ = build_permission_event(msg, mcp_server_name_cache=mcp_cache)
    assert event.mcp_server_name == "mochi:mochi"
    # .get() (not .pop()): a later tool_call_update for the same id re-reads it.
    assert mcp_cache.get("tc-1") == "mochi:mochi"


def test_build_permission_event_mcp_server_name_empty_without_cache():
    """No cache / no entry → mcp_server_name stays "" (fail-closed: the app-own
    auto-approve never matches on a forged title with no trusted server name)."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    msg = JsonRpcMessage.from_dict(
        {
            "id": 8,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {"toolCall": {"toolCallId": "tc-y", "title": "x"}, "options": []},
        }
    )
    event, _ = build_permission_event(msg, mcp_server_name_cache={})
    assert event.mcp_server_name == ""


def test_build_permission_event_recovers_tool_name_from_cache():
    """Mirror of the mcp_server_name recovery: the permission payload carries no
    _meta, so build_permission_event recovers the trusted tool name from the
    preceding tool_call via tool_name_cache. This is what lets the
    app-own-server auto-approve rebuild the canonical mcp__<server>__<tool> and
    govern the real tool on the permission path."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    name_cache = {"tc-1": "perform_pet_action"}
    msg = JsonRpcMessage.from_dict(
        {
            "id": 9,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "toolCall": {"toolCallId": "tc-1", "title": "perform_pet_action"},
                "options": [],
            },
        }
    )
    event, _ = build_permission_event(msg, tool_name_cache=name_cache)
    assert event.tool_name == "perform_pet_action"
    # .get() (not .pop()): a later tool_call_update for the same id re-reads it.
    assert name_cache.get("tc-1") == "perform_pet_action"


def test_build_permission_event_tool_name_empty_without_cache():
    """No cache / no entry → tool_name stays "" (fail-closed: the app-own-server
    auto-approve cannot identify the tool to govern it, so it never fires)."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    msg = JsonRpcMessage.from_dict(
        {
            "id": 10,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {"toolCall": {"toolCallId": "tc-z", "title": "x"}, "options": []},
        }
    )
    event, _ = build_permission_event(msg, tool_name_cache={})
    assert event.tool_name == ""


def test_shared_handle_permission_inherits_origin_bound_tool_identity():
    """Shared-runtime transport carries identity through its real cache path."""
    rt, _, _ = _make_runtime()
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt)
    tool_events = handle._handle_update(
        JsonRpcMessage.from_dict(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-shared",
                        "title": "Shared model-authored title",
                        "kind": "other",
                        "rawInput": {},
                        "_meta": {
                            "kiro": {
                                "toolName": "delete_record",
                                "mcpServerName": "records:primary",
                            }
                        },
                    },
                },
            }
        )
    )
    assert tool_events and tool_events[0].tool_name == "delete_record"

    permission = handle._build_permission_event(
        JsonRpcMessage.from_dict(
            {
                "id": 11,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "sA",
                    "toolCall": {
                        "toolCallId": "tc-shared",
                        "title": "Shared model-authored title",
                    },
                    "options": [],
                },
            }
        )
    )

    assert permission.tool_name == "delete_record"
    assert permission.mcp_server_name == "records:primary"


def test_shared_handle_structured_non_shell_reprompt_keeps_argument_provenance():
    """Shared transport retains display and raw params across a re-prompt."""
    from kiro_crew.trust_patterns import approval_command

    rt, _, _ = _make_runtime()
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt)
    handle._handle_update(
        JsonRpcMessage.from_dict(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-shared-args",
                        "title": "Looking up the record",
                        "kind": "other",
                        "rawInput": {"record_id": "sensitive-record"},
                        "_meta": {
                            "kiro": {
                                "toolName": "read_record",
                                "mcpServerName": "records:primary",
                            }
                        },
                    },
                },
            }
        )
    )
    request = JsonRpcMessage.from_dict(
        {
            "id": 12,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sA",
                "toolCall": {
                    "toolCallId": "tc-shared-args",
                    "title": "Looking up the record",
                },
                "options": [],
            },
        }
    )

    first = handle._build_permission_event(request)
    repeated = handle._build_permission_event(request)

    assert first.tool_input
    assert repeated.tool_input == first.tool_input
    assert repeated.raw_tool_params == {"record_id": "sensitive-record"}
    assert (
        approval_command(
            repeated.tool_input,
            is_shell=repeated.is_shell,
            tool_name=repeated.tool_name,
            mcp_server_name=repeated.mcp_server_name,
            raw_tool_params=repeated.raw_tool_params,
        )
        == ""
    )


@pytest.mark.parametrize("raw_input", ["/etc/secret", ["/etc/secret"]])
def test_shared_non_dict_non_shell_reprompt_cannot_become_durable_tool_trust(raw_input):
    """String/list rawInput remains visible to the repeat trust gate."""
    from kiro_crew.trust_patterns import approval_command

    rt, _, _ = _make_runtime()
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt)
    handle._handle_update(
        JsonRpcMessage.from_dict(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-shared-nondict",
                        "title": "Reading a path",
                        "kind": "read",
                        "rawInput": raw_input,
                        "_meta": {"kiro": {"toolName": "read_path", "mcpServerName": "files"}},
                    },
                },
            }
        )
    )
    request = JsonRpcMessage.from_dict(
        {
            "id": 13,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sA",
                "toolCall": {
                    "toolCallId": "tc-shared-nondict",
                    "title": "Reading a path",
                },
                "options": [],
            },
        }
    )

    first = handle._build_permission_event(request)
    repeated = handle._build_permission_event(request)

    assert repeated.tool_input == first.tool_input
    assert repeated.tool_input
    assert (
        approval_command(
            repeated.tool_input,
            is_shell=repeated.is_shell,
            tool_name=repeated.tool_name,
            mcp_server_name=repeated.mcp_server_name,
            raw_tool_params=repeated.raw_tool_params,
        )
        == ""
    )


def test_build_permission_event_non_string_option_entries_skipped():
    """The shared parser feeds AcpSessionHandle's prompt event generator; a
    truthy non-string id (e.g. {"id": 42}) crashed opt_id.lower() in the
    legacy-kind synthesis, tearing down the turn on the shared-runtime
    transport — the same class of crash AcpClient's copy guards against.
    Non-dict entries and non-string label/kind must be skipped/coerced while
    valid entries still parse."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    msg = JsonRpcMessage.from_dict(
        {
            "id": 7,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "toolCall": {"toolCallId": "tc-y", "title": "shell"},
                "options": [
                    "allow",  # non-dict
                    None,  # non-dict
                    {"id": 42, "label": "int id"},  # non-string id → skipped
                    {"id": "allow_once", "label": 7, "kind": ["x"]},  # coerced
                    {"id": "allow_always", "label": "Always"},
                ],
            },
        }
    )
    event, recorded = build_permission_event(msg, raw_params_cache={})  # must not raise
    assert event.options == [
        {"id": "allow_once", "label": ""},
        {"id": "allow_always", "label": "Always"},
    ]
    assert recorded is not None


def test_mark_dead_unregisters_protected_pid():
    """Regression (PR #21 follow-up): _mark_dead must release the sweep-protection
    shield on ANY death path (not just kill()), else the dead PID lingers in
    _PROTECTED_PIDS forever and could shield a recycled-orphan from the sweep."""
    from kiro_crew.session_pid import _protected_pids, register_protected_pid

    rt, _, _ = _make_runtime()
    rt._pid = 515151
    register_protected_pid(rt._pid)
    assert rt._pid in _protected_pids()
    rt._mark_dead("simulated EOF")
    assert rt._pid not in _protected_pids()


def test_protected_runtime_pid_lands_in_sweep_active_set():
    """Companion (``_subagent_runtimes``) and background (``_bg_runtime``)
    AcpRuntimes live only in SessionManager instance attributes, NOT in
    ``self._sessions``, so ``_collect_active_pids`` cannot see them via a session
    provider. They stay protected only because ``AcpRuntime.spawn()`` shields
    their PID via ``register_protected_pid``, and ``_collect_active_pids`` seeds
    from ``_protected_pids()``. This asserts both a companion and a bg runtime
    PID land in the sweep's active set (so phase-2 never confirms them orphans) —
    the KiroCrew analog of the upstream project's end-to-end guard.
    """
    from kiro_crew.session_pid import (
        _collect_active_pids,
        register_protected_pid,
        unregister_protected_pid,
    )

    companion_pid, bg_pid = 717171, 727272
    register_protected_pid(companion_pid)
    register_protected_pid(bg_pid)
    try:
        # Empty session map == neither runtime is a registered session; they are
        # shielded ONLY via the register_protected_pid path that spawn() uses.
        active, ok = _collect_active_pids({})
        assert ok
        assert companion_pid in active
        assert bg_pid in active
    finally:
        unregister_protected_pid(companion_pid)
        unregister_protected_pid(bg_pid)

    # Once unregistered (runtime died), they are no longer shielded.
    active_after, _ = _collect_active_pids({})
    assert companion_pid not in active_after
    assert bg_pid not in active_after


def test_periodic_sweep_skips_protected_runtime_pid():
    """Reproduce the exact orphan-sweep path for a live companion/bg runtime: its
    kiro-cli PID is tagged in ``kiro_session_pids.txt`` and is NOT a registered
    session, so it would be confirmed an orphan and SIGKILLed — except the sweep
    wires ``is_managed = (pid in active_pids)`` and ``active_pids`` includes
    ``_protected_pids()``. With the runtime's PID registered (as ``spawn`` does),
    ``_sweep_pid_entries`` skips it (0 killed, entry retained).
    """
    from unittest.mock import patch

    from kiro_crew.session_pid import (
        _collect_active_pids,
        _sweep_pid_entries,
        register_protected_pid,
        unregister_protected_pid,
    )

    runtime_pid = 969696
    register_protected_pid(runtime_pid)
    try:
        active, ok = _collect_active_pids({})
        assert ok and runtime_pid in active
        with patch("os.kill", side_effect=lambda pid, sig: None):  # all alive
            killed, dead, _ = _sweep_pid_entries(
                [f"1:{runtime_pid}"],
                should_skip_tagged=lambda gw, p: False,
                should_skip_bare=lambda p: False,
                is_managed=lambda p: p in active,  # mirrors the real periodic sweep
            )
        assert killed == 0
        assert f"1:{runtime_pid}" not in dead
    finally:
        unregister_protected_pid(runtime_pid)


@pytest.mark.asyncio
async def test_runtime_spawn_scrubs_sensitive_env_on_default_auto(monkeypatch):
    """AcpRuntime.spawn applies the full ACP child scrub on the default tier.

    This parent-side enforcement is what protects raw Windows Kiro delegation;
    POSIX launchers apply the same sensitive/Python scrub inline.
    """
    import kiro_crew.acp.runtime as runtime_mod

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "0000:FAKE-telegram")
    monkeypatch.setenv("WECOM_BOT_ID", "FAKE-wecom-bot")
    monkeypatch.setenv("WECOM_SECRET", "FAKE-wecom-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-FAKE")
    monkeypatch.setenv("KIROCREW_OWNER_ID", "U_FAKE_OWNER")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "FAKE-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    monkeypatch.setenv("PYTHONPATH", "/gateway/pythonpath")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", "/gateway/pycache")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FAKE-akid")
    monkeypatch.setenv("KIROCREW_UNRELATED_KEEPME", "keep-this-value")

    captured: dict[str, object] = {}

    class _StopSpawn(Exception):
        pass

    async def _fake_exec(*_args, **kwargs):
        captured["env"] = kwargs.get("env")
        raise _StopSpawn()

    async def resolve_kiro_bin(*, environ=None, home=None):
        return "/fake/kiro"

    monkeypatch.setattr(
        runtime_mod,
        "_resolve_kiro_bin_for_spawn",
        resolve_kiro_bin,
    )
    monkeypatch.setattr(
        runtime_mod,
        "wrap_argv",
        lambda argv, mode, strip_python_env=False, is_kiro_cli=None: (argv, None),
    )
    monkeypatch.setattr(runtime_mod, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(runtime_mod, "augmented_path", lambda p: p)
    monkeypatch.setattr(runtime_mod, "resolve_krb5_ccname", lambda env: None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    rt = AcpRuntime(sandbox_mode="auto")  # default tier
    with pytest.raises(_StopSpawn):
        await rt.spawn()

    env = captured["env"]
    assert isinstance(env, dict)
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "WECOM_BOT_ID",
        "WECOM_SECRET",
        "SLACK_BOT_TOKEN",
        "KIROCREW_OWNER_ID",
        "AWS_SECRET_ACCESS_KEY",
        "SSH_AUTH_SOCK",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
    ):
        assert key not in env, f"{key} leaked into runtime child env"
    assert env.get("KIROCREW_UNRELATED_KEEPME") == "keep-this-value"
    assert env.get("AWS_ACCESS_KEY_ID") == "FAKE-akid"


@pytest.mark.asyncio
async def test_runtime_spawn_names_its_own_browser_session(monkeypatch):
    """A subagent gets its own playwright-cli browser, not the parent's.

    AcpRuntime builds its child environment independently of AcpClient, so this
    is the drift guard: without it a subagent's ``goto`` lands in whatever page
    the parent was reading, and its ``close`` takes the parent's browser down.
    """
    import kiro_crew.acp.runtime as runtime_mod

    monkeypatch.delenv("PLAYWRIGHT_CLI_SESSION", raising=False)
    captured: dict[str, object] = {}

    class _StopSpawn(Exception):
        pass

    async def _fake_exec(*_args, **kwargs):
        captured["env"] = kwargs.get("env")
        raise _StopSpawn()

    async def resolve_kiro_bin(*, environ=None, home=None):
        return "/fake/kiro"

    monkeypatch.setattr(runtime_mod, "_resolve_kiro_bin_for_spawn", resolve_kiro_bin)
    monkeypatch.setattr(
        runtime_mod,
        "wrap_argv",
        lambda argv, mode, strip_python_env=False, is_kiro_cli=None: (argv, None),
    )
    monkeypatch.setattr(runtime_mod, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(runtime_mod, "augmented_path", lambda p: p)
    monkeypatch.setattr(runtime_mod, "resolve_krb5_ccname", lambda env: None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    names = []
    for _ in range(2):
        rt = AcpRuntime(sandbox_mode="auto")
        with pytest.raises(_StopSpawn):
            await rt.spawn()
        env = captured["env"]
        assert isinstance(env, dict)
        names.append(env["PLAYWRIGHT_CLI_SESSION"])

    assert all(name.startswith("kc-") for name in names)
    assert names[0] != names[1]


# ── Unroutable-frame drop accounting (log-flood containment) ──
#
# The reader drops any frame it cannot route. Logging that per frame turned a
# multiplexed backend's post-teardown / transcript-replay stream into ~60
# lines/second sustained for hours, taking 33–59% of every 2MB gateway.log
# rotation and rolling incident evidence out of the retained window. These tests
# lock in the replacement: one throttled summary per (sessionId, method) carrying
# the count, with the DROP behaviour itself unchanged.


async def _drain(reader: asyncio.StreamReader, timeout: float = 5.0) -> None:
    """Wait until the reader loop has consumed everything fed, or fail loudly.

    A fixed ``asyncio.sleep(0.05)`` encodes an assumption about scheduler
    latency that a loaded CI runner breaks -- it is why this suite's Windows
    shard failed while its siblings passed. Waiting on the observable condition
    (stdout buffer drained, then a bounded number of turns for the handler that
    follows ``readline``) is deterministic under load and faster locally.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while reader._buffer:
        if loop.time() >= deadline:
            raise AssertionError("reader loop did not consume the fed frames in time")
        await asyncio.sleep(0)
    for _ in range(10):
        await asyncio.sleep(0)


def _drop_records(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "unroutable frame(s)" in r.getMessage()]


@pytest.mark.asyncio
async def test_unknown_session_drops_aggregate_into_one_counted_record(caplog):
    """N drops of the same (sid, method) inside one window → ONE record, count N."""
    import logging

    import kiro_crew.acp.runtime as runtime_mod

    rt, reader, _ = _make_runtime()
    task = await _start_reader(rt)
    try:
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
            for _ in range(5):
                _feed(reader, {"method": "session/update", "params": {"sessionId": "ghost"}})
            await _drain(reader)
            # Still inside the first window: aggregated, nothing emitted yet —
            # this is the assertion that fails on the per-frame implementation.
            assert _drop_records(caplog) == []
            assert rt._dropped_frames == {("ghost", "session/update"): 5}

            # Age the window out, then one more drop triggers the flush.
            rt._dropped_frames_flushed_at -= runtime_mod._DROP_SUMMARY_INTERVAL_SECS + 1.0
            _feed(reader, {"method": "session/update", "params": {"sessionId": "ghost"}})
            await _drain(reader)

        records = _drop_records(caplog)
        assert len(records) == 1, records
        assert (
            "Dropped 6 unroutable frame(s) for session ghost (method=session/update)" in records[0]
        )
        # The point of the change: SIX dropped frames produce ONE log record,
        # not six. Counts every record naming the session, whatever its wording.
        assert len([r for r in caplog.records if "ghost" in r.getMessage()]) == 1
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_two_unknown_sessions_are_counted_separately(caplog):
    """A global tally would hide that two distinct session UUIDs are flooding."""
    import logging

    rt, reader, _ = _make_runtime()
    task = await _start_reader(rt)
    try:
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
            for _ in range(3):
                _feed(reader, {"method": "session/update", "params": {"sessionId": "sid-aaa"}})
            for _ in range(2):
                _feed(reader, {"method": "session/update", "params": {"sessionId": "sid-bbb"}})
            await _drain(reader)
            # Residual flush on reader exit reports both keys.
            await _stop_reader(task)

        records = _drop_records(caplog)
        assert len(records) == 2, records
        joined = "\n".join(records)
        assert "Dropped 3 unroutable frame(s) for session sid-aaa" in joined
        assert "Dropped 2 unroutable frame(s) for session sid-bbb" in joined
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_counted_drop_is_still_dropped_not_delivered():
    """Logging change only: an unroutable frame reaches no queue, as before."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": "session/update", "params": {"sessionId": "ghost"}})
        await _drain(reader)
        # Not routed to the co-tenant, not broadcast — just counted.
        assert q["sA"].empty()
        assert rt._dropped_frames == {("ghost", "session/update"): 1}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_no_session_broadcast_drops_are_counted(caplog):
    """With zero registered sessions every global frame drops — same shape."""
    import logging

    import kiro_crew.acp.runtime as runtime_mod

    rt, reader, _ = _make_runtime()
    task = await _start_reader(rt)
    try:
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
            for _ in range(4):
                _feed(reader, {"method": "mcp/status", "params": {}})
            await _drain(reader)
            assert rt._dropped_frames == {(runtime_mod._DROP_NO_SESSION, "mcp/status"): 4}
            await _stop_reader(task)

        records = _drop_records(caplog)
        assert len(records) == 1, records
        assert "Dropped 4 unroutable frame(s)" in records[0]
        assert "(method=mcp/status)" in records[0]
    finally:
        await _stop_reader(task)


def test_drop_counter_state_does_not_leak_between_intervals(caplog):
    """A flushed window starts empty — the next record counts only new drops."""
    import logging

    rt, _reader, _ = _make_runtime()

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        rt._note_dropped_frame("sid-x", "session/update")
        rt._note_dropped_frame("sid-x", "session/update")
        rt._flush_dropped_frames()
        assert rt._dropped_frames == {}

        rt._note_dropped_frame("sid-x", "session/update")
        rt._flush_dropped_frames()
        assert rt._dropped_frames == {}

    records = _drop_records(caplog)
    assert len(records) == 2, records
    assert "Dropped 2 unroutable frame(s) for session sid-x" in records[0]
    # Not 3 — the first window's count did not carry over.
    assert "Dropped 1 unroutable frame(s) for session sid-x" in records[1]


def test_drop_counter_map_is_bounded(caplog):
    """A wide fan-out of distinct keys flushes early instead of growing."""
    import logging

    import kiro_crew.acp.runtime as runtime_mod

    rt, _reader, _ = _make_runtime()
    cap = runtime_mod._DROP_SUMMARY_MAX_KEYS

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        for i in range(cap * 3):
            rt._note_dropped_frame(f"sid-{i}", "session/update")
            assert len(rt._dropped_frames) <= cap

    # Overflow forced flushes rather than an unbounded map.
    assert len(_drop_records(caplog)) >= cap


def test_drop_counter_truncates_backend_controlled_key_text():
    """A pathological sessionId/method cannot be retained at full length."""
    import kiro_crew.acp.runtime as runtime_mod

    rt, _reader, _ = _make_runtime()
    limit = runtime_mod._DROP_SUMMARY_KEY_MAX_CHARS

    rt._note_dropped_frame("s" * (limit * 10), "m" * (limit * 10))

    (session_id, method), count = next(iter(rt._dropped_frames.items()))
    assert count == 1
    assert len(session_id) == limit
    assert len(method) == limit


def test_drop_counter_handles_missing_method():
    """A frame with no `method` is still counted, under a placeholder key."""
    rt, _reader, _ = _make_runtime()

    rt._note_dropped_frame("sid-x", None)

    assert rt._dropped_frames == {("sid-x", "?"): 1}


# The two key halves come straight from backend JSON, which is untrusted and
# type-unchecked (JsonRpcMessage.from_dict copies `method` / `params` verbatim).
# A wrong-typed value used to raise TypeError inside _reader_loop — the SINGLE
# owner of this process's stdout — killing every multiplexed session over one
# malformed frame. These lock in that the frame is counted and the demux lives.


@pytest.mark.asyncio
async def test_numeric_method_is_counted_and_reader_survives():
    """`{"method": 123}` must not kill the shared reader (all sessions with it)."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": 123, "params": {"sessionId": "ghost"}})
        await _drain(reader)

        # Counted under the placeholder, not crashed.
        assert rt._dropped_frames == {("ghost", "?"): 1}
        # The property the finding is about: the demux is still alive...
        assert rt._dead is False
        assert rt.is_alive() is True
        assert not task.done()
        # ...and still routing for every co-tenant session.
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await asyncio.wait_for(q["sA"].get(), timeout=1.0)
        assert msg.params["sessionId"] == "sA"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_non_string_session_id_is_counted_and_reader_survives():
    """Same hazard on the sessionId half: `params.sessionId` is Any, not str."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        # Truthy, unregistered, and not a str → reaches the drop counter.
        _feed(reader, {"method": "session/update", "params": {"sessionId": 12345}})
        await _drain(reader)

        assert rt._dropped_frames == {("?", "session/update"): 1}
        assert rt._dead is False
        assert rt.is_alive() is True
        assert not task.done()
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await asyncio.wait_for(q["sA"].get(), timeout=1.0)
        assert msg.params["sessionId"] == "sA"
    finally:
        await _stop_reader(task)


def test_drop_counter_placeholder_appears_in_flushed_summary(caplog):
    """A coerced key half is reported as the placeholder, wording unchanged."""
    import logging

    rt, _reader, _ = _make_runtime()

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        rt._note_dropped_frame(12345, 123)
        rt._flush_dropped_frames()

    records = _drop_records(caplog)
    assert len(records) == 1, records
    assert "Dropped 1 unroutable frame(s) for session ? (method=?)" in records[0]


class TestToolPurposeExtraction:
    """The reserved purpose arg is what the dashboard's concise tool pill shows
    instead of the literal invocation. kiro-cli echoes it back under EITHER
    spelling, so the shared runtime path must accept both — matching only the
    snake_case key silently degraded half the pills to raw command text."""

    def _update(self, raw_input: dict) -> dict:
        return {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-purpose",
            "kind": "execute",
            "title": "Running: node kc-shot.mjs",
            "rawInput": raw_input,
        }

    def test_snake_case_key(self):
        from kiro_crew.acp._dispatch import _build_tool_call_event

        event = _build_tool_call_event(
            self._update({"command": "node kc-shot.mjs", "__tool_use_purpose": "check harness"}),
            None,
        )
        assert event.tool_purpose == "check harness"

    def test_camel_case_key(self):
        from kiro_crew.acp._dispatch import _build_tool_call_event

        event = _build_tool_call_event(
            self._update({"command": "node kc-shot.mjs", "__toolUsePurpose": "check harness"}),
            None,
        )
        assert event.tool_purpose == "check harness"

    def test_no_purpose_key_yields_empty(self):
        from kiro_crew.acp._dispatch import _build_tool_call_event

        event = _build_tool_call_event(self._update({"command": "node kc-shot.mjs"}), None)
        assert event.tool_purpose == ""

    def test_blank_and_non_string_values_ignored(self):
        from kiro_crew.acp._dispatch import extract_tool_purpose

        assert extract_tool_purpose({"__tool_use_purpose": "   "}) == ""
        assert extract_tool_purpose({"__toolUsePurpose": 123}) == ""
        assert extract_tool_purpose("not a dict") == ""
        # A blank snake_case value must not shadow a real camelCase one.
        assert (
            extract_tool_purpose({"__tool_use_purpose": "", "__toolUsePurpose": "real"}) == "real"
        )


# ── set_mode availableModes guard (regression: "Mode '<agent>' not found") ──


def _new_resp(modes: dict | None) -> dict:
    r: dict = {"sessionId": "s1"}
    if modes is not None:
        r["modes"] = modes
    return r


@pytest.mark.asyncio
async def test_create_session_sets_mode_when_agent_is_advertised():
    """Happy path: the requested agent is in availableModes → set_mode fires."""
    rt, _, _ = _make_runtime()
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    resp = _new_resp(
        {"currentModeId": "kirocrew", "availableModes": [{"id": "kirocrew"}, {"id": "ops"}]}
    )
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        handle = await rt.create_session(agent="ops", mcp_servers=[])
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert methods == [METHOD_SESSION_NEW, METHOD_SET_MODE]
    assert rt._send_and_await.call_args_list[1].args[1] == {
        "sessionId": "s1",
        "modeId": "ops",
    }
    assert handle.session_id == "s1"


@pytest.mark.asyncio
async def test_create_session_fails_closed_when_agent_not_advertised():
    """Guard (A): modes advertised but the agent is absent → FAIL CLOSED
    (terminate + raise), never silently run the backend default. Substituting a
    broader default for a requested restricted agent would be a privilege
    escalation."""
    rt, _, _ = _make_runtime()
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    resp = _new_resp({"currentModeId": "default", "availableModes": [{"id": "default"}]})
    # session/new response, then the terminate roundtrip from the fail-closed path
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        with pytest.raises(AcpRuntimeError, match="not available"):
            await rt.create_session(agent="kirocrew", mcp_servers=[])
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert METHOD_SET_MODE not in methods  # never activated the wrong mode
    assert METHOD_SESSION_TERMINATE in methods  # created session cleaned up
    assert "s1" not in rt._session_queues  # unregistered


@pytest.mark.asyncio
async def test_create_session_fails_closed_when_available_modes_empty():
    """Regression (GPT round 2): an explicitly-empty `availableModes: []` is
    ADVERTISED (not absent), so it must fail closed — not be treated as
    "no modes → attempt" and then fault with "Mode not found"."""
    rt, _, _ = _make_runtime()
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    resp = _new_resp({"currentModeId": "kirocrew", "availableModes": []})
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        with pytest.raises(AcpRuntimeError, match="not available"):
            await rt.create_session(agent="kirocrew", mcp_servers=[])
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert METHOD_SET_MODE not in methods
    assert METHOD_SESSION_TERMINATE in methods


@pytest.mark.asyncio
async def test_create_session_sets_mode_when_no_modes_advertised():
    """Backward compat: a backend that omits `modes` (older kiro-cli / fake
    backend) still gets set_mode attempted."""
    rt, _, _ = _make_runtime()
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    resp = _new_resp(None)
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        await rt.create_session(agent="kirocrew", mcp_servers=[])
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert METHOD_SET_MODE in methods


def test_mode_available_helper():
    """Unit: the guard predicate. Empty modes ⇒ attempt (True); advertised ⇒
    membership test."""
    from kiro_crew.acp.runtime import AcpRuntime

    assert AcpRuntime._mode_available("kirocrew", _new_resp(None)) is True
    assert (
        AcpRuntime._mode_available("kirocrew", _new_resp({"availableModes": [{"id": "kirocrew"}]}))
        is True
    )
    assert (
        AcpRuntime._mode_available("kirocrew", _new_resp({"availableModes": [{"id": "default"}]}))
        is False
    )
    # Present-but-empty availableModes → advertised, agent absent → fail closed.
    assert AcpRuntime._mode_available("kirocrew", _new_resp({"availableModes": []})) is False
    # A modes dict WITHOUT an availableModes list → not advertised → attempt.
    assert AcpRuntime._mode_available("kirocrew", _new_resp({"currentModeId": "x"})) is True


def test_parse_session_modes_shapes():
    """The shared parser: absent/odd `modes` ⇒ ([], '', False); a present
    availableModes list ⇒ advertised=True (even when empty); id read from
    id → modeId → value fallbacks."""
    from kiro_crew.acp._dispatch import parse_session_modes

    assert parse_session_modes({}) == ([], "", False)
    assert parse_session_modes({"modes": "nonsense"}) == ([], "", False)
    # modes dict but no availableModes list → not advertised (attempt path).
    assert parse_session_modes({"modes": {"currentModeId": "x"}}) == ([], "x", False)
    # present but empty → advertised True (fail-closed path).
    assert parse_session_modes({"modes": {"availableModes": []}}) == ([], "", True)
    ids, current, advertised = parse_session_modes(
        {
            "modes": {
                "currentModeId": "kirocrew",
                "availableModes": [
                    {"id": "kirocrew"},
                    {"modeId": "ops"},
                    {"value": "code-reviewer"},
                    {"name": "no-id-dropped"},
                    "not-a-dict",
                ],
            }
        }
    )
    assert ids == ["kirocrew", "ops", "code-reviewer"]
    assert current == "kirocrew"
    assert advertised is True


# ── Session-start timeout budget (#2946) ──
#
# kiro-cli blocks the session/new (and session/load) response while it
# initializes the session's MCP servers; a remote server pending OAuth holds
# that for its full 30s authorization wait. These tests lock in the CALL SITE
# — the timeout actually handed to _send_and_await — not just the constant,
# because dropping the ``timeout=`` argument silently reverts to the generic
# 30s _REQUEST_TIMEOUT, which is the exact regression.


@pytest.mark.asyncio
async def test_session_new_call_site_passes_budget_above_request_timeout(monkeypatch):
    """create_session must hand _send_and_await an explicit session/new timeout
    that exceeds _REQUEST_TIMEOUT — the generic default equals the backend's
    30s OAuth wait, turning session start into a race the client loses."""
    rt, _, _ = _make_runtime()
    rt._expect_mcp_reports = False  # skip the MCP drain wait — not under test
    seen: dict[str, object] = {}

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            seen["timeout"] = timeout
            return {"sessionId": "sid-budget"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    await rt.create_session(cwd="/w", mcp_servers=[])

    assert seen["timeout"] == _SESSION_NEW_TIMEOUT
    assert isinstance(seen["timeout"], float)
    assert seen["timeout"] > _REQUEST_TIMEOUT


@pytest.mark.asyncio
async def test_session_load_call_site_passes_budget_above_request_timeout(monkeypatch):
    """load_session is gated by the same MCP re-initialization (kiro-cli
    re-initializes servers on load; oauth_request frames are staged while
    either request is in flight), so it must carry the same budget."""
    rt, _, _ = _make_runtime()
    rt._can_load_session = True
    rt._expect_mcp_reports = False  # skip the MCP drain wait — not under test
    seen: dict[str, object] = {}

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_LOAD:
            seen["timeout"] = timeout
            return {"modes": {"currentModeId": "kirocrew"}}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    await rt.load_session("/home/u/.kiro/sessions/cli/sid-9.json", "sid-9", cwd="/w")

    assert seen["timeout"] == _SESSION_NEW_TIMEOUT
    assert seen["timeout"] > _REQUEST_TIMEOUT


@pytest.mark.asyncio
async def test_session_start_budget_follows_config(monkeypatch):
    """agent.session_start_timeout_secs raises the session/new budget: the
    configured value is resolved lazily (off-loop, on first session start —
    never in __init__, where a config cache miss would block the event loop)
    and handed to _send_and_await, so a large agent whose MCP fleet needs
    longer than the 90s default (many servers, sandboxed per-server
    launchers, loaded hosts) can extend the budget without patching the
    constant. Resolved once per runtime and cached thereafter."""
    from types import SimpleNamespace

    from kiro_crew.config.loader import KiroCrewConfig

    fake_cfg = SimpleNamespace(agent=SimpleNamespace(session_start_timeout_secs=240))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: fake_cfg))

    rt, _, _ = _make_runtime()
    rt._expect_mcp_reports = False
    # Lazy: construction must not have resolved (or read) config.
    assert rt._session_start_timeout is None
    seen: dict[str, object] = {}

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            seen["timeout"] = timeout
            return {"sessionId": "sid-cfg"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    await rt.create_session(cwd="/w", mcp_servers=[])

    assert seen["timeout"] == 240.0
    # Cached: later session starts on this runtime reuse the snapshot.
    assert rt._session_start_timeout == 240.0
    assert await rt._session_start_budget() == 240.0


def test_runtime_construction_never_touches_config(monkeypatch):
    """AcpRuntime.__init__ runs on the event loop; KiroCrewConfig.load() is a
    synchronous disk read + schema validation on a cache miss, so the budget
    must resolve lazily (off-loop) on first session start — never at
    construction."""
    from kiro_crew.config.loader import KiroCrewConfig

    def _boom(cls):
        raise AssertionError("config must not be consulted at construction")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    rt = AcpRuntime(work_dir="/tmp")
    assert rt._session_start_timeout is None


def test_resolve_session_start_timeout_floors_and_falls_back(monkeypatch):
    """The resolver never returns below the built-in floor (a budget under the
    backend's 30s OAuth wait recreates the #2946 race), and any config-load
    failure degrades to the default instead of breaking runtime construction."""
    from types import SimpleNamespace

    from kiro_crew.acp.runtime import _resolve_session_start_timeout
    from kiro_crew.config.loader import KiroCrewConfig

    # Below-floor value (belt-and-braces: the loader clamp already prevents
    # this on disk, but a degraded load must not shrink the budget either).
    low_cfg = SimpleNamespace(agent=SimpleNamespace(session_start_timeout_secs=10))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: low_cfg))
    assert _resolve_session_start_timeout() == _SESSION_NEW_TIMEOUT

    # Config load blowing up falls back to the default.
    def _boom(cls):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    assert _resolve_session_start_timeout() == _SESSION_NEW_TIMEOUT


@pytest.mark.asyncio
async def test_send_and_await_timeout_error_names_the_budget():
    """The timeout message must carry the budget that elapsed so a 90s
    session-start timeout is distinguishable from a generic 30s one. The
    'timed out' substring is load-bearing (chat_runner matches on it)."""
    rt, _, _ = _make_runtime()

    with pytest.raises(AcpRuntimeError) as exc_info:
        # stdin is mocked and nothing ever responds → wait_for times out.
        await rt._send_and_await("probe/method", {}, timeout=0.01)

    msg = str(exc_info.value)
    assert "timed out" in msg
    assert "0.01s" in msg
    assert "probe/method" in msg


# ── Unroutable permission requests (backend-internal subagents) ──────────────
#
# A `session/request_permission` REQUEST for a sessionId this client never
# registered comes from a backend-internal subagent (e.g. kiro-cli's own
# `subagent` tool). Dropping it strands the backend's response oneshot and
# wedges the child's whole tool batch until process teardown — the 2026-08-15
# crew incident hung 13 such approvals for 2 hours. These tests pin the fix:
# the runtime answers the request itself, with the request's own reject
# option, and never counts it as a dropped frame.


def _last_written_frame(proc) -> dict:
    """The most recent JSON frame written to the fake process stdin."""
    assert proc.stdin.write.call_args is not None, "nothing was written to stdin"
    raw = proc.stdin.write.call_args[0][0]
    return json.loads(raw.decode())


@pytest.fixture(autouse=True)
def _stub_sel_for_permission_tests(request, monkeypatch):
    """Stub the SEL for the auto-reject tests in this section.

    The production path fires the audit on a background ``asyncio.to_thread``
    task; letting it hit the real SEL from unit tests is slow (first-use
    filesystem setup) and races the test's event-loop teardown ("Event loop is
    closed" noise on loaded CI shards). Scoped by test-name prefix so the rest
    of the module keeps its behavior.
    """
    if not request.node.name.startswith(
        ("test_unroutable", "test_registered_session_permission", "test_ambiguous")
    ):
        yield
        return
    import kiro_crew.sel as sel_mod

    class _StubSel:
        def log_tool_invocation(self, **kwargs):  # noqa: D401 - stub
            return None

    monkeypatch.setattr(sel_mod, "sel", lambda: _StubSel())
    yield


async def _drain_audits(rt) -> None:
    """Await in-flight audit tasks so none outlives the test's event loop."""
    if rt._audit_tasks:
        await asyncio.gather(*list(rt._audit_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_unroutable_permission_request_is_auto_rejected(caplog):
    """Unknown-session permission REQUEST → answered with its reject option."""
    import logging

    rt, reader, proc = _make_runtime()
    task = await _start_reader(rt)
    try:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.runtime"):
            _feed(
                reader,
                {
                    "jsonrpc": "2.0",
                    "id": 77,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": "ghost-child",
                        "toolCall": {"toolCallId": "tc-1", "title": "glob /home"},
                        "options": [
                            {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                        ],
                    },
                },
            )
            await _drain(reader)

        frame = _last_written_frame(proc)
        assert frame["id"] == 77
        assert frame["result"] == {"outcome": {"outcome": "selected", "optionId": "reject_once"}}
        # Answered, not dropped: the drop counter must stay empty so the
        # summary log cannot misattribute an answered request as a drop.
        assert rt._dropped_frames == {}
        warnings = [r.getMessage() for r in caplog.records if "ghost-child" in r.getMessage()]
        assert warnings and "auto-rejected" in warnings[0]
        assert "glob /home" in warnings[0]
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unroutable_permission_never_picks_an_allow_option():
    """A payload with ONLY allow options must answer `cancelled`, never allow."""
    rt, reader, proc = _make_runtime()
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 78,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "ghost-child",
                    "options": [
                        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "allow_always", "name": "Always", "kind": "allow_always"},
                    ],
                },
            },
        )
        await _drain(reader)

        frame = _last_written_frame(proc)
        assert frame["id"] == 78
        assert frame["result"] == {"outcome": {"outcome": "cancelled"}}
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unroutable_permission_legacy_options_without_kind():
    """Legacy kiro options omit `kind`; only a well-known reject id matches."""
    rt, reader, proc = _make_runtime()
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 79,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "ghost-child",
                    "options": [
                        {"optionId": "allow_once", "name": "Allow"},
                        {"optionId": "reject_once", "name": "Reject"},
                    ],
                },
            },
        )
        await _drain(reader)

        frame = _last_written_frame(proc)
        assert frame["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_registered_session_permission_still_routes_to_queue():
    """The fix must not intercept permission requests for registered sessions."""
    rt, reader, proc = _make_runtime()
    queues = _register(rt, "known-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 80,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "known-session",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        await _drain(reader)

        routed = queues["known-session"].get_nowait()
        assert routed.id == 80
        # The runtime did not answer on the session's behalf.
        proc.stdin.write.assert_not_called()
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unroutable_non_permission_request_still_drops():
    """Only permission requests get the auto-answer; other unknown-session
    frames keep the counted-drop behavior."""
    rt, reader, proc = _make_runtime()
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": "session/update", "params": {"sessionId": "ghost"}})
        await _drain(reader)
        assert rt._dropped_frames == {("ghost", "session/update"): 1}
        proc.stdin.write.assert_not_called()
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_subagent_list_update_snapshots_child_ids():
    """A broadcast list_update replaces the known-child set (full list each time)."""
    rt, reader, _ = _make_runtime()
    _register(rt, "parent-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {
                    "subagents": [
                        {"sessionId": "child-a", "sessionName": "correctness"},
                        {"sessionId": "child-b", "sessionName": "security"},
                    ],
                    "pendingStages": [],
                },
            },
        )
        await _drain(reader)
        assert rt._subagent_sessions == {"child-a", "child-b"}

        # Next update omits child-a (terminated) — the set is replaced, not grown.
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-b"}]},
            },
        )
        await _drain(reader)
        assert rt._subagent_sessions == {"child-b"}
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_known_subagent_permission_routes_to_slot_queue():
    """A child the backend announced gets the SAME policy pipeline as the main
    agent: its permission request lands on the slot's session queue instead of
    being auto-rejected."""
    rt, reader, proc = _make_runtime()
    queues = _register(rt, "parent-session")
    # An explicitly marked active turn — requests are only routed while the
    # owner's prompt dispatch loop is consuming the queue. (_routed_requests
    # is NOT a proxy for this: it also holds set_mode/steer/config ids.)
    rt.mark_turn_active("parent-session", True)
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 90,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "child-a",
                    "toolCall": {"toolCallId": "tc-9", "title": "glob /home"},
                    "options": [
                        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                    ],
                },
            },
        )
        await _drain(reader)

        # list_update broadcast + the routed permission request both arrive.
        frames = []
        while not queues["parent-session"].empty():
            frames.append(queues["parent-session"].get_nowait())
        assert any(f.id == 90 for f in frames), frames
        # The runtime did NOT answer it — the slot's consumer owns the decision.
        proc.stdin.write.assert_not_called()
        assert rt._dropped_frames == {}
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unannounced_session_permission_still_auto_rejected():
    """A sessionId the backend never announced cannot ride the routing path —
    it keeps the fail-closed auto-reject."""
    rt, reader, proc = _make_runtime()
    _register(rt, "parent-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 91,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "never-announced",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        await _drain(reader)

        frame = _last_written_frame(proc)
        assert frame["id"] == 91
        assert frame["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_ambiguous_multi_session_runtime_falls_back_to_reject():
    """With several registered sessions the child→consumer mapping is ambiguous
    — the frame names no owner — so the runtime fails closed instead of handing
    the approval to an arbitrary sibling's policy."""
    rt, reader, proc = _make_runtime()
    queues = _register(rt, "session-one", "session-two")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 92,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "child-a",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        await _drain(reader)

        frame = _last_written_frame(proc)
        assert frame["id"] == 92
        assert frame["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
        # Neither sibling consumer received the request frame.
        for q in queues.values():
            while not q.empty():
                assert q.get_nowait().id != 92
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_announced_child_session_update_routes_for_cache_population():
    """A child's session/update (tool_call) frame is routed to the slot queue
    so the consumer's caches capture the real command bytes for a later
    permission request — the payload full mode-parity depends on."""
    rt, reader, proc = _make_runtime()
    queues = _register(rt, "parent-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "method": "session/update",
                "params": {
                    "sessionId": "child-a",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-child-1",
                        "title": "Running: sha256sum README.md",
                        "kind": "execute",
                        "rawInput": {"command": "sha256sum README.md"},
                    },
                },
            },
        )
        await _drain(reader)

        frames = []
        while not queues["parent-session"].empty():
            frames.append(queues["parent-session"].get_nowait())
        routed = [f for f in frames if f.method == "session/update"]
        assert routed, "child session/update must reach the slot queue"
        assert (routed[0].params or {}).get("sessionId") == "child-a"
        # Not answered by the runtime, not counted as a drop.
        proc.stdin.write.assert_not_called()
        assert rt._dropped_frames == {}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unannounced_child_session_update_still_drops():
    """Updates for sessions the backend never announced keep the counted-drop
    path — routing is gated on the announce, same as permission requests."""
    rt, reader, proc = _make_runtime()
    q = _register(rt, "parent-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "session/update",
                "params": {
                    "sessionId": "never-announced",
                    "update": {"sessionUpdate": "tool_call"},
                },
            },
        )
        await _drain(reader)
        assert rt._dropped_frames == {("never-announced", "session/update"): 1}
        assert q["parent-session"].empty()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_session_swap_on_warm_runtime_does_not_inherit_child_routing():
    """A new session registered after the announcing owner departs must NOT
    receive the stale child's permission requests — they fail closed."""
    rt, reader, proc = _make_runtime()
    _register(rt, "owner-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        await _drain(reader)
        assert rt._subagent_owner == "owner-session"

        # Owner departs; a different session takes the warm runtime.
        rt.unregister_session("owner-session")
        assert rt._subagent_owner is None and rt._subagent_sessions == set()
        q2 = _register(rt, "successor-session")

        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 95,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "child-a",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        await _drain(reader)

        frame = _last_written_frame(proc)
        assert frame["id"] == 95
        assert frame["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
        assert q2["successor-session"].empty()
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


def test_child_low_fidelity_requires_structured_security_context():
    """A rendered-diff tool_input alone is NOT fidelity: the child gate
    requires cache-provenance structured params, a resolved shell
    classification, and (for shells) a recoverable command."""
    from kiro_crew.acp.types import AcpEvent

    # Child edit refinement: diff text cached, no structured params → LOW.
    ev = AcpEvent(kind="permission_request", sub_session_id="child-a", tool_input="--- a\n+++ b")
    assert ev.child_low_fidelity is True
    # Child shell with params but unrecoverable command → LOW.
    ev = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        is_shell=True,
        raw_tool_params={"note": "no command key"},
        raw_params_trusted=True,
        shell_classified=True,
    )
    assert ev.child_low_fidelity is True
    # Inline (agent-authored) params without cache provenance → LOW even
    # with a recoverable command.
    ev = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        is_shell=True,
        raw_tool_params={"command": "sha256sum README.md"},
        raw_params_trusted=False,
        shell_classified=True,
    )
    assert ev.child_low_fidelity is True
    # Unresolved shell classification (cache miss defaults is_shell=False) → LOW.
    ev = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        raw_tool_params={"path": "/tmp/x"},
        raw_params_trusted=True,
        shell_classified=False,
    )
    assert ev.child_low_fidelity is True
    # Child with full provenance context → parity (not low).
    ev = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        is_shell=True,
        raw_tool_params={"command": "sha256sum README.md"},
        raw_params_trusted=True,
        shell_classified=True,
    )
    assert ev.child_low_fidelity is False
    # Non-child events are never low-fidelity.
    ev = AcpEvent(kind="permission_request")
    assert ev.child_low_fidelity is False


def test_child_mcp_identity_trusted_isolates_verified_identity():
    """The identity half of the fidelity split: verified server/tool pair on a
    child event whose ARGUMENTS never reached the cache. Each requirement is
    individually load-bearing (fail-closed on its own cache miss)."""
    from kiro_crew.acp.types import AcpEvent

    def _ev(**overrides):
        base: dict = dict(
            kind="permission_request",
            sub_session_id="child-a",
            shell_classified=True,
            is_shell=False,
            mcp_server_name="example-server",
            tool_name="get-item",
            mcp_identity_trusted=True,
        )
        base.update(overrides)
        return AcpEvent(**base)

    # The issue's shape: remote MCP tool_call streamed no rawInput — low
    # fidelity (args unverified) but identity verified.
    ev = _ev()
    assert ev.child_low_fidelity is True
    assert ev.child_mcp_identity_trusted is True
    # A parent event never needs the split.
    assert _ev(sub_session_id="").child_mcp_identity_trusted is False
    # Unresolved shell classification: is_shell=False is only the miss
    # default, so nothing proves this is not a shell tool.
    assert _ev(shell_classified=False).child_mcp_identity_trusted is False
    # A resolved SHELL tool: its deny gates need the command bytes this
    # event lacks — never identity-eligible.
    assert _ev(is_shell=True).child_mcp_identity_trusted is False
    # Cache-missed identity halves are each fail-closed.
    assert _ev(mcp_server_name="").child_mcp_identity_trusted is False
    assert _ev(tool_name="").child_mcp_identity_trusted is False
    # THE HARDENING: non-empty identity fields alone are NOT provenance. An
    # event populated by any path that did not earn the explicit flag (e.g. a
    # future inline/agent-authored fallback) stays untrusted.
    assert _ev(mcp_identity_trusted=False).child_mcp_identity_trusted is False
    # Full-fidelity child: the property may hold too, and grant callers use
    # ``child_unconditional_grant_eligible`` — both True is consistent, not
    # contradictory.
    full = _ev(raw_params_trusted=True, raw_tool_params={"itemId": "i-1"})
    assert full.child_low_fidelity is False
    assert full.child_mcp_identity_trusted is True


def test_mcp_identity_trusted_defaults_false():
    """The provenance flag is opt-in at trusted population sites only: a bare
    construction (the shape any future untrusted path would produce) reads
    False."""
    from kiro_crew.acp.types import AcpEvent

    assert AcpEvent(kind="permission_request").mcp_identity_trusted is False


def test_child_unconditional_grant_eligible_matches_consumer_shapes():
    """The hoisted grant-eligibility property is exactly
    ``not child_low_fidelity or child_mcp_identity_trusted`` — pinned across
    every fidelity/identity combination the three approval surfaces
    (dashboard runner, Slack gateway, subagent manager) can see."""
    from kiro_crew.acp.types import AcpEvent

    # Full-fidelity child (low_fidelity False): eligible regardless of identity.
    full = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        shell_classified=True,
        is_shell=False,
        raw_params_trusted=True,
        raw_tool_params={"k": "v"},
    )
    assert full.child_low_fidelity is False
    assert full.child_unconditional_grant_eligible is True
    # Low-fidelity child with verified identity: eligible.
    identity = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        shell_classified=True,
        is_shell=False,
        mcp_server_name="example-server",
        tool_name="get-item",
        mcp_identity_trusted=True,
    )
    assert identity.child_low_fidelity is True
    assert identity.child_unconditional_grant_eligible is True
    # Low-fidelity child, identity unverified: NOT eligible.
    blind = AcpEvent(kind="permission_request", sub_session_id="child-a")
    assert blind.child_low_fidelity is True
    assert blind.child_unconditional_grant_eligible is False
    # Non-empty identity WITHOUT the provenance flag: still NOT eligible (the
    # hardening the flag buys, seen from the grant surface).
    forged = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        shell_classified=True,
        is_shell=False,
        mcp_server_name="example-server",
        tool_name="get-item",
    )
    assert forged.child_low_fidelity is True
    assert forged.child_unconditional_grant_eligible is False
    # Non-child events are always eligible.
    assert AcpEvent(kind="permission_request").child_unconditional_grant_eligible is True
    # Exhaustive equivalence against the un-hoisted expression.
    for lf_overrides in (
        {},  # low fidelity (no trusted params)
        {"raw_params_trusted": True, "raw_tool_params": {"k": "v"}},  # full fidelity
    ):
        for id_overrides in (
            {},
            {
                "mcp_server_name": "example-server",
                "tool_name": "get-item",
                "mcp_identity_trusted": True,
            },
        ):
            ev = AcpEvent(
                kind="permission_request",
                sub_session_id="child-a",
                shell_classified=True,
                is_shell=False,
                **lf_overrides,
                **id_overrides,
            )
            assert ev.child_unconditional_grant_eligible == (
                not ev.child_low_fidelity or ev.child_mcp_identity_trusted
            )


def test_remote_mcp_empty_rawinput_keeps_identity_through_dispatch():
    """End-to-end through the real _dispatch functions: a remote MCP server's
    tool_call frame with EMPTY/absent rawInput leaves the params cache empty
    (low fidelity) while the _meta.kiro identity still reaches the permission
    event's trusted fields — the split the grant paths rely on."""
    from kiro_crew.acp._dispatch import build_permission_event, parse_session_update

    for raw_input_shape in ({}, None):
        caches: dict = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
        }
        child_sid, tcid = "child-a", "tc-1"
        update = {
            "sessionUpdate": "tool_call",
            "toolCallId": tcid,
            "title": "@example-server/get-item",
            "kind": "other",
            "_meta": {"kiro": {"mcpServerName": "example-server", "toolName": "get-item"}},
        }
        if raw_input_shape is not None:
            update["rawInput"] = raw_input_shape
        parse_session_update(update, cache_scope=child_sid, **caches)

        class _Msg:
            id = 90
            method = "session/request_permission"
            params = {
                "sessionId": child_sid,
                "toolCall": {
                    "toolCallId": tcid,
                    "title": "@example-server/get-item",
                    "input": {"itemId": "item-0001"},
                },
                "options": [
                    {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                ],
            }

        event, _ = build_permission_event(_Msg(), cache_scope=child_sid, **caches)
        event.sub_session_id = child_sid
        assert event.raw_params_trusted is False
        assert event.child_low_fidelity is True
        assert event.mcp_server_name == "example-server"
        assert event.tool_name == "get-item"
        # The real permission builder earns the provenance flag: the identity
        # pair resolved from the origin-scoped caches, never inline.
        assert event.mcp_identity_trusted is True
        assert event.child_mcp_identity_trusted is True
        assert event.child_unconditional_grant_eligible is True


def test_permission_event_cache_miss_does_not_earn_identity_flag():
    """The provenance flag is HIT-derived, never availability-derived: a
    permission frame whose toolCallId has NO cache entry (wired caches, no
    preceding tool_call) must read mcp_identity_trusted False — the flag
    reports where the values CAME FROM, so a future inline fallback that
    populates the identity fields on a miss stays untrusted."""
    from kiro_crew.acp._dispatch import build_permission_event

    class _Msg:
        id = 91
        method = "session/request_permission"
        params = {
            "sessionId": "child-a",
            "toolCall": {
                "toolCallId": "tc-never-seen",
                "title": "@example-server/get-item",
                "input": {"itemId": "item-0001"},
            },
            "options": [{"optionId": "allow_once", "name": "Allow", "kind": "allow_once"}],
        }

    event, _ = build_permission_event(
        _Msg(),
        tool_input_cache={},
        shell_cache={},
        raw_params_cache={},
        mcp_server_name_cache={},
        tool_name_cache={},
        cache_scope="child-a",
    )
    assert event.mcp_server_name == ""
    assert event.tool_name == ""
    assert event.mcp_identity_trusted is False
    # Asymmetric hit: BOTH reads must hit — a lone server-name entry (a
    # partial/older writer) earns nothing.
    event2, _ = build_permission_event(
        _Msg(),
        tool_input_cache={},
        shell_cache={},
        raw_params_cache={},
        mcp_server_name_cache={"child-a|tc-never-seen": "example-server"},
        tool_name_cache={},
        cache_scope="child-a",
    )
    assert event2.mcp_server_name == "example-server"
    assert event2.tool_name == ""
    assert event2.mcp_identity_trusted is False
    event3, _ = build_permission_event(
        _Msg(),
        tool_input_cache={},
        shell_cache={},
        raw_params_cache={},
        mcp_server_name_cache={},
        tool_name_cache={"child-a|tc-never-seen": "get-item"},
        cache_scope="child-a",
    )
    assert event3.mcp_server_name == ""
    assert event3.tool_name == "get-item"
    assert event3.mcp_identity_trusted is False
    # The None-vs-"" distinction is the load-bearing half: a written entry may
    # legitimately be "" (host shell/builtin tool_call caches "" for both), and
    # that HIT still earns the flag — deriving trust from value non-emptiness
    # instead of the hit is exactly the conflation the flag exists to remove.
    event4, _ = build_permission_event(
        _Msg(),
        tool_input_cache={},
        shell_cache={},
        raw_params_cache={},
        mcp_server_name_cache={"child-a|tc-never-seen": ""},
        tool_name_cache={"child-a|tc-never-seen": ""},
        cache_scope="child-a",
    )
    assert event4.mcp_server_name == ""
    assert event4.tool_name == ""
    assert event4.mcp_identity_trusted is True


@pytest.mark.asyncio
async def test_between_turns_child_permission_is_answered_not_queued():
    """With no in-flight prompt nothing consumes the slot queue until the next
    turn's drain — a queued request would strand the backend. It is answered
    fail-closed immediately instead."""
    rt, reader, proc = _make_runtime()
    queues = _register(rt, "parent-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 96,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "child-a",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        await _drain(reader)
        await _drain_audits(rt)

        frame = _last_written_frame(proc)
        assert frame["id"] == 96
        assert frame["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
        # Nothing left queued for a consumer that may not exist for hours.
        while not queues["parent-session"].empty():
            assert queues["parent-session"].get_nowait().id != 96
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_pending_non_prompt_request_does_not_enable_routing():
    """A pending set_mode/steer/config request leaves an entry in
    _routed_requests but proves nothing about a consuming prompt loop — a
    child permission arriving then must be answered fail-closed, not parked
    on a queue nobody reads."""
    rt, reader, proc = _make_runtime()
    _register(rt, "parent-session")
    rt._routed_requests[5] = "parent-session"  # e.g. an unanswered set_mode
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 97,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "child-a",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        await _drain(reader)
        await _drain_audits(rt)

        frame = _last_written_frame(proc)
        assert frame["id"] == 97
        assert frame["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_notice_yield_abandonment_clears_turn_state():
    """The drain-time rejection notices are yields, i.e. abandonment points.
    A consumer that closes the stream at a notice yield must not leave the
    handle permanently turn-active (mark_turn_active leaked True /
    _turn_done cleared) — the notice loop must live inside the same
    try/finally as the dispatch loop."""
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY

    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=7)
    handle._pending_reject_notices.append(("child-a", "Running: sha256sum x"))

    gen = handle.prompt("hi", timeout=3.0)
    first = await gen.__anext__()
    assert first.kind == EVENT_SUBAGENT_ACTIVITY
    assert "auto-rejected" in (first.text or "")
    # Consumer abandons the stream at the notice yield.
    await gen.aclose()

    assert handle.is_turn_active is False
    assert "sA" not in rt._turn_active_sessions


@pytest.mark.asyncio
async def test_handle_owned_rejections_are_sel_audited():
    """Every permission decision leaves a SEL record (repo convention). The
    fail-close fidelity gate and the pre-turn drain answer requests that no
    consumer ever sees, so the handle must emit the denial audit itself —
    otherwise those rejections are invisible to SEL."""
    import contextlib

    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)

    audited: list[tuple[object, str, str]] = []
    handle._audit_handle_reject = (  # type: ignore[method-assign]
        lambda request_id, title, error, sub_session_id="": audited.append(
            (request_id, title, error)
        )
    )
    rt.send_response = AsyncMock()

    # Pre-turn drain path: a stranded permission request in the queue.
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "jsonrpc": "2.0",
                "id": 55,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "child-a",
                    "toolCall": {"toolCallId": "tc-9", "title": "Running: rm -rf x"},
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            }
        )
    )
    rt.send_request = AsyncMock(return_value=9)
    gen = handle.prompt("hi", timeout=0.2)
    with contextlib.suppress(StopAsyncIteration, asyncio.TimeoutError, Exception):
        await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    await gen.aclose()

    assert audited, "pre-turn drain reject was not SEL-audited"
    assert audited[0][2] == "stranded_request_pre_turn_drain"


def test_missing_kind_is_not_a_resolved_shell_classification():
    """A tool_call whose `kind` never arrived must NOT cache a shell
    classification: the miss-default False would otherwise read as a RESOLVED
    non-shell on the later permission frame (shell_classified=True), skipping
    the low-fidelity downgrade without any classification having happened."""
    from kiro_crew.acp._dispatch import _build_tool_call_event, build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    shell_cache: dict[str, bool] = {}
    raw_cache: dict[str, dict] = {}
    # No `kind` key at all — classification unresolved.
    _build_tool_call_event(
        {"title": "Doing something", "toolCallId": "tc-nk", "rawInput": {"path": "/tmp/x"}},
        None,
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
    )
    assert "tc-nk" not in shell_cache  # unresolved, NOT cached False

    msg = JsonRpcMessage.from_dict(
        {
            "id": 7,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "sessionId": "child-a",
                "toolCall": {"toolCallId": "tc-nk", "title": "Doing something"},
                "options": [],
            },
        }
    )
    event, _ = build_permission_event(msg, shell_cache=shell_cache, raw_params_cache=raw_cache)
    event.sub_session_id = "child-a"
    assert event.shell_classified is False
    assert event.child_low_fidelity is True  # downgrade applies

    # An explicit kind DOES resolve (even a non-shell one).
    _build_tool_call_event(
        {"title": "Reading", "kind": "read", "toolCallId": "tc-rk"},
        None,
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
    )
    assert shell_cache.get("tc-rk") is False  # resolved non-shell


def test_shared_permission_event_carries_redaction_provenance_without_secret():
    """The shared-runtime cache must remember that its display input changed.

    Re-redacting the already-clean permission event cannot recover this fact;
    command trust needs the separate boolean while the removed bytes stay out
    of the event's display input.
    """
    from kiro_crew.acp._dispatch import _build_tool_call_event, build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    input_cache: dict[str, str] = {}
    redacted_cache: dict[str, bool] = {}
    shell_cache: dict[str, bool] = {}
    _build_tool_call_event(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-secret",
            "title": "Run Command",
            "kind": "execute",
            "rawInput": {"command": "echo AKIAIOSFODNN7EXAMPLE"},
        },
        input_cache,
        shell_cache=shell_cache,
        tool_input_redacted_cache=redacted_cache,
    )
    assert redacted_cache["tc-secret"] is True

    msg = JsonRpcMessage.from_dict(
        {
            "id": 71,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "toolCall": {"toolCallId": "tc-secret", "title": "Run Command"},
                "options": [],
            },
        }
    )
    event, _ = build_permission_event(
        msg,
        tool_input_cache=input_cache,
        tool_input_redacted_cache=redacted_cache,
        shell_cache=shell_cache,
    )

    assert event.tool_input_redacted is True
    assert "AKIAIOSFODNN7EXAMPLE" not in event.tool_input
    assert "[REDACTED: credential]" in event.tool_input
    assert redacted_cache["tc-secret"] is True
    repeated, _ = build_permission_event(
        msg,
        tool_input_cache=input_cache,
        tool_input_redacted_cache=redacted_cache,
        shell_cache=shell_cache,
    )
    assert repeated.tool_input_redacted is True
    assert repeated.tool_input == event.tool_input


def test_refinement_fills_raw_params_cache_for_following_permission():
    """A tool_call_update refinement carrying rawInput must make the FOLLOWING
    permission frame full-provenance (raw_params_trusted=True) — this is the
    path that keeps backends streaming an empty initial rawInput out of the
    low-fidelity downgrade. Pins the required frame ordering explicitly."""
    from kiro_crew.acp._dispatch import build_permission_event, parse_session_update
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    shell_cache: dict[str, bool] = {}
    raw_cache: dict[str, dict] = {}
    input_cache: dict[str, str] = {}
    # Initial tool_call with EMPTY rawInput but a real kind.
    parse_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-rf",
            "title": "Running: sha256sum x",
            "kind": "execute",
        },
        tool_input_cache=input_cache,
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
    )
    assert "tc-rf" not in raw_cache
    # Refinement supplies the complete params.
    parse_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-rf",
            "rawInput": {"command": "sha256sum x"},
        },
        tool_input_cache=input_cache,
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
    )
    assert raw_cache.get("tc-rf") == {"command": "sha256sum x"}

    msg = JsonRpcMessage.from_dict(
        {
            "id": 8,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "sessionId": "child-a",
                "toolCall": {"toolCallId": "tc-rf", "title": "Running: sha256sum x"},
                "options": [],
            },
        }
    )
    event, _ = build_permission_event(msg, shell_cache=shell_cache, raw_params_cache=raw_cache)
    event.sub_session_id = "child-a"
    assert event.raw_params_trusted is True
    assert event.shell_classified is True
    assert event.child_low_fidelity is False


@pytest.mark.asyncio
async def test_fidelity_unaware_consumer_gate_rejects_and_audits():
    """The fail-close choke point protecting every non-dashboard consumer:
    a low-fidelity child permission request reaching _dispatch_events on a
    handle whose consumer never opted in must be REJECTED (answered, never
    yielded as a permission event) and SEL-audited."""
    from kiro_crew.acp.types import (
        EVENT_PERMISSION_REQUEST,
        METHOD_REQUEST_PERMISSION,
    )

    rt, reader, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    assert handle.child_fidelity_aware is False

    audited: list[tuple[object, str, str]] = []
    handle._audit_handle_reject = (  # type: ignore[method-assign]
        lambda request_id, title, error, sub_session_id="": audited.append(
            (request_id, title, error)
        )
    )

    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        # Announce the child so the frame routes to the owner's queue.
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        # Child permission frame with NO preceding tool_call → low fidelity.
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 77,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "sessionId": "child-a",
                    "toolCall": {"toolCallId": "tc-77", "title": "Running: rm -rf /"},
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        _feed(reader, {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
        await asyncio.wait_for(driver, timeout=5.0)

        kinds = [ev.kind for ev in events]
        assert EVENT_PERMISSION_REQUEST not in kinds  # never yielded
        assert audited and audited[0][2] == "child_low_fidelity_unaware_consumer"
        # The reject was actually SENT (answered, not dropped).
        answer = None
        for call in proc.stdin.write.call_args_list:
            frame = json.loads(call.args[0].decode())
            if frame.get("id") == 77 and "result" in frame:
                answer = frame
        assert answer is not None
        assert answer["result"]["outcome"]["outcome"] in ("selected", "cancelled")
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_answer_task_cap_marks_dead_instead_of_growing_unbounded():
    """A backend that floods permission frames while never reading stdin
    blocks each answer task on drain(). The in-flight set must be BOUNDED,
    and the overflow must be neither a hang nor an unaudited drop: past the
    cap the runtime is marked dead (teardown resolves EVERY pending wait)
    and the denial is SEL-audited."""
    rt, _, _ = _make_runtime()
    rt._max_answer_tasks = 3

    import asyncio as _asyncio

    _never = _asyncio.Event()

    async def _blocked_answer(msg, session_id, *, reason="x"):
        await _never.wait()  # simulates send_response stuck on drain()

    rt._answer_unroutable_permission = _blocked_answer  # type: ignore[method-assign]
    audited: list[str] = []
    rt._audit_denied_off_loop = (  # type: ignore[method-assign]
        lambda msg, session_id, reason, title=None: audited.append(reason)
    )
    dead: list[str] = []

    def _fake_mark_dead(reason):
        dead.append(reason)
        rt._dead = True  # mirror the real _mark_dead contract

    rt._mark_dead = _fake_mark_dead  # type: ignore[method-assign]

    def _frame(i):
        return JsonRpcMessage.from_dict(
            {
                "jsonrpc": "2.0",
                "id": 500 + i,
                "method": "session/request_permission",
                "params": {"sessionId": f"child-{i}", "options": []},
            }
        )

    rt._answer_cap_wait_secs = 0.05
    for i in range(5):
        await rt._spawn_answer_task(_frame(i), f"child-{i}")
    await _asyncio.sleep(0)  # let the tasks start (and block)

    assert len(rt._answer_tasks) == 3  # capped, not 5
    # Overflow was audited and escalated to mark-dead — not silently dropped.
    # Only the FIRST overflow frame audits + marks dead; once dead, further
    # frames are gated out entirely (no audit-task growth on a dead runtime).
    assert audited == ["answer_task_cap_runtime_dead"]
    assert dead and "cap" in dead[0]
    # A frame arriving after death spawns NOTHING.
    await rt._spawn_answer_task(_frame(9), "child-9")
    await _asyncio.sleep(0)
    assert len(rt._answer_tasks) == 3
    assert audited == ["answer_task_cap_runtime_dead"]
    _never.set()
    await _asyncio.sleep(0)


@pytest.mark.asyncio
async def test_capacity_freed_but_runtime_died_still_audits_the_refusal():
    """A waiter parked at the cap can be woken by a completing answer AND find
    the runtime condemned by a concurrent waiter in the same moment. Capacity
    was freed, so this is not the timeout path, but admission still fails — and
    a refused permission decision must leave a SEL record either way."""
    rt, _, _ = _make_runtime()
    rt._max_answer_tasks = 1

    import asyncio as _asyncio

    audited: list[str] = []
    rt._audit_denied_off_loop = (  # type: ignore[method-assign]
        lambda msg, session_id, reason, title=None: audited.append(reason)
    )

    release = _asyncio.Event()

    async def _held() -> None:
        await release.wait()

    holder = _asyncio.ensure_future(_held())
    rt._answer_tasks.add(holder)

    frame = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "id": 907,
            "method": "session/request_permission",
            "params": {"sessionId": "child-x", "options": []},
        }
    )

    async def _condemn_then_release() -> None:
        await _asyncio.sleep(0)
        rt._dead = True  # a sibling waiter's _mark_dead lands first
        release.set()

    condemner = _asyncio.ensure_future(_condemn_then_release())
    admitted = await rt._wait_for_answer_capacity(
        frame,
        request_kind="permission",
        session_id="child-x",
        audit_reason="answer_task_cap_runtime_dead",
    )
    await condemner

    assert admitted is False
    assert audited == ["answer_task_cap_runtime_dead"], "the refusal must be audited"


@pytest.mark.asyncio
async def test_sel_audit_tasks_do_not_count_toward_answer_cap():
    """SEL audit tasks are short-lived thread offloads; a burst of them must
    never satisfy the flood cap and trip a false mark_dead that kills every
    multiplexed session."""
    rt, _, _ = _make_runtime()
    rt._max_answer_tasks = 2

    import asyncio as _asyncio

    # Simulate pending SEL audits filling the AUDIT set well past the cap.
    for _ in range(5):
        _t = _asyncio.ensure_future(_asyncio.sleep(30))
        rt._audit_tasks.add(_t)
        _t.add_done_callback(rt._audit_tasks.discard)

    dead: list[str] = []
    rt._mark_dead = lambda reason: dead.append(reason)  # type: ignore[method-assign]

    answered: list[object] = []

    async def _quick_answer(msg, session_id, *, reason="x"):
        answered.append(msg.id)

    rt._answer_unroutable_permission = _quick_answer  # type: ignore[method-assign]

    await rt._spawn_answer_task(
        JsonRpcMessage.from_dict(
            {
                "jsonrpc": "2.0",
                "id": 700,
                "method": "session/request_permission",
                "params": {"sessionId": "child-x", "options": []},
            }
        ),
        "child-x",
    )
    await _asyncio.sleep(0)

    assert answered == [700]  # answered normally
    assert dead == []  # audit backlog did NOT trip the cap
    for _t in list(rt._audit_tasks):
        _t.cancel()
    await _asyncio.sleep(0)


@pytest.mark.asyncio
async def test_buffered_burst_with_responsive_backend_does_not_trip_cap():
    """129+ frames can be buffered so readline() never suspends; the reader
    must still yield between spawns so QUICK answers drain and a responsive
    backend is not falsely marked dead by the flood cap. (The cap fires only
    when answers genuinely cannot complete — a wedged pipe.)"""
    rt, reader, proc = _make_runtime()
    _register(rt, "sA")
    rt._max_answer_tasks = 4
    dead: list[str] = []
    rt._mark_dead = lambda reason: dead.append(reason)  # type: ignore[method-assign]

    # Buffer MORE frames than the cap before the reader runs at all.
    for i in range(10):
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 800 + i,
                "method": "session/request_permission",
                "params": {
                    "sessionId": f"child-{i}",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
    task = await _start_reader(rt)
    try:
        await _drain(reader)
        await _drain_audits(rt)
        # Responsive backend (writes complete immediately): every request
        # answered, cap never tripped.
        assert dead == []
        answered = {
            json.loads(c.args[0].decode()).get("id")
            for c in proc.stdin.write.call_args_list
            if "result" in json.loads(c.args[0].decode())
        }
        assert {800 + i for i in range(10)} <= answered
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


def test_cross_session_toolcallid_replay_does_not_inherit_provenance():
    """A child session reusing a PARENT's toolCallId must NOT inherit the
    parent's trusted provenance (raw params / shell class / MCP identity) —
    cache keys are origin-scoped, so cross-session replay misses and the
    request stays low fidelity, while a SAME-origin repeat frame still
    resolves."""
    from kiro_crew.acp._dispatch import build_permission_event, parse_session_update
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    shell_cache: dict[str, bool] = {}
    raw_cache: dict[str, dict] = {}
    input_cache: dict[str, str] = {}

    # Parent's tool_call writes trusted provenance under the PARENT scope.
    parse_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-shared",
            "title": "Reading README.md",
            "kind": "read",
            "rawInput": {"path": "README.md"},
        },
        tool_input_cache=input_cache,
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
        cache_scope="parent-session",
    )

    def _perm(req_id):
        return JsonRpcMessage.from_dict(
            {
                "id": req_id,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "toolCall": {"toolCallId": "tc-shared", "title": "Reading README.md"},
                    "options": [],
                },
            }
        )

    # CHILD replays the same toolCallId under its own scope: provenance MISS.
    child_ev, _ = build_permission_event(
        _perm(1),
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
        cache_scope="child-session",
    )
    child_ev.sub_session_id = "child-session"
    assert child_ev.raw_params_trusted is False
    assert child_ev.shell_classified is False
    assert child_ev.child_low_fidelity is True  # downgrade applies

    # SAME-origin permission frame (and a repeat of it) still resolves.
    for req_id in (2, 3):
        parent_ev, _ = build_permission_event(
            _perm(req_id),
            shell_cache=shell_cache,
            raw_params_cache=raw_cache,
            cache_scope="parent-session",
        )
        assert parent_ev.raw_params_trusted is True
        assert parent_ev.shell_classified is True


@pytest.mark.asyncio
async def test_cancel_during_drain_reject_does_not_wedge_handle():
    """_turn_done is cleared before the pre-turn drain; a cancellation while
    the drain awaits reject_tool() must restore it (the BaseException guard
    wraps only the later send_request) — otherwise the handle reports
    turn-active forever and every subsequent prompt() is rejected."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)

    import asyncio as _asyncio

    async def _cancelled_reject(_rid):
        raise _asyncio.CancelledError()

    handle.reject_tool = _cancelled_reject  # type: ignore[method-assign]
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "jsonrpc": "2.0",
                "id": 60,
                "method": "session/request_permission",
                "params": {"sessionId": "child-a", "options": []},
            }
        )
    )
    gen = handle.prompt("hi", timeout=1.0)
    with pytest.raises(_asyncio.CancelledError):
        await gen.__anext__()
    assert handle.is_turn_active is False  # not wedged
    # The stranded request went back on the queue for the next drain.
    assert not q["sA"].empty()


# ── store_session_config: resolved-model capture (issue #5869) ──


def test_store_session_config_adopts_sole_advertised_model_when_no_current_id():
    """An unpinned session whose ``session/new`` advertises exactly one model
    but omits ``currentModelId`` must still resolve that model, so ``served_model``
    is non-empty for the whole run (the panel model chip depends on it)."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config(
        {"models": {"availableModels": [{"modelId": "kiro-model-x", "name": "X"}]}}
    )
    assert handle._resolved_model_id == "kiro-model-x"
    assert handle.served_model == "kiro-model-x"


def test_store_session_config_current_model_id_wins_over_sole_advertised():
    """When the backend DOES echo ``currentModelId`` it is authoritative — the
    sole-advertised fallback must not override it."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config(
        {
            "models": {
                "currentModelId": "kiro-current",
                "availableModels": [{"modelId": "kiro-other", "name": "Other"}],
            }
        }
    )
    assert handle._resolved_model_id == "kiro-current"


def test_store_session_config_leaves_model_empty_when_ambiguous():
    """Two or more advertised models and no ``currentModelId`` is genuinely
    ambiguous — do not guess; ``served_model`` stays empty."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config(
        {
            "models": {
                "availableModels": [
                    {"modelId": "kiro-a", "name": "A"},
                    {"modelId": "kiro-b", "name": "B"},
                ]
            }
        }
    )
    assert handle._resolved_model_id == ""
    assert handle.served_model == ""


# ── probe_advertised_models (entitlement revalidation) ──


_PROBE_RESP = {
    "sessionId": "probe-1",
    "models": {
        "currentModelId": "claude-opus-5",
        "availableModels": [
            {"modelId": "auto", "name": "auto"},
            {"modelId": "claude-opus-5", "name": "claude-opus-5"},
        ],
    },
}


@pytest.mark.asyncio
async def test_probe_returns_fresh_set_and_terminates_probe_session():
    """The probe re-asks entitlement with a throwaway minimal session/new
    (mcpServers present-but-empty — kiro-cli treats a missing field as
    malformed), reads the advertised set, and evicts the probe session so it
    never accumulates in the shared process."""
    rt, _, _ = _make_runtime()
    rt._send_and_await = AsyncMock(side_effect=[_PROBE_RESP, {}])  # type: ignore[method-assign]
    fresh = await rt.probe_advertised_models()
    assert [m["modelId"] for m in fresh] == ["auto", "claude-opus-5"]
    calls = rt._send_and_await.call_args_list
    assert calls[0].args[0] == METHOD_SESSION_NEW
    assert calls[0].args[1]["mcpServers"] == []
    assert calls[1].args[0] == METHOD_SESSION_TERMINATE
    assert calls[1].args[1] == {"sessionId": "probe-1"}
    # Init scope closed — staged init notifications cannot leak into a later
    # real session.
    assert rt._session_inits_in_flight == 0


@pytest.mark.asyncio
async def test_probe_failure_returns_empty_and_closes_init_scope():
    """A failed probe is not evidence: it returns [] (caller keeps its prior
    snapshot) and must not leave the init-notification scope open."""
    rt, _, _ = _make_runtime()
    rt._send_and_await = AsyncMock(  # type: ignore[method-assign]
        side_effect=AcpRuntimeError("boom")
    )
    assert await rt.probe_advertised_models() == []
    assert rt._session_inits_in_flight == 0


@pytest.mark.asyncio
async def test_probe_advertising_nothing_returns_empty_but_still_terminates():
    """A session/new that omits models yields [] — and the probe session is
    still evicted, and the empty answer is NOT cached (the next call probes
    again rather than repeating a non-answer)."""
    rt, _, _ = _make_runtime()
    rt._send_and_await = AsyncMock(  # type: ignore[method-assign]
        side_effect=[{"sessionId": "probe-2"}, {}, {"sessionId": "probe-3"}, {}]
    )
    assert await rt.probe_advertised_models() == []
    assert rt._send_and_await.call_args_list[1].args[0] == METHOD_SESSION_TERMINATE
    assert await rt.probe_advertised_models() == []
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 2


@pytest.mark.asyncio
async def test_probe_result_reused_within_ttl():
    """A fresh non-empty answer is served from cache inside the TTL, so a burst
    of rejections costs one round-trip."""
    rt, _, _ = _make_runtime()
    rt._send_and_await = AsyncMock(side_effect=[_PROBE_RESP, {}])  # type: ignore[method-assign]
    first = await rt.probe_advertised_models()
    second = await rt.probe_advertised_models()
    assert second == first
    # One session/new + one terminate total: the second call never hit the wire.
    assert rt._send_and_await.await_count == 2


@pytest.mark.asyncio
async def test_probe_single_flight_concurrent_callers_share_one_probe():
    rt, _, _ = _make_runtime()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            started.set()
            await release.wait()
            return dict(_PROBE_RESP)
        return {}

    rt._send_and_await = AsyncMock(side_effect=slow_send)  # type: ignore[method-assign]
    t1 = asyncio.ensure_future(rt.probe_advertised_models())
    t2 = asyncio.ensure_future(rt.probe_advertised_models())
    await started.wait()
    release.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1 == r2 != []
    news = [c for c in rt._send_and_await.call_args_list if c.args[0] == METHOD_SESSION_NEW]
    assert len(news) == 1


@pytest.mark.asyncio
async def test_probe_on_dead_or_uninitialized_runtime_returns_empty():
    rt, _, _ = _make_runtime()
    rt._dead = True
    assert await rt.probe_advertised_models() == []
    rt2, _, _ = _make_runtime()
    rt2._initialized = False
    assert await rt2.probe_advertised_models() == []


# ── AcpSessionHandle.refresh_available_models ──


@pytest.mark.asyncio
async def test_refresh_replaces_snapshot_on_nonempty_probe():
    """One refresh heals every consumer of the handle's snapshot: the fresh
    probe answer replaces the session-init availableModels in place."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config(
        {
            "models": {
                "availableModels": [
                    {"modelId": "claude-sonnet-4"},
                    {"modelId": "claude-sonnet-4.5"},
                ]
            }
        }
    )
    fresh_set = [
        {"modelId": "auto", "name": "auto", "description": ""},
        {"modelId": "claude-opus-5", "name": "claude-opus-5", "description": ""},
    ]
    rt.probe_advertised_models = AsyncMock(return_value=fresh_set)  # type: ignore[method-assign]
    fresh = await handle.refresh_available_models()
    assert fresh == fresh_set
    assert [m["modelId"] for m in handle.available_models] == [
        "auto",
        "claude-opus-5",
    ]


@pytest.mark.asyncio
async def test_refresh_keeps_snapshot_on_empty_probe():
    """A failed/empty probe is not evidence — the prior snapshot survives so a
    flaky probe can never WIDEN or clear entitlement."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config({"models": {"availableModels": [{"modelId": "claude-sonnet-4"}]}})
    rt.probe_advertised_models = AsyncMock(return_value=[])  # type: ignore[method-assign]
    assert await handle.refresh_available_models() == []
    assert [m["modelId"] for m in handle.available_models] == ["claude-sonnet-4"]


class TestParseAdvertisedModels:
    """Both response shapes normalize identically, so a probe answer and a
    session-init snapshot are directly comparable."""

    def test_models_object_shape(self):
        from kiro_crew.acp.session_handle import parse_advertised_models

        out = parse_advertised_models(_PROBE_RESP)
        assert [m["modelId"] for m in out] == ["auto", "claude-opus-5"]
        assert all(set(m) == {"modelId", "name", "description"} for m in out)

    def test_bare_list_shape(self):
        from kiro_crew.acp.session_handle import parse_advertised_models

        out = parse_advertised_models({"availableModels": [{"modelId": "claude-sonnet-4"}]})
        assert [m["modelId"] for m in out] == ["claude-sonnet-4"]

    def test_absent_or_malformed_yields_empty(self):
        from kiro_crew.acp.session_handle import parse_advertised_models

        assert parse_advertised_models({}) == []
        assert parse_advertised_models({"models": {"availableModels": "nope"}}) == []
        assert parse_advertised_models({"models": 7}) == []


class TestStoreSessionConfigParseConsolidation:
    """Drift-pin (#6382): ``store_session_config`` sources its model list from
    ``parse_advertised_models``, so the session-init snapshot can never drift
    from what a pooled-runtime probe would parse out of the same payload."""

    def _handle(self):
        rt, _, _ = _make_runtime()
        q = _register(rt, "sA")
        return AcpSessionHandle("sA", q["sA"], rt)

    def test_models_object_shape_matches_canonical_parser(self):
        # Mixed modelId/value spellings + a missing description exercise every
        # normalization branch; hard-coded expectation so a regression inside
        # parse_advertised_models fails this pin too.
        resp = {
            "models": {
                "currentModelId": "kiro-model-x",
                "availableModels": [
                    {"modelId": "kiro-model-x", "name": "X", "description": "d"},
                    {"value": "kiro-model-y"},
                    {"name": "no id — skipped"},
                    "not-a-dict",
                ],
            }
        }
        handle = self._handle()
        handle.store_session_config(resp)
        assert handle.available_models == [
            {"modelId": "kiro-model-x", "name": "X", "description": "d"},
            {"modelId": "kiro-model-y", "name": "kiro-model-y", "description": ""},
        ]

    def test_bare_list_shape_matches_canonical_parser(self):
        resp = {"availableModels": [{"modelId": "kiro-model-x"}, {"value": "kiro-model-y"}]}
        handle = self._handle()
        handle.store_session_config(resp)
        assert handle.available_models == [
            {"modelId": "kiro-model-x", "name": "kiro-model-x", "description": ""},
            {"modelId": "kiro-model-y", "name": "kiro-model-y", "description": ""},
        ]

    def test_bare_list_under_models_key_matches_canonical_parser(self):
        """The bare-list branch is the only site that RE-KEYS the payload
        (``models`` list → ``{"availableModels": models}`` envelope) — reach
        it via the ``models`` key so the re-key itself is pinned."""
        handle = self._handle()
        handle.store_session_config(
            {"models": [{"modelId": "kiro-model-x"}, {"value": "kiro-model-y"}]}
        )
        assert handle.available_models == [
            {"modelId": "kiro-model-x", "name": "kiro-model-x", "description": ""},
            {"modelId": "kiro-model-y", "name": "kiro-model-y", "description": ""},
        ]

    def test_dict_branch_delegates_to_canonical_parser(self, monkeypatch):
        """Anti-re-fork pin (#6382): the dict branch must SOURCE its list from
        ``parse_advertised_models`` AND call it with the checked-binding
        envelope — a restored inline walk, a whole-response re-resolution, or
        a wrong envelope all fail this pin."""
        import kiro_crew.acp.session_handle as sh

        sentinel = [
            {"modelId": "sentinel-a", "name": "A", "description": ""},
            {"modelId": "sentinel-b", "name": "B", "description": ""},
        ]
        calls: list = []

        def _fake(resp):
            calls.append(resp)
            return list(sentinel)

        monkeypatch.setattr(sh, "parse_advertised_models", _fake)
        handle = self._handle()
        handle.store_session_config({"models": {"availableModels": [{"modelId": "real"}]}})
        assert handle.available_models == sentinel
        assert calls == [{"models": {"availableModels": [{"modelId": "real"}]}}]

    def test_bare_list_branch_delegates_to_canonical_parser(self, monkeypatch):
        import kiro_crew.acp.session_handle as sh

        sentinel = [
            {"modelId": "sentinel-a", "name": "A", "description": ""},
            {"modelId": "sentinel-b", "name": "B", "description": ""},
        ]
        calls: list = []

        def _fake(resp):
            calls.append(resp)
            return list(sentinel)

        monkeypatch.setattr(sh, "parse_advertised_models", _fake)
        handle = self._handle()
        handle.store_session_config({"availableModels": [{"modelId": "real"}]})
        assert handle.available_models == sentinel
        assert calls == [{"availableModels": [{"modelId": "real"}]}]

    def test_well_formed_empty_list_still_overwrites_prior_snapshot(self):
        """Pre-existing call-site policy pinned through the consolidation: a
        WELL-FORMED empty ``availableModels`` list DOES clear a prior snapshot
        here (unlike ``AcpClient._capture_available_models``'s non-empty
        guard). Switching this site to a client-style guard would be a
        behavior change this test exists to catch."""
        handle = self._handle()
        handle.store_session_config({"models": {"availableModels": [{"modelId": "kiro-model-x"}]}})
        assert [m["modelId"] for m in handle.available_models] == ["kiro-model-x"]
        handle.store_session_config({"models": {"availableModels": []}})
        assert handle.available_models == []

    def test_malformed_inner_shape_does_not_clobber_prior_snapshot(self):
        """The assignment guard survives the consolidation: a later response
        whose ``availableModels`` is malformed must not clear an
        already-captured list (the canonical parser returns ``[]`` for it, but
        assignment policy is the call site's, not the parser's)."""
        handle = self._handle()
        handle.store_session_config({"models": {"availableModels": [{"modelId": "kiro-model-x"}]}})
        assert [m["modelId"] for m in handle.available_models] == ["kiro-model-x"]
        handle.store_session_config({"models": {"availableModels": "nope"}})
        assert [m["modelId"] for m in handle.available_models] == ["kiro-model-x"]
