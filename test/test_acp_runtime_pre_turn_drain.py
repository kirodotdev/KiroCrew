"""``AcpSessionHandle``'s pre-turn drain.

What the drain counts, logs, answers and hands back when leftover frames sit on
the session queue before a prompt starts.
"""

import asyncio
from unittest.mock import AsyncMock, call

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import _bounded, _make_runtime, _permission_msg, _register

from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.types import JsonRpcMessage


@pytest.mark.asyncio
async def test_pre_turn_drain_counts_discarded_frames_without_logging_content(caplog):
    """The pre-turn drain logs how many leftover frames it dropped, and nothing else.

    A dropped frame is invisible above, so the count is the only trace. No text,
    arguments, results or size: size leaks response length, and the source data
    dir is fenced because it holds credentials.
    """
    import contextlib
    import logging

    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)

    # Three leftover NOTIFICATIONS from a prior abandoned turn. Not permission
    # requests: those are answered rather than dropped, and must not be counted.
    for _i, _secret in enumerate(("SECRETALPHA", "SECRETBETA", "SECRETGAMMA")):
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "sA",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": _secret},
                        },
                    },
                }
            )
        )

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
        gen = handle.prompt("hi", timeout=0.2)
        with contextlib.suppress(StopAsyncIteration, asyncio.TimeoutError, Exception):
            await _bounded(gen.__anext__(), what="the first prompt event")
        await gen.aclose()

    _drain_lines = [
        rec.getMessage() for rec in caplog.records if "pre-turn drain discarded" in rec.getMessage()
    ]
    # ONE line per turn, not one per frame: a burst must not flood the log.
    assert len(_drain_lines) == 1, f"expected one drain warning, got {_drain_lines}"
    assert "3 leftover frame(s)" in _drain_lines[0]
    for _secret in ("SECRETALPHA", "SECRETBETA", "SECRETGAMMA"):
        assert _secret not in _drain_lines[0], f"{_secret!r} leaked into the drain warning"


async def _drain_warning_lines(handle, caplog):
    """Run one prompt through the pre-turn drain and return its warning lines."""
    import contextlib
    import logging

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
        gen = handle.prompt("hi", timeout=0.2)
        with contextlib.suppress(StopAsyncIteration, asyncio.TimeoutError, Exception):
            await _bounded(gen.__anext__(), what="the first prompt event")
        await gen.aclose()
    return [
        rec.getMessage() for rec in caplog.records if "pre-turn drain discarded" in rec.getMessage()
    ]


@pytest.mark.asyncio
async def test_pre_turn_drain_names_a_discarded_terminal(caplog):
    """A discarded response with a non-empty stopReason is named as the terminal.

    A JSON-RPC response has ``method is None``, so it always lands in the discard
    arm and the warning can say for certain whether the terminal was dropped.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)

    # A leftover notification plus the abandoned turn's terminal response.
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "SECRETALPHA"},
                    },
                },
            }
        )
    )
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "cancelled"}})
    )

    _drain_lines = await _drain_warning_lines(handle, caplog)
    assert len(_drain_lines) == 1, f"expected one drain warning, got {_drain_lines}"
    assert "2 leftover frame(s)" in _drain_lines[0]
    # The tally states the fact — 1 terminal — and names its closed stopReason.
    assert "1 of them" in _drain_lines[0], _drain_lines[0]
    assert "cancelled" in _drain_lines[0]
    # The hedge is gone.
    assert "possibly" not in _drain_lines[0]
    assert "SECRETALPHA" not in _drain_lines[0]


@pytest.mark.asyncio
async def test_pre_turn_drain_states_the_zero_terminal_case(caplog):
    """When no discarded frame was a terminal the warning says '0 of them'.

    Only a response (``method is None``, ``id`` set) with a non-empty ``stopReason``
    counts; empty or non-dict results and a malformed request do not.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)

    # (a) response, empty result dict — no stopReason, not a terminal
    q["sA"].put_nowait(JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": 4, "result": {}}))
    # (b) response, non-dict result — not a terminal
    q["sA"].put_nowait(JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": 5, "result": "done"}))
    # (c) a REQUEST (method set) that malformedly carries a stopReason result:
    # kills the mutant that drops the ``method is None`` condition
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "some/other_request",
                "result": {"stopReason": "end_turn"},
            }
        )
    )

    _drain_lines = await _drain_warning_lines(handle, caplog)
    assert len(_drain_lines) == 1, f"expected one drain warning, got {_drain_lines}"
    assert "3 leftover frame(s)" in _drain_lines[0]
    assert "0 of them" in _drain_lines[0], _drain_lines[0]
    # Zero terminals ⇒ no stopReason clause at all.
    assert "stopReason" not in _drain_lines[0]


