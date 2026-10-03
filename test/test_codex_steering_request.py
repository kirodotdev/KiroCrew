"""codex user steer over ``_session/steering`` (``ACP_BACKENDS_STEERING_REQUEST``).

codex-acp answers the request with an outcome and sends no ``steering_consumed``
echo, so the handle reads the answer: ``injected`` is settled by the turn it was
aimed at, from its own dispatch loop, ``startedNewTurn`` is cancelled and reported
undelivered, and anything else is reported undelivered so the caller queues it.
The wire shapes here were captured off a live codex-acp 1.11.0.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.acp import session_handle as sh
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
    METHOD_CANCEL,
)
from kiro_crew.dashboard.steer_settle import settle_consumed_steers


class _Runtime:
    """The three runtime calls a steer can make, recorded."""

    def __init__(self, backend: str = ACP_BACKEND_CODEX) -> None:
        self.acp_backend = backend
        self.is_alive = lambda: True
        self.answer: asyncio.Future = asyncio.get_running_loop().create_future()
        self.requests: list[tuple[str, dict]] = []
        self.notifications: list[tuple[str, dict]] = []
        self.forgotten: list[asyncio.Future] = []
        self.send_request = AsyncMock(return_value=7)

    async def send_request_for_answer(self, method: str, params: dict, on_registered=None):
        self.requests.append((method, params))
        if on_registered is not None:
            on_registered(self.answer)
        return self.answer

    async def send_notification(self, method: str, params: dict) -> None:
        self.notifications.append((method, params))

    def forget_request(self, future) -> None:
        self.forgotten.append(future)
        if not future.done():
            future.cancel()


def _handle(rt: _Runtime, *, active: bool = True) -> AcpSessionHandle:
    handle = AcpSessionHandle("sess-1", asyncio.Queue(), rt)
    if active:
        # What ``prompt()`` does at turn start: the turn is open until done.
        handle._turn_done.clear()
        handle._prompt_starts += 1
        handle._prompt_written = True
    return handle


def _drain(handle: AcpSessionHandle, can_settle: bool = True) -> list[str]:
    """What the turn's dispatch loop would settle now (echo texts)."""
    return handle._take_injected_steers(can_settle=can_settle)


async def _steer_and_settle(handle: AcpSessionHandle, text: str) -> tuple[bool, list[str]]:
    """Steer, then let the turn's dispatch loop take one settle-able frame."""
    steer = asyncio.ensure_future(handle.steer(text))
    for _ in range(3):
        await asyncio.sleep(0)
    echoes = _drain(handle)
    return await asyncio.wait_for(steer, 5), echoes


@pytest.mark.asyncio
async def test_injected_settles_through_the_consumed_echo():
    rt = _Runtime()
    rt.answer.set_result({"outcome": "injected"})
    handle = _handle(rt)

    delivered, echoes = await _steer_and_settle(handle, "  use the other file  ")
    assert delivered is True

    # The request is codex's own verb and shape: a ContentBlock prompt, no wrapper.
    assert rt.requests == [
        (
            "_session/steering",
            {"sessionId": "sess-1", "prompt": [{"type": "text", "text": "use the other file"}]},
        )
    ]
    rt.send_request.assert_not_awaited()  # kiro's _session/steer never goes out
    assert rt.notifications == []
    assert handle.last_steer_monotonic > 0

    # The echo the turn settles with names exactly the steer that was sent, and
    # nothing is left on the queue for a later turn's stale drain to discard.
    assert handle._queue.empty()
    (content,) = echoes
    assert settle_consumed_steers(["use the other file", "later"], content) == ["later"]
    assert _drain(handle) == []  # settled once


@pytest.mark.asyncio
async def test_an_injected_answer_is_never_settled_where_order_cannot_be_read():
    """At the terminal frame, or with frames buffered behind, nothing is proven."""
    rt = _Runtime()
    rt.answer.set_result({"outcome": "injected"})
    handle = _handle(rt)
    assert await asyncio.wait_for(handle.steer("hello"), 5) is True
    assert _drain(handle, can_settle=False) == []
    assert len(handle._steering_answers) == 1
    # A clean terminal reports only proven steers; the caller queues this one.
    assert handle._release_proven_steers("end_turn", None) == []


@pytest.mark.asyncio
async def test_a_cancelled_turn_reports_no_steer_consumed():
    """After cancel() the turn discards injected text, so nothing is reported."""
    rt = _Runtime()
    rt.answer.set_result({"outcome": "injected"})
    handle = _handle(rt)
    assert await asyncio.wait_for(handle.steer("hello"), 5) is True
    handle._steers_proven.extend(_drain(handle))
    assert len(handle._steers_proven) == 1
    handle._cancelled = True  # what cancel() sets
    assert handle._release_proven_steers("end_turn", None) == []
    assert handle._steers_proven == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["refusal", "cancelled", "stale_recover", "max_tokens", "error: tool stall"],
)
async def test_only_a_clean_end_turn_reports_a_proven_steer(reason):
    """A bare refusal (no RefusalInfo), a cancel or a stale recovery reports nothing."""
    rt = _Runtime()
    rt.answer.set_result({"outcome": "injected"})
    handle = _handle(rt)
    assert await asyncio.wait_for(handle.steer("hello"), 5) is True
    handle._steers_proven.extend(_drain(handle))
    assert len(handle._steers_proven) == 1
    assert handle._release_proven_steers(reason, None) == []
    assert handle._steers_proven == []


