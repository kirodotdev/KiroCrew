"""A busy MCP gateway daemon is not a dead one, and the supervisor must not
confuse them.

The field case these pin: a translation workflow fanned out enough one-off
sessions to put ~185 concurrent stub connections on one ``mcp-gatewayd``. The
daemon stayed healthy -- its own probe still reported ``is_serving`` -- but a
saturated event loop could not reach the pong handler inside the supervisor's 2s
ping bound, so three misses (~90s) declared it a zombie and SIGKILLed it. That
dropped every attached stub at once; all of them reconnected against the
replacement, loading its loop at least as hard, and the next three pings missed
again. It repeated ten times at a ~2.5 minute cadence, which the exponential
backoff could not damp because the backoff resets to its floor once a daemon
survives 30s -- and a merely-overloaded daemon always does. Sessions whose stub
reconnect budget ran out during the outage lost the server permanently.

Four properties close it, each pinned below:

1. **Load evidence exists.** The pong carries self-measured event-loop lag, so
   "how loaded am I" is answerable separately from "did you reply in time".
2. **Busy is not killed.** A fast-ping miss triggers one escalated probe; a
   daemon that answers it without reporting a wedge does not count a failure.
3. **Wedged is still killed.** Self-reported loop lag past the threshold is a
   blocked loop, not a busy one, and needs no further grace.
4. **Recovery is bounded.** A rolling-window breaker stops respawning when
   respawning is what has stopped working.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import manager as mgr


def _manager(tmp_path) -> mgr.GatewayManager:
    return mgr.GatewayManager(mgr.GatewaySpec(socket_path=tmp_path / "gw.sock"))


def _fast_miss_then(escalated: dict | None):
    """A ``_ping_payload`` double: the fast probe misses, the escalated one answers.

    Keyed on the ``timeout`` kwarg rather than on call order, so the test pins
    that the escalated probe really is the one carrying a longer deadline
    instead of merely being the second call.
    """
    calls = {"fast": 0, "escalated": 0}

    async def _payload(*, timeout=None):
        if timeout is None:
            calls["fast"] += 1
            return None
        assert timeout == mgr._LIVENESS_ESCALATED_TIMEOUT_SECS
        calls["escalated"] += 1
        return escalated

    return _payload, calls


@pytest.mark.asyncio
async def test_a_loaded_daemon_that_answers_the_escalated_probe_is_not_killed(
    tmp_path, monkeypatch
):
    """The whole defect: a slow reply is load, not death."""
    monkeypatch.setattr(mgr, "_LIVENESS_PING_INTERVAL_SECS", 0.0)
    m = _manager(tmp_path)
    payload, calls = _fast_miss_then(
        {"type": "pong", "loop_lag_ms": 12.5, "connections_in_flight": 185}
    )
    monkeypatch.setattr(m, "_ping_payload", payload)

    task = asyncio.create_task(m._liveness_probe_loop())
    with pytest.raises(asyncio.TimeoutError):
        # A verdict here at all would be the bug. Every cycle must reset.
        await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
    task.cancel()

    # It really did keep probing -- the assertion above is not passing because
    # the loop stalled somewhere before the verdict.
    assert calls["fast"] > mgr._LIVENESS_MAX_CONSECUTIVE_FAILURES
    assert calls["escalated"] == calls["fast"]


@pytest.mark.asyncio
async def test_a_daemon_reporting_a_sustained_wedge_is_killed_after_the_grace(
    tmp_path, monkeypatch
):
    """A stall that keeps being reported is a wedge, and still dies."""
    monkeypatch.setattr(mgr, "_LIVENESS_PING_INTERVAL_SECS", 0.0)
    m = _manager(tmp_path)
    payload, calls = _fast_miss_then({"type": "pong", "loop_lag_ms": mgr._LOOP_LAG_WEDGE_MS + 1.0})
    monkeypatch.setattr(m, "_ping_payload", payload)

    reason = await asyncio.wait_for(m._liveness_probe_loop(), timeout=1.0)

    assert "zombie detected" in reason
    assert "event-loop lag" in reason
    # Three readings, not one: one recovered spike cannot survive the window.
    assert calls["fast"] == mgr._LIVENESS_MAX_CONSECUTIVE_FAILURES


@pytest.mark.asyncio
async def test_a_recovered_lag_spike_does_not_kill_a_responsive_daemon(tmp_path, monkeypatch):
    """The reported lag is a windowed PEAK, so it outlives the stall itself.

    Both review lanes found this independently: a daemon answering the escalated
    probe is provably not blocked now, so killing it on a spike that has already
    passed drops every attached stub for nothing -- and five such kills would
    trip the respawn breaker into a full pooling outage.
    """
    monkeypatch.setattr(mgr, "_LIVENESS_PING_INTERVAL_SECS", 0.0)
    m = _manager(tmp_path)
    seq = [
        {"type": "pong", "loop_lag_ms": mgr._LOOP_LAG_WEDGE_MS + 1.0},  # the spike
        {"type": "pong", "loop_lag_ms": mgr._LOOP_LAG_WEDGE_MS + 1.0},  # still in window
        {"type": "pong", "loop_lag_ms": 3.0},  # window expired; daemon is fine
    ]
    calls = {"fast": 0}

    async def _payload(*, timeout=None):
        if timeout is None:
            calls["fast"] += 1
            return None
        return seq[min(calls["fast"] - 1, len(seq) - 1)]

    monkeypatch.setattr(m, "_ping_payload", _payload)

    task = asyncio.create_task(m._liveness_probe_loop())
    with pytest.raises(asyncio.TimeoutError):
        # Two high readings then a clean one resets the streak, so the loop
        # never reaches a verdict.
        await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
    task.cancel()

    assert calls["fast"] > mgr._LIVENESS_MAX_CONSECUTIVE_FAILURES


@pytest.mark.asyncio
async def test_a_daemon_without_load_fields_is_treated_as_alive(tmp_path, monkeypatch):
    """Absence of ``loop_lag_ms`` is the capability signal, not a wedge."""
    monkeypatch.setattr(mgr, "_LIVENESS_PING_INTERVAL_SECS", 0.0)
    m = _manager(tmp_path)
    payload, calls = _fast_miss_then({"type": "pong", "targets": ["kirocrew"]})
    monkeypatch.setattr(m, "_ping_payload", payload)

    task = asyncio.create_task(m._liveness_probe_loop())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
    task.cancel()

    assert calls["fast"] > mgr._LIVENESS_MAX_CONSECUTIVE_FAILURES


@pytest.mark.asyncio
async def test_a_silent_daemon_still_trips_at_the_existing_threshold(tmp_path, monkeypatch):
    """Unreachable even with the longer deadline keeps today's exact behaviour."""
    monkeypatch.setattr(mgr, "_LIVENESS_PING_INTERVAL_SECS", 0.0)
    m = _manager(tmp_path)
    payload, calls = _fast_miss_then(None)
    monkeypatch.setattr(m, "_ping_payload", payload)

    reason = await asyncio.wait_for(m._liveness_probe_loop(), timeout=1.0)

    assert "zombie detected" in reason
    assert calls["fast"] == mgr._LIVENESS_MAX_CONSECUTIVE_FAILURES


