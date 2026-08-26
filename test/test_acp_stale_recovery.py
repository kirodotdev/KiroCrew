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
from concurrent.futures import Future as ThreadFuture
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.liveness import (
    VERDICT_DEAD,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    ToolCallState,
)
from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    METHOD_SESSION_UPDATE,
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


class _FreshActivityRuntime:
    """Runtime double whose ``_last_activity`` always reads as the current instant.

    Stands in for a stderr drain that keeps refreshing the keepalive clock while
    stdout is silent.

    This replaces a refresher task that rewrote the attribute every 20ms against
    the 50ms ``stale_window_secs`` above. That left only 30ms of margin, and an
    ``asyncio.sleep`` on a loaded CI runner overruns easily — Windows timer
    granularity alone is about 15.6ms — at which point the attribute looks stale
    and the stale guard trips, failing the test for a reason unrelated to the
    behaviour under test. A property cannot be late.

    ``AcpSessionHandle`` only ever READS ``_runtime._last_activity`` (the writes
    live in the real runtime's own drain), so exposing it read-only is faithful.
    """

    def __init__(self) -> None:
        # pid None keeps the liveness oracle at UNKNOWN, matching _make_handle.
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.send_notification = AsyncMock()

    @property
    def _last_activity(self) -> float:
        return time.monotonic()


def _make_handle(
    last_activity: float | None = None,
    watchdog: WatchdogSettings = _FAST_WD,
    fresh_activity: bool = False,
) -> AcpSessionHandle:
    """A handle over a fake runtime with a controllable ``_last_activity``.

    ``rt.pid`` is None so the liveness oracle returns UNKNOWN ("no runtime
    pid") and the timeout-governed UNKNOWN class — the legacy-equivalent
    behavior these tests exercise — applies.

    With ``fresh_activity=True`` the clock reports "now" on every read, which is
    what a continuously-active stderr drain looks like.
    """
    rt: object
    if fresh_activity:
        rt = _FreshActivityRuntime()
    else:
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
    # The activity clock reports "now" on every read, which is what a
    # continuously-active stderr drain looks like -- and unlike a refresher task
    # it cannot be late when the event loop is contended.
    handle = _make_handle(fresh_activity=True)

    events = await _drain(handle, req_id=1, timeout=0.3)

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

    def qsize(self) -> int:
        """Always empty — no frames accumulate in the silent queue."""
        return 0


class _SilentQueueWithBacklog:
    """Queue that times out when empty but delivers items added via put_nowait.

    Used for TOCTOU Path A tests: a frame put_nowait()-ed DURING the oracle
    await stays in the backlog. qsize() reflects the actual count, so the
    TOCTOU guard's queue-depth check fires correctly. Once items are present
    they are delivered immediately on the next get(), so the loop consumes
    them and updates last_data_ts normally.
    """

    def __init__(self, tick: float = 0.02) -> None:
        self._tick = tick
        self._items: list = []

    async def get(self):
        if self._items:
            return self._items.pop(0)
        await asyncio.sleep(self._tick)
        raise asyncio.TimeoutError

    def put_nowait(self, item) -> None:
        self._items.append(item)

    def qsize(self) -> int:
        return len(self._items)


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
async def test_established_flat_tool_verdict_narrows_to_model_silent_window():
    """UNKNOWN tool evidence tagged established_flat (flat subtree, backend
    socket on the runtime itself — an LLM turn riding inside a tool, e.g.
    kiro-cli use_subagent) uses min(model_silent_probe_secs,
    tool_stall_suspect_secs) as the effective suspect window instead of the
    build-scale forbearance."""
    from kiro_crew.acp.liveness import ToolCallState

    # Build-scale suspect window (999s) but a tight model-silent budget: only
    # the narrowed window can trigger the cancel inside this test's runtime.
    wd = WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=999.0,
                          tool_stall_hard_cap_secs=999.0, model_silent_probe_secs=0.05)
    handle = _make_handle(watchdog=wd)
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._inflight_tool = ToolCallState(title="use_subagent", command="{}", is_shell=False)
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_tool = lambda pid, tool: (
        "unknown", "established_flat: mcp subtree flat (io +0B cpu +0t)"
    )

    events = await _drain(handle, req_id=1, timeout=5.0)

    handle._runtime.send_notification.assert_awaited()
    assert handle._runtime.send_notification.await_args.args[0] == "session/cancel"
    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL


@pytest.mark.asyncio
async def test_plain_flat_tool_verdict_keeps_full_suspect_window():
    """UNKNOWN tool evidence WITHOUT the established_flat tag (a quiet MCP
    tool / build) keeps the full tool_stall_suspect_secs — the narrowed
    model-silent window must never leak onto build-shaped stalls."""
    from kiro_crew.acp.liveness import ToolCallState

    # Tight model-silent budget, build-scale suspect window: if the narrowing
    # incorrectly applied here, the cancel would fire within this test.
    wd = WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=999.0,
                          tool_stall_hard_cap_secs=999.0, model_silent_probe_secs=0.05)
    handle = _make_handle(watchdog=wd)
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._inflight_tool = ToolCallState(title="mystery", command="", is_shell=False)
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_tool = lambda pid, tool: (
        "unknown", "mcp subtree flat (io +0B cpu +0t)"
    )

    events = await _drain(handle, req_id=1, timeout=0.3)

    handle._runtime.send_notification.assert_not_awaited()
    assert all(ev.stop_reason != STOP_REASON_TOOL_STALL for ev in events)