@pytest.mark.asyncio
async def test_started_new_turn_is_cancelled_and_reported_undelivered():
    rt = _Runtime()
    rt.answer.set_result({"outcome": "startedNewTurn"})
    handle = _handle(rt)

    assert await handle.steer("hello") is False
    await asyncio.sleep(0)  # the cancel is written from a task
    assert rt.notifications == [(METHOD_CANCEL, {"sessionId": "sess-1"})]
    assert _drain(handle) == []
    assert handle.last_steer_monotonic == 0.0
    # Not the handle's own cancel: no turn of ours was cancelled.
    assert handle._cancelled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [{"outcome": "failed"}, {}, {"outcome": "something-new"}])
async def test_any_other_outcome_is_undelivered_and_cancels_nothing(answer):
    rt = _Runtime()
    rt.answer.set_result(answer)
    handle = _handle(rt)
    assert await handle.steer("hello") is False
    await asyncio.sleep(0)
    assert rt.notifications == []
    assert _drain(handle) == []


@pytest.mark.asyncio
async def test_an_error_answer_is_undelivered():
    """An adapter without the method answers -32601; the caller queues instead."""
    rt = _Runtime()
    rt.answer.set_exception(AcpRuntimeError("Method not found"))
    handle = _handle(rt)
    assert await handle.steer("hello") is False
    assert _drain(handle) == []


@pytest.mark.asyncio
async def test_an_error_answer_logs_its_class_not_the_adapter_text(caplog):
    """The adapter's error text never reaches the gateway log."""
    rt = _Runtime()
    rt.answer.set_exception(AcpRuntimeError("boom secret-looking-text-xyz"))
    handle = _handle(rt)
    with caplog.at_level("DEBUG", logger=sh.logger.name):
        assert await handle.steer("hello") is False
    assert "secret-looking-text-xyz" not in caplog.text
    assert "AcpRuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_no_request_goes_out_without_a_running_turn():
    """With no turn of ours the adapter would start its own, so nothing is sent."""
    rt = _Runtime()
    handle = _handle(rt, active=False)
    assert await handle.steer("hello") is False
    assert rt.requests == []


@pytest.mark.asyncio
async def test_a_cancel_that_cannot_be_written_is_logged_not_raised():
    """A dead adapter fails the cancel; that must not become an unhandled task error."""
    rt = _Runtime()
    rt.answer.set_result({"outcome": "startedNewTurn"})

    async def dead(method, params):
        raise AcpRuntimeError("runtime is dead")

    rt.send_notification = dead  # type: ignore[method-assign]
    handle = _handle(rt)
    assert await handle.steer("hello") is False
    (task,) = list(handle._steering_cancel_tasks)
    await task
    assert task.exception() is None


@pytest.mark.asyncio
async def test_no_request_goes_out_while_the_turn_waits_on_an_approval():
    """codex's reject cancels the turn and drops an injected steer, so it is queued."""
    rt = _Runtime()
    rt.answer.set_result({"outcome": "injected"})
    handle = _handle(rt)
    handle._awaiting_permission = True
    assert await handle.steer("hello") is False
    assert rt.requests == []
    handle._awaiting_permission = False
    assert (await _steer_and_settle(handle, "hello"))[0] is True


async def _steer_then_end_turn(handle: AcpSessionHandle) -> bool:
    """Send a steer, then end the turn before the adapter answers it."""
    steer = asyncio.ensure_future(handle.steer("hello"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not steer.done()  # awaiting the answer, with no timer of its own
    handle._turn_done.set()
    return await asyncio.wait_for(steer, 5)


@pytest.mark.asyncio
async def test_an_unanswered_steer_is_undelivered_once_its_turn_ends():
    """No answer before the turn ends: the caller queues it, and nothing is retained."""
    rt = _Runtime()
    handle = _handle(rt)
    assert await _steer_then_end_turn(handle) is False
    assert handle._steering_answers == []
    assert handle.last_steer_monotonic == 0.0


@pytest.mark.asyncio
async def test_a_late_started_new_turn_is_cancelled():
    rt = _Runtime()
    handle = _handle(rt)
    assert await _steer_then_end_turn(handle) is False
    rt.answer.set_result({"outcome": "startedNewTurn"})
    for _ in range(3):
        await asyncio.sleep(0)
    assert rt.notifications == [(METHOD_CANCEL, {"sessionId": "sess-1"})]


@pytest.mark.asyncio
async def test_a_late_started_new_turn_never_cancels_our_next_turn():
    """``session/cancel`` names no turn: once our next prompt runs, it would hit that."""
    rt = _Runtime()
    handle = _handle(rt)
    assert await _steer_then_end_turn(handle) is False
    # The queue drain starts the next prompt of ours before the answer lands.
    handle._turn_done.clear()
    handle._prompt_starts += 1
    rt.answer.set_result({"outcome": "startedNewTurn"})
    for _ in range(3):
        await asyncio.sleep(0)
    assert rt.notifications == []


@pytest.mark.asyncio
async def test_an_answer_aimed_at_an_earlier_turn_never_settles_a_newer_one():
    """A registration left from an earlier prompt is dropped, never reported."""
    rt = _Runtime()
    rt.answer.set_result({"outcome": "injected"})
    handle = _handle(rt)
    assert await asyncio.wait_for(handle.steer("hello"), 5) is True
    handle._prompt_starts += 1  # the next prompt of ours began before a frame
    assert _drain(handle) == []
    assert handle._steering_answers == []


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, "injected", 7, ["injected"], {"outcome": 1}])
async def test_a_malformed_answer_is_undelivered_and_never_raises(result):
    """The answer is adapter-authored JSON; a non-object result reads as no outcome."""
    rt = _Runtime()
    rt.answer.set_result(result)
    handle = _handle(rt)
    assert await handle.steer("hello") is False
    assert _drain(handle) == []
    assert handle._steering_answers == []


