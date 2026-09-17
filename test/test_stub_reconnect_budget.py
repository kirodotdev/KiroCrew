"""The stub's reconnect budget must outlast a gatewayd supervisor crash loop.

Failure shape pinned here: the supervisor kills and respawns gatewayd ~10 times in
35 minutes and the endpoint was absent for ~10 minutes. The per-session stub
gave up after its 60s budget and took the terminal exit; kiro-cli logged
``Transport to MCP server 'kirocrew-core' is closed`` and -- because it never
re-mounts a closed server -- every later kirocrew-core call in those long-lived
sessions hung for hours. A stub that had kept waiting would have re-attached.

These tests pin the budget to the supervisor's own timing (so it cannot drift
back below one crash loop), and drive ``_reconnect`` through ten simulated
minutes with an injected clock so the retry loop is proven to SPEND that budget
rather than merely declare it.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.mcp_gateway import manager as manager_mod
from kiro_crew.mcp_gateway import stub as stub_mod
from kiro_crew.mcp_gateway.shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS

_SERVER_RESULT = {
    "protocolVersion": "2024-11-05",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "fake", "version": "1"},
}

#: The old budget, kept as a literal so the regression this file guards against
#: stays legible: the reconnect must keep going well past this.
_OLD_BUDGET_SECS = 60.0


# --- fakes (same shapes as test_stub_broker_reconnect.py) --------------------


class _CaptureWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self._mc_write_lock = asyncio.Lock()

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _reader_with(*frames: dict) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for frame in frames:
        reader.feed_data(json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n")
    reader.feed_eof()
    return reader


def _session_with_captured_init() -> stub_mod.StubSession:
    session = stub_mod.StubSession()
    session.captured_init = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        + "\n"
    ).encode("utf-8")
    session.init_result = dict(_SERVER_RESULT)
    return session


class _FakeClock:
    """Simulated monotonic time the reconnect loop is driven against.

    ``wait`` replaces :func:`stub._reconnect_wait`: it advances the clock by the
    requested backoff instead of sleeping, records the delay so the backoff
    curve can be asserted, and yields to the loop once so the test stays a real
    coroutine rather than a tight synchronous spin.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        self.delays: list[float] = []

    def time(self) -> float:
        return self.now

    async def wait(self, stop_event: asyncio.Event, delay: float) -> bool:
        if stop_event.is_set():
            return False
        self.delays.append(delay)
        self.now += delay
        await asyncio.sleep(0)
        return True


def _install_clock(monkeypatch) -> _FakeClock:  # noqa: ANN001
    clock = _FakeClock()
    monkeypatch.setattr(stub_mod, "_reconnect_now", clock.time)
    monkeypatch.setattr(stub_mod, "_reconnect_wait", clock.wait)
    return clock


def _absent_until(clock: _FakeClock, back_at: float | None):  # noqa: ANN202
    """A handshake that times out until ``back_at`` seconds of simulated time.

    ``None`` means the gateway never comes back. Attempts are counted on the
    returned coroutine function's ``attempts`` list.
    """
    attempts: list[float] = []
    start = clock.now

    async def _hs(_socket_path: str, _payload: dict):
        attempts.append(clock.now - start)
        if back_at is None or clock.now - start < back_at:
            raise asyncio.TimeoutError()
        return (
            _reader_with({"jsonrpc": "2.0", "id": 7, "result": dict(_SERVER_RESULT)}),
            _CaptureWriter(),
            "stub-uuid",
            {"type": "registered", "capabilities": ["poolable_ack"]},
        )

    _hs.attempts = attempts  # type: ignore[attr-defined]
    return _hs


async def _run_reconnect(session: stub_mod.StubSession):  # noqa: ANN202
    return await stub_mod._reconnect(
        "unused",
        {"stub_uuid": "u", "session_key": "dashboard:x"},
        session,
        asyncio.Event(),
        poolable=True,
        pool_label="probe:fake",
    )


# --- (a) the budget is derived from the supervisor, not picked ---------------


def test_budget_is_at_least_ten_minutes() -> None:
    assert stub_mod._RECONNECT_TOTAL_BUDGET_SECS >= 600.0, (
        "a 60s-class budget is what lost every long-lived session its "
        "kirocrew-core tools on 2026-09-12"
    )
    assert stub_mod._RECONNECT_TOTAL_BUDGET_SECS > 5 * _OLD_BUDGET_SECS