@pytest.mark.asyncio
async def test_narrowed_tool_window_never_exceeds_suspect_window():
    """min() semantics: when the per-agent suspect window is ALREADY tighter
    than model_silent_probe_secs, the tighter one governs an established_flat
    tool stall (an override can only ever narrow, never extend)."""
    from kiro_crew.acp.liveness import ToolCallState

    wd = WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=0.05,
                          tool_stall_hard_cap_secs=999.0, model_silent_probe_secs=999.0)
    handle = _make_handle(watchdog=wd)
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._inflight_tool = ToolCallState(title="use_subagent", command="{}", is_shell=False)
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_tool = lambda pid, tool: (
        "unknown", "established_flat: mcp subtree flat (io +0B cpu +0t)"
    )

    events = await _drain(handle, req_id=1, timeout=5.0)

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL


@pytest.mark.asyncio
async def test_working_verdict_still_never_acted_on_with_established_flat_windows():
    """Invariant: the narrowed window governs only UNKNOWN — a WORKING tool
    verdict is never cancelled regardless of the model-silent budget."""
    from kiro_crew.acp.liveness import ToolCallState

    wd = WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=0.05,
                          tool_stall_hard_cap_secs=0.05, model_silent_probe_secs=0.05)
    handle = _make_handle(watchdog=wd)
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._inflight_tool = ToolCallState(title="use_subagent", command="{}", is_shell=False)
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_tool = lambda pid, tool: ("working", "backend bytes flowing")

    events = await _drain(handle, req_id=1, timeout=0.3)

    handle._runtime.send_notification.assert_not_awaited()
    assert all(ev.stop_reason != STOP_REASON_TOOL_STALL for ev in events)


# ── Per-agent watchdog-window overrides (WatchdogSettings snapshot) ──────────


def _cfg_with_agent_overrides(monkeypatch, agents: dict) -> None:
    """Patch KiroCrewConfig.load() with a real default config carrying *agents*."""
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agents = agents
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))


def test_per_agent_override_narrows_watchdog_snapshot(monkeypatch):
    """A crew declaring watchdog_tool_stall_* overrides gets them in the
    WatchdogSettings snapshot; the untouched windows keep global values."""
    from kiro_crew.acp.session_handle import _load_watchdog_settings
    from kiro_crew.config.loader import KiroCrewAgentConfig

    _cfg_with_agent_overrides(monkeypatch, {
        "pr-reviewer": KiroCrewAgentConfig(
            kiro_agent="pr-reviewer-kiro",
            watchdog_tool_stall_suspect_secs=900.0,
            watchdog_tool_stall_hard_cap_secs=1800.0,
        ),
    })

    wd = _load_watchdog_settings("pr-reviewer")
    assert wd.tool_stall_suspect_secs == 900.0
    assert wd.tool_stall_hard_cap_secs == 1800.0
    # Non-overridden windows inherit the globals untouched.
    assert wd.model_silent_probe_secs == 900.0
    assert wd.stale_window_secs == 300.0


def test_per_agent_override_zero_inherits_global(monkeypatch):
    """0 (the default) inherits the global window — the same empty-inherits
    convention as the agent's model field."""
    from kiro_crew.acp.session_handle import _load_watchdog_settings
    from kiro_crew.config.loader import KiroCrewAgentConfig

    _cfg_with_agent_overrides(monkeypatch, {
        "builder": KiroCrewAgentConfig(kiro_agent="builder-kiro"),
    })

    wd = _load_watchdog_settings("builder")
    assert wd.tool_stall_suspect_secs == 3600.0
    assert wd.tool_stall_hard_cap_secs == 3600.0


def test_kiro_binding_name_is_not_resolved(monkeypatch):
    """Resolution is a direct lookup on the CANONICAL crew name only. A bound
    kiro agent name is a different namespace: it inherits the globals rather
    than being reverse-matched to the crew that binds it — the surface that
    owns the identity passes the crew name (see the chat_runner call sites),
    so no cross-namespace guessing happens here."""
    from kiro_crew.acp.session_handle import _load_watchdog_settings
    from kiro_crew.config.loader import KiroCrewAgentConfig

    _cfg_with_agent_overrides(monkeypatch, {
        "pr-reviewer": KiroCrewAgentConfig(
            kiro_agent="pr-reviewer-kiro",
            watchdog_tool_stall_suspect_secs=600.0,
        ),
    })

    assert _load_watchdog_settings("pr-reviewer-kiro").tool_stall_suspect_secs == 3600.0


def test_shared_binding_cannot_collide_canonical_names(monkeypatch):
    """Two crews binding the same kiro agent were a collision under the old
    cross-namespace match; canonical resolution keys each crew's overrides to
    its own name, so both apply independently."""
    from kiro_crew.acp.session_handle import _load_watchdog_settings
    from kiro_crew.config.loader import KiroCrewAgentConfig

    _cfg_with_agent_overrides(monkeypatch, {
        "a": KiroCrewAgentConfig(kiro_agent="shared", watchdog_tool_stall_suspect_secs=60.0),
        "b": KiroCrewAgentConfig(kiro_agent="shared", watchdog_tool_stall_suspect_secs=120.0),
    })

    assert _load_watchdog_settings("a").tool_stall_suspect_secs == 60.0
    assert _load_watchdog_settings("b").tool_stall_suspect_secs == 120.0