@pytest.mark.asyncio
async def test_retained_steers_are_bounded_in_count_and_size():
    rt = _Runtime()
    handle = _handle(rt)
    assert await handle.steer("x" * (sh._MAX_STEERING_TEXT_CHARS + 1)) is False
    assert rt.requests == []
    fut = asyncio.get_running_loop().create_future()
    handle._steering_answers = [(fut, handle._prompt_starts, "t")] * sh._MAX_STEERING_ANSWERS
    assert await handle.steer("hello") is False
    assert rt.requests == []


@pytest.mark.asyncio
async def test_proven_steers_awaiting_the_terminal_count_toward_the_cap():
    """Text held for the clean-terminal report is bounded by the same cap."""
    rt = _Runtime()
    handle = _handle(rt)
    handle._steers_proven = ["t"] * sh._MAX_STEERING_ANSWERS
    assert await handle.steer("hello") is False
    assert rt.requests == []


def test_only_codex_takes_the_steering_request():
    """A mutation pin: a backend added here would inherit this steer transport."""
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_STEERING_REQUEST

    assert ACP_BACKENDS_STEERING_REQUEST == frozenset({ACP_BACKEND_CODEX})


@pytest.mark.asyncio
async def test_steer_runs_on_the_real_turn_state_not_a_test_double():
    """The capability check reads the handle's own turn state (no patched method)."""
    rt = _Runtime()
    rt.answer.set_result({"outcome": "injected"})
    handle = AcpSessionHandle("sess-1", asyncio.Queue(), rt)
    assert await handle.steer("hello") is False  # no prompt started yet
    handle._turn_done.clear()
    handle._prompt_starts += 1
    assert await handle.steer("hello") is False  # prompt not written yet
    handle._prompt_written = True
    assert (await _steer_and_settle(handle, "hello"))[0] is True


@pytest.mark.asyncio
async def test_the_kiro_family_keeps_its_fire_and_forget_verb():
    rt = _Runtime(ACP_BACKEND_KIRO)
    handle = _handle(rt)
    assert await handle.steer("hello") is True
    rt.send_request.assert_awaited_once_with(
        "_session/steer",
        {"sessionId": "sess-1", "message": "<user_message>\nhello\n</user_message>"},
    )
    assert rt.requests == []


# ── AcpRuntime.send_request_for_answer ──


def _runtime_with_fake_process() -> tuple[AcpRuntime, asyncio.StreamReader, MagicMock]:
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
    return rt, reader, proc


@pytest.mark.asyncio
async def test_the_answer_resolves_the_future_and_never_reaches_the_session_queue():
    rt, reader, proc = _runtime_with_fake_process()
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["sA"] = queue
    task = asyncio.ensure_future(rt._reader_loop())
    try:
        fut = await rt.send_request_for_answer(
            "_session/steering", {"sessionId": "sA", "prompt": []}
        )
        sent = json.loads(proc.stdin.write.call_args.args[0].decode())
        assert sent["method"] == "_session/steering"
        assert sent["id"] not in rt._routed_requests  # not routed to the turn's queue
        reader.feed_data(
            (
                json.dumps({"jsonrpc": "2.0", "id": sent["id"], "result": {"outcome": "injected"}})
                + "\n"
            ).encode()
        )
        assert await asyncio.wait_for(fut, 5) == {"outcome": "injected"}
        assert queue.empty()
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_an_error_answer_fails_the_future():
    rt, reader, proc = _runtime_with_fake_process()
    task = asyncio.ensure_future(rt._reader_loop())
    try:
        fut = await rt.send_request_for_answer(
            "_session/steering", {"sessionId": "sA", "prompt": []}
        )
        sent = json.loads(proc.stdin.write.call_args.args[0].decode())
        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": sent["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )
                + "\n"
            ).encode()
        )
        with pytest.raises(AcpRuntimeError):
            await asyncio.wait_for(fut, 5)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


