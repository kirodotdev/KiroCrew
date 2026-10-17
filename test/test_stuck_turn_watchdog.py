"""Tests for consumer-park accounting and the out-of-band stuck_turn hook.

The per-turn watchdog lives in the ``except asyncio.TimeoutError`` arm of
``AcpSessionHandle._dispatch_events`` — an async generator, so it advances only
when a consumer pulls it. A consumer that awaits inside its own ``async for``
body freezes the generator at the yield, after which that arm never runs again
for the rest of the turn. Two consequences are covered here:

1. **The idle clocks must not charge consumer time to the backend.** They exist
   to measure runtime silence, but the loop is suspended for the whole of a
   consumer-side await, so without a correction a turn can be cancelled moments
   after a human approves a tool.
2. **Something with its own timer has to notice.** ``SessionManager``'s cleanup
   loop already has one, so a ``stuck_turn`` hook reports a park the in-band arm
   structurally cannot see. Detection only: a turn waiting on a human is
   excluded (that wait is bounded by ``agent.tool_approval_timeout_secs``), and
   ending a live turn stays with the in-band recovery path.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.types import (
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_UPDATE,
    STOP_REASON_TOOL_STALL,
    JsonRpcMessage,
)

# Windows tight enough that a park of a few hundred ms is many multiples of the
# suspect window: without the correction the arm would act well inside the test.
_FAST_WD = WatchdogSettings(
    check_after_secs=0.01,
    stale_window_secs=0.05,
    tool_stall_suspect_secs=0.05,
    tool_stall_hard_cap_secs=1.0,
    model_silent_probe_secs=0.5,
)


def _make_handle(watchdog: WatchdogSettings = _FAST_WD) -> AcpSessionHandle:
    """A handle over a fake runtime.

    ``pid`` is None so the liveness oracle stays at UNKNOWN ("no runtime pid"),
    which is the timeout-governed class — i.e. the one the park correction has
    to keep from firing.
    """
    rt = MagicMock()
    rt._last_activity = time.monotonic()
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    rt.send_request = AsyncMock(return_value=99)
    rt.send_response = AsyncMock()
    rt.supports_image_prompt = False
    return AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=watchdog)


def _feed_on_prompt(handle: AcpSessionHandle, *frames: JsonRpcMessage) -> None:
    """Deliver ``frames`` when the prompt is sent, not before.

    ``prompt()`` drains the queue at turn start (leftovers from an abandoned turn
    must not bleed into this one), so anything pre-queued is thrown away and the
    turn then sits idle until its timeout — a test written that way passes or
    fails for reasons unrelated to what it claims to check. Feeding from
    ``send_request`` also matches the real order: frames arrive *after* the
    prompt goes out.
    """

    async def _send(*_a: object, **_k: object) -> int:
        for f in frames:
            handle._queue.put_nowait(f)
        return 99

    handle._runtime.send_request = AsyncMock(side_effect=_send)


def _tool_call_frame() -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sA",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc-1",
                "title": "bash",
                "kind": "execute",
                "rawInput": {"command": "make test"},
            },
        },
    )


def _permission_frame() -> JsonRpcMessage:
    return JsonRpcMessage(
        id=7,
        method=METHOD_REQUEST_PERMISSION,
        params={
            "sessionId": "sA",
            "toolCall": {"toolCallId": "tc-1", "title": "bash", "kind": "execute"},
            "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow"}],
        },
    )


# ── Park accounting is observable from outside the turn ──────────────────────


@pytest.mark.asyncio
async def test_parked_for_secs_reports_a_consumer_holding_an_event():
    """While the consumer holds an event, the park is readable from outside.

    This is the signal the in-band arm cannot produce about itself, so it has to
    be an attribute rather than a local of the generator frame.
    """
    handle = _make_handle()
    _feed_on_prompt(handle, _tool_call_frame())

    assert handle.parked_for_secs() == 0.0  # no turn yet

    agen = handle.prompt("hi", timeout=2.0)
    ev = await agen.__anext__()  # consumer now holds the tool-call event
    assert "make test" in ev.tool_input, "parked on the real event, not a timeout"
    await asyncio.sleep(0.05)

    parked = handle.parked_for_secs()
    assert parked >= 0.04, f"park not observable from outside (got {parked})"

    await agen.aclose()
    # An abandoned generator unwinds with GeneratorExit; the `finally` must clear
    # the marker, or the handle reads as parked since the abandonment forever.
    assert handle.parked_for_secs() == 0.0


@pytest.mark.asyncio
async def test_park_finally_survives_none_parked_since():
    """GeneratorExit when _parked_since is already None must not raise TypeError.

    Regression: if a turn boundary resets _parked_since before a lingering
    generator is GC'd, the finally block attempted `time.monotonic() - None`,
    raising TypeError and producing ERROR-level crash_guard log noise.
    """
    handle = _make_handle()
    _feed_on_prompt(handle, _tool_call_frame())

    agen = handle.prompt("hi", timeout=2.0)
    await agen.__anext__()  # consumer holds the event (parks)

    # Simulate the race: a turn boundary resets park state before GC fires.
    handle._parked_since = None
    handle._parked_total = 0.0

    # aclose() triggers GeneratorExit inside the yield -> finally fires.
    # Before the fix this raised TypeError; now it must be a no-op.
    await agen.aclose()

    # No crash, park total unchanged (the abandoned park is silently discarded).
    assert handle._parked_total == 0.0
    assert handle._parked_since is None


@pytest.mark.asyncio
async def test_park_state_does_not_carry_across_turns():
    """Park state is per-turn. Carrying it would charge the previous turn's
    consumer time to this one, and a permission left unanswered when the last
    turn died would mask every later stall."""
    handle = _make_handle()
    handle._parked_total = 12.0
    handle._parked_since = time.monotonic() - 5.0
    handle._awaiting_permission = True
    _feed_on_prompt(handle, _tool_call_frame())

    agen = handle.prompt("hi", timeout=2.0)
    await agen.__anext__()

    assert handle._awaiting_permission is False
    # Reset to 0 at turn start, so the only park counted is this turn's.
    assert handle._parked_total < 1.0
    await agen.aclose()


# ── The idle clocks must not charge consumer time to the backend ─────────────


@pytest.mark.asyncio
async def test_a_long_consumer_park_does_not_trip_the_tool_stall_arm():
    """A park many times the suspect window must not read as backend silence.

    Without the correction this is the "cancelled moments after the human
    approved it" bug: the arm resumes, computes an idle interval that includes
    the whole park, and acts on an UNKNOWN verdict.

    Windows are chosen so the test DISCRIMINATES. The park (1.0s) is longer than
    the suspect window (0.9s) so an uncorrected clock acts, while the genuine
    post-park silence (~0.4s, bounded by the prompt deadline) stays under it so a
    corrected clock does not. Both margins are ~0.5s, so a sleep overrunning on a
    loaded runner cannot flip the verdict either way.
    """
    wd = WatchdogSettings(
        check_after_secs=0.05,
        stale_window_secs=0.9,
        tool_stall_suspect_secs=0.9,
        tool_stall_hard_cap_secs=5.0,
        model_silent_probe_secs=2.0,
    )
    handle = _make_handle(watchdog=wd)
    _feed_on_prompt(handle, _tool_call_frame())

    stop_reasons: list[str] = []
    agen = handle.prompt("hi", timeout=1.4)
    ev = await agen.__anext__()
    assert handle._tool_dispatched is True, "precondition: the tool clock is armed"
    assert "make test" in ev.tool_input

    await asyncio.sleep(1.0)  # park, longer than tool_stall_suspect_secs

    async for later in agen:
        stop_reasons.append(later.stop_reason)

    assert STOP_REASON_TOOL_STALL not in stop_reasons, (
        "the consumer's park was charged to the backend as silence"
    )
    handle._runtime.send_notification.assert_not_awaited()  # no probe cancel


@pytest.mark.asyncio
async def test_backend_silence_without_a_park_still_trips_the_arm():
    """The correction must not disarm the watchdog: with no park, an idle tool
    clock past the suspect window still ends the turn. Guards against "fixing"
    the misattribution by simply never acting."""
    handle = _make_handle()
    handle._queue.put_nowait(_tool_call_frame())

    # Drive the generator directly: no consumer park is recorded, so the idle
    # interval is pure backend silence.
    events = [ev async for ev in handle._dispatch_events(1, timeout=0.4)]

    assert any(ev.stop_reason == STOP_REASON_TOOL_STALL for ev in events), (
        "a genuine tool stall must still be caught"
    )


# ── Waiting on a human is a distinct state, not a stall ─────────────────────


@pytest.mark.asyncio
async def test_awaiting_permission_is_set_before_the_yield_and_cleared_on_answer():
    """An observer reading a park mid-flight must be able to tell "waiting for a
    human" from "the consumer stopped pulling"."""
    handle = _make_handle()
    _feed_on_prompt(handle, _permission_frame())

    agen = handle.prompt("hi", timeout=2.0)
    ev = await agen.__anext__()

    assert handle.awaiting_permission is True, "must be marked BEFORE the yield"

    await handle.approve_tool(ev.request_id)
    assert handle.awaiting_permission is False
    await agen.aclose()