def test_handle_snapshots_crew_agent_overrides(monkeypatch):
    """The handle keys its construction-time watchdog snapshot on crew_agent
    (the canonical identity); a construction without it snapshots the
    globals."""
    from kiro_crew.config.loader import KiroCrewAgentConfig

    _cfg_with_agent_overrides(monkeypatch, {
        "pr-reviewer": KiroCrewAgentConfig(
            kiro_agent="pr-reviewer-kiro",
            watchdog_tool_stall_suspect_secs=450.0,
        ),
    })

    rt = MagicMock()
    rt.pid = None
    handle = AcpSessionHandle("s1", asyncio.Queue(), rt, crew_agent="pr-reviewer")
    assert handle._watchdog.tool_stall_suspect_secs == 450.0
    assert handle._watchdog.agent_override is True
    bare = AcpSessionHandle("s2", asyncio.Queue(), rt)
    assert bare._watchdog.tool_stall_suspect_secs == 3600.0
    assert bare._watchdog.agent_override is False


def test_rebind_watchdog_follows_warm_pool_rekey(monkeypatch):
    """rebind_watchdog() re-snapshots for the claiming crew (identity travels
    with the session, not the pool key), and an empty rebind drops a previous
    crew's windows back to the globals."""
    from kiro_crew.config.loader import KiroCrewAgentConfig

    _cfg_with_agent_overrides(monkeypatch, {
        "claimer": KiroCrewAgentConfig(
            kiro_agent="shared", watchdog_tool_stall_suspect_secs=300.0
        ),
    })

    rt = MagicMock()
    rt.pid = None
    handle = AcpSessionHandle("s1", asyncio.Queue(), rt)  # pool spawn: no crew
    assert handle._watchdog.tool_stall_suspect_secs == 3600.0

    handle.rebind_watchdog("claimer")
    assert handle._crew_agent == "claimer"
    assert handle._watchdog.tool_stall_suspect_secs == 300.0
    assert handle._watchdog.agent_override is True

    handle.rebind_watchdog("")
    assert handle._watchdog.tool_stall_suspect_secs == 3600.0
    assert handle._watchdog.agent_override is False


def test_unknown_agent_inherits_global(monkeypatch):
    """An agent with no config entry (or no agent name at all) snapshots the
    plain global windows."""
    from kiro_crew.acp.session_handle import _load_watchdog_settings

    _cfg_with_agent_overrides(monkeypatch, {})

    assert _load_watchdog_settings("nope").tool_stall_suspect_secs == 3600.0
    assert _load_watchdog_settings("").tool_stall_suspect_secs == 3600.0


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


# ── F2: TOCTOU race — progress frame during oracle must prevent cancel ────────


