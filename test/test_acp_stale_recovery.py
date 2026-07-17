"""Tests for in-gateway stale-turn auto-recovery on AcpSessionHandle.

Covers the two-stage fix:

1. **Fold-in (fix #1):** the per-session stale check must fold the runtime's
   stderr/keepalive clock (``_runtime._last_activity``) into the idle window, so
   a turn that streams its final text and then thinks silently on stdout (while
   still emitting ``thinking_tokens`` on stderr) is NOT falsely declared stale.
   Mirrors ``TestAcpClientStaleTurn`` for the ``AcpClient`` path.

2. **Cancel-ack probe → auto-recovery:** a genuine stale (silent on BOTH clocks)
   is probed via ``session/cancel`` rather than blindly ended. If kiro acks
   (done-but-missing-frame) the turn completes normally — no re-drive. If the
   cancel goes unacked past the grace window it is a confirmed wedge and the
   handle signals ``STOP_REASON_STALE_RECOVER`` so the dashboard reset+resume+
   continue-nudge path recovers the turn in place. The pre-existing user-cancel
   path (not a stale probe) still yields ``error: cancel unacked`` unchanged.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    STOP_REASON_CANCELLED,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
    JsonRpcMessage,
)
from kiro_crew.dashboard.state import (
    STALE_RECOVERY_PREFIX,
    TOOL_STALL_RECOVERY_PREFIX,
    build_stale_recovery_prompt,
    build_tool_stall_recovery_prompt,
    extract_log_redirect_target,
)

# Tight windows for tests: consult the oracle almost immediately and act on
# UNKNOWN verdicts after 50ms idle.
_FAST_WD = WatchdogSettings(
    check_after_secs=0.01,
    stale_window_secs=0.05,
    tool_stall_suspect_secs=0.05,
    tool_stall_hard_cap_secs=1.0,
    model_silent_probe_secs=0.5,
)


def _make_handle(
    last_activity: float | None = None,
    watchdog: WatchdogSettings = _FAST_WD,
) -> AcpSessionHandle:
    """A handle over a fake runtime with a controllable ``_last_activity``.

    ``rt.pid`` is None so the liveness oracle returns UNKNOWN ("no runtime
    pid") and the timeout-governed UNKNOWN class — the legacy-equivalent
    behavior these tests exercise — applies.
    """
    rt = MagicMock()
    rt._last_activity = last_activity if last_activity is not None else time.monotonic()
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=watchdog)
    handle._turn_done.clear()  # a turn is in flight
    handle._stale_eligible = True  # text was streamed → staleness eligible
    return handle


async def _drain(handle: AcpSessionHandle, req_id: int, timeout: float) -> list:
    return [ev async for ev in handle._dispatch_events(req_id, timeout)]


# ── Fix #1: stderr fold-in prevents false stale ──────────────────────────────


@pytest.mark.asyncio
async def test_fresh_stderr_activity_prevents_stale_probe():
    """Recent ``_runtime._last_activity`` (thinking on stderr) keeps the turn
    alive: no probe cancel is sent even though stdout is silent."""
    handle = _make_handle()

    # Emulate the stderr drain refreshing _last_activity while stdout is silent.
    stop = asyncio.Event()

    async def refresher():
        while not stop.is_set():
            handle._runtime._last_activity = time.monotonic()
            await asyncio.sleep(0.02)

    task = asyncio.ensure_future(refresher())
    try:
        events = await _drain(handle, req_id=1, timeout=0.3)
    finally:
        stop.set()
        await task

    assert handle._stale_probe is False  # fold-in: not falsely stale
    handle._runtime.send_notification.assert_not_awaited()  # no probe cancel
    # An overall-timeout terminal event may be yielded, but never a stale-recover.
    assert all(ev.stop_reason != STOP_REASON_STALE_RECOVER for ev in events)


# ── Fix #2a: genuine stale → probe via session/cancel ────────────────────────


@pytest.mark.asyncio
async def test_genuine_stale_probes_via_cancel():
    """Silence on BOTH clocks trips the stale guard (UNKNOWN verdict past the
    stale window), which PROBES via session/cancel (sets _stale_probe) rather
    than blindly ending the turn."""
    # _last_activity is old and never refreshes → genuinely silent everywhere.
    handle = _make_handle(last_activity=time.monotonic() - 10.0)

    await _drain(handle, req_id=1, timeout=0.2)

    assert handle._stale_probe is True
    handle._runtime.send_notification.assert_awaited()  # session/cancel probe
    assert handle._runtime.send_notification.await_args.args[0] == "session/cancel"


# ── Fix #2b: unacked probe → STOP_REASON_STALE_RECOVER ───────────────────────


@pytest.mark.asyncio
async def test_unacked_stale_probe_signals_recovery():
    """A stale turn probed via cancel that never acks within the grace window is
    a confirmed wedge → yields STOP_REASON_STALE_RECOVER for the dashboard to
    auto-recover (reset+resume+continue-nudge)."""
    handle = _make_handle()
    # Simulate: probe cancel already sent, grace already elapsed, no ack.
    handle._stale_probe = True
    handle._cancelled = True
    handle._cancel_ts = time.monotonic() - 1.0
    handle._cancel_grace_secs = 0.05

    events = await _drain(handle, req_id=1, timeout=5.0)

    assert len(events) == 1
    assert events[0].kind == EVENT_COMPLETE
    assert events[0].stop_reason == STOP_REASON_STALE_RECOVER


@pytest.mark.asyncio
async def test_acked_stale_probe_completes_normally_no_redrive():
    """A probed turn that ACKs the cancel (done-but-missing-frame) completes via
    the normal turn-complete branch — NOT STALE_RECOVER — so it is never
    re-driven (no double-answer)."""
    handle = _make_handle()
    handle._stale_probe = True
    handle._cancelled = True
    handle._cancel_ts = time.monotonic()  # grace NOT yet exceeded
    handle._cancel_grace_secs = 10.0
    # kiro acks: the prompt response frame arrives on the queue.
    handle._queue.put_nowait(
        JsonRpcMessage(id=1, result={"stopReason": "end_turn"})
    )

    events = await _drain(handle, req_id=1, timeout=5.0)

    assert len(events) == 1
    assert events[0].kind == EVENT_COMPLETE
    assert events[0].stop_reason == "end_turn"
    assert events[0].stop_reason != STOP_REASON_STALE_RECOVER


# ── _stale_probe is single-shot: consumed on use, superseded by a user cancel ─


@pytest.mark.asyncio
async def test_stale_probe_flag_consumed_on_reclassification():
    """The reclassification branch CONSUMES ``_stale_probe`` — after a probe-ack
    is rewritten to STALE_RECOVER the flag is clear, so nothing later in the
    session can be misattributed to an already-spent probe."""
    handle = _make_handle()
    handle._stale_probe = True
    handle._cancelled = True
    handle._cancel_ts = time.monotonic()
    handle._cancel_grace_secs = 10.0
    handle._queue.put_nowait(
        JsonRpcMessage(id=1, result={"stopReason": STOP_REASON_CANCELLED})
    )

    events = await _drain(handle, req_id=1, timeout=5.0)

    assert events[0].stop_reason == STOP_REASON_STALE_RECOVER
    assert handle._stale_probe is False  # consumed, not sticky


@pytest.mark.asyncio
async def test_genuine_cancel_supersedes_pending_probe():
    """A genuine (non-probe) ``cancel()`` arriving after a stale probe clears
    ``_stale_probe``: the eventual cancel ack surfaces as a USER cancellation,
    never reclassified to auto-recovery against the user's intent."""
    handle = _make_handle()
    # Watchdog probe already sent (probe-marked cancel sets the flag).
    await handle.cancel(_stale_probe=True)
    assert handle._stale_probe is True
    # User hits Stop before the probe ack lands — supersedes the probe.
    await handle.cancel()
    assert handle._stale_probe is False

    handle._queue.put_nowait(
        JsonRpcMessage(id=1, result={"stopReason": STOP_REASON_CANCELLED})
    )
    events = await _drain(handle, req_id=1, timeout=5.0)

    assert events[0].stop_reason == STOP_REASON_CANCELLED  # NOT stale-recover


@pytest.mark.asyncio
async def test_stale_probe_flag_consumed_on_wedge_recovery():
    """The unresponsive-cancel wedge branch also consumes ``_stale_probe``
    (single-shot, mirroring the reclassification branch)."""
    handle = _make_handle()
    handle._stale_probe = True
    handle._cancelled = True
    handle._cancel_ts = time.monotonic() - 1.0
    handle._cancel_grace_secs = 0.05

    events = await _drain(handle, req_id=1, timeout=5.0)

    assert events[0].stop_reason == STOP_REASON_STALE_RECOVER
    assert handle._stale_probe is False  # consumed, not sticky


@pytest.mark.asyncio
async def test_user_cancel_unacked_unchanged():
    """Regression: an ordinary (non-stale-probe) unacked cancel still yields
    'error: cancel unacked' — the stale-recovery path must not hijack it."""
    handle = _make_handle()
    handle._stale_probe = False  # a user/stop cancel, not a stale probe
    handle._cancelled = True
    handle._cancel_ts = time.monotonic() - 1.0
    handle._cancel_grace_secs = 0.05

    events = await _drain(handle, req_id=1, timeout=5.0)

    assert len(events) == 1
    assert events[0].stop_reason == "error: cancel unacked"


# ── The continue-nudge injected on recovery ──────────────────────────────────


def test_build_stale_recovery_prompt_says_continue_not_restart():
    body = build_stale_recovery_prompt()
    low = body.lower()
    assert "continue" in low
    assert "not a user action" in low  # framed as a system stall, not a cancel
    assert "restart" in low  # explicitly tells the model not to restart
    assert STALE_RECOVERY_PREFIX.startswith("[") and STALE_RECOVERY_PREFIX.endswith("]")


# ── Verdict policy: WORKING is never acted on; DEAD acts immediately ─────────


class _SilentQueue:
    """Queue that always times out, so every poll is a watchdog tick."""

    def __init__(self, tick: float = 0.02) -> None:
        self._tick = tick

    async def get(self):
        await asyncio.sleep(self._tick)
        raise asyncio.TimeoutError


@pytest.mark.asyncio
async def test_working_verdict_never_probed_at_any_idle():
    """A WORKING model-wait verdict suppresses the stale probe far past every
    window — THE success criterion: healthy-but-slow is never touched."""
    handle = _make_handle(last_activity=time.monotonic() - 100.0)
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_model_wait = lambda pid: ("working", "backend bytes flowing")

    await _drain(handle, req_id=1, timeout=0.3)

    assert handle._stale_probe is False
    handle._runtime.send_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_working_verdict_never_cancels_tool_at_any_idle():
    """A WORKING tool verdict (live matched build child) suppresses the stall
    cancel far past the suspect window — a 30-min silent build runs untouched."""
    handle = _make_handle()
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_tool = lambda pid, tool: ("working", "shell child 1234 alive")
    handle._inflight_tool = None  # _consult_tool_oracle guards; force via oracle
    from kiro_crew.acp.liveness import ToolCallState

    handle._inflight_tool = ToolCallState(title="bash", command="long-build > build.log 2>&1")

    events = await _drain(handle, req_id=1, timeout=0.3)

    handle._runtime.send_notification.assert_not_awaited()
    assert all(ev.stop_reason != STOP_REASON_TOOL_STALL for ev in events)


@pytest.mark.asyncio
async def test_dead_tool_verdict_cancels_within_one_tick():
    """A DEAD tool verdict (child exited, no result frame) acts immediately —
    no waiting for the 600s-equivalent suspect window."""
    from kiro_crew.acp.liveness import ToolCallState

    # Huge UNKNOWN windows: only a DEAD verdict can trigger the cancel here.
    wd = WatchdogSettings(
        check_after_secs=0.01,
        stale_window_secs=999.0,
        tool_stall_suspect_secs=999.0,
        tool_stall_hard_cap_secs=999.0,
    )
    handle = _make_handle(watchdog=wd)
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._inflight_tool = ToolCallState(
        title="bash", command="long-build release > build.log 2>&1", is_shell=True
    )
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_tool = lambda pid, tool: (
        "dead", "shell child 4242 exited 16s ago, no result frame"
    )

    events = await _drain(handle, req_id=1, timeout=5.0)

    handle._runtime.send_notification.assert_awaited_once()
    assert handle._runtime.send_notification.await_args.args[0] == "session/cancel"
    assert events and events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    # Stall metadata for the chat_runner recovery nudge rides the event.
    assert events[-1].title == "bash"
    assert "build.log" in events[-1].tool_input
    assert "idle_secs=" in events[-1].text


@pytest.mark.asyncio
async def test_stuck_input_verdict_flagged_in_evidence():
    """A STUCK_INPUT verdict acts immediately and the evidence marker survives
    on the terminal event so the recovery nudge can name the cause."""
    from kiro_crew.acp.liveness import ToolCallState

    wd = WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=999.0,
                          tool_stall_hard_cap_secs=999.0)
    handle = _make_handle(watchdog=wd)
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._inflight_tool = ToolCallState(title="bash", command="ssh host", is_shell=True)
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_tool = lambda pid, tool: (
        "stuck_input", "stuck_input: pid 7 blocked reading /dev/tty with flat subtree"
    )

    events = await _drain(handle, req_id=1, timeout=5.0)

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert "stuck_input" in events[-1].text


