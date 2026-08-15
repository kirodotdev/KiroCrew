"""Tests for the watchdog telemetry (kirocrew.watchdog.*).

Drives the REAL production emit paths — ``AcpSessionHandle._emit_watchdog_metric``
via the dispatch-loop decision points, ``_watchdog_evidence_class``, and
``chat_runner._emit_recovery_outcome`` — with a patched recorder, so the metric
names, the attribute enums, the evidence bucketing, and the cardinality rule
(agent_override is a BOOLEAN, never the agent name) all live in production and
any drift fails here (tests must exercise real production logic).
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.liveness import ToolCallState
from kiro_crew.acp.session_handle import (
    AcpSessionHandle,
    WatchdogSettings,
    _watchdog_evidence_class,
)


class _CapturingRecorder:
    """Stand-in recorder that captures counter() and histogram() calls."""

    def __init__(self) -> None:
        self.counters: list = []
        self.histograms: list = []

    def counter(self, name, value=1, *, unit="1", attrs=None, **kwargs) -> None:
        self.counters.append({"name": name, "value": value, "attrs": dict(attrs or {})})

    def histogram(self, name, value, *, unit="ms", attrs=None, **kwargs) -> None:
        self.histograms.append(
            {"name": name, "value": value, "unit": unit, "attrs": dict(attrs or {})}
        )


def _action_calls(rec, action=None):
    calls = [c for c in rec.counters if c["name"] == "kirocrew.watchdog.action"]
    if action is not None:
        calls = [c for c in calls if c["attrs"].get("action") == action]
    return calls


def _idle_calls(rec):
    return [h for h in rec.histograms if h["name"] == "kirocrew.watchdog.idle.duration"]


# ── Evidence bucketing (the cardinality firewall) ────────────────────────────


class TestEvidenceClass:
    """Free-form oracle evidence buckets to a closed enum — pids, byte deltas
    and command fragments must never become OTel attribute values."""

    def test_established_flat_tool_shape(self):
        assert (
            _watchdog_evidence_class("established_flat: mcp subtree flat (io +0B cpu +0t)")
            == "established_flat"
        )

    def test_established_flat_model_wait_shape(self):
        assert _watchdog_evidence_class("established_flat: io +0B cpu +0t") == "established_flat"

    def test_mcp_flat(self):
        assert _watchdog_evidence_class("mcp subtree flat (io +0B cpu +0t)") == "mcp_flat"
        assert _watchdog_evidence_class("mcp subtree active (io +512B cpu +3t)") == "mcp_flat"

    def test_shell(self):
        assert _watchdog_evidence_class("shell child 4242 alive") == "shell"
        assert (
            _watchdog_evidence_class("shell child 4242 exited 16s ago, no result frame")
            == "shell"
        )

    def test_wait(self):
        assert _watchdog_evidence_class("wait tool declared 1800s (60s elapsed)") == "wait"

    def test_degraded_catchall(self):
        for e in ("sampling", "no readable counters", "no runtime pid", "oracle error", ""):
            assert _watchdog_evidence_class(e) == "degraded", e

    def test_no_free_form_leak(self):
        """Whatever the evidence carries, the output is from the closed set."""
        closed = {"established_flat", "mcp_flat", "shell", "wait", "degraded"}
        for e in (
            "shell child 999999 alive",
            "stuck_input: pid 7 blocked reading /dev/tty with flat subtree",
            "established_flat: mcp subtree flat (io +12345B cpu +0t)",
            "backend activity (io +9999B cpu +1t)",
        ):
            assert _watchdog_evidence_class(e) in closed


# ── The emit helper itself ───────────────────────────────────────────────────


def _handle(watchdog=None):
    rt = MagicMock()
    rt._last_activity = time.monotonic()
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=watchdog)


def _emit(handle, *args, **kwargs):
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        handle._emit_watchdog_metric(*args, **kwargs)
    return rec


class TestEmitWatchdogMetric:
    def test_counter_and_histogram_emitted_together(self):
        rec = _emit(_handle(), "cancel", "unknown", "mcp subtree flat (io +0B cpu +0t)", 613.0)

        (c,) = _action_calls(rec)
        assert c["attrs"] == {
            "action": "cancel",
            "verdict": "unknown",
            "evidence_class": "mcp_flat",
            "window": "standard",
            "agent_override": False,
        }
        (h,) = _idle_calls(rec)
        assert h["value"] == 613000.0  # ms — the emit site converts from the seconds idle clock
        assert h["unit"] == "ms"
        assert h["attrs"] == {"action": "cancel", "evidence_class": "mcp_flat"}

    def test_narrowed_window_attr(self):
        """Tool-branch established_flat narrows the suspect window → window=narrowed."""
        rec = _emit(
            _handle(),
            "cancel",
            "unknown",
            "established_flat: mcp subtree flat (io +0B cpu +0t)",
            901.0,
            window="narrowed",
        )
        (c,) = _action_calls(rec)
        assert c["attrs"]["window"] == "narrowed"
        assert c["attrs"]["evidence_class"] == "established_flat"

    def test_extended_window_attr(self):
        """Model-wait established_flat extends the stale window → window=extended."""
        rec = _emit(
            _handle(),
            "probe",
            "unknown",
            "established_flat: io +0B cpu +0t",
            310.0,
            window="extended",
        )
        (c,) = _action_calls(rec, "probe")
        assert c["attrs"]["window"] == "extended"
        assert c["attrs"]["evidence_class"] == "established_flat"

    def test_agent_override_boolean_from_settings_snapshot(self):
        wd = WatchdogSettings(tool_stall_suspect_secs=900.0, agent_override=True)
        rec = _emit(_handle(watchdog=wd), "cancel", "unknown", "mcp subtree flat", 950.0)
        (c,) = _action_calls(rec)
        assert c["attrs"]["agent_override"] is True

    def test_agent_name_never_an_attribute(self):
        """The cardinality rule: the crew agent NAME must not appear in any
        attr — it only keys the WatchdogSettings snapshot on the handle."""
        handle = AcpSessionHandle(
            "sA",
            asyncio.Queue(),
            _handle()._runtime,
            watchdog=WatchdogSettings(agent_override=True),
            crew_agent="my-very-custom-agent-name",
        )
        rec = _emit(handle, "probe", "dead", "no established backend socket", 120.0)
        for call in rec.counters + rec.histograms:
            assert "my-very-custom-agent-name" not in str(call["attrs"])
            assert "agent" not in call["attrs"]  # only agent_override, on the counter
        assert _action_calls(rec)[0]["attrs"]["agent_override"] is True

    def test_emit_failure_never_raises(self):
        handle = _handle()
        with patch(
            "kiro_crew.metrics.provider.get_recorder", side_effect=RuntimeError("boom")
        ):
            handle._emit_watchdog_metric("cancel", "unknown", "x", 1.0)  # must not raise


# ── Decision-point wiring (through the real dispatch loop) ───────────────────


class _SilentQueue:
    """Queue that always times out, so every poll is a watchdog tick."""

    def __init__(self, tick: float = 0.02) -> None:
        self._tick = tick

    async def get(self):
        await asyncio.sleep(self._tick)
        raise asyncio.TimeoutError

    def qsize(self) -> int:
        # The silent queue never accumulates frames (get() always times out
        # before any put_nowait could be served), so depth is always 0.  The
        # TOCTOU guard reads qsize() before and after the oracle; equal values
        # here mean "no progress arrived" — correct for all tests that use this
        # queue, where the test scenario has no concurrent producer.
        return 0


async def _drain(handle, req_id, timeout):
    return [ev async for ev in handle._dispatch_events(req_id, timeout)]


def _stalling_handle(wd, evidence, verdict="unknown"):
    handle = _handle(watchdog=wd)
    handle._turn_done.clear()
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._inflight_tool = ToolCallState(title="t", command="{}", is_shell=False)
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_tool = lambda pid, tool: (verdict, evidence)
    return handle


@pytest.mark.asyncio
async def test_tool_stall_cancel_emits_action_point():
    wd = WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=0.05,
                          tool_stall_hard_cap_secs=999.0, model_silent_probe_secs=999.0)
    handle = _stalling_handle(wd, "mcp subtree flat (io +0B cpu +0t)")

    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        await _drain(handle, req_id=1, timeout=5.0)

    (c,) = _action_calls(rec, "cancel")
    assert c["attrs"]["verdict"] == "unknown"
    assert c["attrs"]["evidence_class"] == "mcp_flat"
    assert c["attrs"]["window"] == "standard"
    (h,) = _idle_calls(rec)
    assert h["value"] > 50  # ms — the idle clock at decision time


@pytest.mark.asyncio
async def test_narrowed_established_flat_cancel_tagged_narrowed():
    wd = WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=999.0,
                          tool_stall_hard_cap_secs=999.0, model_silent_probe_secs=0.05)
    handle = _stalling_handle(wd, "established_flat: mcp subtree flat (io +0B cpu +0t)")

    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        await _drain(handle, req_id=1, timeout=5.0)

    (c,) = _action_calls(rec, "cancel")
    assert c["attrs"]["window"] == "narrowed"
    assert c["attrs"]["evidence_class"] == "established_flat"


@pytest.mark.asyncio
async def test_stale_probe_emits_probe_point():
    wd = WatchdogSettings(check_after_secs=0.01, stale_window_secs=0.05,
                          model_silent_probe_secs=999.0, tool_stall_hard_cap_secs=999.0)
    handle = _handle(watchdog=wd)
    handle._runtime._last_activity = time.monotonic() - 100.0
    handle._turn_done.clear()
    handle._stale_eligible = True
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_model_wait = lambda pid: ("unknown", "no readable counters")

    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        await _drain(handle, req_id=1, timeout=0.3)

    calls = _action_calls(rec, "probe")
    assert calls, "stale probe must emit an action point"
    assert calls[0]["attrs"]["verdict"] == "unknown"
    assert calls[0]["attrs"]["evidence_class"] == "degraded"
    assert calls[0]["attrs"]["window"] == "standard"


@pytest.mark.asyncio
async def test_stale_probe_established_flat_tagged_extended():
    """F-minor regression: model-wait established_flat EXTENDS the stale window
    (900s instead of 300s). The probe point must emit window=extended, not
    window=narrowed, so dashboards can distinguish the two cases.

    Configuration: stale_window_secs=999 (never fires without established_flat),
    model_silent_probe_secs=0.05 (governs when established_flat; fires quickly).
    This shows the EXTENSION direction: the established_flat path narrows down
    from stale_window=999 to model_silent=0.05. In production the relationship
    is reversed (300s → 900s extension), but both cases emit window=extended.
    """
    wd = WatchdogSettings(
        check_after_secs=0.01,
        stale_window_secs=999.0,           # would never fire without established_flat
        model_silent_probe_secs=0.05,      # governs when established_flat fires quickly
        tool_stall_hard_cap_secs=999.0,
    )
    handle = _handle(watchdog=wd)
    handle._runtime._last_activity = time.monotonic() - 100.0
    handle._turn_done.clear()
    handle._stale_eligible = True
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    # established_flat evidence → model-wait branch selects model_silent_probe window
    handle._oracle.check_model_wait = lambda pid: (
        "unknown", "established_flat: io +0B cpu +0t"
    )

    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        await _drain(handle, req_id=1, timeout=0.5)

    calls = _action_calls(rec, "probe")
    assert calls, "stale probe must emit an action point"
    assert calls[0]["attrs"]["evidence_class"] == "established_flat"
    # Must be "extended" (model-wait established_flat is an extension of the
    # probe window, not a narrowing) — never "narrowed"
    assert calls[0]["attrs"]["window"] == "extended", (
        "model-wait established_flat extends the probe window — must not emit 'narrowed'"
    )


@pytest.mark.asyncio
async def test_working_deferral_emits_rate_limited_deferral_point():
    """The deferral point rides _log_working_deferral's 10-min rate limit —
    a long WORKING build yields ONE point per interval, not one per tick."""
    wd = WatchdogSettings(check_after_secs=0.01)
    handle = _stalling_handle(wd, "shell child 1234 alive", verdict="working")

    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
        await _drain(handle, req_id=1, timeout=0.3)  # many ticks

    calls = _action_calls(rec, "deferral")
    assert len(calls) == 1  # rate-limited, not per-tick
    assert calls[0]["attrs"]["verdict"] == "working"
    assert calls[0]["attrs"]["evidence_class"] == "shell"
    # No cancel/probe: WORKING is never acted on.
    assert not _action_calls(rec, "cancel")
    assert not _action_calls(rec, "probe")


@pytest.mark.asyncio
async def test_first_working_deferral_always_logged_regardless_of_host_uptime():
    """_working_logged_ts is initialised to -inf so the FIRST deferral is
    always emitted even on a host that has been running less than the 10-minute
    rate-limit interval. With 0.0 initialisation, now - 0.0 < 600 on young
    hosts would incorrectly suppress it."""
    wd = WatchdogSettings(check_after_secs=0.01)
    handle = _stalling_handle(wd, "shell child 1234 alive", verdict="working")
    # Patch monotonic to a very small value (simulating a recently-booted host
    # whose clock is below the 600s interval). The deferral must still fire.
    short_uptime_ts = 5.0  # 5 seconds since boot
    with patch("kiro_crew.acp.session_handle.time") as mock_time:
        mock_time.monotonic.return_value = short_uptime_ts
        rec = _CapturingRecorder()
        with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
            handle._log_working_deferral(120.0, "shell child 1234 alive", 7200.0)

    calls = _action_calls(rec, "deferral")
    assert len(calls) == 1, (
        "first deferral must be emitted even on hosts with monotonic time < rate-limit interval"
    )


# ── kirocrew.watchdog.recovery.outcome (chat_runner) ─────────────────────────


def _run_recovery(mechanism, outcome, attempts):
    from kiro_crew.dashboard import chat_runner

    rec = _CapturingRecorder()
    # chat_runner imports get_recorder at module top-level → patch the consumer.
    with patch("kiro_crew.dashboard.chat_runner.get_recorder", return_value=rec):
        chat_runner._emit_recovery_outcome(mechanism, outcome, attempts)
    return rec


def _recovery_calls(rec):
    return [c for c in rec.counters if c["name"] == "kirocrew.watchdog.recovery.outcome"]


class TestRecoveryOutcome:
    def test_recovered_point(self):
        (c,) = _recovery_calls(_run_recovery("tool_stall", "recovered", 1))
        assert c["attrs"] == {
            "mechanism": "tool_stall",
            "outcome": "recovered",
            "attempt_bucket": 1,
        }

    def test_exhausted_point(self):
        (c,) = _recovery_calls(_run_recovery("stale_recover", "exhausted", 3))
        assert c["attrs"]["outcome"] == "exhausted"
        assert c["attrs"]["attempt_bucket"] == 3

    def test_attempt_bucket_clamped_to_budget_cap(self):
        # A closed enum: never above the 3-attempt cap, never below 1.
        assert _recovery_calls(_run_recovery("tool_stall", "exhausted", 99))[0]["attrs"][
            "attempt_bucket"
        ] == 3
        assert _recovery_calls(_run_recovery("tool_stall", "recovered", 0))[0]["attrs"][
            "attempt_bucket"
        ] == 1

    def test_emit_failure_never_raises(self):
        from kiro_crew.dashboard import chat_runner

        with patch(
            "kiro_crew.dashboard.chat_runner.get_recorder", side_effect=RuntimeError("boom")
        ):
            chat_runner._emit_recovery_outcome("tool_stall", "recovered", 1)  # must not raise
