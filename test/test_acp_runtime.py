"""Unit tests for the AcpRuntime single-reader demux (Phase 1 multiplexing).

These exercise the routing logic that lets ONE kiro-cli acp process host
multiple concurrent sessions: the single _reader_loop owns stdout and routes
each frame to the right destination —

  - JSON-RPC response whose id is in _pending_requests  → resolve that Future
  - JSON-RPC response whose id is in _routed_requests   → that session's queue
  - notification carrying params.sessionId              → that session's queue
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
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from spawn_test_helpers import strip_spawn_shim

from kiro_crew.acp.client import _OVERSIZE_DRAIN_MAX_BYTES
from kiro_crew.acp.runtime import (
    _TERMINATE_TIMEOUT,
    AcpRuntime,
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpSessionHandle,
)
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    METHOD_COMMANDS_EXECUTE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_TERMINATE,
    METHOD_SESSION_UPDATE,
    METHOD_SET_CONFIG_OPTION,
    METHOD_SET_MODE,
    JsonRpcMessage,
)


# ── Harness ──
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


async def _stop_reader(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _await_routed(
    rt: AcpRuntime, *session_ids: str, timeout: float = 5.0
) -> dict[str, int]:
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

    async def resolve_installed():
        return launch_path

    monkeypatch.setattr(
        runtime_mod,
        "_resolve_kiro_bin_for_spawn",
        resolve_installed,
    )
    monkeypatch.setattr(runtime_mod, "wrap_argv", capture_wrap)
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
    """_get_rss_tree_mb sums at least this process's RSS (>0); nonexistent
    PID returns None. Skips where RSS introspection is unavailable (see
    test_get_rss_mb_real_process)."""
    from kiro_crew.acp.runtime import _get_rss_mb, _get_rss_tree_mb

    self_rss = _get_rss_mb(os.getpid())
    if self_rss is None:
        pytest.skip("RSS introspection unavailable in this environment")
    tree = _get_rss_tree_mb(os.getpid())
    assert tree is not None and tree >= self_rss  # tree includes self (+ children)
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
                assert data["error"]["code"] == -32601
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
    with the same cwd + empty mcpServers + _kiro.dev/session_file _meta. The
    double-session drift it replaces produced stopReason='refusal'."""

    @pytest.mark.asyncio
    async def test_load_session_sends_direct_session_load_params(self, monkeypatch):
        rt, _, _ = _make_runtime()
        rt._can_load_session = True

        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params):
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

        async def _fake_send(method, params):
            return {}  # no "modes" → load did not actually restore state

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

        with pytest.raises(AcpRuntimeError):
            await rt.load_session("/f.json", "sid-y", agent="kirocrew")
        # The queue registered before the send must be cleaned up on failure.
        assert "sid-y" not in rt._session_queues

    @pytest.mark.asyncio
    async def test_load_session_params_match_acp_client(self, monkeypatch):
        """Drift guard: the kiro (non-claude) session/load payload built here
        must equal the one AcpClient._initialize_session builds, so the two
        resume paths never diverge. Compares the field set explicitly."""
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        captured: dict = {}

        async def _fake_send(method, params):
            if method == METHOD_SESSION_LOAD:
                captured.update(params)
                return {"modes": {}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt.load_session("/k/sid.json", "sid", cwd="/w", agent="kirocrew")

        # Mirror of AcpClient's kiro-branch load_params (client.py step 2).
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

    async def _fake_send(method, params):
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


def test_saw_not_logged_in_detects_auth_failure():
    """#1: AcpRuntime.saw_not_logged_in scans captured stderr for kiro-cli's
    'not logged in' signal so a death can be surfaced as AcpAuthRequired."""
    rt, _, _ = _make_runtime()
    rt._stderr_lines = ["startup noise", "error: You are not logged in, please log in"]
    assert rt.saw_not_logged_in() is True
    rt._stderr_lines = ["ordinary stderr", "mcp server ready"]
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
    ok = await handle.steer("please focus on X")
    assert ok is True
    assert sent["method"] == "_session/steer"
    assert "please focus on X" in sent["params"]["message"]
    assert await handle.steer("   ") is False


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
    assert "tc-1" not in raw_cache  # consumed on use


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
async def test_runtime_spawn_scrubs_channel_creds_on_default_auto(monkeypatch):
    """AcpRuntime.spawn strips gateway channel creds on the default auto tier.

    Mirrors the AcpClient guard: the runtime copies a raw os.environ + wrap_argv
    (not sandboxed_spawn_argv), and the default tier launcher does not strip
    _AGENT_DENIED_ENV_KEYS, so scrub_agent_denied_env must remove them.
    """
    import kiro_crew.acp.runtime as runtime_mod

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "0000:FAKE-telegram")
    monkeypatch.setenv("WECOM_BOT_ID", "FAKE-wecom-bot")
    monkeypatch.setenv("WECOM_SECRET", "FAKE-wecom-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-FAKE")
    monkeypatch.setenv("KIROCREW_OWNER_ID", "U_FAKE_OWNER")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FAKE-akid")
    monkeypatch.setenv("KIROCREW_UNRELATED_KEEPME", "keep-this-value")

    captured: dict[str, object] = {}

    class _StopSpawn(Exception):
        pass

    async def _fake_exec(*_args, **kwargs):
        captured["env"] = kwargs.get("env")
        raise _StopSpawn()

    async def resolve_kiro_bin():
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
    ):
        assert key not in env, f"{key} leaked into default-auto runtime child env"
    assert env.get("KIROCREW_UNRELATED_KEEPME") == "keep-this-value"
    assert env.get("AWS_ACCESS_KEY_ID") == "FAKE-akid"


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
        AcpRuntime._mode_available(
            "kirocrew", _new_resp({"availableModes": [{"id": "kirocrew"}]})
        )
        is True
    )
    assert (
        AcpRuntime._mode_available(
            "kirocrew", _new_resp({"availableModes": [{"id": "default"}]})
        )
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