@pytest.mark.asyncio
async def test_unknown_tool_verdict_waits_for_suspect_window():
    """UNKNOWN tool verdicts stay in the timeout-governed class: no cancel
    before tool_stall_suspect_secs, cancel after."""
    from kiro_crew.acp.liveness import ToolCallState

    wd = WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=0.2,
                          tool_stall_hard_cap_secs=999.0)
    handle = _make_handle(watchdog=wd)
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._inflight_tool = ToolCallState(title="mystery", command="", is_shell=False)
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_tool = lambda pid, tool: ("unknown", "mcp subtree flat")

    # Below the suspect window: no action.
    events = await _drain(handle, req_id=1, timeout=0.1)
    handle._runtime.send_notification.assert_not_awaited()
    assert all(ev.stop_reason != STOP_REASON_TOOL_STALL for ev in events)

    # Past the suspect window: cancelled.
    handle._turn_done.clear()
    events = await _drain(handle, req_id=1, timeout=5.0)
    handle._runtime.send_notification.assert_awaited()
    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL


@pytest.mark.asyncio
async def test_established_flat_model_wait_gets_extended_window():
    """UNKNOWN with the established_flat evidence tag (probably a non-streamed
    server-side think) is probed only past model_silent_probe_secs, not the
    ordinary stale window."""
    wd = WatchdogSettings(check_after_secs=0.01, stale_window_secs=0.05,
                          model_silent_probe_secs=10.0, tool_stall_hard_cap_secs=999.0)
    handle = _make_handle(last_activity=time.monotonic() - 100.0, watchdog=wd)
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_model_wait = lambda pid: (
        "unknown", "established_flat: io +0B cpu +0t"
    )

    # Well past stale_window (0.05) but far below the extended window (10s):
    await _drain(handle, req_id=1, timeout=0.3)

    assert handle._stale_probe is False
    handle._runtime.send_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_dead_model_wait_probes_immediately():
    """A DEAD model-wait verdict (no backend socket, flat counters — the
    done-but-lost-frame wedge) probes without waiting for the stale window."""
    wd = WatchdogSettings(check_after_secs=0.01, stale_window_secs=999.0,
                          model_silent_probe_secs=999.0, tool_stall_hard_cap_secs=999.0)
    handle = _make_handle(last_activity=time.monotonic() - 100.0, watchdog=wd)
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_model_wait = lambda pid: (
        "dead", "no established backend socket and flat counters"
    )

    await _drain(handle, req_id=1, timeout=0.3)

    assert handle._stale_probe is True
    handle._runtime.send_notification.assert_awaited()