_CHUNK = {
    "jsonrpc": "2.0",
    "method": "session/update",
    "params": {
        "sessionId": "sA",
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "x"},
        },
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("answer_first", [True, False])
async def test_an_injected_answer_buffered_with_the_terminal_is_queued_not_settled(answer_first):
    """The ordering the review found, through the real reader and dispatch.

    The answer and the turn's terminal land in one read, in either order. The
    reader resolves both before the dispatch loop runs, so their order cannot
    be read there. Near the end of a turn the steer is reported undelivered and
    the caller queues it: never marked consumed by a turn that had finished.
    """
    rt, reader, proc = _runtime_with_fake_process()
    rt._acp_backend = ACP_BACKEND_CODEX
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["sA"] = queue
    handle = AcpSessionHandle("sA", queue, rt)
    task = asyncio.ensure_future(rt._reader_loop())
    kinds: list[str] = []
    try:

        async def drive():
            async for ev in handle.prompt("hi", timeout=5.0):
                kinds.append(ev.kind)

        driver = asyncio.ensure_future(drive())
        for _ in range(200):
            if rt._routed_requests:
                break
            await asyncio.sleep(0.01)
        (prompt_id,) = rt._routed_requests
        steer = asyncio.ensure_future(handle.steer("hello"))
        for _ in range(200):
            if rt._pending_requests:
                break
            await asyncio.sleep(0.01)
        (steer_id,) = rt._pending_requests
        answer = json.dumps({"jsonrpc": "2.0", "id": steer_id, "result": {"outcome": "injected"}})
        terminal = json.dumps(
            {"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": "end_turn"}}
        )
        lines = [answer, terminal] if answer_first else [terminal, answer]
        reader.feed_data(("\n".join(lines) + "\n").encode())
        await asyncio.wait_for(driver, 5)
        assert await asyncio.wait_for(steer, 5) is False
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    assert "steer_consumed" not in kinds
    assert handle._steering_answers == []
    assert handle._steering_settled == {}


@pytest.mark.asyncio
async def test_an_answer_read_while_the_write_is_still_draining_still_settles_first():
    """The write's drain() can suspend; the answer may land inside it.

    The answer is registered before the drain, so when the reader resolves it and
    a later frame of the still-running turn arrives while the steer is suspended
    in its own write, the dispatch loop settles the steer at that frame, before
    the terminal exists.
    """
    rt, reader, proc = _runtime_with_fake_process()
    rt._acp_backend = ACP_BACKEND_CODEX
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["sA"] = queue
    handle = AcpSessionHandle("sA", queue, rt)
    task = asyncio.ensure_future(rt._reader_loop())
    release = asyncio.Event()
    kinds: list[str] = []

    async def drain() -> None:
        # Only the steering write is held; the prompt's own write goes through.
        last = proc.stdin.write.call_args.args[0].decode()
        if "_session/steering" in last:
            await release.wait()

    proc.stdin.drain = drain
    try:

        async def drive():
            async for ev in handle.prompt("hi", timeout=5.0):
                kinds.append(ev.kind)

        driver = asyncio.ensure_future(drive())
        for _ in range(200):
            if rt._routed_requests:
                break
            await asyncio.sleep(0.01)
        (prompt_id,) = rt._routed_requests
        steer = asyncio.ensure_future(handle.steer("hello"))
        for _ in range(200):
            if rt._pending_requests:
                break
            await asyncio.sleep(0.01)
        (steer_id,) = rt._pending_requests
        reader.feed_data(
            (
                json.dumps({"jsonrpc": "2.0", "id": steer_id, "result": {"outcome": "injected"}})
                + "\n"
            ).encode()
        )
        for _ in range(20):
            await asyncio.sleep(0.01)
        reader.feed_data((json.dumps(_CHUNK) + "\n").encode())
        for _ in range(200):
            if "steer_consumed" in kinds:
                break
            await asyncio.sleep(0.01)
        release.set()
        assert await asyncio.wait_for(steer, 5) is True
        reader.feed_data(
            (
                json.dumps(
                    {"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": "end_turn"}}
                )
                + "\n"
            ).encode()
        )
        await asyncio.wait_for(driver, 5)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    assert kinds.index("steer_consumed") < kinds.index("complete")