def test_budget_equals_the_spawn_queue_wait_default() -> None:
    """The daemon holds a queued stub for ``mcp_gateway.spawn_queue_wait_secs``;
    the stub keeps the transport open for this budget. The stub reads no config,
    so the two are coupled by value at the key's DEFAULT: whoever raises the
    default must raise the constant with it."""
    from kiro_crew.config.sections import McpGatewayConfig

    assert stub_mod._RECONNECT_TOTAL_BUDGET_SECS == float(McpGatewayConfig().spawn_queue_wait_secs)


def test_budget_covers_a_supervisor_crash_loop() -> None:
    """Derived from the manager's REAL constants, not the stub's mirrors."""
    one_cycle = (
        manager_mod._LIVENESS_PING_INTERVAL_SECS * manager_mod._LIVENESS_MAX_CONSECUTIVE_FAILURES
        + TOTAL_SHUTDOWN_BUDGET_SECS
        + manager_mod._RESPAWN_BACKOFF_MAX_SECS
        + stub_mod._SUPERVISOR_COLD_START_SECS
    )
    assert stub_mod._RECONNECT_CRASH_LOOP_CYCLES >= 3
    assert stub_mod._RECONNECT_TOTAL_BUDGET_SECS >= (
        stub_mod._RECONNECT_CRASH_LOOP_CYCLES * one_cycle
    ), (
        f"budget {stub_mod._RECONNECT_TOTAL_BUDGET_SECS}s is shorter than "
        f"{stub_mod._RECONNECT_CRASH_LOOP_CYCLES} supervisor kill->respawn "
        f"cycles of {one_cycle}s each"
    )
    assert stub_mod._RECONNECT_TOTAL_BUDGET_SECS >= (
        stub_mod._RECONNECT_CRASH_LOOP_CYCLES * stub_mod._SUPERVISOR_RESPAWN_CYCLE_SECS
    )


def test_stub_mirrors_of_manager_constants_have_not_drifted() -> None:
    """The stub cannot import ``manager`` (import weight), so it mirrors three
    of its constants by name. This is the pin that keeps those mirrors honest."""
    assert stub_mod._SUPERVISOR_LIVENESS_PING_INTERVAL_SECS == (
        manager_mod._LIVENESS_PING_INTERVAL_SECS
    )
    assert stub_mod._SUPERVISOR_LIVENESS_MAX_FAILURES == (
        manager_mod._LIVENESS_MAX_CONSECUTIVE_FAILURES
    )
    assert stub_mod._SUPERVISOR_RESPAWN_BACKOFF_MAX_SECS == (manager_mod._RESPAWN_BACKOFF_MAX_SECS)


def test_backoff_cap_is_gentle_on_the_socket_but_still_prompt() -> None:
    assert stub_mod._RECONNECT_BACKOFF_START_SECS == 0.5
    assert 5.0 <= stub_mod._RECONNECT_BACKOFF_MAX_SECS <= 30.0
    # ~60 probes over the whole budget, not ~150.
    probes = stub_mod._RECONNECT_TOTAL_BUDGET_SECS / stub_mod._RECONNECT_BACKOFF_MAX_SECS
    assert probes <= 120


# --- (b) the loop actually spends the budget ---------------------------------


@pytest.mark.asyncio
async def test_reconnect_keeps_retrying_past_the_old_budget_and_reattaches(
    monkeypatch,
) -> None:
    """Endpoint absent for five simulated minutes, then back. The stub must
    still be trying when it comes back -- the 60s budget would have taken the
    terminal exit four minutes earlier."""
    clock = _install_clock(monkeypatch)
    hs = _absent_until(clock, back_at=300.0)
    monkeypatch.setattr(stub_mod, "handshake", hs)
    session = _session_with_captured_init()

    attached = await _run_reconnect(session)

    assert attached is not None, (
        "the reconnect gave up before a gateway that was back after 300s; the "
        "session loses its servers on an outage the budget is sized to cover"
    )
    assert session.reconnects == 1
    elapsed = clock.now - 1_000.0
    assert elapsed >= 300.0
    assert elapsed < 300.0 + stub_mod._RECONNECT_BACKOFF_MAX_SECS + 1e-6, (
        f"re-attached {elapsed - 300.0:.1f}s after the endpoint returned; the "
        "backoff cap should bound that to one step"
    )
    # Proof that it was retrying past 60s rather than sitting idle: attempts
    # were spread across the whole gap.
    attempt_times = hs.attempts  # type: ignore[attr-defined]
    assert any(
        _OLD_BUDGET_SECS < t < 300.0 for t in attempt_times
    ), f"no handshake attempt between 60s and 300s: {attempt_times!r}"
    assert attempt_times[-1] >= 300.0