# ── Probe-ack reclassification (the non-lethal harness) ──────────────────────


@pytest.mark.asyncio
async def test_probe_ack_cancelled_reclassified_to_stale_recover():
    """kiro acks the probe cancel on a LIVE turn with stopReason=cancelled —
    the original session-killer. It must be reclassified to STALE_RECOVER so
    the dashboard auto-recovers instead of logging a user cancellation."""
    handle = _make_handle()
    handle._stale_probe = True
    handle._cancelled = True
    handle._cancel_ts = time.monotonic()
    handle._cancel_grace_secs = 10.0
    handle._queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "cancelled"}))

    events = await _drain(handle, req_id=1, timeout=5.0)

    assert len(events) == 1
    assert events[0].stop_reason == STOP_REASON_STALE_RECOVER


@pytest.mark.asyncio
async def test_genuine_user_cancel_not_reclassified():
    """A user cancel (no _stale_probe) acked as cancelled stays 'cancelled' —
    the reclassification must never hijack real user stops."""
    handle = _make_handle()
    handle._stale_probe = False
    handle._cancelled = True
    handle._cancel_ts = time.monotonic()
    handle._cancel_grace_secs = 10.0
    handle._queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "cancelled"}))

    events = await _drain(handle, req_id=1, timeout=5.0)

    assert events[0].stop_reason == "cancelled"