@pytest.mark.asyncio
def _make_one_shot_oracle(dead_evidence: str):
    """Factory for a one-shot oracle mock. The FIRST call blocks until
    released and returns a DEAD verdict (so the cancel branch IS reached
    without the TOCTOU guard). Subsequent calls return WORKING (simulating
    the oracle observing the activity that arrived during the first wait).

    This models real behavior: if a session is alive (frame arrived during
    the oracle), the oracle detects activity on the NEXT check and returns
    WORKING — the TOCTOU guard is only needed to bridge the window between
    the stale snapshot and the real activity observation.

    Mutation check: removing the TOCTOU guard leaves the first DEAD verdict
    unintercepted → session/cancel is sent. With the guard the first call is
    skipped, last_data_ts is reset, and the second call returns WORKING →
    no cancel.
    """
    oracle_entered = asyncio.Event()
    oracle_release = asyncio.Event()
    call_count = 0

    async def oracle(*, model_wait: bool) -> tuple[str, str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            oracle_entered.set()
            await oracle_release.wait()
            return ("dead", dead_evidence)
        return ("working", "activity observed")

    return oracle, oracle_entered, oracle_release


@pytest.mark.asyncio
async def test_wait_for_response_counts_buffered_responses_in_ingress_seq():
    """EVERY frame buffered by a concurrent _wait_for_response advances
    _ingress_seq — responses included, not just notifications. A buffered
    response can be the prompt turn's own terminal frame; when only
    notifications counted, the TOCTOU guard could not see it and the watchdog
    could cancel a turn whose completion sat in the buffer."""
    handle = _make_handle()
    seq_before = handle._ingress_seq
    # A non-matching RESPONSE (method=None) arrives before the awaited one.
    handle._queue.put_nowait(JsonRpcMessage(id=42, result={"stopReason": "end_turn"}))
    handle._queue.put_nowait(JsonRpcMessage(id=7, result={}))

    msg = await handle._wait_for_response(7, timeout=5.0)

    assert msg.id == 7
    assert handle._ingress_seq == seq_before + 1  # the buffered response counted
    # The buffered frame is re-injected, not dropped.
    assert handle._queue.get_nowait().id == 42


@pytest.mark.asyncio
async def test_toctou_path_b_ingress_seq_prevents_cancel():
    """F2 Path B: concurrent _wait_for_response consumed the progress frame.

    The frame is NOT in the queue when the oracle returns — it is in
    _wait_for_response's buffer list. qsize() is still 0, but _ingress_seq
    was incremented when _wait_for_response buffered the notification.
    The TOCTOU guard detects _ingress_seq advanced and skips the cancel.

    The oracle returns DEAD on its first call so the cancel branch IS
    reached when the guard is absent. Subsequent calls return WORKING.
    Mutation check: removing the _ingress_seq arm lets the first DEAD
    verdict reach _end_stalled_tool → session/cancel.
    """
    evidence = "no established backend socket and flat counters (io +0B cpu +0t)"
    oracle, oracle_entered, oracle_release = _make_one_shot_oracle(evidence)

    wd = WatchdogSettings(check_after_secs=0.01)
    handle = _make_handle(watchdog=wd)
    handle._stale_eligible = False
    handle._tool_dispatched = True
    from kiro_crew.acp.liveness import ToolCallState
    handle._inflight_tool = ToolCallState(title="ReadInternalWebsites", command="")
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._consult_oracle_offloaded = oracle  # type: ignore[method-assign]

    async def do_drain():
        return [ev async for ev in handle._dispatch_events(req_id=99, timeout=2.0)]

    drain_task = asyncio.create_task(do_drain())
    await asyncio.wait_for(oracle_entered.wait(), timeout=1.0)

    # Simulate _wait_for_response buffering a notification during oracle.
    # Path B: frame is NOT in the queue; _ingress_seq is the only signal.
    handle._ingress_seq += 1

    oracle_release.set()
    await asyncio.sleep(0.1)
    drain_task.cancel()
    try:
        await drain_task
    except asyncio.CancelledError:
        pass

    # _ingress_seq advanced → TOCTOU guard fired → first DEAD verdict skipped
    # → second call returned WORKING → no session/cancel.
    handle._runtime.send_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_toctou_path_a_queue_depth_prevents_cancel():
    """F2 Path A: no concurrent _wait_for_response — the progress frame lands
    directly in the session queue and stays there during the oracle await.

    qsize() grows from 0 to 1 while the oracle is blocked. The TOCTOU guard
    detects the queue-depth change and skips the cancel.

    Oracle returns DEAD on first call (cancel branch reached without guard);
    subsequent calls return WORKING (activity observed after reset).
    Mutation check: removing the qsize() arm lets the first DEAD reach
    _end_stalled_tool → session/cancel.

    Uses _SilentQueueWithBacklog: times out when empty (fast watchdog trigger)
    but delivers items added via put_nowait() on the next get() call.
    """
    evidence = "no established backend socket and flat counters (io +0B cpu +0t)"
    oracle, oracle_entered, oracle_release = _make_one_shot_oracle(evidence)

    wd = WatchdogSettings(check_after_secs=0.01)
    handle = _make_handle(watchdog=wd)
    handle._stale_eligible = False
    handle._tool_dispatched = True
    from kiro_crew.acp.liveness import ToolCallState
    handle._inflight_tool = ToolCallState(title="ReadInternalWebsites", command="")
    # Backlog queue: times out fast when empty, delivers put_nowait items.
    handle._queue = _SilentQueueWithBacklog()  # type: ignore[assignment]
    handle._consult_oracle_offloaded = oracle  # type: ignore[method-assign]

    async def do_drain():
        return [ev async for ev in handle._dispatch_events(req_id=99, timeout=2.0)]

    drain_task = asyncio.create_task(do_drain())
    await asyncio.wait_for(oracle_entered.wait(), timeout=1.0)

    # Put a progress frame directly into the queue DURING the oracle.
    # Path A: no _wait_for_response active; qsize() grows from 0 to 1.
    handle._queue.put_nowait(  # type: ignore[union-attr]
        JsonRpcMessage(method="notifications/progress", params={})
    )

    oracle_release.set()
    await asyncio.sleep(0.1)
    drain_task.cancel()
    try:
        await drain_task
    except asyncio.CancelledError:
        pass

    # qsize() grew → TOCTOU guard fired → first DEAD skipped → WORKING →
    # no session/cancel.
    handle._runtime.send_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_toctou_stale_eligible_path_also_guarded():
    """F2: the model-wait (_stale_eligible) oracle path has the same TOCTOU
    race as the tool-dispatch path and must be guarded too.

    Without the guard, a DEAD verdict on the stale branch would probe/cancel
    a live session that had a progress frame arrive during the oracle await.
    The guard detects the queue-depth change and skips the first DEAD cancel.

    Oracle returns DEAD on first call; WORKING on subsequent calls.
    """
    evidence = "no established backend socket and flat counters (io +0B cpu +0t)"
    oracle, oracle_entered, oracle_release = _make_one_shot_oracle(evidence)

    wd = WatchdogSettings(check_after_secs=0.01)
    handle = _make_handle(last_activity=time.monotonic() - 100.0, watchdog=wd)
    handle._stale_eligible = True
    handle._tool_dispatched = False
    handle._queue = _SilentQueueWithBacklog()  # type: ignore[assignment]
    handle._consult_oracle_offloaded = oracle  # type: ignore[method-assign]

    async def do_drain():
        return [ev async for ev in handle._dispatch_events(req_id=99, timeout=2.0)]

    drain_task = asyncio.create_task(do_drain())
    await asyncio.wait_for(oracle_entered.wait(), timeout=1.0)

    # Progress frame arrives during oracle — qsize grows from 0 to 1.
    handle._queue.put_nowait(  # type: ignore[union-attr]
        JsonRpcMessage(method="notifications/progress", params={})
    )

    oracle_release.set()
    await asyncio.sleep(0.1)
    drain_task.cancel()
    try:
        await drain_task
    except asyncio.CancelledError:
        pass

    # TOCTOU guard on the stale branch prevented the probe cancel.
    handle._runtime.send_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_toctou_stale_path_c_runtime_activity_prevents_probe():
    """TOCTOU Path C, stale branch only: runtime activity without a frame.

    The stale idle clock folds in ``_runtime._last_activity`` (stderr
    thinking-tokens, keepalives, stdin writes), none of which enqueue a
    session frame or advance ``_ingress_seq`` — so the two frame-path signals
    are blind to it. Activity that would have deferred the probe had it
    landed one tick before the snapshot must defer it when it lands during
    the oracle await too: the guard also snapshots/rechecks the runtime
    clock on this branch.

    Oracle returns DEAD on first call (probe reached without the guard);
    WORKING on subsequent calls. Mutation check: removing the runtime-clock
    arm lets the first DEAD verdict reach the probe → session/cancel.
    """
    evidence = "no established backend socket and flat counters (io +0B cpu +0t)"
    oracle, oracle_entered, oracle_release = _make_one_shot_oracle(evidence)

    wd = WatchdogSettings(check_after_secs=0.01)
    handle = _make_handle(last_activity=time.monotonic() - 100.0, watchdog=wd)
    handle._stale_eligible = True
    handle._tool_dispatched = False
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._consult_oracle_offloaded = oracle  # type: ignore[method-assign]

    async def do_drain():
        return [ev async for ev in handle._dispatch_events(req_id=99, timeout=2.0)]

    drain_task = asyncio.create_task(do_drain())
    await asyncio.wait_for(oracle_entered.wait(), timeout=1.0)

    # Runtime clock advances during the oracle — no frame, no _ingress_seq.
    handle._runtime._last_activity = time.monotonic()

    oracle_release.set()
    await asyncio.sleep(0.1)
    drain_task.cancel()
    try:
        await drain_task
    except asyncio.CancelledError:
        pass

    # Runtime-clock arm fired → first DEAD skipped → WORKING → no probe.
    handle._runtime.send_notification.assert_not_awaited()


# ── F3: hard cap bounds UNKNOWN forbearance absolutely ───────────────────────


@pytest.mark.asyncio
async def test_hard_cap_below_suspect_window_fires_at_cap():
    """F3: when tool_stall_hard_cap_secs < tool_stall_suspect_secs, the cancel
    must fire just after the hard cap, not after the (larger) suspect window.

    With cap=0.05s and suspect=999s, the cancel would never fire within this
    test's runtime unless the hard cap is applied as min(suspect, hard_cap).
    """
    from kiro_crew.acp.liveness import ToolCallState

    wd = WatchdogSettings(
        check_after_secs=0.01,
        tool_stall_suspect_secs=999.0,   # would never fire without the cap
        tool_stall_hard_cap_secs=0.05,   # hard cap < suspect → governs
    )
    handle = _make_handle(watchdog=wd)
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._inflight_tool = ToolCallState(title="mystery", command="", is_shell=False)
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_tool = lambda pid, tool: ("unknown", "mcp subtree flat")

    events = await _drain(handle, req_id=1, timeout=5.0)

    # The cancel must have fired (hard cap governs) ...
    handle._runtime.send_notification.assert_awaited()
    assert handle._runtime.send_notification.await_args.args[0] == "session/cancel"
    # ... and the terminal event carries the tool-stall stop reason.
    assert events and events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL

# ── Offloaded-consult hygiene: one walk per generation, retire don't reset ────
#
# The handle offloads its oracle consult to the shared subprocess_executor(), so
# a timed-out await leaves the /proc walk running with a bound reference to the
# oracle it was handed. These cover the two consequences: blocked workers
# stacking up one per watchdog tick, and a detached walk writing evidence into
# the generation that replaced it.


def _consult_handle(sample_secs: float = 3.0) -> AcpSessionHandle:
    """A handle with a real oracle and a runtime pid, turn in flight."""
    rt = MagicMock()
    rt._last_activity = time.monotonic()
    rt.pid = 4242
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    wd = WatchdogSettings(wellness_sample_secs=sample_secs)
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=wd)
    handle._turn_done.clear()
    handle._inflight_tool = ToolCallState(title="bash", command="sleep 1", is_shell=True)
    return handle