@pytest.mark.asyncio
async def test_answering_restarts_the_park_clock_so_the_human_wait_is_not_reported():
    """The consumer is still parked when it answers: it resolves the approval and
    then finishes its own branch before coming back. An observer must see only the
    consumer's own time, not the person's thinking time — the same misattribution
    the in-band clocks were fixed to avoid, one layer out."""
    handle = _make_handle()
    _feed_on_prompt(handle, _permission_frame())

    agen = handle.prompt("hi", timeout=2.0)
    ev = await agen.__anext__()

    # Backdate the park to stand in for a long human wait.
    handle._parked_since = time.monotonic() - 600.0
    assert handle.parked_for_secs() >= 600.0

    await handle.approve_tool(ev.request_id)

    assert handle.parked_for_secs() < 1.0, "human wait still counted as consumer time"
    # …but the in-band correction must still see the WHOLE park: from the
    # runtime's point of view every second of it was consumer time.
    assert handle._parked_total >= 600.0
    await agen.aclose()


@pytest.mark.asyncio
async def test_rejecting_also_ends_the_human_wait():
    handle = _make_handle()
    _feed_on_prompt(handle, _permission_frame())

    agen = handle.prompt("hi", timeout=2.0)
    ev = await agen.__anext__()
    await handle.reject_tool(ev.request_id)

    assert handle.awaiting_permission is False
    await agen.aclose()


