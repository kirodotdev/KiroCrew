"""A busy MCP gateway daemon is not a dead one, and the supervisor must not
confuse them.

The field case this pins: a translation workflow fanned out enough one-off
sessions to put ~185 concurrent stub connections on one ``mcp-gatewayd``. The
daemon stayed healthy, but a saturated event loop could not reach the pong
handler inside the supervisor's 2s ping bound, so three misses (~90s) declared it
a zombie and SIGKILLed it. That dropped every attached stub at once; all of them
reconnected against the replacement, loading its loop at least as hard, and the
next three pings missed again. It repeated ten times at a ~2.5 minute cadence,
which the exponential backoff could not damp because the backoff resets to its
floor once a daemon survives 30s -- and a merely-overloaded daemon always does.
Sessions attached to the killed daemon could serve no calls until a replacement
permanently.

Reply latency measures load, not liveness, and ``_ping_raw`` gives up at its own
deadline, so a late pong is never seen at all. Three properties close it:

1. **Answering is alive.** A missed fast ping buys ONE escalated probe, which is
   the only way a loaded daemon's reply can be collected. If it answers, the
   miss streak resets.
2. **Silence still kills.** A daemon that answers neither probe trips the
   existing three-strike grace unchanged.
3. **The sibling path is covered.** The adopted-daemon branch made the same
   misjudgement with no grace at all, and there concluding death does not end
   the daemon -- it clears the socket and spawns a rival for the address while
   the live one keeps serving.
"""

from __future__ import annotations

import asyncio

import pytest

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
    payload, calls = _fast_miss_then({"type": "pong"})
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
async def test_a_silent_daemon_still_trips_at_the_existing_threshold(tmp_path, monkeypatch):
    """Unreachable even with the longer deadline keeps today's exact behaviour."""
    monkeypatch.setattr(mgr, "_LIVENESS_PING_INTERVAL_SECS", 0.0)
    m = _manager(tmp_path)
    payload, calls = _fast_miss_then(None)
    monkeypatch.setattr(m, "_ping_payload", payload)

    reason = await asyncio.wait_for(m._liveness_probe_loop(), timeout=1.0)

    assert "zombie detected" in reason
    assert calls["fast"] == mgr._LIVENESS_MAX_CONSECUTIVE_FAILURES


@pytest.mark.asyncio
async def test_a_loaded_adopted_daemon_is_not_replaced_on_one_missed_ping(tmp_path, monkeypatch):
    """The same misjudgement on the sibling path, where it costs more.

    The adopted branch holds no process handle, so concluding death does not
    kill the daemon -- it clears the socket and spawns a rival for the address
    while the live daemon keeps running. It also had no three-strike grace at
    all, so one 2s timeout was enough.
    """
    m = _manager(tmp_path)
    payload, calls = _fast_miss_then({"type": "pong"})
    monkeypatch.setattr(m, "_ping_payload", payload)

    pong = await m._ping_with_escalation()

    assert pong is not None, "a loaded adopted daemon must not read as gone"
    assert calls["escalated"] == 1


@pytest.mark.asyncio
async def test_an_absent_daemon_still_reads_as_gone(tmp_path, monkeypatch):
    m = _manager(tmp_path)
    payload, _ = _fast_miss_then(None)
    monkeypatch.setattr(m, "_ping_payload", payload)

    assert await m._ping_with_escalation() is None


@pytest.mark.asyncio
async def test_one_escalated_miss_replaces_an_adopted_daemon(tmp_path, monkeypatch):
    """The adopted branch acts on ONE escalated miss, and that is deliberate.

    A grace period here is not free patience: if the adopted daemon really is
    gone, nothing is listening on its address and no attached stub can serve a
    call until a replacement binds, so the verdict's latency IS the outage. One
    escalated miss displaces at 26s worst case (2+2+2 fast, then 20 escalated);
    three cycles would take 26 + 30 + 26 + 30 + 26 = 138s, five times the outage
    in exchange for evidence the escalation already provides. The owned path can afford its grace because its own socket stays
    bound while it waits.
    """
    m = _manager(tmp_path)
    m._adopted = True
    m._process = None
    steps = []

    async def _never_answers():
        steps.append("probed")
        return None

    async def _clear():
        steps.append("cleared")

    async def _spawn():
        steps.append("spawned")
        m._stopping = True

    async def _sleep(secs):
        steps.append(f"slept {secs}")

    monkeypatch.setattr(m, "_ping_with_escalation", _never_answers)
    monkeypatch.setattr(m, "_clear_stale_socket", _clear)
    monkeypatch.setattr(m, "_spawn_once", _spawn)
    monkeypatch.setattr(mgr.asyncio, "sleep", _sleep)

    await m._run_watchdog()

    # One probe, then straight to replacement: no waiting, no second probe.
    assert steps == ["probed", "cleared", "spawned"]


@pytest.mark.asyncio
async def test_the_escalated_bound_reaches_every_step_and_caps_their_total(tmp_path, monkeypatch):
    """A longer probe is only longer if the read is longer too.

    Against a saturated accept backlog the connect is what blocks, but a probe
    that widened only the connect and left the read at the fast bound would
    collect no more late pongs than the fast probe did -- which is the whole
    point of escalating. So the escalated timeout must bound each step.
    """
    m = _manager(tmp_path)
    waits: list[float] = []
    real_wait_for = asyncio.wait_for

    class _Writer:
        def write(self, _data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class _Reader:
        async def readuntil(self, _sep):
            return b'{"type":"pong"}\n'

    async def _connect(*_a, **_k):
        # Burn part of the budget, so a SHARED deadline is visibly smaller for
        # the steps that follow and a per-step one is not.
        await asyncio.sleep(0.05)
        return _Reader(), _Writer()

    async def _spy(aw, timeout=None):
        waits.append(timeout)
        return await real_wait_for(aw, timeout=timeout)

    monkeypatch.setattr(mgr.transport, "connect", _connect)
    monkeypatch.setattr(mgr.asyncio, "wait_for", _spy)

    assert await m._ping_raw(timeout=mgr._LIVENESS_ESCALATED_TIMEOUT_SECS) == {"type": "pong"}
    # Every step is bounded by the escalated deadline AND they share it: the
    # first wait is the full bound, each later one is no larger, and none
    # exceeds the total. Per-step budgets would show three full 20s waits.
    assert len(waits) == 3
    assert waits[0] == pytest.approx(mgr._LIVENESS_ESCALATED_TIMEOUT_SECS, abs=0.05)
    # Strictly smaller than the FIRST wait, not than its immediate predecessor:
    # two adjacent steps can read equal at the clock's granularity (they do on a
    # Windows runner), while a per-step budget gives every step the full bound
    # and so never shrinks at all.
    assert waits[1] <= waits[0]
    assert waits[2] < waits[0]
    assert max(waits) <= mgr._LIVENESS_ESCALATED_TIMEOUT_SECS

    # And the default path is the fast bound on every step, unchanged from base.
    waits.clear()
    assert await m._ping_raw() == {"type": "pong"}
    assert waits == [mgr._PING_TIMEOUT_SECS] * 3