@pytest.mark.asyncio
async def test_a_frame_read_before_the_answer_is_handled_before_the_steer_settles():
    """An update read ahead of the answer keeps its place in the transcript.

    The chunk and the ``injected`` answer land in one read, chunk first, so the
    reader queues the chunk and resolves the answer before the dispatch loop
    dequeues the chunk. The steer settles only once that chunk is handled.
    """
    rt, reader, proc = _runtime_with_fake_process()
    rt._acp_backend = ACP_BACKEND_CODEX
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["sA"] = queue
    handle = AcpSessionHandle("sA", queue, rt)
    task = asyncio.ensure_future(rt._reader_loop())
    kinds: list[str] = []
    try:

        async def drive():
            async for ev in handle.prompt("hi", timeout=5.0):
                kinds.append(ev.kind)

        driver = asyncio.ensure_future(drive())
        for _ in range(200):
            if rt._routed_requests:
                break
            await asyncio.sleep(0.01)
        (prompt_id,) = rt._routed_requests
        steer = asyncio.ensure_future(handle.steer("hello"))
        for _ in range(200):
            if rt._pending_requests:
                break
            await asyncio.sleep(0.01)
        (steer_id,) = rt._pending_requests
        answer = json.dumps({"jsonrpc": "2.0", "id": steer_id, "result": {"outcome": "injected"}})
        reader.feed_data(("\n".join([json.dumps(_CHUNK), answer]) + "\n").encode())
        assert await asyncio.wait_for(steer, 5) is True
        reader.feed_data(
            (
                json.dumps(
                    {"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": "end_turn"}}
                )
                + "\n"
            ).encode()
        )
        await asyncio.wait_for(driver, 5)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    assert kinds == ["text_chunk", "steer_consumed", "complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["denied", "cancelled"])
async def test_a_steer_proven_mid_turn_is_not_reported_consumed_if_the_turn_is_cut(ending):
    """A denial or a cancel after the steer was proven still leaves it queued.

    codex drops injected text with a turn its reject or ``session/cancel``
    ended, so the steer is never reported consumed and the caller's pending
    entry for it is queued by the turn's teardown.
    """
    rt, reader, proc = _runtime_with_fake_process()
    rt._acp_backend = ACP_BACKEND_CODEX
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["sA"] = queue
    handle = AcpSessionHandle("sA", queue, rt)
    task = asyncio.ensure_future(rt._reader_loop())
    kinds: list[str] = []
    try:

        async def drive():
            async for ev in handle.prompt("hi", timeout=5.0):
                kinds.append(ev.kind)

        driver = asyncio.ensure_future(drive())
        for _ in range(200):
            if rt._routed_requests:
                break
            await asyncio.sleep(0.01)
        (prompt_id,) = rt._routed_requests
        steer = asyncio.ensure_future(handle.steer("hello"))
        for _ in range(200):
            if rt._pending_requests:
                break
            await asyncio.sleep(0.01)
        (steer_id,) = rt._pending_requests
        answer = json.dumps({"jsonrpc": "2.0", "id": steer_id, "result": {"outcome": "injected"}})
        reader.feed_data((answer + "\n" + json.dumps(_CHUNK) + "\n").encode())
        assert await asyncio.wait_for(steer, 5) is True
        for _ in range(200):
            if "text_chunk" in kinds and handle._queue.empty():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        if ending == "denied":
            await handle.reject_tool("perm-1")
            stop = "end_turn"
        else:
            await handle.cancel()
            stop = "cancelled"
        reader.feed_data(
            (
                json.dumps({"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": stop}})
                + "\n"
            ).encode()
        )
        await asyncio.wait_for(driver, 5)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    assert "steer_consumed" not in kinds, kinds
    assert kinds[-1] == "complete"


@pytest.mark.asyncio
async def test_an_answer_slower_than_the_send_request_bound_is_queued(monkeypatch):
    """The composer's send request cannot outwait the browser's abort."""
    monkeypatch.setattr(sh, "_STEERING_ANSWER_WAIT_SECS", 0.05)
    rt = _Runtime()  # the answer never arrives
    handle = _handle(rt)
    assert await asyncio.wait_for(handle.steer("hello"), 5) is False
    assert handle._steering_answers == []
    assert handle._abandoned_steering == [rt.answer]


@pytest.mark.asyncio
async def test_a_terminal_read_before_an_injected_answer_reports_undelivered(caplog):
    """The inverse order codex-acp does not rule out, through the real reader.

    The terminal arrives first, so nothing settles in the turn and the steer is
    reported undelivered: the caller queues it (at-least-once). The late
    ``injected`` is logged as the counter for how often that window occurs, and
    nothing is left registered or raised.
    """
    rt, reader, proc = _runtime_with_fake_process()
    rt._acp_backend = ACP_BACKEND_CODEX
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["sA"] = queue
    handle = AcpSessionHandle("sA", queue, rt)
    task = asyncio.ensure_future(rt._reader_loop())
    kinds: list[str] = []
    try:

        async def drive():
            async for ev in handle.prompt("hi", timeout=5.0):
                kinds.append(ev.kind)

        driver = asyncio.ensure_future(drive())
        for _ in range(200):
            if rt._routed_requests:
                break
            await asyncio.sleep(0.01)
        (prompt_id,) = rt._routed_requests
        steer = asyncio.ensure_future(handle.steer("hello"))
        for _ in range(200):
            if rt._pending_requests:
                break
            await asyncio.sleep(0.01)
        (steer_id,) = rt._pending_requests
        terminal = {"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": "end_turn"}}
        reader.feed_data((json.dumps(terminal) + "\n").encode())
        await asyncio.wait_for(driver, 5)
        assert await asyncio.wait_for(steer, 5) is False
        with caplog.at_level("WARNING"):
            answer = {"jsonrpc": "2.0", "id": steer_id, "result": {"outcome": "injected"}}
            reader.feed_data((json.dumps(answer) + "\n").encode())
            for _ in range(20):
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    assert "steer_consumed" not in kinds
    assert handle._steering_answers == []
    assert "injected after its turn ended" in caplog.text