# ── Wait-tool declared-duration contract ─────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_tool_declared_duration_reads_working():
    """A wait(1800) is WORKING by contract until its declared duration + slack
    elapses — the real oracle (not a stub) must defer the stall cancel."""
    from kiro_crew.acp.liveness import ToolCallState

    wd = WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=0.05,
                          tool_stall_hard_cap_secs=999.0)
    handle = _make_handle(watchdog=wd)
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._runtime.pid = 999999  # oracle path needs a pid to reach the contract
    handle._inflight_tool = ToolCallState(
        title="wait", command='{"seconds": 1800, "reason": "babysit"}',
        dispatch_ts=time.monotonic(), is_shell=False,
    )
    handle._queue = _SilentQueue()  # type: ignore[assignment]

    events = await _drain(handle, req_id=1, timeout=0.3)

    handle._runtime.send_notification.assert_not_awaited()
    assert all(ev.stop_reason != STOP_REASON_TOOL_STALL for ev in events)


# ── The continue-nudge injected on tool-stall recovery ───────────────────────


def test_build_tool_stall_recovery_prompt_basics():
    body = build_tool_stall_recovery_prompt("bash", 613, command="long-build release > build.log 2>&1")
    low = body.lower()
    assert "not a user action" in low
    assert "partial results" in low
    assert "build.log" in body  # redirect target extracted into the log hint
    assert "tail" in low and "cat" in low  # tail, don't cat
    assert "re-run the whole task" in low or "re-run" in low
    assert TOOL_STALL_RECOVERY_PREFIX.startswith("[") and TOOL_STALL_RECOVERY_PREFIX.endswith("]")


def test_build_tool_stall_recovery_prompt_stuck_input():
    body = build_tool_stall_recovery_prompt("bash", 90, command="ssh host cmd", stuck_input=True)
    assert "non-interactively" in body
    assert "--no-input" in body or "-y" in body


def test_extract_log_redirect_target():
    assert extract_log_redirect_target("long-build > build.log 2>&1") == "build.log"
    assert extract_log_redirect_target("cmd >> out.txt") == "out.txt"
    assert extract_log_redirect_target("cmd 2>&1") == ""  # fd-dup only — no file
    assert extract_log_redirect_target("cmd > /dev/null 2>&1") == ""
    assert extract_log_redirect_target("plain command") == ""