@pytest.mark.asyncio
async def test_pre_turn_drain_never_logs_a_non_closed_stop_reason(caplog):
    """A terminal whose stopReason is NOT a closed protocol value still counts,
    but its value never reaches the log — an unrecognized wire string could
    carry anything, and frame content never belongs in a log (the same
    closed-values discipline as chat_runner's empty-turn line).
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)

    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "SECRETREASON"}}
        )
    )

    _drain_lines = await _drain_warning_lines(handle, caplog)
    assert len(_drain_lines) == 1, f"expected one drain warning, got {_drain_lines}"
    assert "1 of them" in _drain_lines[0]
    assert "SECRETREASON" not in _drain_lines[0], "raw wire string leaked into the log"


@pytest.mark.asyncio
async def test_pre_turn_drain_classifies_alongside_an_answered_permission_request(caplog):
    """A mixed leftover set: the permission request is still answered and
    SEL-audited exactly as today, the terminal is counted, and the request is
    NOT in the discard count. Discard behaviour itself is unchanged."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)
    rt.send_response = AsyncMock()

    audited: list[tuple[object, str, str]] = []
    handle._audit_handle_reject = (  # type: ignore[method-assign]
        lambda request_id, title, error, sub_session_id="": audited.append(
            (request_id, title, error)
        )
    )

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
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "end_turn"}})
    )

    _drain_lines = await _drain_warning_lines(handle, caplog)
    assert audited, "stranded permission request was not SEL-audited"
    assert audited[0][2] == "stranded_request_pre_turn_drain"
    assert len(_drain_lines) == 1, f"expected one drain warning, got {_drain_lines}"
    # The answered request is not discarded: 1 frame, and it is the terminal.
    assert "1 leftover frame(s)" in _drain_lines[0]
    assert "1 of them" in _drain_lines[0]
    assert "end_turn" in _drain_lines[0]


@pytest.mark.asyncio
async def test_pre_turn_drain_survives_a_non_string_stop_reason(caplog):
    """A non-string stopReason neither crashes the drain nor counts as a terminal.

    The drain runs after ``_turn_done.clear()`` and before the guard that restores
    it, so a ``TypeError: unhashable type`` here would leave the handle turn-active
    for good. The leaf is type-guarded as in ``_dispatch.py``.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)

    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {"jsonrpc": "2.0", "id": 4, "result": {"stopReason": ["end_turn"]}}
        )
    )
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": 5, "result": {"stopReason": {"v": 1}}})
    )

    _drain_lines = await _drain_warning_lines(handle, caplog)
    # The drain completed: the warning was emitted and the handle is NOT wedged.
    assert len(_drain_lines) == 1, f"expected one drain warning, got {_drain_lines}"
    assert "2 leftover frame(s)" in _drain_lines[0]
    assert "0 of them" in _drain_lines[0]
    assert handle.is_turn_active is False, "drain crash left the handle turn-active"


@pytest.mark.asyncio
async def test_pre_turn_drain_counts_an_error_response_terminal(caplog):
    """An error response counts as a terminal, as ``_run_turn`` treats it.

    The payload never leaks, and the line names the shape, not "that turn": a late
    error to a timed-out command call looks the same here."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)

    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "error": {"code": -32000, "message": "SECRETBOOM"},
            }
        )
    )

    _drain_lines = await _drain_warning_lines(handle, caplog)
    assert len(_drain_lines) == 1, f"expected one drain warning, got {_drain_lines}"
    assert "1 of them" in _drain_lines[0]
    assert "stopReason" not in _drain_lines[0]
    assert "SECRETBOOM" not in _drain_lines[0], "error payload leaked into the drain warning"
    # No attribution: the drain cannot know which caller owned this response.
    assert "that turn's" not in _drain_lines[0], _drain_lines[0]