def _stub_pool() -> tuple[MagicMock, ThreadFuture]:
    """An executor whose submitted job never finishes on its own."""
    thread_future: ThreadFuture = ThreadFuture()
    thread_future.set_running_or_notify_cancel()
    pool = MagicMock()
    pool.submit.return_value = thread_future
    return pool, thread_future


async def _settle() -> None:
    """Let add_done_callback land — it is scheduled via call_soon."""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_consult_skips_while_prior_walk_is_in_flight():
    """An unfinished walk makes the next tick answer UNKNOWN without submitting.

    Without the guard a permanently wedged /proc read grows one blocked worker
    per ``check_after_secs`` tick in the shared pool that teardown's
    ``_get_child_pids`` also draws from.
    """
    handle = _consult_handle()
    handle._oracle = MagicMock()
    handle._consult_future = asyncio.get_running_loop().create_future()

    assert await handle._consult_oracle_offloaded(model_wait=False) == (
        VERDICT_UNKNOWN,
        "prior consult still in flight",
    )
    handle._oracle.check_tool.assert_not_called()

    # Once it finishes, the gate reopens and the next tick submits normally.
    handle._consult_future.set_result((VERDICT_WORKING, "done"))
    handle._oracle.check_tool.return_value = (VERDICT_DEAD, "child exited")
    assert await handle._consult_oracle_offloaded(model_wait=False) == (
        VERDICT_DEAD,
        "child exited",
    )
    handle._oracle.check_tool.assert_called_once()