def test_the_respawn_breaker_stops_a_repeating_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr, "_RESPAWN_BREAKER_MAX_IN_WINDOW", 3)
    m = _manager(tmp_path)

    assert [m._respawn_breaker_allows() for _ in range(3)] == [True, True, True]
    # The fourth inside the window is what the backoff could never stop.
    assert m._respawn_breaker_allows() is False


def test_the_respawn_breaker_forgets_attempts_older_than_its_window(tmp_path, monkeypatch):
    """A crash a week ago is not evidence that recovery is broken now."""
    monkeypatch.setattr(mgr, "_RESPAWN_BREAKER_MAX_IN_WINDOW", 2)
    m = _manager(tmp_path)
    stale = time.monotonic() - mgr._RESPAWN_BREAKER_WINDOW_SECS - 1.0
    m._respawn_times = [stale, stale, stale]

    assert m._respawn_breaker_allows() is True
    assert m._respawn_times == pytest.approx(m._respawn_times[-1:], abs=1.0)


def test_the_pong_publishes_the_load_evidence_the_verdict_needs(monkeypatch):
    monkeypatch.setattr(gw, "_LAST_LOOP_LAG_MS", 42.0)
    monkeypatch.setattr(gw, "_LOOP_LAG_SAMPLES", [])
    monkeypatch.setattr(gw, "_LOAD_CONNECTIONS", {object(), object()})
    monkeypatch.setattr(gw, "_LOAD_SERVER", None)
    monkeypatch.setattr(gw, "_LOAD_POOL", None)

    evidence = gw._pong_load_evidence()

    assert evidence["loop_lag_ms"] == pytest.approx(42.0)
    assert evidence["connections_in_flight"] == 2
    # Absent sources omit their field rather than reporting a wrong number.
    assert "is_serving" not in evidence
    assert "backends_in_flight" not in evidence