@pytest.mark.asyncio
async def test_a_cancelled_caller_moves_its_answer_to_the_bounded_abandoned_set():
    rt = _Runtime()
    handle = _handle(rt)
    steer = asyncio.ensure_future(handle.steer("hello"))
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(handle._steering_answers) == 1
    steer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await steer
    assert handle._steering_answers == []
    # The runtime still owes the answer, so it stays counted until it resolves.
    assert handle._abandoned_steering == [rt.answer]
    rt.answer.set_result({"outcome": "failed"})
    await asyncio.sleep(0)
    assert handle._abandoned_steering == []


@pytest.mark.asyncio
async def test_answers_still_awaited_after_their_turn_share_the_bound():
    """An unanswered steer's registration is bounded with the rest, not beside it."""
    rt = _Runtime()
    handle = _handle(rt)
    assert await _steer_then_end_turn(handle) is False
    assert handle._abandoned_steering == [rt.answer]
    handle._steering_answers = [(rt.answer, handle._prompt_starts, "t")] * (
        sh._MAX_STEERING_ANSWERS - 1
    )
    handle._turn_done.clear()
    sent = len(rt.requests)
    assert await handle.steer("more") is False  # 15 + 1 abandoned == the bound
    assert len(rt.requests) == sent
    handle._steering_answers = []
    rt.answer.set_result({"outcome": "failed"})
    await asyncio.sleep(0)
    assert handle._abandoned_steering == []  # released once answered


@pytest.mark.asyncio
async def test_destroy_forgets_every_answer_it_still_holds():
    rt = _Runtime()
    rt.terminate_session = AsyncMock()
    handle = _handle(rt)
    assert await _steer_then_end_turn(handle) is False
    abandoned = rt.answer
    await handle.destroy()
    assert rt.forgotten == [abandoned]
    assert handle._abandoned_steering == []
    assert rt.notifications == []  # the forgotten answer cancels nothing


@pytest.mark.asyncio
async def test_forget_request_drops_the_runtime_registration():
    rt, _reader, _proc = _runtime_with_fake_process()
    fut = await rt.send_request_for_answer("_session/steering", {"sessionId": "sA", "prompt": []})
    assert fut in rt._pending_requests.values()
    rt.forget_request(fut)
    assert fut not in rt._pending_requests.values()
    assert fut.cancelled()


def _written_methods(proc) -> list[str]:
    out = []
    for call in proc.stdin.write.call_args_list:
        frame = json.loads(call.args[0].decode())
        out.append(frame.get("method") or "")
    return out


@pytest.mark.asyncio
async def test_a_late_started_new_turn_is_cancelled_before_the_next_prompt_goes_out(monkeypatch):
    """The next prompt waits for owed answers, so the cancel cannot hit it."""
    rt, reader, proc = _runtime_with_fake_process()
    rt._acp_backend = ACP_BACKEND_CODEX
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["sA"] = queue
    handle = AcpSessionHandle("sA", queue, rt)
    owed = asyncio.get_running_loop().create_future()
    handle._abandoned_steering = [owed]
    asyncio.get_running_loop().call_later(0.05, owed.set_result, {"outcome": "startedNewTurn"})
    # The callbacks _abandon() would have attached.
    owed.add_done_callback(
        lambda f: handle._steering_cancel_tasks.add(
            asyncio.ensure_future(rt.send_notification("session/cancel", {"sessionId": "sA"}))
        )
    )
    task = asyncio.ensure_future(rt._reader_loop())
    try:

        async def drive():
            async for _ in handle.prompt("next", timeout=5.0):
                pass

        driver = asyncio.ensure_future(drive())
        for _ in range(300):
            if rt._routed_requests:
                break
            await asyncio.sleep(0.01)
        (prompt_id,) = rt._routed_requests
        methods = _written_methods(proc)
        assert methods.index("session/cancel") < methods.index("session/prompt")
        reader.feed_data(
            (
                json.dumps(
                    {"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": "end_turn"}}
                )
                + "\n"
            ).encode()
        )
        await asyncio.wait_for(driver, 5)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    assert handle._abandoned_steering == []


@pytest.mark.asyncio
async def test_an_answer_that_never_arrives_is_forgotten_and_cancelled_before_the_next_prompt(
    monkeypatch,
):
    monkeypatch.setattr(sh, "_STEERING_SETTLE_SECS", 0.01)
    rt = _Runtime()
    handle = _handle(rt, active=False)
    owed = asyncio.get_running_loop().create_future()
    handle._abandoned_steering = [owed]
    await handle._settle_abandoned_steering()
    assert rt.forgotten == [owed]
    assert handle._abandoned_steering == []
    assert rt.notifications == [(METHOD_CANCEL, {"sessionId": "sess-1"})]


# ── A denied approval or a cancel reports no steer consumed ──


@pytest.mark.asyncio
async def test_after_a_denied_approval_nothing_in_the_turn_is_reported_consumed():
    """codex's reject cancels the turn and drops what was injected into it."""
    rt = _Runtime()
    rt.send_response = AsyncMock()
    handle = _handle(rt)
    rt.answer.set_result({"outcome": "injected"})
    assert await asyncio.wait_for(handle.steer("and this"), 5) is True
    handle._steers_proven.extend(_drain(handle))
    await handle.reject_tool("perm-1")
    assert handle._turn_steering_denied is True
    assert handle._release_proven_steers("end_turn", None) == []