@pytest.mark.asyncio
async def test_no_inflight_tool_answer_is_not_masked_by_the_guard():
    """The pure-state answer is resolved BEFORE the in-flight guard.

    "no in-flight tool state" needs no worker, so gating it behind a wedged walk
    would replace an accurate reason with an unrelated one — and the tool branch
    reads the evidence string into its logs and SEL.
    """
    handle = _consult_handle()
    handle._inflight_tool = None
    handle._consult_future = asyncio.get_running_loop().create_future()

    assert await handle._consult_oracle_offloaded(model_wait=False) == (
        VERDICT_UNKNOWN,
        "no in-flight tool state",
    )


@pytest.mark.asyncio
async def test_a_real_submission_is_recorded_and_gates_the_next_tick():
    """Injecting the future by hand proves nothing about the submission path.

    If the assignment were dropped, every timed-out walk would leave the field
    ``None`` and the next tick would submit another executor job — the exact
    starvation this guard exists to stop.
    """
    handle = _consult_handle()
    handle._oracle = MagicMock()
    pool, thread_future = _stub_pool()
    _real_wait_for = asyncio.wait_for

    async def _fast_timeout(awaitable, timeout=None):
        return await _real_wait_for(awaitable, timeout=0.01)

    with (
        patch("kiro_crew.acp.session_handle.subprocess_executor", return_value=pool),
        patch("kiro_crew.acp.session_handle.asyncio.wait_for", _fast_timeout),
    ):
        assert await handle._consult_oracle_offloaded(model_wait=False) == (
            VERDICT_UNKNOWN,
            "oracle offload error",
        )
        assert handle._consult_future is not None
        assert pool.submit.call_count == 1

        assert await handle._consult_oracle_offloaded(model_wait=False) == (
            VERDICT_UNKNOWN,
            "prior consult still in flight",
        )
        assert pool.submit.call_count == 1

    thread_future.set_exception(OSError("wedged /proc read"))
    await _settle()


@pytest.mark.asyncio
async def test_failed_submission_reports_unknown_without_latching_the_gate():
    """A refused executor job must degrade to UNKNOWN and leave the gate open.

    Submission fails for ordinary reasons — a pool shut down during teardown,
    thread creation refused under load. Recording nothing keeps the guard from
    latching shut on a walk that never started.
    """
    handle = _consult_handle()
    handle._oracle = MagicMock()

    with patch(
        "kiro_crew.acp.session_handle.subprocess_executor",
        side_effect=RuntimeError("cannot schedule new futures after shutdown"),
    ):
        assert await handle._consult_oracle_offloaded(model_wait=False) == (
            VERDICT_UNKNOWN,
            "oracle offload error",
        )

    assert handle._consult_future is None


@pytest.mark.asyncio
async def test_pending_walk_exception_is_consumed_without_any_boundary():
    """The retrieval callback must be attached at SUBMISSION.

    A turn that ends on a tool-stall verdict returns while the walk is still
    running; if the handle then goes idle, no later tick and no boundary ever
    observes it, and a walk that raises reaches ``Future.__del__`` unretrieved —
    reported through the loop exception handler as an unhandled-asyncio crash for
    an ordinary probe failure.
    """
    handle = _consult_handle()
    handle._oracle = MagicMock()
    pool, thread_future = _stub_pool()
    _real_wait_for = asyncio.wait_for

    async def _fast_timeout(awaitable, timeout=None):
        # Delegate to the REAL wait_for: its cancellation of shield's outer
        # future is what detaches the inner-done callback. A patched raise would
        # leave that callback attached and retrieve the exception for us — a
        # vacuous pass.
        return await _real_wait_for(awaitable, timeout=0.01)

    with (
        patch("kiro_crew.acp.session_handle.subprocess_executor", return_value=pool),
        patch("kiro_crew.acp.session_handle.asyncio.wait_for", _fast_timeout),
    ):
        await handle._consult_oracle_offloaded(model_wait=False)

    tracked = handle._consult_future
    assert tracked is not None and not tracked.done()

    thread_future.set_exception(OSError("wedged /proc read"))
    await _settle()

    assert tracked.done()
    # _log_traceback is the flag Future.__del__ consults to decide whether to
    # report an exception as never retrieved.
    assert tracked._log_traceback is False


