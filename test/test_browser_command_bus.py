"""Direct unit tests for the in-memory browser command bus.

Drives submit/drain/complete with an injectable clock (no sleeping for TTL) and
exercises the memory-safety/lifecycle guarantees the module docstring promises:
NoPanelError fast-fail vs cold-start wait, QueueFullError, timeout reclamation,
complete()-unknown-id, and TTL expiry.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.browser.command_bus import (
    DEFAULT_PANEL_TTL_S,
    BrowserCommandBus,
    NoPanelError,
    QueueFullError,
)


class Clock:
    """Deterministic monotonic clock for the bus's ``now=`` hook."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.mark.asyncio
async def test_submit_fails_fast_when_no_host_is_polling() -> None:
    # No drain has ever run -> no Electron host present -> immediate NoPanelError
    # (does NOT burn the panel_wait window), so the tool falls back at once.
    bus = BrowserCommandBus(now=Clock(), panel_wait_ms=5000)
    with pytest.raises(NoPanelError):
        await asyncio.wait_for(bus.submit("s1", "snapshot"), 1.0)


@pytest.mark.asyncio
async def test_submit_drain_complete_roundtrip() -> None:
    bus = BrowserCommandBus(now=Clock())
    drain_task = asyncio.create_task(bus.drain(["s1"], wait_ms=2000))
    await asyncio.sleep(0.02)  # let drain register the panel and block
    submit_task = asyncio.create_task(bus.submit("s1", "snapshot", {"a": 1}))

    cmd = await asyncio.wait_for(drain_task, 2.0)
    assert cmd is not None
    assert cmd["op"] == "snapshot" and cmd["session_key"] == "s1" and cmd["args"] == {"a": 1}

    assert await bus.complete(cmd["id"], True, result={"ok": 1}) is True
    res = await asyncio.wait_for(submit_task, 2.0)
    assert res["ok"] is True and res["result"] == {"ok": 1}


@pytest.mark.asyncio
async def test_cold_start_host_present_but_panel_unregistered_times_out() -> None:
    # Empty-keys heartbeat marks a host present but registers no panel for s1;
    # submit waits the bounded panel window, then NoPanelError (cold-start).
    bus = BrowserCommandBus(now=Clock(), panel_wait_ms=30)
    assert await bus.drain([], wait_ms=0) is None
    with pytest.raises(NoPanelError):
        await asyncio.wait_for(bus.submit("s1", "snapshot"), 1.0)


@pytest.mark.asyncio
async def test_queue_full_rejects_when_panel_present_but_undrained() -> None:
    bus = BrowserCommandBus(now=Clock(), max_queue_per_session=1)
    assert await bus.drain(["s1"], wait_ms=0) is None  # registers panel, no active drain
    pending = asyncio.create_task(bus.submit("s1", "snapshot"))  # fills the 1-slot queue
    await asyncio.sleep(0.02)
    with pytest.raises(QueueFullError):
        await asyncio.wait_for(bus.submit("s1", "snapshot"), 1.0)
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)  # drain before loop close


@pytest.mark.asyncio
async def test_timeout_reclaims_the_queued_command() -> None:
    bus = BrowserCommandBus(now=Clock())
    assert await bus.drain(["s1"], wait_ms=0) is None  # register, nobody drains after
    with pytest.raises(asyncio.TimeoutError):
        await bus.submit("s1", "snapshot", timeout_ms=20)
    # The timed-out command must not linger in memory.
    assert "s1" not in bus._queues
    assert bus._inflight == {}


@pytest.mark.asyncio
async def test_complete_unknown_id_returns_false() -> None:
    bus = BrowserCommandBus(now=Clock())
    assert await bus.complete("no-such-id", True, result={}) is False


@pytest.mark.asyncio
async def test_panel_registration_expires_on_ttl() -> None:
    clock = Clock()
    bus = BrowserCommandBus(now=clock)
    assert await bus.drain(["s1"], wait_ms=0) is None
    assert await bus.is_registered("s1") is True
    clock.advance(DEFAULT_PANEL_TTL_S + 1)
    assert await bus.is_registered("s1") is False