@pytest.mark.asyncio
async def test_a_denial_on_the_kiro_family_loses_no_steer():
    """kiro-cli's clean reject keeps the turn, so later settles are unaffected."""
    rt = _Runtime(ACP_BACKEND_KIRO)
    rt.send_response = AsyncMock()
    handle = _handle(rt)
    await handle.reject_tool("perm-1")
    assert handle._turn_steering_denied is False


# ── Only the dashboard composer steers codex ──


def test_the_handle_names_codex_as_needing_loss_recovery():
    assert _handle_for(ACP_BACKEND_CODEX).steer_needs_loss_recovery is True
    assert _handle_for(ACP_BACKEND_KIRO).steer_needs_loss_recovery is False


def _handle_for(backend: str) -> AcpSessionHandle:
    rt = MagicMock()
    rt.acp_backend = backend
    return AcpSessionHandle("sess-1", asyncio.Queue(), rt)


@pytest.mark.asyncio
async def test_the_provider_wrapper_refuses_a_codex_steer():
    """Channels and Side Chat steer AcpProvider; for codex it reports no steer
    and refuses one, so those surfaces take their queue paths."""
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider

    handle = _handle_for(ACP_BACKEND_CODEX)
    handle.steer = AsyncMock(return_value=True)  # type: ignore[method-assign]
    inner = AcpSessionProvider(handle, handle._runtime, session_key="k")
    outer = AcpProvider.__new__(AcpProvider)
    outer._client = inner  # type: ignore[assignment]

    assert inner.supports_steer is True  # the dashboard steers this one
    assert outer.supports_steer is False
    assert await outer.steer("hi") is False
    handle.steer.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_provider_wrapper_still_steers_kiro():
    from kiro_crew.providers.acp import AcpProvider

    inner = MagicMock()
    inner.supports_steer = True
    inner.steer_needs_loss_recovery = False
    inner.steer = AsyncMock(return_value=True)
    outer = AcpProvider.__new__(AcpProvider)
    outer._client = inner
    assert outer.supports_steer is True
    assert await outer.steer("hi") is True


@pytest.mark.asyncio
async def test_a_scheduled_cancel_alone_still_holds_the_next_prompt_back():
    """The answer already left the abandoned set; only its cancel task remains.

    The next prompt must still wait for that cancel to be written, or an
    untargeted ``session/cancel`` lands on the prompt we are about to send.
    """
    rt, reader, proc = _runtime_with_fake_process()
    rt._acp_backend = ACP_BACKEND_CODEX
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["sA"] = queue
    handle = AcpSessionHandle("sA", queue, rt)
    release = asyncio.Event()

    async def late_cancel() -> None:
        await release.wait()
        await rt.send_notification("session/cancel", {"sessionId": "sA"})

    handle._steering_cancel_tasks.add(asyncio.ensure_future(late_cancel()))
    assert handle._abandoned_steering == []
    asyncio.get_running_loop().call_later(0.05, release.set)
    task = asyncio.ensure_future(rt._reader_loop())
    try:

        async def drive():
            async for _ in handle.prompt("next", timeout=5.0):
                pass

        driver = asyncio.ensure_future(drive())
        for _ in range(300):
            if rt._routed_requests:
                break
            await asyncio.sleep(0.01)
        (prompt_id,) = rt._routed_requests
        methods = _written_methods(proc)
        assert methods.index("session/cancel") < methods.index("session/prompt")
        reader.feed_data(
            (
                json.dumps(
                    {"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": "end_turn"}}
                )
                + "\n"
            ).encode()
        )
        await asyncio.wait_for(driver, 5)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_a_clean_terminal_never_reports_a_steer_its_caller_has_not_accepted():
    """Consumption is reported only after ``steer()`` has returned True.

    The dashboard persists the steer's row synchronously on that return, so a
    report ahead of it would clear the pending entry before any row exists. Held
    back, the caller resumes after the turn is done, returns False, and the text
    is queued rather than lost.
    """
    rt = _Runtime()
    rt.answer.set_result({"outcome": "injected"})
    draining = asyncio.Event()
    release = asyncio.Event()

    async def suspended_write(method, params, on_registered=None):
        rt.requests.append((method, params))
        if on_registered is not None:
            on_registered(rt.answer)
        draining.set()
        await release.wait()
        return rt.answer

    rt.send_request_for_answer = suspended_write  # type: ignore[method-assign]
    handle = _handle(rt)
    steer = asyncio.ensure_future(handle.steer("hello"))
    await asyncio.wait_for(draining.wait(), 5)
    handle._steers_proven.extend(_drain(handle))
    assert len(handle._steers_proven) == 1
    # A clean terminal while the caller is still suspended in its write.
    assert handle._release_proven_steers("end_turn", None) == []
    handle._turn_done.set()
    release.set()
    assert await asyncio.wait_for(steer, 5) is False


@pytest.mark.asyncio
async def test_a_clean_terminal_reports_a_steer_its_caller_accepted():
    rt = _Runtime()
    rt.answer.set_result({"outcome": "injected"})
    handle = _handle(rt)
    delivered, echoes = await _steer_and_settle(handle, "hello")
    assert delivered is True
    handle._steers_proven.extend(echoes)
    (released,) = handle._release_proven_steers("end_turn", None)
    assert "hello" in released
    assert handle._steers_accepted == []