@pytest.mark.asyncio
async def test_cancelled_consult_still_consumes_a_later_failure():
    """Cancellation is what proves the callback is not in the ``except`` arm.

    Attaching it in ``except Exception`` would cover the timeout path and look
    equivalent — but ``CancelledError`` is a ``BaseException``, so a turn
    cancelled mid-walk would skip it.
    """
    handle = _consult_handle()
    handle._oracle = MagicMock()
    pool, thread_future = _stub_pool()

    with patch("kiro_crew.acp.session_handle.subprocess_executor", return_value=pool):
        task = asyncio.ensure_future(handle._consult_oracle_offloaded(model_wait=False))
        while handle._consult_future is None:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    tracked = handle._consult_future
    assert tracked is not None and not tracked.done()

    thread_future.set_exception(OSError("wedged /proc read"))
    await _settle()

    assert tracked.done()
    assert tracked._log_traceback is False


@pytest.mark.asyncio
async def test_reopening_the_gate_consumes_a_failed_prior_walk():
    """A prior walk that already failed must have its exception retrieved.

    ``wait_for`` cancels shield's outer future and shield then detaches the
    inner-done callback, so a walk that raises after the timeout can arrive with
    its exception unread.
    """
    handle = _consult_handle()
    handle._oracle = MagicMock()
    handle._oracle.check_tool.return_value = (VERDICT_DEAD, "child exited")

    prior = asyncio.get_running_loop().create_future()
    prior.set_exception(OSError("wedged /proc read"))
    handle._consult_future = prior

    assert await handle._consult_oracle_offloaded(model_wait=False) == (
        VERDICT_DEAD,
        "child exited",
    )
    assert prior._log_traceback is False


@pytest.mark.asyncio
async def test_retirement_releases_the_walk_and_swaps_the_oracle():
    """Both halves of the liveness state retire together.

    Retiring only the future would leave a detached walk writing into the live
    baseline; retiring only the oracle would leave that walk answering every
    later tick "still in flight", so the new generation never samples its own
    process and the tool branch acts on UNKNOWN at the suspect window.
    """
    handle = _consult_handle()
    retired = handle._oracle
    retired._samples["io"] = (0.0, 12_345)
    retired._samples["cpu"] = (0.0, 678)
    retired._tracked_child = 9999
    retired._child_gone_ts = 1.0
    wedged = asyncio.get_running_loop().create_future()
    handle._consult_future = wedged

    handle._retire_liveness_state()

    assert handle._consult_future is None
    assert handle._oracle is not retired
    assert handle._oracle._samples == {}
    assert handle._oracle._tracked_child is None
    assert handle._oracle._child_gone_ts is None

    # A late write from the detached walk reaches the retired instance only.
    retired._samples["io"] = (0.0, 999_999)
    assert "io" not in handle._oracle._samples

    # And its eventual failure is not reported as an unhandled crash.
    wedged.set_exception(OSError("wedged /proc read"))
    await _settle()
    assert wedged._log_traceback is False


@pytest.mark.asyncio
async def test_retired_oracle_keeps_the_sessions_sampling_config():
    """``fresh()`` not ``LivenessOracle()``: the swap must not silently repoint.

    The handle constructs its oracle with the session's
    ``watchdog.wellness_sample_secs``; a default-constructed replacement would
    quietly revert the movement-sample interval at the first boundary.
    """
    handle = _consult_handle(sample_secs=0.25)
    configured = handle._oracle
    assert configured._sample_min_secs == 0.25

    handle._retire_liveness_state()

    assert handle._oracle is not configured
    assert handle._oracle._sample_min_secs == 0.25


@pytest.mark.asyncio
async def test_the_submitted_walk_is_bound_to_the_oracle_it_sampled():
    """Retirement isolates a late writer only if the job captured its oracle.

    Handing the executor something that resolved ``self._oracle`` at execution
    time would make a detached walk write into whatever oracle is live *then*,
    defeating every retirement here while leaving the other tests green. Guards
    behaviour that is already correct rather than fixing anything.
    """
    handle = _consult_handle()
    submitted_against = handle._oracle
    pool, thread_future = _stub_pool()
    _real_wait_for = asyncio.wait_for

    async def _fast_timeout(awaitable, timeout=None):
        return await _real_wait_for(awaitable, timeout=0.01)

    with (
        patch("kiro_crew.acp.session_handle.subprocess_executor", return_value=pool),
        patch("kiro_crew.acp.session_handle.asyncio.wait_for", _fast_timeout),
    ):
        await handle._consult_oracle_offloaded(model_wait=False)

    walk = pool.submit.call_args[0][0]
    assert getattr(walk, "__self__", None) is submitted_against

    handle._retire_liveness_state()
    assert handle._oracle is not submitted_against
    assert walk.__self__ is submitted_against

    thread_future.set_exception(OSError("wedged /proc read"))
    await _settle()


