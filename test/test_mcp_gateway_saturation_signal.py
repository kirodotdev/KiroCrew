"""The loaded-but-alive gateway daemon must be VISIBLE.

A daemon that misses the fast 2s ping and answers the escalated one is the
precursor state to every kill/reconnect cycle. The supervisor spends one longer
probe before declaring such a daemon dead, so it survives the load — but that
survival is otherwise silent, invisible to an operator until it turns into an
outage.

This signal makes it observable: a COUNTER of escalated probes (``answered``
telling loaded-but-alive from dead) plus the OBSERVED pong LATENCY the daemon
took to answer, produced at the supervisor's escalation site under
``observe=True``. The alert threshold is left to ops; the code only emits the
metric. The emit is fire-and-forget so telemetry never gates the liveness
verdict, and it fires only for the watchdog callers — the start-up confirmation
caller (``observe=False``) fast-misses on a daemon that is merely coming up, not
a saturated one.

These tests drive the REAL production site (``_ping_with_escalation``) with the
emit facade captured, so a renamed metric, a changed attribute, or a removed
emit fails here.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.mcp_gateway import manager as mgr
from kiro_crew.metrics import events as metric_events


def _manager(tmp_path) -> mgr.GatewayManager:
    return mgr.GatewayManager(mgr.GatewaySpec(socket_path=tmp_path / "gw.sock"))


def _fast_miss_then(escalated: dict | None, *, escalated_delay: float = 0.0):
    """A ``_ping_payload`` double: fast probe misses, escalated probe answers.

    ``escalated_delay`` makes the escalated round-trip take real wall-clock
    time, so the observed-latency histogram has something above zero to record —
    the "slow but responding" state the acceptance test needs.
    """

    async def _payload(*, timeout=None):
        if timeout is None:
            return None
        assert timeout == mgr._LIVENESS_ESCALATED_TIMEOUT_SECS
        if escalated_delay:
            await asyncio.sleep(escalated_delay)
        return escalated

    return _payload


async def _drain(m: mgr.GatewayManager) -> None:
    """Await the fire-and-forget telemetry tasks the probe detached.

    The emit is scheduled with ``asyncio.ensure_future`` and never awaited by
    the production caller (so it cannot gate the liveness verdict), so a test
    must drain the manager's task set before reading the recorded emits.
    """
    while m._telemetry_tasks:
        await asyncio.gather(*tuple(m._telemetry_tasks))


@pytest.fixture()
def recorded(monkeypatch):
    """Capture emit_counter / emit_histogram routed through metrics.events.

    Patched at the source module AND at manager's bound references, because
    manager imports the functions by name (see test_hang_resilience_metrics).
    """
    counters: list[tuple[str, dict]] = []
    histograms: list[tuple[str, float, dict]] = []

    def _fake_counter(name: str, attrs: dict) -> None:
        counters.append((name, dict(attrs)))

    def _fake_histogram(name: str, value: float, attrs: dict, *, unit: str = "1") -> None:
        histograms.append((name, value, dict(attrs)))

    for mod in ("kiro_crew.metrics.events", "kiro_crew.mcp_gateway.manager"):
        monkeypatch.setattr(f"{mod}.emit_counter", _fake_counter, raising=False)
        monkeypatch.setattr(f"{mod}.emit_histogram", _fake_histogram, raising=False)
    return counters, histograms


@pytest.mark.asyncio
async def test_a_slow_but_responding_daemon_raises_both_signals(tmp_path, monkeypatch, recorded):
    """The core acceptance: loaded-but-alive is now observable.

    Fast probe misses, escalated probe answers after a measurable delay. The
    escalated-probe counter increments with ``answered=True`` and the observed
    pong-latency gauge records a value above zero for ``process=gatewayd``.
    """
    counters, histograms = recorded
    m = _manager(tmp_path)
    monkeypatch.setattr(m, "_ping_payload", _fast_miss_then({"type": "pong"}, escalated_delay=0.02))

    pong = await m._ping_with_escalation(observe=True)
    await _drain(m)

    assert pong == {"type": "pong"}, "a loaded daemon that answers must not read as gone"

    probe_hits = [a for n, a in counters if n == metric_events.LIVENESS_ESCALATED_PROBES]
    assert probe_hits == [{"answered": True}]

    latency_hits = [
        (v, a) for n, v, a in histograms if n == metric_events.LIVENESS_ESCALATED_LATENCY_MS
    ]
    assert len(latency_hits) == 1
    value, attrs = latency_hits[0]
    assert attrs == {"process": "gatewayd"}
    assert value > 0.0, "the observed escalated-probe latency must be recorded"


@pytest.mark.asyncio
async def test_a_healthy_daemon_leaves_both_signals_flat(tmp_path, monkeypatch, recorded):
    """A daemon answering the FAST probe never escalates, so neither signal fires."""
    counters, histograms = recorded
    m = _manager(tmp_path)

    async def _answers_fast(*, timeout=None):
        return {"type": "pong"}

    monkeypatch.setattr(m, "_ping_payload", _answers_fast)

    assert await m._ping_with_escalation(observe=True) == {"type": "pong"}
    await _drain(m)

    assert [n for n, _ in counters if n == metric_events.LIVENESS_ESCALATED_PROBES] == []
    assert [n for n, _, _ in histograms if n == metric_events.LIVENESS_ESCALATED_LATENCY_MS] == []


@pytest.mark.asyncio
async def test_a_dead_daemon_counts_the_escalation_but_records_no_latency(
    tmp_path, monkeypatch, recorded
):
    """Both probes miss: the escalated probe is still counted (``answered=False``)
    so the escalation rate is complete, but there is no answer and therefore no
    latency to record — the histogram stays flat."""
    counters, histograms = recorded
    m = _manager(tmp_path)
    monkeypatch.setattr(m, "_ping_payload", _fast_miss_then(None))

    assert await m._ping_with_escalation(observe=True) is None
    await _drain(m)

    probe_hits = [a for n, a in counters if n == metric_events.LIVENESS_ESCALATED_PROBES]
    assert probe_hits == [{"answered": False}]
    assert [n for n, _, _ in histograms if n == metric_events.LIVENESS_ESCALATED_LATENCY_MS] == []


@pytest.mark.asyncio
async def test_start_up_confirmation_caller_emits_nothing(tmp_path, monkeypatch, recorded):
    """``observe=False`` (the default, and the start-up confirmation caller) is
    silent even on a fast miss: a daemon still coming up is not a saturated one,
    and a sample there would pollute the series with start-up noise."""
    counters, histograms = recorded
    m = _manager(tmp_path)
    monkeypatch.setattr(m, "_ping_payload", _fast_miss_then({"type": "pong"}, escalated_delay=0.02))

    # Default observe=False: the escalation still happens (a loaded daemon still
    # must not read as gone), but nothing is emitted and no task is scheduled.
    assert await m._ping_with_escalation() == {"type": "pong"}

    assert m._telemetry_tasks == set(), "no telemetry task may be scheduled when not observing"
    assert [n for n, _ in counters if n == metric_events.LIVENESS_ESCALATED_PROBES] == []
    assert [n for n, _, _ in histograms if n == metric_events.LIVENESS_ESCALATED_LATENCY_MS] == []