# ── The out-of-band stuck_turn hook ─────────────────────────────────────────


def _make_manager():
    from kiro_crew.session import SessionManager

    cfg = MagicMock()
    cfg.session.pool_size = 0
    cfg.session.pool_agent = ""
    cfg.session.pool_ttl_secs = 0
    cfg.session.watchdog_rss_max_mb = 0
    return SessionManager(cfg=cfg, provider_factory=None)


def _registered(manager, handle, *, busy: bool) -> None:
    """Register one session whose provider carries a REAL AcpSessionHandle.

    Deliberately not a MagicMock handle: the hook reads ``parked_for_secs()`` and
    ``awaiting_permission``, and a mock would answer both regardless of whether
    the production class actually exposes them — the test would pass while the
    hook was dead at runtime.
    """
    sess = MagicMock()
    sess.semaphore = MagicMock()
    sess.semaphore.locked.return_value = busy
    sess.provider = MagicMock()
    sess.provider._handle = handle
    manager._sessions["dashboard:1"] = sess


@pytest.mark.asyncio
async def test_stuck_turn_reports_a_parked_consumer(caplog):
    manager = _make_manager()
    handle = _make_handle()
    handle._parked_since = time.monotonic() - 600.0  # parked past the threshold
    _registered(manager, handle, busy=True)

    seen: list[tuple[str, float]] = []
    manager.on_stuck_turn = lambda key, parked: seen.append((key, parked))

    with caplog.at_level("WARNING"):
        await manager._stuck_turn_check()

    assert [k for k, _ in seen] == ["dashboard:1"]
    assert seen[0][1] >= 600.0
    assert "has not been pulled" in caplog.text


@pytest.mark.asyncio
async def test_stuck_turn_excludes_a_turn_waiting_on_a_human():
    """``agent.tool_approval_timeout_secs`` owns that bound. Acting here too
    would put two components on different budgets racing the same wait."""
    manager = _make_manager()
    handle = _make_handle()
    handle._parked_since = time.monotonic() - 600.0
    handle._awaiting_permission = True
    _registered(manager, handle, busy=True)

    seen: list[str] = []
    manager.on_stuck_turn = lambda key, parked: seen.append(key)

    await manager._stuck_turn_check()

    assert seen == []


@pytest.mark.asyncio
async def test_stuck_turn_skips_a_session_with_no_turn_in_flight():
    """The semaphore is the only in-flight signal at this layer, and a park is
    meaningless without one."""
    manager = _make_manager()
    handle = _make_handle()
    handle._parked_since = time.monotonic() - 600.0
    _registered(manager, handle, busy=False)

    seen: list[str] = []
    manager.on_stuck_turn = lambda key, parked: seen.append(key)

    await manager._stuck_turn_check()

    assert seen == []


@pytest.mark.asyncio
async def test_stuck_turn_ignores_a_short_park():
    manager = _make_manager()
    handle = _make_handle()
    handle._parked_since = time.monotonic() - 1.0
    _registered(manager, handle, busy=True)

    seen: list[str] = []
    manager.on_stuck_turn = lambda key, parked: seen.append(key)

    await manager._stuck_turn_check()

    assert seen == []