@pytest.mark.asyncio
async def test_turn_start_retires_the_liveness_state():
    """``prompt()`` must retire before it sends, not merely clear the oracle.

    A walk left over from the previous turn would otherwise gate this turn's
    first ticks with "still in flight" while writing the previous process tree's
    counters into the baseline this turn reads.
    """
    handle = _consult_handle()
    handle._runtime.supports_image_prompt = False
    handle._runtime.send_request = AsyncMock(side_effect=RuntimeError("stop after retirement"))
    handle._turn_done.set()  # prompt() refuses a turn that is still active
    prior_oracle = handle._oracle
    prior_oracle._samples["io"] = (0.0, 12_345)
    prior_walk = asyncio.get_running_loop().create_future()
    handle._consult_future = prior_walk

    with pytest.raises(RuntimeError, match="stop after retirement"):
        async for _ev in handle.prompt("hi", timeout=1.0):
            pass

    assert handle._oracle is not prior_oracle
    assert handle._oracle._samples == {}
    assert handle._consult_future is None

    prior_walk.set_exception(OSError("wedged /proc read"))
    await _settle()
    assert prior_walk._log_traceback is False


@pytest.mark.asyncio
async def test_tool_dispatch_retires_the_liveness_state():
    """Every new tool dispatch is a liveness generation boundary.

    ``reset()`` here was the site Luca Chang's ``AcpClient`` fix named as the
    remaining gap: it drops the baseline in place while a walk submitted for the
    PREVIOUS tool may still be running against it.
    """
    handle = _consult_handle()
    prior_oracle = handle._oracle
    prior_oracle._samples["cpu"] = (0.0, 678)
    prior_walk = asyncio.get_running_loop().create_future()
    handle._consult_future = prior_walk

    handle._handle_update(
        JsonRpcMessage(
            method=METHOD_SESSION_UPDATE,
            params={
                "sessionId": "sA",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-2",
                    "title": "bash",
                    "kind": "execute",
                    "rawInput": {"command": "make test"},
                },
            },
        )
    )

    assert handle._tool_dispatched is True
    assert handle._oracle is not prior_oracle
    assert handle._oracle._samples == {}
    assert handle._consult_future is None

    prior_walk.set_exception(OSError("wedged /proc read"))
    await _settle()
    assert prior_walk._log_traceback is False


@pytest.mark.asyncio
async def test_a_previous_tools_walk_cannot_claim_the_new_tools_child():
    """The tool path's sharper version of the stale-write hazard.

    ``_check_shell_child`` matches a descendant against the DISPATCHED command
    and stores it as ``_tracked_child`` so later ticks get exact exit detection.
    A walk still running with the previous tool's ``ToolCallState`` matches a
    child of the previous command; with an in-place ``reset()`` that write lands
    on the live oracle and the new tool's next tick reports
    ``WORKING "shell child N alive"`` for a process that has nothing to do with
    it — deferring recovery of a genuinely stalled tool to the hard cap.
    """
    handle = _consult_handle()
    tool_a_oracle = handle._oracle

    handle._handle_update(
        JsonRpcMessage(
            method=METHOD_SESSION_UPDATE,
            params={
                "sessionId": "sA",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-b",
                    "title": "bash",
                    "kind": "execute",
                    "rawInput": {"command": "make test"},
                },
            },
        )
    )

    # Tool A's detached walk finally finishes and writes what it matched.
    tool_a_oracle._tracked_child = 9999
    tool_a_oracle._child_gone_ts = None

    assert handle._oracle._tracked_child is None
    verdict, evidence = handle._oracle.check_tool(4242, handle._inflight_tool)
    assert "shell child 9999" not in evidence
    assert verdict != VERDICT_WORKING


@pytest.mark.asyncio
async def test_tracked_child_still_carries_across_ticks_after_retirement():
    """Retirement must not break the cross-tick contract it sits next to.

    ``check_tool``'s exact-exit detection depends on ``_tracked_child`` surviving
    from the tick that matched it to the tick that checks it. ``fresh()`` starts
    in exactly the state ``reset()`` produced and the consult binds
    ``self._oracle`` at submission, so ticks after a boundary accumulate on the
    new instance the way they did before — including through the in-flight gate,
    which releases as soon as a walk completes.
    """
    handle = _consult_handle()
    handle._retire_liveness_state()
    live = handle._oracle

    # Tick 1: the walk matches a child and writes it into the LIVE oracle.
    def _match_child(pid, tool):
        live._tracked_child = 4321
        return VERDICT_WORKING, "shell child 4321 alive"

    with patch.object(live, "check_tool", _match_child):
        assert await handle._consult_oracle_offloaded(model_wait=False) == (
            VERDICT_WORKING,
            "shell child 4321 alive",
        )

    # The completed walk released the gate, and tick 2 reads tick 1's match: the
    # exact-exit branch is reachable ONLY when _tracked_child survived the tick
    # boundary, and it is what starts the child-gone grace clock.
    assert handle._consult_future is not None and handle._consult_future.done()
    assert live._tracked_child == 4321
    verdict, evidence = await handle._consult_oracle_offloaded(model_wait=False)
    assert evidence.startswith("shell child exited")
    assert verdict == VERDICT_UNKNOWN  # inside CHILD_EXIT_GRACE_SECS
    assert live._child_gone_ts is not None