@pytest.mark.asyncio
async def test_pre_turn_drain_keeps_a_response_a_live_waiter_is_owed(caplog):
    """A response still registered in ``_awaited_responses`` is handed back, not dropped.

    The abandoned turn's frames are owed to nobody, but a command call
    (``send_command`` / ``compact`` / ``set_config_option``) does not touch
    ``_turn_done`` and can still be waiting; dropping its answer strands it until
    its timeout (60s for ``send_command``).
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)

    # A live set_config_option waiter: registered, still inside its wait.
    handle._awaited_responses.add(77)
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": 77, "result": {"ok": True}})
    )
    # An ordinary leftover from the abandoned turn — owed to nobody, still dropped.
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "cancelled"}})
    )

    _drain_lines = await _drain_warning_lines(handle, caplog)

    # The owed frame survived the drain and is readable by its waiter.
    _kept = await handle._wait_for_response(77, timeout=1.0)
    assert _kept.id == 77
    assert _kept.result == {"ok": True}

    # Only the unowed frame was counted as discarded.
    assert len(_drain_lines) == 1, f"expected one drain warning, got {_drain_lines}"
    assert "1 leftover frame(s)" in _drain_lines[0], _drain_lines[0]
    assert "1 of them" in _drain_lines[0], _drain_lines[0]


@pytest.mark.asyncio
async def test_pre_turn_drain_drops_a_response_no_waiter_is_owed(caplog):
    """The retention is scoped to a REGISTERED waiter, not to every response.

    A timed-out command call has dropped its req_id, so its late answer must
    drain or it leaks into every following turn.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)

    assert not handle._awaited_responses
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": 77, "result": {"ok": True}})
    )

    _drain_lines = await _drain_warning_lines(handle, caplog)

    assert len(_drain_lines) == 1, f"expected one drain warning, got {_drain_lines}"
    assert "1 leftover frame(s)" in _drain_lines[0], _drain_lines[0]
    assert handle._queue.empty(), "an unowed response was retained instead of drained"


@pytest.mark.asyncio
async def test_pre_turn_drain_answers_a_permission_request_whose_id_collides():
    """A server→client request sharing an awaited response's id is still answered.

    The id spaces are independent (``AcpRuntime._next_id`` vs the backend's), so
    collisions are ordinary; ``method is None`` limits retention to responses.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)
    handle.reject_tool = AsyncMock()

    # Our own outstanding command call is waiting on id 3 ...
    handle._awaited_responses.add(3)
    # ... and the backend's stranded permission REQUEST also has id 3.
    q["sA"].put_nowait(_permission_msg(3))

    import contextlib

    gen = handle.prompt("hi", timeout=0.2)
    with contextlib.suppress(StopAsyncIteration, asyncio.TimeoutError, Exception):
        await _bounded(gen.__anext__(), what="the first prompt event")
    await gen.aclose()

    assert handle.reject_tool.await_args_list == [call(3)]
    # Answering the request did not settle our own call that shares its id.
    assert 3 in handle._awaited_responses


@pytest.mark.asyncio
async def test_pre_turn_drain_keeps_an_owed_frame_when_cancelled_mid_drain():
    """A cancellation mid-drain still returns a response collected for a live waiter.

    Frames go back from a ``finally``. The owed response is queued first, so it is
    already collected when the permission request triggers the abort.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)
    handle.reject_tool = AsyncMock(side_effect=asyncio.CancelledError())

    handle._awaited_responses.add(77)
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": 77, "result": {"ok": True}})
    )
    q["sA"].put_nowait(_permission_msg(5))

    gen = handle.prompt("hi", timeout=0.2)
    with pytest.raises(asyncio.CancelledError):
        await gen.__anext__()

    # The abort still handed the owed response back, and its waiter can read it.
    _kept = await handle._wait_for_response(77, timeout=1.0)
    assert _kept.id == 77
    assert _kept.result == {"ok": True}
    # The handle is reusable: the cancel path re-set _turn_done.
    assert handle._turn_done.is_set()


@pytest.mark.asyncio
async def test_pre_turn_drain_hands_back_an_owed_frame_before_awaiting_a_reject():
    """A retained frame is never held across an await.

    ``reject_tool``'s stdin write can block on backpressure past the waiter's 60s
    deadline, and the await is where ``_wait_for_response`` gets scheduled.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)

    _seen_at_reject: list[list[int | str | None]] = []

    async def _reject(_req_id):
        # What the queue holds at the moment the slow write would begin.
        _seen_at_reject.append([m.id for m in list(handle._queue._queue) if m is not None])

    handle.reject_tool = _reject

    handle._awaited_responses.add(77)
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": 77, "result": {"ok": True}})
    )
    q["sA"].put_nowait(_permission_msg(5))

    import contextlib

    gen = handle.prompt("hi", timeout=0.2)
    with contextlib.suppress(StopAsyncIteration, asyncio.TimeoutError, Exception):
        await _bounded(gen.__anext__(), what="the first prompt event")
    await gen.aclose()

    assert _seen_at_reject, "reject_tool was never reached"
    assert 77 in _seen_at_reject[0], (
        "the owed response was still parked in the retain list while reject_tool "
        f"was awaited; queue held {_seen_at_reject[0]}"
    )
    # And it is still there afterwards for its waiter, re-injected exactly once.
    _kept = await handle._wait_for_response(77, timeout=1.0)
    assert _kept.result == {"ok": True}
    assert all(
        m is None or m.id != 77 for m in list(handle._queue._queue)
    ), "the owed response was re-injected twice"


@pytest.mark.asyncio
async def test_pre_turn_drain_normalizes_a_closed_stop_reason_spelling(caplog):
    """A closed value arriving with stray whitespace still logs as the
    canonical constant, not as the '<non-standard>' placeholder — the repo's
    other wire-stopReason reader (``_dispatch.py``) normalizes before
    comparing, and an operator reading a standard terminal as garbage defeats
    the classification's purpose."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)

    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {"jsonrpc": "2.0", "id": 4, "result": {"stopReason": " cancelled "}}
        )
    )

    _drain_lines = await _drain_warning_lines(handle, caplog)
    assert len(_drain_lines) == 1, f"expected one drain warning, got {_drain_lines}"
    assert "1 of them" in _drain_lines[0]
    assert "cancelled" in _drain_lines[0]
    assert "non-standard" not in _drain_lines[0]


