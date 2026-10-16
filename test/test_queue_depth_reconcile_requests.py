"""Every queued-depth request is answered, once, and the delayed re-read.

A request made while a frame is being sent, or coalesced before the read
starts, is still answered, and a steady stream cannot withhold every frame. A
parent has at most one armed retry, which any published frame disarms and
shutdown cancels. Shared manager, recorder and waits live in
``queue_depth_reconcile_helpers.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from overload_fakes import settle_depth_emits
from queue_depth_reconcile_helpers import (
    _DEPTH_LOGGER,
    _PARENT,
    _STALE_WAIT,
    PUMP_MODES,
    _close,
    _defer,
    _depths,
    _fail_chip_reads,
    _manager,
    _park,
    _record,
    _settle,
    _store_waiting,
    _until,
    _warned,
)

import kiro_crew.subagent_manager.run as run_mod
from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.subagent_manager.run import _QUEUE_DEPTH_RETRIES
from kiro_crew.testing.wait import await_until
from kiro_crew.testing.wait import finished as _finished

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# ── every request is answered, and only once ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_request_while_a_frame_is_being_sent_is_answered(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A consumer that suspends while it takes the frame: a stop landing then
    must still get a frame read after it."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    sending, release = asyncio.Event(), asyncio.Event()
    frames: list[int] = []

    async def on_event(etype: str, info: Any, extra: dict[str, Any]) -> None:
        if etype == "subagent_queued" and info.parent_session_key == _PARENT:
            frames.append(extra["queued"])
            if len(frames) == 1:
                sending.set()
                await await_until(release, what="the test's release")

    try:
        with patch.object(SubagentManager, "_run", new=_park):
            mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
            rows = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(2)]
            await _settle(mgr)
            mgr._on_event = on_event
            mgr._emit_queue_depth(_PARENT)
            await await_until(sending, what="the consumer taking its first frame")
            for row in rows:
                assert await mgr.cancel(row.id) is True
            release.set()
            await _settle(mgr)

        assert frames[0] == 2
        assert frames[-1] == 0 == _store_waiting(mgr)
    finally:
        release.set()
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_requests_coalesced_before_the_read_starts_are_still_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In one loop step: a request starts a burst, a store write is posted, and
    the depth is asked for again. The burst has not read yet, and the posted
    write is queued on the writer thread ahead of the burst's first read, so
    that one read answers both requests: none is discarded and re-read."""
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    try:
        (row,) = _defer(mgr, 1)
        await _settle(mgr)
        events = _record(mgr)
        admission = mgr._admission
        real = SpawnAdmissionCoordinator.taskq_chip_overflow_async
        reads: list[str] = []

        async def counted(self: Any, parent: str) -> int | None:
            reads.append(parent)
            return await real(self, parent)

        monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_chip_overflow_async", counted)

        mgr._emit_queue_depth(_PARENT)
        admission._post_store_write(
            mgr._taskq, "test cancel", admission.taskq_cancel_queued, row.id
        )
        mgr._emit_queue_depth(_PARENT)
        await _settle(mgr)

        assert _store_waiting(mgr) == 0
        assert _depths(events) == [{"queued": 0}]
        assert reads == [_PARENT], "one read answers the whole coalesced burst"
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_steady_stream_of_requests_cannot_withhold_every_frame(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """Every read is overlapped by a request, so each is discarded and read
    again; once frames have been withheld for the cap, the read is published
    anyway and the burst reads again behind it. The burst's clock is the
    test's: each read "takes" 0.3 s, so the fourth one crosses the 1.0 s cap.
    That overlapped 0 read is published bare and keeps the parent's label (the
    overlapping request may have written it); the read behind it forgets it."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    clock = {"now": 0.0}
    monkeypatch.setattr(run_mod, "_queue_depth_clock", lambda: clock["now"])
    real = SpawnAdmissionCoordinator.taskq_chip_overflow_async
    reads: asyncio.Queue[asyncio.Event] = asyncio.Queue()

    async def held(self: Any, parent: str) -> int | None:
        gate = asyncio.Event()
        reads.put_nowait(gate)
        await await_until(gate, what="the read's gate")
        return await real(self, parent)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_chip_overflow_async", held)
    try:
        events = _record(mgr)
        mgr._queue_wait[_PARENT] = dict(_STALE_WAIT)
        mgr._emit_queue_depth(_PARENT)
        for _ in range(4):
            gate = await _finished(reads.get(), "the next read starting")
            assert _depths(events) == []
            mgr._emit_queue_depth(_PARENT)  # lands during this read
            clock["now"] += 0.3
            gate.set()
        # The fourth read was published although a request overlapped it, and
        # that request is answered by one more read.
        gate = await _finished(reads.get(), "the next read starting")
        assert _depths(events) == [{"queued": 0}]
        assert mgr._queue_wait[_PARENT] == _STALE_WAIT, "an overlapped 0 keeps the label"
        gate.set()
        await _settle(mgr)

        assert _depths(events) == [{"queued": 0}, {"queued": 0}]
        assert _PARENT not in mgr._queue_wait, "a 0 no request overlapped forgets it"
    finally:
        _close(mgr)


# ── the delayed re-read: one per parent, answered by any frame ──────────────


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_failing_requests_share_one_retry_chain(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        calls = _fail_chip_reads(monkeypatch, times=1000)
        with caplog.at_level("WARNING", logger=_DEPTH_LOGGER):
            for _ in range(3):
                mgr._emit_queue_depth(_PARENT)
                await settle_depth_emits(mgr)
            await _until(
                lambda: _warned(caplog, _DEPTH_LOGGER, "unreadable after"),
                "the last retry gave up, at WARNING",
            )
        await _settle(mgr)

        assert len(calls) == 3 + _QUEUE_DEPTH_RETRIES
        assert _warned(caplog, _DEPTH_LOGGER, "unreadable after") == 1
        assert mgr._queue_depth_retries == {}
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_parent_has_at_most_one_armed_retry(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    monkeypatch.setattr(run_mod, "_QUEUE_DEPTH_RETRY_SECS", 60.0)
    try:
        _fail_chip_reads(monkeypatch, times=1000)
        mgr._emit_queue_depth(_PARENT, "wave-a")
        await settle_depth_emits(mgr)
        armed = mgr._queue_depth_retries[_PARENT]
        mgr._emit_queue_depth(_PARENT, "wave-b")
        await settle_depth_emits(mgr)

        assert mgr._queue_depth_retries[_PARENT] is armed
        assert not armed.handle.cancelled()
        assert armed.batch_ids == {"wave-a", "wave-b"}
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_frame_published_first_disarms_the_retry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    monkeypatch.setattr(run_mod, "_QUEUE_DEPTH_RETRY_SECS", 60.0)
    try:
        events = _record(mgr)
        _fail_chip_reads(monkeypatch, times=1)
        mgr._emit_queue_depth(_PARENT)
        await settle_depth_emits(mgr)
        handle = mgr._queue_depth_retries[_PARENT].handle

        with caplog.at_level("WARNING", logger=_DEPTH_LOGGER):
            mgr._emit_queue_depth(_PARENT)
            await _settle(mgr)

        assert _depths(events) == [{"queued": 0}]
        assert handle.cancelled()
        assert mgr._queue_depth_retries == {}
        # The disarm is a sanctioned cancel of a manager-owned timer, so the
        # chokepoint must recognize it rather than log a missing marker.
        assert _warned(caplog, _DEPTH_LOGGER, "WITHOUT a terminal marker") == 0
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_shutdown_cancels_an_armed_retry(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    monkeypatch.setattr(run_mod, "_QUEUE_DEPTH_RETRY_SECS", 60.0)
    try:
        _fail_chip_reads(monkeypatch, times=1)
        mgr._emit_queue_depth(_PARENT)
        await settle_depth_emits(mgr)
        handle = mgr._queue_depth_retries[_PARENT].handle

        await mgr.cancel_all()

        assert handle.cancelled()
        assert mgr._queue_depth_retries == {}
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_fresh_request_restores_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A request that joins a burst on its last retry gets retries of its own:
    otherwise a card stopped during a flapping outage is never repaired."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    outage = {"on": True}
    real = SpawnAdmissionCoordinator.taskq_chip_overflow_async
    reading, release = asyncio.Event(), asyncio.Event()

    async def flapping(self: Any, parent: str) -> int | None:
        if not release.is_set():
            reading.set()
            await await_until(release, what="the test's release")
        return None if outage["on"] else await real(self, parent)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_chip_overflow_async", flapping)
    try:
        events = _record(mgr)
        mgr._run_events._request_queue_depth(_PARENT, set(), attempt=_QUEUE_DEPTH_RETRIES)
        await await_until(reading, what="the flapping read starting")
        mgr._emit_queue_depth(_PARENT)
        release.set()
        await settle_depth_emits(mgr)
        assert _PARENT in mgr._queue_depth_retries
        outage["on"] = False
        await _until(lambda: bool(_depths(events)), "the retry published")

        assert _depths(events) == [{"queued": 0}]
    finally:
        release.set()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_frame_names_a_wave_only_when_every_request_named_it(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        batches: list[str] = []

        async def on_event(etype: str, info: Any, extra: dict[str, Any]) -> None:
            if etype == "subagent_queued":
                batches.append(info.batch_id)

        mgr._on_event = on_event
        mgr._emit_queue_depth(_PARENT, "wave-a")
        mgr._emit_queue_depth(_PARENT, "wave-a")
        await _settle(mgr)
        mgr._emit_queue_depth(_PARENT, "wave-a")
        mgr._emit_queue_depth(_PARENT, "wave-b")
        await _settle(mgr)

        assert batches == ["wave-a", ""]
    finally:
        _close(mgr)