@pytest.mark.asyncio
async def test_a_steer_cancelled_after_it_was_proven_is_never_reported_consumed():
    """The caller never saw True, so the clean terminal must not report it."""
    rt = _Runtime()
    rt.answer.set_result({"outcome": "injected"})
    draining = asyncio.Event()

    async def suspended_write(method, params, on_registered=None):
        rt.requests.append((method, params))
        if on_registered is not None:
            on_registered(rt.answer)
        draining.set()
        await asyncio.Event().wait()  # drain() never returns before the cancel
        return rt.answer

    rt.send_request_for_answer = suspended_write  # type: ignore[method-assign]
    handle = _handle(rt)
    steer = asyncio.ensure_future(handle.steer("hello"))
    await asyncio.wait_for(draining.wait(), 5)
    # The dispatch loop proves the answered steer while the write is suspended.
    handle._steers_proven.extend(_drain(handle))
    assert len(handle._steers_proven) == 1
    steer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await steer
    assert handle._steers_proven == []
    assert handle._release_proven_steers("end_turn", None) == []


@pytest.mark.asyncio
async def test_a_steer_cancelled_mid_write_leaves_no_registered_answer():
    """Cancelled while the write's drain() is suspended, after registration.

    The answer must not stay registered: it would later settle the steer as
    consumed for a caller that never saw True, and the text would run with no
    transcript row. The cancellation still propagates.
    """
    rt = _Runtime()
    draining = asyncio.Event()

    async def suspended_write(method, params, on_registered=None):
        rt.requests.append((method, params))
        if on_registered is not None:
            on_registered(rt.answer)
        draining.set()
        await asyncio.Event().wait()  # drain() never returns before the cancel
        return rt.answer

    rt.send_request_for_answer = suspended_write  # type: ignore[method-assign]
    handle = _handle(rt)
    steer = asyncio.ensure_future(handle.steer("hello"))
    await asyncio.wait_for(draining.wait(), 5)
    assert len(handle._steering_answers) == 1
    steer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await steer
    assert handle._steering_answers == []
    assert handle._steering_settled == {}
    assert rt.forgotten == [rt.answer]


@pytest.mark.asyncio
async def test_a_caller_cancelled_after_started_new_turn_still_cancels_that_turn():
    """The answer resolved in the same iteration the caller was cancelled in.

    The abandon path must still act on it: an adapter-owned turn left running
    would collide with the next prompt.
    """
    rt = _Runtime()
    rt.answer.set_result({"outcome": "startedNewTurn"})
    handle = _handle(rt)
    steer = asyncio.ensure_future(handle.steer("hello"))
    await asyncio.sleep(0)  # the steer is now suspended in its wait
    steer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await steer
    for _ in range(3):
        await asyncio.sleep(0)
    assert rt.notifications == [(METHOD_CANCEL, {"sessionId": "sess-1"})]
    assert handle._steering_answers == []


@pytest.mark.asyncio
async def test_no_request_goes_out_before_the_prompt_is_written():
    """Between turn start and the prompt write, codex has no turn of ours.

    A steer there would be answered ``startedNewTurn``, and its untargeted
    cancel could land on the prompt about to go out, so it is queued instead.
    """
    rt = _Runtime()
    handle = _handle(rt)
    handle._prompt_written = False
    assert await handle.steer("too early") is False
    assert rt.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("buffered", [False, True])
async def test_a_stalled_steering_write_raises_only_when_its_frame_was_buffered(buffered):
    """A lock-phase stall wrote nothing, so the steer is plainly undelivered. A
    drain-phase stall left the frame in a pipe the live child may still read:
    it raises, so the caller requeues the steer as possibly delivered instead
    of re-sending it as fresh text."""
    from kiro_crew.acp.runtime import AcpRuntimeStdinStalled

    rt = _Runtime()

    async def _stall(method, params, on_registered=None):
        if on_registered is not None:
            on_registered(rt.answer)
        raise AcpRuntimeStdinStalled("stdin stalled", ambiguous_delivery=buffered)

    rt.send_request_for_answer = _stall  # type: ignore[method-assign]
    handle = _handle(rt)
    if buffered:
        with pytest.raises(AcpRuntimeStdinStalled):
            await handle.steer("hello")
    else:
        assert await handle.steer("hello") is False
    assert rt.forgotten == [rt.answer], "the registration is dropped either way"


@pytest.mark.asyncio
async def test_a_written_steer_whose_runtime_then_stalls_is_ambiguous():
    """A sibling's stall kills the runtime after this steer's frame drained: the
    unanswered steer may still be read and injected, so it raises an ambiguous
    death instead of reporting itself undelivered."""
    from kiro_crew.acp.session_handle import AcpRuntimeDead

    rt = _Runtime()
    rt.stdin_stall_death = True
    rt.answer.set_exception(AcpRuntimeDead("runtime is dead"))
    handle = _handle(rt)
    with pytest.raises(AcpRuntimeDead) as ei:
        await handle.steer("hello")
    assert ei.value.ambiguous_delivery is True