@pytest.mark.asyncio
async def test_the_same_park_is_reported_once_not_every_tick():
    """The report threshold sits below the cleanup tick so a park is caught on the
    first pass that sees it. A park outliving the tick must therefore be latched,
    or every pass re-warns and re-fires the callback — and a consumer that DMs the
    user would inherit that dedup burden."""
    manager = _make_manager()
    handle = _make_handle()
    handle._parked_since = time.monotonic() - 600.0
    _registered(manager, handle, busy=True)

    seen: list[str] = []
    manager.on_stuck_turn = lambda key, parked: seen.append(key)

    await manager._stuck_turn_check()
    await manager._stuck_turn_check()
    await manager._stuck_turn_check()

    assert seen == ["dashboard:1"], f"same park reported {len(seen)} times"


@pytest.mark.asyncio
async def test_a_new_park_on_the_same_session_reports_again():
    """The latch keys on the park's identity, not the session, so it must not
    silence a genuinely new park later in the session's life."""
    manager = _make_manager()
    handle = _make_handle()
    handle._parked_since = time.monotonic() - 600.0
    _registered(manager, handle, busy=True)

    seen: list[str] = []
    manager.on_stuck_turn = lambda key, parked: seen.append(key)

    await manager._stuck_turn_check()
    # Consumer came back, then parked again on a later event.
    handle._parked_since = time.monotonic() - 700.0
    await manager._stuck_turn_check()

    assert len(seen) == 2


@pytest.mark.asyncio
async def test_the_latch_is_dropped_once_the_park_ends():
    """A stale latch entry must not accumulate for a session that stopped
    parking, nor silence it if it parks again."""
    manager = _make_manager()
    handle = _make_handle()
    handle._parked_since = time.monotonic() - 600.0
    _registered(manager, handle, busy=True)
    manager.on_stuck_turn = lambda key, parked: None

    await manager._stuck_turn_check()
    assert "dashboard:1" in manager._stuck_reported

    handle._parked_since = None  # consumer resumed
    await manager._stuck_turn_check()
    assert manager._stuck_reported == {}


@pytest.mark.asyncio
async def test_stuck_turn_survives_a_raising_callback():
    """An observer must never break the cleanup pass it rides on."""
    manager = _make_manager()
    handle = _make_handle()
    handle._parked_since = time.monotonic() - 600.0
    _registered(manager, handle, busy=True)

    def _boom(key: str, parked: float) -> None:
        raise RuntimeError("consumer blew up")

    manager.on_stuck_turn = _boom

    await manager._stuck_turn_check()  # must not raise


@pytest.mark.asyncio
async def test_stuck_turn_tolerates_a_provider_without_a_handle():
    """Non-ACP providers have no handle; the hook must skip, not crash."""
    manager = _make_manager()
    sess = MagicMock()
    sess.semaphore = MagicMock()
    sess.semaphore.locked.return_value = True
    sess.provider = object()  # no _handle attribute at all
    manager._sessions["dashboard:1"] = sess

    seen: list[str] = []
    manager.on_stuck_turn = lambda key, parked: seen.append(key)

    await manager._stuck_turn_check()

    assert seen == []


# ── The signal reaches a user, not just the journal ─────────────────────────


def test_the_notice_states_the_duration_and_floors_at_one_minute():
    """The duration is the point — it is what separates this from a slow turn.
    Flooring at 1 matters because integer division of a minutes-scale threshold
    would otherwise render "0 min", which only reads as a bug."""
    from kiro_crew.dashboard.state import stuck_turn_notice

    assert "10 min" in stuck_turn_notice(600.0)
    assert "1 min" in stuck_turn_notice(59.0)  # floored, never "0 min"
    assert "0 min" not in stuck_turn_notice(0.5)


def test_the_notice_promises_no_remedy_it_did_not_perform():
    """The hook cancels nothing, so the notice must not imply recovery is under
    way — what the turn is blocked on is not knowable from where it was seen."""
    from kiro_crew.dashboard.state import stuck_turn_notice

    text = stuck_turn_notice(600.0).lower()
    assert "nothing has been cancelled" in text
    for promise in ("retrying", "recovering", "will recover", "restarted"):
        assert promise not in text, f"notice implies a remedy it does not perform: {promise}"


def test_the_dashboard_subscribes_to_the_hook():
    """The callback must be the attribute the session layer actually fires.

    A rename on either side would leave the seam silently unsubscribed, and that
    subscription is the whole user-visible payoff — without it the feature is one
    WARNING line in a journal nobody reads.
    """
    import inspect

    from kiro_crew.dashboard import state as state_mod
    from kiro_crew.session import SessionManager

    src = inspect.getsource(state_mod.DashboardState)
    assert "self.sessions.on_stuck_turn = " in src, "dashboard no longer subscribes"
    # …and the name it assigns really exists on the producer.
    assert "on_stuck_turn" in SessionManager.__init__.__code__.co_names
