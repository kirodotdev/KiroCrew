"""Tests for kirocrew.turn.duration, emitted at turn completion.

Drives the REAL production helper ``chat_runner._emit_turn_metric`` with a
patched recorder, so the metric name, attributes, the stop_reason→outcome
mapping, the session_source derivation, and the zero/None-duration skip all
live in production — a change there fails these tests instead of passing green.
"""

from unittest.mock import patch


class _CapturingRecorder:
    """Stand-in recorder that captures histogram() calls."""

    def __init__(self) -> None:
        self.calls: list = []

    def histogram(self, name, value, *, unit="ms", attrs=None, **kwargs) -> None:
        self.calls.append(
            {"name": name, "value": value, "unit": unit, "attrs": dict(attrs or {})}
        )


def _run(duration_ms, stop_reason, slot_key="dashboard:abc123", elapsed_ms=None):
    """Invoke the production emit helper with a patched recorder; return it."""
    from kiro_crew.dashboard import chat_runner

    rec = _CapturingRecorder()
    # chat_runner imports get_recorder at module top-level → patch the consumer.
    with patch("kiro_crew.dashboard.chat_runner.get_recorder", return_value=rec):
        chat_runner._emit_turn_metric(
            duration_ms, stop_reason, slot_key, elapsed_ms=elapsed_ms
        )
    return rec


def _turn_call(rec):
    calls = [c for c in rec.calls if c["name"] == "kirocrew.turn.duration"]
    assert calls, "kirocrew.turn.duration histogram must be emitted"
    return calls[-1]


class TestTurnMetricOutcomeMapping:
    """stop_reason → outcome mapping, driven through production."""

    def test_end_turn_is_ok(self):
        c = _turn_call(_run(1500, "end_turn"))
        assert c["attrs"]["outcome"] == "ok"
        assert c["value"] == 1500
        assert c["unit"] == "ms"

    def test_none_stop_reason_is_ok(self):
        assert _turn_call(_run(800, None))["attrs"]["outcome"] == "ok"

    def test_empty_stop_reason_is_ok(self):
        assert _turn_call(_run(200, ""))["attrs"]["outcome"] == "ok"

    def test_stop_is_ok(self):
        assert _turn_call(_run(3000, "stop"))["attrs"]["outcome"] == "ok"

    def test_completed_is_ok(self):
        assert _turn_call(_run(5000, "completed"))["attrs"]["outcome"] == "ok"

    def test_cancelled_is_error(self):
        assert _turn_call(_run(400, "cancelled"))["attrs"]["outcome"] == "error"

    def test_refusal_is_error(self):
        assert _turn_call(_run(100, "refusal"))["attrs"]["outcome"] == "error"

    def test_error_prefix_is_error(self):
        assert _turn_call(_run(2000, "error: cancel unacked"))["attrs"]["outcome"] == "error"

    def test_stale_recover_is_error(self):
        assert _turn_call(_run(7000, "stale_recover"))["attrs"]["outcome"] == "error"

    def test_timeout_is_timeout(self):
        assert _turn_call(_run(120000, "timeout"))["attrs"]["outcome"] == "timeout"

    def test_timeout_substring_is_timeout(self):
        assert _turn_call(_run(60000, "error: timeout exceeded"))["attrs"]["outcome"] == "timeout"

    def test_zero_duration_skips_emit(self):
        assert _run(0, "end_turn").calls == []

    def test_none_duration_skips_emit(self):
        assert _run(None, "end_turn").calls == []


class TestTurnMetricElapsedFallback:
    """The acp backend always reports duration_ms=0 — the wall clock must win.

    This is the regression guard for the bug where the histogram was never
    emitted for the default backend, leaving the Telemetry page's turn latency,
    fault rate, and throughput cards showing a flat 0 with no data behind them.
    """

    def test_zero_provider_duration_uses_elapsed(self):
        c = _turn_call(_run(0, "end_turn", elapsed_ms=1500))
        assert c["value"] == 1500
        assert c["attrs"]["outcome"] == "ok"

    def test_none_provider_duration_uses_elapsed(self):
        assert _turn_call(_run(None, "end_turn", elapsed_ms=42))["value"] == 42

    def test_provider_duration_wins_when_present(self):
        # claude_code reports a real API duration; prefer it over the local clock.
        assert _turn_call(_run(900, "end_turn", elapsed_ms=1500))["value"] == 900

    def test_outcome_still_applies_on_the_fallback_path(self):
        c = _turn_call(_run(0, "timeout", elapsed_ms=120000))
        assert c["attrs"]["outcome"] == "timeout"

    def test_both_zero_skips_emit(self):
        # Absence reads as "no data"; a recorded 0 would render as a plausible
        # 0ms p50, which is the failure mode this whole guard exists to avoid.
        assert _run(0, "end_turn", elapsed_ms=0).calls == []


class TestTurnMetricSessionSource:
    """session_source derived via the real infer_use_case, through production."""

    def test_dashboard_key(self):
        c = _turn_call(_run(500, "end_turn", "dashboard:session-xyz"))
        assert c["attrs"]["session_source"] == "dashboard"

    def test_cron_key(self):
        c = _turn_call(_run(500, "end_turn", "cron:daily-check"))
        assert c["attrs"]["session_source"] == "cron"

    def test_subagent_key(self):
        c = _turn_call(_run(500, "end_turn", "subagent:abc123"))
        assert c["attrs"]["session_source"] == "subagent"

    def test_cli_key(self):
        c = _turn_call(_run(500, "end_turn", "cli_chat"))
        assert c["attrs"]["session_source"] == "cli"

    def test_slack_key(self):
        c = _turn_call(_run(500, "end_turn", "1234567890.123456"))
        assert c["attrs"]["session_source"] == "slack"

    def test_unknown_key(self):
        c = _turn_call(_run(500, "end_turn", "something_random"))
        assert c["attrs"]["session_source"] == "unknown"
