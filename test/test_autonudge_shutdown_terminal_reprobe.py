"""AutoNudgeService shutdown-drain tests: terminal settlement and reprobe retention.

One of the sibling topic modules split from the original
``test_autonudge_shutdown_drain.py``. These cover a delivered terminal marker
settling before the gate opt-out, a terminal settlement draining its writes but
not its reprobe, and a restart settling an owed terminal turn exactly once. New
reprobe-retention tests belong here.

``_enable`` is autouse (imported from ``autonudge_helpers``); ``svc_fixture``
registers the ``svc`` fixture. ``_reading``/``_terminal_poll`` are the terminal
reading fakes, used only by this module.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import (
    AsyncMock,
    patch,
)

import pytest
from autonudge_helpers import _enable  # noqa: F401
from autonudge_helpers import _structured_monitor
from autonudge_shutdown_drain_helpers import _LOST_RUN_SECS, _wait_until

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import (
    AutoNudgeService,
    NudgeLoop,
)
from kiro_crew.monitoring import models as monitor_models
from kiro_crew.monitoring.models import (
    MONITOR_STATE_VERSION,
    MonitorBudgets,
    MonitorOutcome,
    MonitorState,
)


def _reading(state: str = "MERGED", *, status: str = "ok", head: str = "a" * 40):
    """One published reading, in the shape the fetcher puts on the probe.

    The terminal decision is the core's, taken from a typed fact, so a test that
    wants a terminal tick has to supply the FACT rather than a verdict. Pinning only
    the kernel verdict would test a state the code cannot reach: the fetcher emits no
    terminal observation, so no kernel verdict is ever TERMINAL.
    """
    from kiro_crew.probes import gh_pr

    return gh_pr.PrObservation(
        repo="acme/widgets",
        pr=42,
        host="github.com",
        status=status,
        observed_at=1_000.0,
        state=state,
        merged_at="2026-09-25T00:00:00Z" if state == "MERGED" else "",
        head=head,
    )


def _terminal_poll(state: str = "MERGED", body: str = "read"):
    """A poll fake that publishes a terminal reading, the way ``observe`` does."""

    def _poll(identity, message, probe):
        probe.observation = _reading(state)
        # The real gh-pr probe is a FETCHER: it returns ``observations=[]`` and never
        # attributes a TERMINAL key, so the kernel's verdict carries no keys on this
        # path. Emitting a key here would feed the code a signal the real probe never
        # produces and mask the merged-versus-blocked decision, which for a pull
        # request comes from ``observation.merged``, not from ``verdict.keys``.
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, body)

    return _poll


@pytest.mark.asyncio
async def test_a_delivered_terminal_marker_settles_before_the_gate_opt_out(tmp_path, monkeypatch):
    """A gate edit cannot buy a second turn after terminal delivery landed."""
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_delivered = "success"
    loop = NudgeLoop(
        id="monitor-delivered-gate-off",
        slot_key="slack:C123:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        cycle_count=1,
        monitor=monitor,
        gate=False,
    )
    service._loops[loop.id] = loop
    judge = AsyncMock(return_value=False)
    reprobe = AsyncMock(return_value="holds")
    monkeypatch.setattr(service, "_judge_tick_is_quiet", judge)
    monkeypatch.setattr(service, "_terminal_still_holds", reprobe)

    try:
        assert await service._monitor_tick_is_quiet(loop) is True
    finally:
        service.stop()

    judge.assert_not_awaited()
    reprobe.assert_awaited_once_with(loop, monitor)
    on_fire.assert_not_awaited()
    assert loop.active is False
    assert monitor.outcome is MonitorOutcome.SUCCESS
    assert monitor.terminal_delivered == ""


@pytest.mark.asyncio
async def test_a_retarget_drops_the_previous_subjects_delivered_terminal_marker(tmp_path):
    """The marker names what was delivered for the OLD subject. Kept across a
    retarget, the next tick would settle the new subject on it without observing it,
    and a new subject already in the same terminal state would never get its turn."""
    service = AutoNudgeService(base_dir=tmp_path)
    try:
        loop = await service.add_monitor(
            slot_key="retarget-after-delivery",
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/42",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )
        assert loop.monitor is not None
        loop.monitor.terminal_delivered = "success"
        service._terminal_reprobe_unknowns[loop.id] = 3

        updated = await service.update_monitor(
            loop.id, target="https://github.com/acme/widgets/pull/43"
        )

        assert updated is not None and updated.monitor is not None
        assert updated.monitor.target.endswith("/pull/43")
        assert updated.monitor.terminal_delivered == ""
        # The count goes with the marker, or the new subject's first delivered
        # terminal would skip straight past its settlement-only re-probes.
        assert loop.id not in service._terminal_reprobe_unknowns
        assert _stored_row(tmp_path)["monitor"]["terminal_delivered"] == ""
    finally:
        service.stop()


async def _write_terminal_restart_row(
    tmp_path, owed: str = "", *, delivered: str = "", cycle_count: int
) -> None:
    service = AutoNudgeService(base_dir=tmp_path)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_pending = owed
    monitor.terminal_delivered = delivered
    loop = NudgeLoop(
        id="monitor-terminal-restart",
        slot_key="slack:C0123456:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        cycle_count=cycle_count,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop
    await asyncio.to_thread(service._save)


def _owed_terminal_loop(loop_id: str) -> NudgeLoop:
    """A channel PR watch whose final turn is owed, ready for its delivering fire."""
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_pending = "success"
    return NudgeLoop(
        id=loop_id,
        slot_key="slack:C0123456:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )


async def _cancel_pending(*tasks: "asyncio.Task | None") -> None:
    """Cancel and reap whatever a failed assertion left running."""
    pending = [task for task in tasks if task is not None and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=_LOST_RUN_SECS
        )


def _stored_row(tmp_path) -> dict:
    stored = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
    return stored["loops"][0]


def _writes_the_settlement(payload, loop_id: str) -> bool:
    """Whether a snapshot payload carries *loop_id*'s staged settlement row.

    The settlement stages the deactivated row and writes it while the live loop is
    still active, so the write is told apart from the marker writes by the row it
    carries, never by live state.
    """
    if payload is None:
        return False
    return any(row.get("id") == loop_id and row.get("active") is False for row in payload["loops"])


@pytest.mark.asyncio
async def test_shutdown_skips_a_terminal_settlement_provider_reprobe(tmp_path, monkeypatch):
    """A delivery record that shutdown drains starts no re-probe once it lands.

    The record is a drained section, so shutdown waits for the delivered marker to
    reach the store. The settlement after it reads admission before its first await,
    so the drained callback leaves without a lock wait or provider I/O that shutdown,
    not watching it, could not cancel.
    """
    service = AutoNudgeService(base_dir=tmp_path, on_fire=AsyncMock(return_value=True))
    await service.start()
    loop = _owed_terminal_loop("monitor-terminal-shutdown")
    monitor = loop.monitor
    assert monitor is not None
    service._loops[loop.id] = loop
    monkeypatch.setattr(service, "_monitor_tick_is_quiet", AsyncMock(return_value=False))
    probe = AsyncMock(return_value="holds")
    monkeypatch.setattr(service, "_terminal_still_holds", probe)
    lock_requests: list[str] = []
    real_acquire = service._acquire_mutation_lock

    async def _record_lock_request(loop_id, **kwargs):
        lock_requests.append(loop_id)
        return await asyncio.wait_for(real_acquire(loop_id, **kwargs), timeout=_LOST_RUN_SECS)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _record_lock_request)
    real_persist = service._persist_locked
    record_parked = asyncio.Event()
    release_record = asyncio.Event()

    async def _park_the_delivery_record(**kwargs):
        if monitor.terminal_delivered == "success" and not record_parked.is_set():
            record_parked.set()
            await asyncio.wait_for(release_record.wait(), timeout=_LOST_RUN_SECS)
        await real_persist(**kwargs)

    monkeypatch.setattr(service, "_persist_locked", _park_the_delivery_record)
    service._arm_timer(loop, delay=0.0)
    settlement = service._timers[loop.id]
    shutdown = None
    try:
        await asyncio.wait_for(record_parked.wait(), timeout=_LOST_RUN_SECS)
        assert settlement in _an._timers._drain_registered_owners(service)

        shutdown = asyncio.create_task(service.shutdown())
        await _wait_until(lambda: not service._accepting_mutations, "shutdown admission closure")
        release_record.set()
        await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)
        await asyncio.wait_for(settlement, timeout=_LOST_RUN_SECS)
    finally:
        release_record.set()
        await _cancel_pending(settlement, shutdown)

    assert lock_requests == [], "a drained callback must not queue for the settlement lock"
    probe.assert_not_awaited()
    assert monitor.terminal_delivered == "success"
    stored = _stored_row(tmp_path)
    assert stored["monitor"]["terminal_delivered"] == "success"
    assert stored["cycle_count"] == 1


async def _cut_an_in_flight_terminal_reprobe(tmp_path, monkeypatch) -> AsyncMock:
    """Deliver an owed terminal turn, then shut down while its re-probe is running.

    The stand-in re-probe waits on an event nothing sets, so only shutdown can end
    it before the lost-run ceiling does. Returns the delivery callback, which must
    have run exactly once.
    """
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    await service.start()
    loop = _owed_terminal_loop("monitor-terminal-reprobe-cut")
    monitor = loop.monitor
    assert monitor is not None
    service._loops[loop.id] = loop
    monkeypatch.setattr(service, "_monitor_tick_is_quiet", AsyncMock(return_value=False))
    probe_started = asyncio.Event()
    probe_cancelled = asyncio.Event()
    never_answers = asyncio.Event()
    drained_while_probing: list[bool] = []

    async def _reprobe_that_never_answers(probed_loop, probed_monitor):
        current = asyncio.current_task()
        drained_while_probing.append(current in _an._timers._drain_registered_owners(service))
        probe_started.set()
        try:
            await asyncio.wait_for(never_answers.wait(), timeout=_LOST_RUN_SECS)
        except asyncio.CancelledError:
            probe_cancelled.set()
            raise
        return "holds"

    monkeypatch.setattr(service, "_terminal_still_holds", _reprobe_that_never_answers)
    service._arm_timer(loop, delay=0.0)
    settlement = service._timers[loop.id]
    shutdown = None
    try:
        await asyncio.wait_for(probe_started.wait(), timeout=_LOST_RUN_SECS)
        assert drained_while_probing == [False], "the re-probe must sit outside the drain"
        assert monitor.terminal_delivered == "success"
        assert _stored_row(tmp_path)["monitor"]["terminal_delivered"] == "success"

        shutdown = asyncio.create_task(service.shutdown())
        try:
            await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)
        except asyncio.TimeoutError:
            pytest.fail("shutdown waited on the in-flight terminal re-probe")
        await asyncio.wait({settlement}, timeout=_LOST_RUN_SECS)
        assert settlement.done(), "the cancelled callback must have unwound"
    finally:
        await _cancel_pending(settlement, shutdown)

    assert probe_cancelled.is_set(), "shutdown must cancel the re-probe, not skip past it"
    assert settlement.cancelled()
    on_fire.assert_awaited_once_with(loop)
    assert monitor.terminal_delivered == "success", "the settlement stays owed"
    assert monitor.outcome is None
    assert loop.active is True
    stored = _stored_row(tmp_path)
    assert stored["monitor"]["terminal_delivered"] == "success"
    assert stored["active"] is True
    assert stored["cycle_count"] == 1
    return on_fire


@pytest.mark.asyncio
async def test_shutdown_cancels_a_terminal_reprobe_already_in_flight(tmp_path, monkeypatch):
    """A re-probe running when shutdown begins is cancelled, never awaited.

    The real re-probe is a ``gh`` call that can take 25 s. Shutdown cancels the
    callback running it like any other; the delivered marker written before it is
    what keeps the settlement owed without repeating the channel turn.
    """
    await _cut_an_in_flight_terminal_reprobe(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_restart_settles_a_cut_terminal_reprobe_once(tmp_path, monkeypatch):
    """The next start settles what the cancelled re-probe left, without a second turn."""
    await _cut_an_in_flight_terminal_reprobe(tmp_path, monkeypatch)
    on_fire = AsyncMock(return_value=True)
    restarted = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monkeypatch.setattr(_an.irq, "poll", _terminal_poll())
    await restarted.start()
    loop = restarted.get_by_id("monitor-terminal-reprobe-cut")
    assert loop is not None and loop.monitor is not None
    assert loop.monitor.terminal_delivered == "success"
    restarted._cancel_timer(loop.id)
    probe = AsyncMock(return_value="holds")
    monkeypatch.setattr(restarted, "_terminal_still_holds", probe)
    try:
        await asyncio.wait_for(restarted._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)
    finally:
        await asyncio.wait_for(restarted.shutdown(), timeout=_LOST_RUN_SECS)

    on_fire.assert_not_awaited()
    probe.assert_awaited_once_with(loop, loop.monitor)
    assert loop.active is False
    assert loop.monitor.outcome is MonitorOutcome.SUCCESS
    assert loop.monitor.terminal_delivered == ""
    assert loop.cycle_count == 1
    stored = _stored_row(tmp_path)
    assert stored["monitor"]["terminal_delivered"] == ""
    assert stored["active"] is False
    assert stored["cycle_count"] == 1


@contextmanager
def _a_version_one_build_that_predates(field_name: str) -> Iterator[None]:
    """Decode and encode monitor rows the way a version-1 build without *field_name* does.

    That build's decoder files every key its ``MonitorState`` does not declare under
    ``extra_fields``, and its encoder writes those keys back after the fields it knows.
    Hiding one field from ``fields()`` and ``asdict()`` inside ``monitoring.models`` is
    exactly that reading; the rest of the service runs unchanged.
    """
    real_fields = monitor_models.fields
    real_asdict = monitor_models.asdict

    def _fields(cls):
        found = real_fields(cls)
        if cls is MonitorState:
            return tuple(item for item in found if item.name != field_name)
        return found

    def _asdict(obj):
        payload = real_asdict(obj)
        if isinstance(obj, MonitorState):
            payload.pop(field_name, None)
        return payload

    with (
        patch.object(monitor_models, "fields", _fields),
        patch.object(monitor_models, "asdict", _asdict),
    ):
        yield


@pytest.mark.asyncio
async def test_a_rollback_keeps_a_delivered_but_unsettled_monitor_running_and_delivers_nothing(
    tmp_path, monkeypatch
):
    """A gateway rolled back past the delivered marker keeps the watch and owes nothing.

    The record stays at state version 1 with ``terminal_pending`` empty, so the older
    build neither blocks it as an unsupported version nor reads an owed turn: it loads
    the loop active, files ``terminal_delivered`` under ``extra_fields``, observes the
    subject as it always did and writes the key back untouched. The upgraded gateway
    then finds the marker and settles once without a second turn. Whether the older
    build owes a fresh terminal turn afterwards is decided by its own new observation,
    as on main; this pins that the stored record itself owes it none.
    """
    await _cut_an_in_flight_terminal_reprobe(tmp_path, monkeypatch)
    stored = _stored_row(tmp_path)["monitor"]
    assert stored["version"] == MONITOR_STATE_VERSION == 1
    assert stored["terminal_pending"] == ""
    assert stored["terminal_delivered"] == "success"

    rolled_back_fire = AsyncMock(return_value=True)
    with _a_version_one_build_that_predates("terminal_delivered"):
        rolled_back = AutoNudgeService(base_dir=tmp_path, on_fire=rolled_back_fire)
        monkeypatch.setattr(
            _an.irq, "poll", lambda *a, **k: _an.irq.Verdict(_an.irq.Outcome.QUIET, "nothing new")
        )
        await rolled_back.start()
        try:
            loop = rolled_back.get_by_id("monitor-terminal-reprobe-cut")
            assert loop is not None and loop.monitor is not None
            assert loop.active is True, "a rollback must not stop the structured monitor"
            assert loop.monitor.version == MONITOR_STATE_VERSION
            assert loop.monitor.stopped_reason == ""
            assert loop.monitor.outcome is None
            assert loop.monitor.terminal_pending == ""
            assert loop.monitor.extra_fields == {"terminal_delivered": "success"}
            assert loop.id in rolled_back._timers, "the older build re-armed the watch"
            rolled_back._cancel_timer(loop.id)
            assert (
                await rolled_back._monitor_tick_is_quiet(loop) is True
            ), "the older build read an owed turn into the stored record"
            async with rolled_back._lock:
                await rolled_back._write_monitor_snapshot_locked()
        finally:
            await asyncio.wait_for(rolled_back.shutdown(), timeout=_LOST_RUN_SECS)
    rolled_back_fire.assert_not_awaited()
    assert _stored_row(tmp_path)["monitor"]["terminal_delivered"] == "success"

    upgraded_fire = AsyncMock(return_value=True)
    upgraded = AutoNudgeService(base_dir=tmp_path, on_fire=upgraded_fire)
    await upgraded.start()
    loop = upgraded.get_by_id("monitor-terminal-reprobe-cut")
    assert loop is not None and loop.monitor is not None
    assert loop.monitor.terminal_delivered == "success"
    upgraded._cancel_timer(loop.id)
    monkeypatch.setattr(upgraded, "_terminal_still_holds", AsyncMock(return_value="holds"))
    try:
        await asyncio.wait_for(upgraded._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)
    finally:
        await asyncio.wait_for(upgraded.shutdown(), timeout=_LOST_RUN_SECS)
    upgraded_fire.assert_not_awaited()
    assert loop.active is False
    assert loop.monitor.outcome is MonitorOutcome.SUCCESS
    assert loop.monitor.terminal_delivered == ""
    assert _stored_row(tmp_path)["monitor"]["terminal_delivered"] == ""


@pytest.mark.asyncio
async def test_a_terminal_settlement_drains_its_writes_but_not_its_reprobe(tmp_path, monkeypatch):
    """With admission open the settlement lands as before, around an undrained re-probe.

    The re-probe runs outside every drained section and the settlement write inside
    one, so moving the re-probe back into the drain -- or the write out of it -- is
    caught here on the ordinary path, before any shutdown.
    """
    service = AutoNudgeService(base_dir=tmp_path, on_fire=AsyncMock(return_value=True))
    loop = _owed_terminal_loop("monitor-terminal-normal")
    monitor = loop.monitor
    assert monitor is not None
    service._loops[loop.id] = loop
    monkeypatch.setattr(service, "_monitor_tick_is_quiet", AsyncMock(return_value=False))
    events: list[str] = []
    monkeypatch.setattr(service, "_emit", lambda event, _loop: events.append(event))
    drained_while_probing: list[bool] = []
    drained_while_settling: list[bool] = []

    def _drained() -> bool:
        return asyncio.current_task() in _an._timers._drain_registered_owners(service)

    async def _reprobe_confirms(probed_loop, probed_monitor):
        drained_while_probing.append(_drained())
        return "holds"

    monkeypatch.setattr(service, "_terminal_still_holds", _reprobe_confirms)
    real_snapshot = service._write_monitor_snapshot_locked
    settlement_write_observed = False

    async def _observe_the_settlement_write(payload=None, **kwargs):
        nonlocal settlement_write_observed
        if _writes_the_settlement(payload, loop.id):
            settlement_write_observed = True
            drained_while_settling.append(_drained())
        await real_snapshot(payload, **kwargs)

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", _observe_the_settlement_write)
    try:
        await asyncio.wait_for(service._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)
    finally:
        service.stop()

    assert drained_while_probing == [False]
    assert drained_while_settling == [True]
    assert settlement_write_observed, "the staged settlement selector must fire"
    assert loop.active is False
    assert monitor.outcome is MonitorOutcome.SUCCESS
    assert monitor.terminal_pending == ""
    assert loop.cycle_count == 1
    assert [event for event in events if event in ("expired", "fired")] == ["expired", "fired"]
    stored = _stored_row(tmp_path)
    assert stored["monitor"]["terminal_pending"] == ""
    assert stored["active"] is False
    assert not [
        future
        for future in service._inflight_adds
        if isinstance(future, _an._timers._RegisteredSectionCompletion)
    ], "every drained section must resolve its completion"


@pytest.mark.asyncio
async def test_MUTATION_a_holds_settlement_retires_a_timer_rearmed_during_its_write(
    tmp_path, monkeypatch
):
    """Publishing a staged stop retires a timer armed while its write waited."""
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _owed_terminal_loop("monitor-terminal-rearmed-during-settlement")
    monitor = loop.monitor
    assert monitor is not None
    monitor.terminal_delivered = "success"
    loop.slot_key = "slot-terminal-rearmed-during-settlement"
    loop.cycle_count = 1
    loop.next_due_ts = time.time() + 60
    service._loops[loop.id] = loop
    events: list[str] = []
    monkeypatch.setattr(service, "_emit", lambda event, _loop: events.append(event))
    monkeypatch.setattr(service, "_terminal_still_holds", AsyncMock(return_value="holds"))
    writer_started = asyncio.Event()
    release_writer = asyncio.Event()
    real_snapshot = service._write_monitor_snapshot_locked

    async def _park_the_settlement_write(payload=None, **kwargs):
        if not _writes_the_settlement(payload, loop.id):
            await real_snapshot(payload, **kwargs)
            return
        writer_started.set()
        try:
            await asyncio.wait_for(release_writer.wait(), timeout=_LOST_RUN_SECS)
        except asyncio.CancelledError:
            await asyncio.wait_for(release_writer.wait(), timeout=_LOST_RUN_SECS)
            await real_snapshot(payload, **kwargs)
            raise
        await real_snapshot(payload, **kwargs)

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", _park_the_settlement_write)
    settlement = asyncio.create_task(service._settle_delivered_terminal(loop))
    service._timers[loop.id] = settlement
    rearmed = None
    try:
        await asyncio.wait_for(writer_started.wait(), timeout=_LOST_RUN_SECS)
        service.notify_turn_complete(loop.slot_key)
        rearmed = service._timers.get(loop.id)
        assert rearmed is not None and rearmed is not settlement
        release_writer.set()
        await asyncio.wait_for(
            asyncio.gather(settlement, return_exceptions=True),
            timeout=_LOST_RUN_SECS,
        )
        assert loop.id not in service._timers
        assert rearmed.done()
    finally:
        release_writer.set()
        await _cancel_pending(settlement, rearmed)
        service.stop()

    assert settlement.cancelled()
    on_fire.assert_not_awaited()
    assert events == ["expired"]
    assert loop.cycle_count == 1
    assert loop.active is False
    assert monitor.outcome is MonitorOutcome.SUCCESS


@pytest.mark.asyncio
async def test_fire_now_refuses_while_a_holds_settlement_write_is_in_flight(tmp_path, monkeypatch):
    """Manual fire cannot report success for a timer the settlement will retire."""
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _owed_terminal_loop("monitor-terminal-manual-fire-during-settlement")
    monitor = loop.monitor
    assert monitor is not None
    monitor.terminal_delivered = "success"
    loop.cycle_count = 1
    service._loops[loop.id] = loop
    events: list[str] = []
    monkeypatch.setattr(service, "_emit", lambda event, _loop: events.append(event))
    monkeypatch.setattr(service, "_terminal_still_holds", AsyncMock(return_value="holds"))
    writer_started = asyncio.Event()
    release_writer = asyncio.Event()
    real_snapshot = service._write_monitor_snapshot_locked

    async def _park_the_settlement_write(payload=None, **kwargs):
        if _writes_the_settlement(payload, loop.id):
            writer_started.set()
            await asyncio.wait_for(release_writer.wait(), timeout=_LOST_RUN_SECS)
        await real_snapshot(payload, **kwargs)

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", _park_the_settlement_write)
    settlement = asyncio.create_task(service._settle_delivered_terminal(loop))
    try:
        await asyncio.wait_for(writer_started.wait(), timeout=_LOST_RUN_SECS)
        result = await service.fire_now(loop.id)
        assert result == (None, "loop terminal settlement is in progress", 409)
        assert loop.id not in service._timers
        release_writer.set()
        await asyncio.wait_for(settlement, timeout=_LOST_RUN_SECS)
    finally:
        release_writer.set()
        await _cancel_pending(settlement, service._timers.get(loop.id))
        service.stop()

    on_fire.assert_not_awaited()
    assert events == ["expired"]
    assert loop.cycle_count == 1
    assert loop.active is False
    assert monitor.outcome is MonitorOutcome.SUCCESS


@pytest.mark.asyncio
async def test_failed_delivery_marker_write_stays_delivered_for_the_cycle_persist(
    tmp_path, monkeypatch
):
    """A failed first persist cannot turn a delivered terminal back into owed work.

    The later cycle-bookkeeping persist is the recovery write. Moving the outcome
    back into ``terminal_pending`` in the first failure branch makes the durable row
    owed again and the restarted service calls ``_on_fire`` a second time.
    """
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _owed_terminal_loop("monitor-terminal-delivery-persist")
    monitor = loop.monitor
    assert monitor is not None
    service._loops[loop.id] = loop
    first_persist_failed = False
    real_persist = service._persist_locked

    async def _fail_the_marker_once(**kwargs):
        nonlocal first_persist_failed
        if monitor.terminal_delivered == "success" and not first_persist_failed:
            first_persist_failed = True
            raise OSError("first marker write failed")
        await real_persist(**kwargs)

    monkeypatch.setattr(service, "_persist_locked", _fail_the_marker_once)
    monkeypatch.setattr(service, "_settle_delivered_terminal", AsyncMock())
    await asyncio.wait_for(service._run_fire_cycle(loop), timeout=_LOST_RUN_SECS)

    assert first_persist_failed, "the marker write failure must be exercised"
    on_fire.assert_awaited_once_with(loop)
    assert monitor.terminal_delivered == "success"
    stored = _stored_row(tmp_path)
    assert stored["monitor"]["terminal_delivered"] == "success"
    assert stored["cycle_count"] == 1
    service.stop()

    restarted_fire = AsyncMock(return_value=True)
    restarted = AutoNudgeService(base_dir=tmp_path, on_fire=restarted_fire)
    await restarted.start()
    restored = restarted.get_by_id(loop.id)
    assert restored is not None and restored.monitor is not None
    restarted._cancel_timer(restored.id)
    reprobe = AsyncMock(return_value="holds")
    monkeypatch.setattr(restarted, "_terminal_still_holds", reprobe)
    try:
        await asyncio.wait_for(restarted._timer(restored, delay=0.0), timeout=_LOST_RUN_SECS)
    finally:
        await asyncio.wait_for(restarted.shutdown(), timeout=_LOST_RUN_SECS)

    restarted_fire.assert_not_awaited()
    reprobe.assert_awaited_once_with(restored, restored.monitor)
    assert restored.active is False
    assert restored.monitor.outcome is MonitorOutcome.SUCCESS
    assert restored.monitor.terminal_delivered == ""
    assert restored.cycle_count == 1


@pytest.mark.asyncio
async def test_MUTATION_a_failed_gone_reprobe_write_keeps_delivered_state_until_retry(
    tmp_path, monkeypatch
):
    """A definite-gone answer reaches memory only after its staged write lands."""
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_delivered = "success"
    loop = NudgeLoop(
        id="monitor-terminal-gone-write-fails",
        slot_key="slack:C0123456:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        cycle_count=1,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop
    reprobe = AsyncMock(side_effect=["gone", "gone"])
    monkeypatch.setattr(service, "_terminal_still_holds", reprobe)
    real_writer = service._write_monitor_snapshot_locked
    writes = 0

    async def _fail_once(payload=None, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("store unavailable")
        await real_writer(payload, **kwargs)

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", _fail_once)
    try:
        await asyncio.wait_for(service._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)
        service._cancel_timer(loop.id)
        assert monitor.terminal_delivered == "success", "the marker cleared in memory only"

        await asyncio.wait_for(service._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)
        service._cancel_timer(loop.id)
    finally:
        service.stop()

    assert writes == 2
    assert reprobe.await_count == 2
    on_fire.assert_not_awaited()
    assert monitor.terminal_delivered == ""
    stored = _stored_row(tmp_path)["monitor"]
    assert stored["terminal_delivered"] == ""


@pytest.mark.asyncio
async def test_MUTATION_a_failed_holds_settlement_write_keeps_memory_store_and_restart_agreed(
    tmp_path, monkeypatch
):
    """A definite-holds settlement reaches memory only after its staged write lands.

    A failed write leaves the live delivered marker and the active loop exactly as
    the store holds them and a restart loads them, and announces nothing.
    Deactivating the loop before the write fails here, because memory then says what
    no store or restart says. The next successful tick settles and announces once.
    """
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_delivered = "success"
    loop = NudgeLoop(
        id="monitor-terminal-holds-write-fails",
        slot_key="slack:C0123456:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        cycle_count=1,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop
    await asyncio.to_thread(service._save)
    events: list[str] = []
    monkeypatch.setattr(service, "_emit", lambda event, _loop: events.append(event))
    reprobe = AsyncMock(side_effect=["holds", "holds"])
    monkeypatch.setattr(service, "_terminal_still_holds", reprobe)
    real_writer = service._write_monitor_snapshot_locked
    writes = 0

    async def _fail_once(payload=None, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("store unavailable")
        await real_writer(payload, **kwargs)

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", _fail_once)
    try:
        await asyncio.wait_for(service._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)
        service._cancel_timer(loop.id)
        assert writes == 1
        assert "expired" not in events, "a finish must not be announced unpersisted"
        assert loop.active is True, "the loop deactivated in memory only"
        assert monitor.outcome is None
        assert monitor.terminal_delivered == "success", "the marker cleared in memory only"
        stored = _stored_row(tmp_path)
        assert stored["active"] is True
        assert stored["monitor"]["terminal_delivered"] == "success"

        restarted = AutoNudgeService(base_dir=tmp_path, on_fire=AsyncMock(return_value=True))
        await restarted.start()
        try:
            restored = restarted.get_by_id(loop.id)
            assert restored is not None and restored.monitor is not None
            restarted._cancel_timer(restored.id)
            assert restored.active is True
            assert restored.monitor.outcome is None
            assert restored.monitor.terminal_delivered == "success"
        finally:
            await asyncio.wait_for(restarted.shutdown(), timeout=_LOST_RUN_SECS)

        await asyncio.wait_for(service._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)
        service._cancel_timer(loop.id)
    finally:
        service.stop()

    assert writes == 2
    assert reprobe.await_count == 2
    on_fire.assert_not_awaited()
    assert events.count("expired") == 1
    assert loop.active is False
    assert monitor.outcome is MonitorOutcome.SUCCESS
    assert monitor.terminal_delivered == ""
    stored = _stored_row(tmp_path)
    assert stored["active"] is False
    assert stored["monitor"]["terminal_delivered"] == ""


@pytest.mark.asyncio
async def test_restart_settles_a_delivered_terminal_without_refiring(tmp_path, monkeypatch):
    await _write_terminal_restart_row(tmp_path, delivered="success", cycle_count=1)
    on_fire = AsyncMock(return_value=True)
    restarted = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monkeypatch.setattr(_an.irq, "poll", _terminal_poll())
    await restarted.start()
    loop = restarted.get_by_id("monitor-terminal-restart")
    assert loop is not None and loop.monitor is not None
    restarted._cancel_timer(loop.id)
    probe = AsyncMock(return_value="holds")
    monkeypatch.setattr(restarted, "_terminal_still_holds", probe)

    await asyncio.wait_for(restarted._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)

    on_fire.assert_not_awaited()
    probe.assert_awaited_once_with(loop, loop.monitor)
    assert loop.active is False
    assert loop.monitor.outcome is MonitorOutcome.SUCCESS
    assert loop.monitor.terminal_delivered == ""
    assert loop.cycle_count == 1
    await restarted.shutdown()


@pytest.mark.asyncio
async def test_restart_legacy_terminal_marker_still_delivers_once(tmp_path, monkeypatch):
    await _write_terminal_restart_row(tmp_path, "success", cycle_count=0)
    on_fire = AsyncMock(return_value=True)
    restarted = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monkeypatch.setattr(_an.irq, "poll", _terminal_poll())
    await restarted.start()
    loop = restarted.get_by_id("monitor-terminal-restart")
    assert loop is not None and loop.monitor is not None
    restarted._cancel_timer(loop.id)

    await asyncio.wait_for(restarted._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)

    on_fire.assert_awaited_once_with(loop)
    assert loop.active is False
    assert loop.monitor.outcome is MonitorOutcome.SUCCESS
    assert loop.monitor.terminal_pending == ""
    assert loop.cycle_count == 1
    await restarted.shutdown()


@pytest.mark.asyncio
async def test_restart_reopened_delivered_terminal_clears_without_refiring(tmp_path, monkeypatch):
    await _write_terminal_restart_row(tmp_path, delivered="blocked", cycle_count=1)
    on_fire = AsyncMock(return_value=True)
    restarted = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monkeypatch.setattr(_an.irq, "poll", _terminal_poll("OPEN"))
    await restarted.start()
    loop = restarted.get_by_id("monitor-terminal-restart")
    assert loop is not None and loop.monitor is not None
    restarted._cancel_timer(loop.id)
    probe = AsyncMock(return_value="gone")
    monkeypatch.setattr(restarted, "_terminal_still_holds", probe)

    await asyncio.wait_for(restarted._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)

    on_fire.assert_not_awaited()
    probe.assert_awaited_once_with(loop, loop.monitor)
    assert loop.active is True
    assert loop.monitor.outcome is None
    assert loop.monitor.terminal_delivered == ""
    assert loop.cycle_count == 1
    await restarted.shutdown()


def _delivered_terminal_loop(loop_id: str, delivered: str = "success") -> NudgeLoop:
    """A channel PR watch whose final turn landed and whose settlement is still owed."""
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_delivered = delivered
    return NudgeLoop(
        id=loop_id,
        slot_key="slack:C0123456:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        cycle_count=1,
        monitor=monitor,
        gate=True,
    )


def _poll_sequence(*steps: tuple[str, str, "_an.irq.Outcome"]):
    """A poll fake answering ordinary ticks in order, one ``(state, status, outcome)``
    per call. The settlement re-probe is patched separately, so only ordinary
    observation reaches this fake."""
    remaining = list(steps)

    def _poll(identity, message, probe):
        state, status, outcome = remaining.pop(0)
        probe.observation = _reading(state, status=status)
        return _an.irq.Verdict(outcome, "read")

    return _poll


async def _tick(service: AutoNudgeService, loop: NudgeLoop) -> None:
    await asyncio.wait_for(service._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)
    service._cancel_timer(loop.id)


@pytest.mark.asyncio
async def test_unobservable_subject_keeps_the_delivered_marker_without_settling(
    tmp_path, monkeypatch
):
    """Absence of evidence must not retire a watch, and it is counted in memory.

    A failed fetch, a probe defect or a binding that does not resolve cannot CONFIRM
    that the subject is finished, and settling on that would be the silent stop this
    design resolves away from. The count is not part of the stored row.
    """

    def _explode(*_a, **_k):
        raise RuntimeError("gh unavailable")

    monkeypatch.setattr(_an.irq, "poll", _explode)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=AsyncMock(return_value=True))
    loop = _owed_terminal_loop("monitor42")
    service._loops[loop.id] = loop

    try:
        await service._run_fire_cycle(loop)
        assert loop.active is True, "an unobserved subject is not a finished one"
        assert loop.monitor is not None
        assert loop.monitor.terminal_pending == ""
        assert loop.monitor.terminal_delivered == "success"
        assert service._terminal_reprobe_unknowns[loop.id] == 1
        persisted = _stored_row(tmp_path)["monitor"]
        assert persisted["terminal_pending"] == ""
        assert persisted["terminal_delivered"] == "success"
        assert "terminal_reprobe_unknowns" not in persisted
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_MUTATION_an_indeterminate_reprobe_writes_nothing_and_keeps_the_marker(
    tmp_path, monkeypatch
):
    """An ``unknown`` answer only counts. A store that refuses every write cannot
    clear the marker or stall the count, because no write is attempted."""
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _delivered_terminal_loop("monitor-terminal-unknown-no-write")
    service._loops[loop.id] = loop
    reprobe = AsyncMock(return_value="unknown")
    monkeypatch.setattr(service, "_terminal_still_holds", reprobe)
    writes = AsyncMock(side_effect=OSError("store unavailable"))
    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", writes)

    try:
        await _tick(service, loop)
        assert service._terminal_reprobe_unknowns[loop.id] == 1
    finally:
        service.stop()

    reprobe.assert_awaited_once()
    writes.assert_not_awaited()
    on_fire.assert_not_awaited()
    assert loop.monitor is not None
    assert loop.monitor.terminal_delivered == "success"


@pytest.mark.asyncio
async def test_exhausted_unknown_reprobes_then_a_holds_reading_settle_without_a_second_turn(
    tmp_path, monkeypatch
):
    """A gh outage longer than the re-probe bound must not buy a second final turn.

    Three indeterminate re-probes hand the tick to ordinary observation with the
    marker kept. A still-unreadable subject then stays quiet (a judge screens it, as
    an installed one would), and the merged reading that finally arrives settles the
    watch through the ordinary terminal branch without firing. Clearing the marker at
    the bound fails here: that reading marks a new owed turn and fires a second
    'merged' turn.
    """
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _delivered_terminal_loop("monitor-terminal-unknown-then-holds")
    monitor = loop.monitor
    assert monitor is not None
    service._loops[loop.id] = loop
    reprobe = AsyncMock(return_value="unknown")
    monkeypatch.setattr(service, "_terminal_still_holds", reprobe)
    monkeypatch.setattr(service, "_judge_tick_is_quiet", AsyncMock(return_value=True))
    monkeypatch.setattr(
        _an.irq,
        "poll",
        _poll_sequence(
            ("MERGED", "unavailable", _an.irq.Outcome.QUIET),
            ("MERGED", "ok", _an.irq.Outcome.QUIET),
        ),
    )

    try:
        for expected in (1, 2, 3):
            await _tick(service, loop)
            assert service._terminal_reprobe_unknowns[loop.id] == expected
            assert monitor.terminal_delivered == "success"

        await _tick(service, loop)
        assert reprobe.await_count == 3, "an exhausted count stops re-probing"
        assert loop.active is True, "an unreadable subject is not a finished one"
        assert monitor.terminal_delivered == "success"
        assert monitor.terminal_pending == ""

        await _tick(service, loop)
        assert loop.id not in service._terminal_reprobe_unknowns
    finally:
        service.stop()

    on_fire.assert_not_awaited()
    assert reprobe.await_count == 3
    assert loop.active is False
    assert monitor.outcome is MonitorOutcome.SUCCESS
    assert monitor.terminal_delivered == ""
    assert monitor.terminal_pending == ""
    stored = _stored_row(tmp_path)
    assert stored["active"] is False
    assert stored["monitor"]["terminal_delivered"] == ""


@pytest.mark.asyncio
async def test_exhausted_unknown_reprobes_do_not_spend_a_busy_retry_credit_on_a_second_turn(
    tmp_path, monkeypatch
):
    """The retry credit a busy session's refusal granted buys no second final turn.

    The refused terminal fire grants one observation-free follow-up tick so the next
    tick retries the delivery. While the turn is owed that credit is never spent, so
    it is still standing when the retry lands. Once the re-probe bound is exhausted
    the tick has to observe the subject with the marker kept; spending the leftover
    credit instead fires a second 'merged' turn for a terminal already delivered.
    """
    refusals = iter([False])

    async def _busy_then_delivered(_loop: NudgeLoop) -> bool:
        return next(refusals, True)

    on_fire = AsyncMock(side_effect=_busy_then_delivered)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _owed_terminal_loop("monitor-terminal-busy-retry-then-unknown")
    monitor = loop.monitor
    assert monitor is not None
    service._loops[loop.id] = loop
    reprobe = AsyncMock(return_value="unknown")
    monkeypatch.setattr(service, "_terminal_still_holds", reprobe)
    monkeypatch.setattr(_an.irq, "poll", _poll_sequence(("MERGED", "ok", _an.irq.Outcome.QUIET)))

    try:
        await service._run_fire_cycle(loop)
        assert monitor.terminal_pending == "success", "a refused final turn stays owed"
        assert monitor.followup_ticks == 1

        await service._run_fire_cycle(loop)
        assert monitor.terminal_pending == ""
        assert monitor.terminal_delivered == "success"
        assert monitor.followup_ticks == 1, "the owed turn never spent the retry credit"

        while service._terminal_reprobe_unknowns.get(loop.id, 0) < 3:
            await _tick(service, loop)
        await _tick(service, loop)
    finally:
        service.stop()

    assert on_fire.await_count == 2, "the exhausted bound fired a second final turn"
    assert reprobe.await_count == 3
    assert loop.active is False
    assert monitor.outcome is MonitorOutcome.SUCCESS
    assert monitor.terminal_delivered == ""


@pytest.mark.asyncio
async def test_exhausted_unknown_reprobes_still_deliver_the_kernel_backstop(tmp_path, monkeypatch):
    """The kept marker does not hide the blind backstop: its wake fires a turn."""
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _delivered_terminal_loop("monitor-terminal-unknown-backstop")
    service._loops[loop.id] = loop
    service._terminal_reprobe_unknowns[loop.id] = 3
    reprobe = AsyncMock(return_value="unknown")
    monkeypatch.setattr(service, "_terminal_still_holds", reprobe)
    monkeypatch.setattr(
        _an.irq, "poll", _poll_sequence(("MERGED", "unavailable", _an.irq.Outcome.WAKE))
    )

    try:
        await _tick(service, loop)
    finally:
        service.stop()

    on_fire.assert_awaited_once_with(loop)
    assert loop.active is True
    assert loop.monitor is not None
    assert loop.monitor.terminal_delivered == "success"


@pytest.mark.asyncio
async def test_exhausted_unknown_reprobes_then_a_reopen_clears_and_the_next_close_fires(
    tmp_path, monkeypatch
):
    """A reopen reaches the user and drops the kept marker, so its next close does too.

    Keeping the marker across the reopen fails the final assertion: the second close
    carries the delivered classification and would settle with no turn.
    """
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _delivered_terminal_loop("monitor-terminal-unknown-reopen", delivered="blocked")
    monitor = loop.monitor
    assert monitor is not None
    service._loops[loop.id] = loop
    service._terminal_reprobe_unknowns[loop.id] = 3
    reprobe = AsyncMock(return_value="unknown")
    monkeypatch.setattr(service, "_terminal_still_holds", reprobe)
    monkeypatch.setattr(
        _an.irq,
        "poll",
        _poll_sequence(
            ("OPEN", "ok", _an.irq.Outcome.QUIET),
            ("CLOSED", "ok", _an.irq.Outcome.QUIET),
        ),
    )

    try:
        await _tick(service, loop)
        on_fire.assert_awaited_once_with(loop)
        assert loop.active is True
        assert monitor.terminal_delivered == ""
        assert loop.id not in service._terminal_reprobe_unknowns
        assert _stored_row(tmp_path)["monitor"]["terminal_delivered"] == ""

        await _tick(service, loop)
        # The new final turn earns its own settlement re-probe and a fresh count.
        assert service._terminal_reprobe_unknowns[loop.id] == 1
    finally:
        service.stop()

    reprobe.assert_awaited_once()
    assert on_fire.await_count == 2, "the second close is a new final turn"
    assert monitor.terminal_delivered == "blocked"


@pytest.mark.asyncio
async def test_MUTATION_a_reopen_publishes_the_cleared_marker_only_after_its_write_lands(
    tmp_path, monkeypatch
):
    """The ordinary-path clear is staged like the ``gone`` one, so no other snapshot
    can store a clear that this write then fails to make durable.

    The first write that stores the cleared marker must run while the live marker is
    still set; publishing the clear before taking ``_lock`` fails the first
    assertion. A failed write leaves memory as stored, and the next open reading
    retries the same way.
    """
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _delivered_terminal_loop("monitor-terminal-reopen-staged", delivered="blocked")
    monitor = loop.monitor
    assert monitor is not None
    service._loops[loop.id] = loop
    service._terminal_reprobe_unknowns[loop.id] = 3
    monkeypatch.setattr(service, "_terminal_still_holds", AsyncMock(return_value="unknown"))
    monkeypatch.setattr(
        _an.irq,
        "poll",
        _poll_sequence(
            ("OPEN", "ok", _an.irq.Outcome.QUIET),
            ("OPEN", "ok", _an.irq.Outcome.QUIET),
        ),
    )
    real_writer = service._write_monitor_snapshot_locked
    live_at_clearing_writes: list[str] = []

    async def _fail_the_first_clearing_write(payload=None, **kwargs):
        rows = (payload if payload is not None else service._serialize_state())["loops"]
        row = next(row for row in rows if row.get("id") == loop.id)
        if row["monitor"]["terminal_delivered"] == "":
            live_at_clearing_writes.append(monitor.terminal_delivered)
            if len(live_at_clearing_writes) == 1:
                raise OSError("store unavailable")
        await real_writer(payload, **kwargs)

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", _fail_the_first_clearing_write)
    try:
        await _tick(service, loop)
        assert live_at_clearing_writes == ["blocked"], "the clear was published before its write"
        assert monitor.terminal_delivered == "blocked", "a failed clear left memory cleared"

        await _tick(service, loop)
    finally:
        service.stop()

    # The retry's staged write is the second clearing write; a later one stores the
    # clear only after it was published.
    assert live_at_clearing_writes[:2] == ["blocked", "blocked"]
    assert monitor.terminal_delivered == ""
    assert loop.id not in service._terminal_reprobe_unknowns
    assert _stored_row(tmp_path)["monitor"]["terminal_delivered"] == ""


@pytest.mark.asyncio
async def test_MUTATION_an_outage_past_one_reprobe_costs_no_extra_turn(tmp_path, monkeypatch):
    """What a bound of three buys over one: an outage that outlasts the first
    re-probe costs no turn on the finished subject.

    The second tick re-probes again and settles. At a bound of one it observes
    normally instead, the kernel cannot conclude on the unreadable subject
    (FALLBACK), and the gate spends a model turn on a watch whose final turn has
    already landed, so this test fails.
    """
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _delivered_terminal_loop("monitor-terminal-outage-past-one-reprobe")
    monitor = loop.monitor
    assert monitor is not None
    service._loops[loop.id] = loop
    reprobe = AsyncMock(side_effect=["unknown", "holds"])
    monkeypatch.setattr(service, "_terminal_still_holds", reprobe)
    monkeypatch.setattr(
        _an.irq, "poll", _poll_sequence(("MERGED", "unavailable", _an.irq.Outcome.FALLBACK))
    )

    try:
        await _tick(service, loop)
        await _tick(service, loop)
    finally:
        service.stop()

    on_fire.assert_not_awaited()
    assert reprobe.await_count == 2
    assert loop.active is False
    assert monitor.outcome is MonitorOutcome.SUCCESS


@pytest.mark.asyncio
async def test_a_definite_reprobe_resets_the_unknown_count_and_settles_once(tmp_path, monkeypatch):
    """A definite answer breaks the unknown streak and settles without delivery."""
    on_fire = AsyncMock(return_value=True)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _delivered_terminal_loop("monitor-terminal-unknown-reset")
    monitor = loop.monitor
    assert monitor is not None
    service._loops[loop.id] = loop
    reprobe = AsyncMock(side_effect=["unknown", "holds"])
    monkeypatch.setattr(service, "_terminal_still_holds", reprobe)

    try:
        await _tick(service, loop)
        assert service._terminal_reprobe_unknowns[loop.id] == 1
        await asyncio.wait_for(service._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)
        assert loop.id not in service._terminal_reprobe_unknowns
    finally:
        service.stop()

    assert reprobe.await_count == 2
    on_fire.assert_not_awaited()
    assert monitor.terminal_delivered == ""
    assert monitor.outcome is MonitorOutcome.SUCCESS
    assert loop.active is False
    assert _stored_row(tmp_path)["monitor"]["terminal_delivered"] == ""
