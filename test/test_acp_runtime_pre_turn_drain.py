"""``AcpSessionHandle``'s pre-turn drain.

What the drain counts, logs, answers and hands back when leftover frames sit on
the session queue before a prompt starts.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import _bounded, _make_runtime, _permission_msg, _register

from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.types import JsonRpcMessage


@pytest.mark.asyncio
async def test_pre_turn_drain_counts_discarded_frames_without_logging_content(caplog):
    """The pre-turn drain destroys leftover frames; it must SAY how many, and
    nothing else.

    A dropped frame is invisible to every layer above: the abandoned turn's
    output vanishes with nothing to show it existed, and a turn that loses its
    terminal this way reaches the dashboard as an empty response with no
    attributable cause. So the drain reports a COUNT — and only a count. Frame
    text, tool arguments, tool results and even frame SIZE are all excluded:
    a size leaks response length, and the kiro-cli data dir this material comes
    from is fenced precisely because it holds credentials.
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
    """A discarded response carrying a non-empty stopReason IS the abandoned
    turn's terminal, and the warning must SAY so instead of hedging.

    The structural fact: a JSON-RPC response has ``method is None``, so a
    leftover prompt response can never reach the drain's permission branch —
    it always lands in the discard arm. Whether the terminal was among the
    discards is therefore decidable, and "possibly including that turn's
    terminal" was speculation about data already in hand.
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
    """When NO discarded frame was a terminal the warning says '0 of them' —
    the reassuring reading an operator could not get from the old hedge.

    The set here also pins the classification guards: a response with an empty
    result dict, a response whose result is not a dict, and a REQUEST that
    (malformed) carries a result with a stopReason must all count as zero —
    only a response (``method is None``, ``id`` set) with a non-empty
    ``stopReason`` is a terminal.
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
    """A JSON-valid response whose stopReason is not a string must neither
    crash the drain nor count as a terminal.

    The hazard is structural: the drain runs AFTER ``_turn_done.clear()`` and
    BEFORE the BaseException guard that restores it, so an exception escaping
    here leaves the handle permanently turn-active — every later prompt on it
    is rejected. A truthy non-str stopReason (``[]``/``{}``) fed to a frozenset
    membership test raises ``TypeError: unhashable type``; the classification
    must type-guard the leaf exactly as ``_dispatch.py``'s wire-stopReason
    reader does.
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
    """An abandoned turn that ended in an ERROR response was still terminated —
    ``_run_turn`` treats an error response as the turn's terminal — so the
    tally must count it, or the warning positively asserts none of the
    discards was terminal-shaped where the old text only hedged. An error
    terminal has no stopReason to name, the error payload must never leak,
    and the warning must NOT attribute the frame to "that turn": a late error
    response to a concurrently timed-out command call (send_command / compact /
    set_config_option, re-injected by _wait_for_response's finally) is
    indistinguishable here, so the line states the shape, not the owner."""
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
    """A response whose req_id is STILL registered in ``_awaited_responses``
    has a live consumer inside ``_wait_for_response``, so the drain must hand
    it back rather than destroy it.

    The abandoned TURN's own frames are owed to nobody: ``_run_turn`` refuses
    to start while ``_turn_done`` is clear, and ``_turn_done`` is set in its
    own ``finally``, so by the time the drain runs that turn's generator has
    already exited. Discarding those is correct. A command/config call
    (``send_command`` / ``compact`` / ``set_config_option``) is different: it
    does not touch ``_turn_done``, so its ``_wait_for_response`` can be in
    flight when the next turn starts — and for a oneshot the response IS the
    terminal. Dropping it strands that caller until its own timeout (60s for
    ``send_command``), which then reports failure for a call the backend
    answered.

    ``_awaited_responses`` is the discriminator that tells the two apart, and
    the dispatch loop already routes on exactly it; the drain was the one queue
    consumer that did not.
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

    A command call that already timed out has discarded its req_id in
    ``_wait_for_response``'s ``finally``, so its late answer is owed to nobody
    and must still drain — otherwise the retention leaks a stray frame into
    every following turn. This is the mutant that would survive a bare
    "keep all responses" rule.
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
    """The retention must not swallow a server→client REQUEST that happens to
    carry the same integer id as an awaited response.

    The two id spaces are independent: our client→server ids come from
    ``AcpRuntime._next_id`` (starts at 1) and the backend mints its own request
    ids, so a numeric collision is ordinary rather than exotic. A retained
    permission request is never answered, which strands the backend's oneshot —
    the exact hang the drain exists to prevent. ``method is None`` is what keeps
    the retention to responses only.
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

    handle.reject_tool.assert_awaited_once_with(3)


@pytest.mark.asyncio
async def test_pre_turn_drain_keeps_an_owed_frame_when_cancelled_mid_drain():
    """A cancellation part-way through the drain must not lose a response
    already collected for a live waiter.

    The permission arm re-raises ``CancelledError`` so the prompt aborts, which
    is exactly when a retained frame is easiest to lose: everything read before
    the cancellation point is held in a local list, and a re-injection reached
    only on the loop's normal exit would never run. The frames go back from a
    ``finally``, so the abort still pays the waiter what it is owed.

    The queue order is the point: the owed response is read FIRST, so it is
    already collected when the stranded permission request triggers the abort.
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

    ``reject_tool`` writes to the child's stdin and that write is not bounded
    short: a backend that already delivered a command response but has stopped
    reading its own stdin applies backpressure that blocks it. An owed waiter
    carries a 60s deadline, so a frame parked in the retain list for the length
    of that write can expire and the call reports "" for a response the backend
    delivered. The await is also the yield point at which the waiting
    ``_wait_for_response`` gets scheduled, so handing the frame back BEFORE it
    is what actually pays the waiter.
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