# --- (c) but the budget is still finite --------------------------------------


@pytest.mark.asyncio
async def test_reconnect_returns_none_once_the_budget_is_spent(monkeypatch) -> None:
    clock = _install_clock(monkeypatch)
    hs = _absent_until(clock, back_at=None)
    monkeypatch.setattr(stub_mod, "handshake", hs)
    session = _session_with_captured_init()

    attached = await _run_reconnect(session)

    assert attached is None
    assert session.reconnects == 0
    elapsed = clock.now - 1_000.0
    budget = stub_mod._RECONNECT_TOTAL_BUDGET_SECS
    assert elapsed >= budget, f"gave up at {elapsed:.0f}s, before the {budget:.0f}s budget"
    assert (
        elapsed <= budget + stub_mod._RECONNECT_BACKOFF_MAX_SECS
    ), f"kept going until {elapsed:.0f}s against a {budget:.0f}s budget"
    attempt_times = hs.attempts  # type: ignore[attr-defined]
    assert attempt_times[-1] < budget
    assert len(attempt_times) >= 50, (
        f"only {len(attempt_times)} attempts over {budget:.0f}s: the loop is not "
        "polling at the backoff cap"
    )


@pytest.mark.asyncio
async def test_a_stop_during_the_wait_ends_the_reconnect_early(monkeypatch) -> None:
    """The budget is an upper bound, never a floor: a stop still wins."""
    clock = _install_clock(monkeypatch)
    monkeypatch.setattr(stub_mod, "handshake", _absent_until(clock, back_at=None))
    stop_event = asyncio.Event()
    session = _session_with_captured_init()
    real_wait = clock.wait

    async def _stop_on_third_wait(evt: asyncio.Event, delay: float) -> bool:
        if len(clock.delays) == 2:
            evt.set()
        return await real_wait(evt, delay)

    monkeypatch.setattr(stub_mod, "_reconnect_wait", _stop_on_third_wait)
    attached = await stub_mod._reconnect(
        "unused",
        {"stub_uuid": "u"},
        session,
        stop_event,
        poolable=True,
        pool_label="probe:fake",
    )
    assert attached is None
    assert clock.now - 1_000.0 < _OLD_BUDGET_SECS


# --- (d) the backoff curve ---------------------------------------------------


@pytest.mark.asyncio
async def test_backoff_doubles_from_start_and_caps_at_the_max(monkeypatch) -> None:
    clock = _install_clock(monkeypatch)
    monkeypatch.setattr(stub_mod, "handshake", _absent_until(clock, back_at=None))

    await _run_reconnect(_session_with_captured_init())

    delays = clock.delays
    start = stub_mod._RECONNECT_BACKOFF_START_SECS
    cap = stub_mod._RECONNECT_BACKOFF_MAX_SECS
    assert delays[0] == start
    expected = start
    for d in delays:
        assert d == pytest.approx(min(expected, cap))
        expected = min(expected * 2, cap)
    assert max(delays) == cap
    assert delays.count(cap) >= 50, (
        f"the cap was reached only {delays.count(cap)} times over a "
        f"{stub_mod._RECONNECT_TOTAL_BUDGET_SECS:.0f}s budget"
    )
    # Never above the cap: a ten-minute budget with an uncapped doubling would
    # spend its last minutes in one sleep and miss the endpoint coming back.
    assert all(d <= cap for d in delays)


@pytest.mark.asyncio
async def test_not_back_yet_log_carries_elapsed_and_remaining(monkeypatch, caplog) -> None:
    clock = _install_clock(monkeypatch)
    monkeypatch.setattr(stub_mod, "handshake", _absent_until(clock, back_at=30.0))
    caplog.set_level("INFO", logger="kiro_crew.mcp_gateway.stub")

    attached = await _run_reconnect(_session_with_captured_init())

    assert attached is not None
    lines = [r.getMessage() for r in caplog.records if "gateway not back yet" in r.getMessage()]
    assert lines, "no 'gateway not back yet' line was logged"
    assert all("elapsed=" in ln and "remaining=" in ln for ln in lines)
    # The first attempt happens at t=0 with the whole budget ahead of it.
    assert "elapsed=0s" in lines[0]
    assert f"remaining={stub_mod._RECONNECT_TOTAL_BUDGET_SECS:.0f}s" in lines[0]