def test_load_evidence_survives_a_source_that_raises(monkeypatch):
    """A health reply that can fail is a health reply that reports death."""

    class _Angry:
        def is_serving(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(gw, "_LOAD_SERVER", _Angry())
    monkeypatch.setattr(gw, "_LOAD_CONNECTIONS", None)
    monkeypatch.setattr(gw, "_LOAD_POOL", None)

    evidence = gw._pong_load_evidence()

    assert "is_serving" not in evidence
    assert "loop_lag_ms" in evidence


def test_the_reported_lag_is_the_windowed_peak_not_the_latest_sample(monkeypatch):
    """A stall must not erase itself before anyone reads it.

    The consumer polls once per supervisor interval; the sampler samples many
    times in between. Reporting the latest sample would hide every stall, since
    the sampler recovers within milliseconds of one ending.
    """
    now = time.monotonic()
    monkeypatch.setattr(gw, "_LAST_LOOP_LAG_MS", 0.4)
    monkeypatch.setattr(gw, "_LOOP_LAG_SAMPLES", [(now - 5.0, 8000.0), (now - 0.1, 0.4)])

    assert gw._loop_lag_peak_ms() == pytest.approx(8000.0)


def test_a_lag_spike_older_than_the_window_is_forgotten(monkeypatch):
    now = time.monotonic()
    monkeypatch.setattr(gw, "_LAST_LOOP_LAG_MS", 0.4)
    monkeypatch.setattr(
        gw,
        "_LOOP_LAG_SAMPLES",
        [(now - gw._LOOP_LAG_PEAK_WINDOW_SECS - 5.0, 8000.0), (now - 0.1, 0.4)],
    )

    assert gw._loop_lag_peak_ms() == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_the_lag_sampler_measures_a_blocked_loop(monkeypatch):
    monkeypatch.setattr(gw, "_LOOP_LAG_SAMPLE_SECS", 0.02)
    monkeypatch.setattr(gw, "_LAST_LOOP_LAG_MS", 0.0)
    monkeypatch.setattr(gw, "_LOOP_LAG_SAMPLES", [])
    stop = asyncio.Event()
    task = asyncio.create_task(gw._loop_lag_sampler(stop))
    try:
        await asyncio.sleep(0.05)
        # Block the loop the way a synchronous call in the forward path does.
        time.sleep(0.3)
        await asyncio.sleep(0.05)
        # The peak is what a supervisor reads one interval later, so that is
        # what has to still carry the stall.
        assert gw._loop_lag_peak_ms() > 100.0
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_a_real_daemon_publishes_load_evidence_in_its_pong(
    short_sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, because the unit tests above cannot prove this.

    Every other test here sets the module globals directly, so all of them pass
    even if ``_amain`` never publishes the load sources or starts the sampler --
    and then the supervisor's new verdict silently degrades to "answered means
    alive" in production, which is the old behaviour wearing the new code. Only
    a daemon this test did not reach inside can settle it.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.chdir(home)
    sock = short_sock_dir / "gw.sock"

    env = {**os.environ, "PYTHONPATH": str(Path(gw.__file__).resolve().parents[2])}
    daemon = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys, asyncio\n"
        "from kiro_crew.mcp_gateway import gatewayd as g\n"
        "sys.exit(asyncio.run(g._amain(sys.argv[1:])))",
        "--socket",
        str(sock),
        "--idle-timeout-secs",
        "60",
        "--max-backends",
        "1",
        "--owner-pid",
        str(os.getpid()),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        cwd=str(home),
    )
    try:
        deadline = time.monotonic() + 20.0
        while not sock.exists() and time.monotonic() < deadline:
            assert daemon.returncode is None, "daemon exited before serving"
            await asyncio.sleep(0.05)
        assert sock.exists(), "the daemon must come up"

        m = mgr.GatewayManager(mgr.GatewaySpec(socket_path=sock))
        pong = await m._ping_payload(timeout=10.0)

        assert pong is not None, "a live daemon must answer a ping"
        # The field the verdict branches on. Its ABSENCE is the compatibility
        # signal, so a daemon that ships this code and omits it would be read as
        # an old one and never judged wedged at all.
        assert isinstance(pong.get("loop_lag_ms"), (int, float))
        # Sampled from the real objects, so these prove publish_load_sources ran.
        assert pong.get("is_serving") is True
        assert isinstance(pong.get("connections_in_flight"), int)
        assert pong.get("backends_in_flight") == 0
    finally:
        # The daemon's own shutdown budget can exceed a short wait, and a
        # swallowed timeout would leave the child alive while the fixture
        # deletes the socket directory under it -- a leak into every later test.
        if daemon.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                daemon.terminate()
            try:
                await asyncio.wait_for(daemon.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    daemon.kill()
                await daemon.wait()