@pytest.mark.asyncio
async def test_pre_turn_drain_dedupes_stop_reasons_in_the_warning(caplog):
    """The session queue is unbounded, so the stopReason clause must not grow
    one token per discarded terminal — the count already carries multiplicity;
    the clause names each DISTINCT closed value once."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)

    for _rid in (4, 5, 6):
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {"jsonrpc": "2.0", "id": _rid, "result": {"stopReason": "cancelled"}}
            )
        )

    _drain_lines = await _drain_warning_lines(handle, caplog)
    assert len(_drain_lines) == 1, f"expected one drain warning, got {_drain_lines}"
    assert "3 of them" in _drain_lines[0]
    assert _drain_lines[0].count("cancelled") == 1, _drain_lines[0]


@pytest.mark.asyncio
async def test_prompt_warns_when_the_stream_ends_without_a_terminal_event(caplog):
    """A clean exhaustion with no EVENT_COMPLETE is reported; a consumer close is not.

    The dashboard reads a missing terminal as an empty response and cannot tell
    whether the backend never closed the turn or the consumer walked away. Only
    the first is a fault, so only the first warns — which is what keeps this line
    out of the log on every ordinary cancel and every abandoned generator.
    """
    import contextlib
    import logging

    from kiro_crew.acp.types import EVENT_TEXT_CHUNK, AcpEvent

    _MARK = "ended without a terminal completion event"

    # 1. The dispatch loop returns of its own accord having yielded no terminal.
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)

    async def _no_terminal(*a, **kw):
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="partial")

    handle._dispatch_events = _no_terminal  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
        async for _ev in handle.prompt("hi", timeout=0.2):
            pass
    assert any(_MARK in rec.getMessage() for rec in caplog.records)

    # 2. The SAME stream, abandoned by the consumer after one event. Identical
    #    absence of a terminal, but the consumer caused it — no warning.
    caplog.clear()
    rt2, _, _ = _make_runtime()
    q2 = _register(rt2, "sB")
    handle2 = AcpSessionHandle("sB", q2["sB"], rt2)
    rt2.send_request = AsyncMock(return_value=9)
    handle2._dispatch_events = _no_terminal  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
        gen = handle2.prompt("hi", timeout=0.2)
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()
        await gen.aclose()
    assert not any(_MARK in rec.getMessage() for rec in caplog.records), (
        "a consumer close was reported as a lost terminal — this is the log-spam "
        "case the guard exists to exclude"
    )


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


@pytest.mark.asyncio
async def test_pre_turn_drain_cancels_a_stranded_elicitation_request():
    """An MCP server's elicitation that arrived while no turn was reading (an
    abandoned turn's tool still running) sits in the queue at the next prompt.
    The drain answers it ``cancel`` -- a server->client request the engine waits
    on, like a stranded permission -- rather than discarding it with the frames
    around it, which would hold that tool call open for the life of the process.
    """
    import contextlib

    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=9)
    rt.send_response = AsyncMock()
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "jsonrpc": "2.0",
                "id": 77,
                "method": "_kiro/mcp/elicitation",
                "params": {
                    "sessionId": "sA",
                    "toolCallId": "toolu_x",
                    "elicitation": {
                        "mode": "url",
                        "message": "go",
                        "url": "https://e.x/c",
                        "elicitationId": "e",
                    },
                },
            }
        )
    )
    gen = handle.prompt("hi", timeout=0.2)
    with contextlib.suppress(StopAsyncIteration, asyncio.TimeoutError, Exception):
        await _bounded(gen.__anext__(), what="the first prompt event")
    await gen.aclose()
    assert any(c.args == (77, {"action": "cancel"}) for c in rt.send_response.await_args_list)
