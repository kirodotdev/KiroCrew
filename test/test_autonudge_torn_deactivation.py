"""Restart resilience for rows deactivated by no stop path of the service.

Every deactivation ``AutoNudgeService`` performs leaves a mark: a non-empty
``stopped_reason`` on the loop (``update`` defaults to ``"manual"``; the timer
bounds and the terminal settlement write theirs) or a terminal ``outcome`` on a
monitor record, and all of them clear ``next_due_ts``. A persisted row that is
inactive with NONE of those marks and a live deadline was therefore flipped by
a write outside the stop paths -- a store migrated between hosts, a hand edit.
Left alone it is a loop nobody stopped that nothing will ever arm again.
``_load`` resumes that shape. A reasonless row WITHOUT a schedule cannot be
told apart from a pause recorded before the reason field existed, so it stays
inactive and the directive re-arm keeps refusing it -- naming what is recorded
rather than a "manual" stop nobody made.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import (
    _OVERDUE_REARM_SECS,
    MANUAL_STOP_REASON,
    SENTINEL_DROPPED_REASON,
    AutoNudgeService,
    MonitorUpdateConflict,
    NudgeLoop,
    _is_torn_deactivation,
    _stopped_row_is_replaceable,
)
from kiro_crew.monitoring.models import (
    MonitorOutcome,
    MonitorState,
    monitor_state_to_dict,
)


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


def _row(**changes: object) -> dict[str, object]:
    """One persisted legacy row in the exact shape seen after the incident:
    ``active`` flipped, ``stopped_reason`` empty, schedule still on the row."""
    row: dict[str, object] = {
        "id": "2f8efda1",
        "slot_key": "chat-746-1788942058",
        "message": "patrol",
        "idle_secs": 1200,
        "max_cycles": 0,
        "cycle_count": 260,
        "active": False,
        "last_fire_ts": time.time() - 1500,
        "created_ts": time.time() - 86_400,
        "stop_sentinel_path": "/nonexistent/workspace/.stop-chat-746-1788942058",
        "max_runtime_secs": 0,
        "gate": False,
        "stopped_reason": "",
        "approval_stalled": False,
        "next_due_ts": time.time() - 300,
        "banner": "",
    }
    row.update(changes)
    return row


def _write_store(tmp_path, *rows: dict[str, object]) -> None:
    (tmp_path / "autonudge.json").write_text(json.dumps({"version": 1, "loops": list(rows)}))


def _monitor(**changes: object) -> MonitorState:
    values: dict[str, object] = {
        "kind": "github_pull_request",
        "target": "owner/repo#123",
        "objective": "review_ready",
        "created_ts": 1_000.0,
    }
    values.update(changes)
    return MonitorState(**values)


def _arm_delays(svc: AutoNudgeService) -> list[float | None]:
    delays: list[float | None] = []
    orig = svc._arm_timer

    def spy(loop: NudgeLoop, delay: float | None = None) -> None:
        delays.append(delay)
        orig(loop, delay)

    svc._arm_timer = spy  # type: ignore[method-assign]
    return delays


# ── the shape predicate ───────────────────────────────────────────────────────


_SENTINEL = "/nonexistent/workspace/.stop-chat-1-1"


def test_torn_shape_is_inactive_reasonless_with_a_live_deadline_and_a_sentinel():
    loop = NudgeLoop(
        id="a",
        slot_key="chat-1-1",
        message="m",
        active=False,
        next_due_ts=10.0,
        stop_sentinel_path=_SENTINEL,
    )
    assert _is_torn_deactivation(loop)


@pytest.mark.parametrize(
    "changes",
    [
        {"active": True},
        {"stopped_reason": MANUAL_STOP_REASON},
        {"stopped_reason": "cycle_cap"},
        {"next_due_ts": 0.0},
        {"stop_sentinel_path": ""},
    ],
)
def test_marked_scheduleless_or_sentinel_less_rows_are_not_torn(changes):
    """A recorded stop or a cleared schedule is a deactivation the service made;
    an empty sentinel is the shape an older sentinel-drop refusal persisted."""
    loop = NudgeLoop(
        id="a",
        slot_key="chat-1-1",
        message="m",
        active=False,
        next_due_ts=10.0,
        stop_sentinel_path=_SENTINEL,
    )
    for name, value in changes.items():
        setattr(loop, name, value)
    assert not _is_torn_deactivation(loop)


def test_settled_or_inflight_monitor_rows_are_not_torn():
    """Monitor state owns its own recovery: a terminal outcome is a recorded stop
    and an in-flight wake belongs to the claim-recovery branches of ``_load``."""
    base = dict(
        id="a",
        slot_key="chat-1-1",
        message="m",
        active=False,
        next_due_ts=10.0,
        stop_sentinel_path=_SENTINEL,
    )
    settled = NudgeLoop(**base, gate=True, monitor=_monitor(outcome=MonitorOutcome.SUCCESS))
    inflight = NudgeLoop(**base, gate=True, monitor=_monitor(wake_in_flight=True))
    unsettled = NudgeLoop(**base, gate=True, monitor=_monitor())
    assert not _is_torn_deactivation(settled)
    assert not _is_torn_deactivation(inflight)
    assert _is_torn_deactivation(unsettled)


# ── _load resumes the torn row; start() reschedules it ───────────────────────


@pytest.mark.asyncio
async def test_start_resumes_a_torn_row_and_reschedules_it(tmp_path, caplog):
    """The incident shape: after a restart the loop is active again, armed at the
    overdue beat, persisted as active, and the resume is logged."""
    _write_store(tmp_path, _row())
    svc = AutoNudgeService(base_dir=tmp_path)
    delays = _arm_delays(svc)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        await svc.start()
    try:
        loop = svc.get_by_slot("chat-746-1788942058")
        assert loop is not None and loop.active
        assert loop.stopped_reason == ""
        assert loop.id in svc._timers and not svc._timers[loop.id].done()
        assert delays == [float(_OVERDUE_REARM_SECS)]
        persisted = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
        assert persisted["active"] is True
        assert any("resuming it" in rec.getMessage() for rec in caplog.records)
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_start_resumes_a_torn_gated_row_with_an_unsettled_monitor(tmp_path):
    """A gate=true prompt loop carrying inferred probe state is a prompt loop:
    its tick is gated, so resuming it cannot inject an ungated turn."""
    row = _row(
        id="1622f9ea",
        slot_key="chat-809-1789214620",
        gate=True,
        monitor=monitor_state_to_dict(_monitor(next_probe_at=0.0)),
    )
    _write_store(tmp_path, row)
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    try:
        loop = svc.get_by_slot("chat-809-1789214620")
        assert loop is not None and loop.active
        assert loop.monitor is not None and loop.monitor.next_probe_at == loop.next_due_ts
        assert loop.id in svc._timers
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_start_leaves_a_legacy_sentinel_dropped_row_alone(tmp_path):
    """The shape an older sentinel-drop refusal persisted -- inactive, no reason,
    deadline untouched, sentinel blanked -- is a fail-closed stop, not a torn
    write: it must not come back without its kill switch."""
    _write_store(tmp_path, _row(stop_sentinel_path=""))
    svc = AutoNudgeService(base_dir=tmp_path)
    delays = _arm_delays(svc)
    await svc.start()
    try:
        loop = svc.get_by_slot("chat-746-1788942058")
        assert loop is not None and not loop.active
        assert delays == []
        assert loop.id not in svc._timers
        persisted = json.loads((tmp_path / "autonudge.json").read_text())["loops"][0]
        assert persisted["active"] is False
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_start_leaves_a_recorded_pause_alone(tmp_path):
    """``update(active=False)`` records ``"manual"`` and clears the deadline; that
    row is the user's decision and must not come back on its own."""
    _write_store(tmp_path, _row(stopped_reason=MANUAL_STOP_REASON, next_due_ts=0.0))
    svc = AutoNudgeService(base_dir=tmp_path)
    delays = _arm_delays(svc)
    await svc.start()
    try:
        loop = svc.get_by_slot("chat-746-1788942058")
        assert loop is not None and not loop.active
        assert loop.stopped_reason == MANUAL_STOP_REASON
        assert delays == []
        assert loop.id not in svc._timers
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_start_leaves_a_legacy_reasonless_pause_without_a_schedule_alone(tmp_path):
    """A row paused before ``stopped_reason`` existed has no live deadline: there
    is no schedule to resume and nothing tells it apart from a pre-field pause,
    so it stays inactive."""
    _write_store(tmp_path, _row(next_due_ts=0.0))
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    try:
        loop = svc.get_by_slot("chat-746-1788942058")
        assert loop is not None and not loop.active
        assert loop.id not in svc._timers
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_pause_through_update_survives_the_next_restart(tmp_path):
    """End to end: the service's own pause is never mistaken for a torn write."""
    svc1 = AutoNudgeService(base_dir=tmp_path)
    await svc1.start()
    loop = await svc1.add(slot_key="chat-1-123", message="go", idle_secs=300)
    await svc1.update(loop.id, active=False)
    svc1.stop()
    svc2 = AutoNudgeService(base_dir=tmp_path)
    await svc2.start()
    try:
        again = svc2.get_by_slot("chat-1-123")
        assert again is not None and not again.active
        assert again.stopped_reason == MANUAL_STOP_REASON
        assert again.id not in svc2._timers
    finally:
        svc2.stop()


@pytest.mark.asyncio
async def test_dropped_sentinel_records_its_reason_and_clears_the_schedule(tmp_path, monkeypatch):
    """The one ``_load`` branch that deactivates without a reason now records one,
    so a fail-closed sentinel refusal is not resumed as a torn write next boot."""
    monkeypatch.setattr(_an, "repair_sentinel_path", lambda raw: "")
    _write_store(tmp_path, _row(active=True))
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    try:
        loop = svc.get_by_slot("chat-746-1788942058")
        assert loop is not None and not loop.active
        assert loop.stopped_reason == SENTINEL_DROPPED_REASON
        assert loop.next_due_ts == 0.0
        assert loop.stop_sentinel_path == ""
        assert loop.id not in svc._timers
        assert not _is_torn_deactivation(loop)
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_dropped_sentinel_keeps_a_recorded_pause_as_a_pause(tmp_path, monkeypatch):
    """A manually paused loop whose sentinel later becomes sensitive stays a
    manual pause: the repair blanks the sentinel but must not relabel the stop,
    or the pause would turn into a system-imposed, re-armable one."""
    monkeypatch.setattr(_an, "repair_sentinel_path", lambda raw: "")
    _write_store(tmp_path, _row(stopped_reason=MANUAL_STOP_REASON, next_due_ts=0.0))
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    try:
        loop = svc.get_by_slot("chat-746-1788942058")
        assert loop is not None and not loop.active
        assert loop.stopped_reason == MANUAL_STOP_REASON
        assert loop.stop_sentinel_path == ""
        assert not _stopped_row_is_replaceable(loop)
        with pytest.raises(MonitorUpdateConflict, match="stop reason: manual"):
            await svc.add(
                slot_key="chat-746-1788942058",
                message="re-arm",
                idle_secs=60,
                replace_existing=False,
                replace_stopped=True,
            )
    finally:
        svc.stop()


# ── the directive re-arm: what stays evidence, and how it is named ───────────


def test_reasonless_and_manual_rows_stay_evidence_while_sentinel_dropped_is_replaceable():
    reasonless = NudgeLoop(id="a", slot_key="chat-1-1", message="m", active=False)
    manual = NudgeLoop(
        id="b", slot_key="chat-1-2", message="m", active=False, stopped_reason=MANUAL_STOP_REASON
    )
    dropped = NudgeLoop(
        id="c",
        slot_key="chat-1-3",
        message="m",
        active=False,
        stopped_reason=SENTINEL_DROPPED_REASON,
    )
    assert not _stopped_row_is_replaceable(reasonless)
    assert not _stopped_row_is_replaceable(manual)
    assert _stopped_row_is_replaceable(dropped)


@pytest.mark.asyncio
async def test_directive_rearm_refuses_a_reasonless_row_without_a_schedule_and_says_so(
    tmp_path,
):
    """A pre-field pause and a torn write on a mid-fire loop share this shape and
    the store cannot tell them apart, so the row stays retained evidence -- and
    the refusal names what is recorded (nothing), not a stop nobody made."""
    _write_store(tmp_path, _row(next_due_ts=0.0))
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    try:
        with pytest.raises(MonitorUpdateConflict, match="stop reason: none recorded"):
            await svc.add(
                slot_key="chat-746-1788942058",
                message="re-arm",
                idle_secs=1200,
                replace_existing=False,
                replace_stopped=True,
            )
        survivor = svc.get_by_slot("chat-746-1788942058")
        assert survivor is not None and survivor.id == "2f8efda1" and not survivor.active
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_directive_rearm_displaces_a_sentinel_dropped_row(tmp_path):
    """A dropped kill switch is a stop the SYSTEM imposed, and the re-arm path
    re-validates the sentinel it arms with -- so the row is re-armable."""
    _write_store(tmp_path, _row(stopped_reason=SENTINEL_DROPPED_REASON, next_due_ts=0.0))
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    try:
        fresh = await svc.add(
            slot_key="chat-746-1788942058",
            message="re-armed",
            idle_secs=1200,
            replace_existing=False,
            replace_stopped=True,
        )
        assert fresh.active and fresh.id != "2f8efda1"
        assert svc.get_by_slot("chat-746-1788942058") is fresh
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_directive_rearm_still_refuses_a_manual_pause_and_names_the_recorded_reason(
    tmp_path,
):
    _write_store(tmp_path, _row(stopped_reason=MANUAL_STOP_REASON, next_due_ts=0.0))
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    try:
        with pytest.raises(MonitorUpdateConflict, match="stop reason: manual"):
            await svc.add(
                slot_key="chat-746-1788942058",
                message="re-arm",
                idle_secs=60,
                replace_existing=False,
                replace_stopped=True,
            )
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_refusal_for_an_unknown_reason_does_not_invent_manual(tmp_path):
    """An unknown reason stays evidence (fail closed), and the message reports
    the recorded reason rather than a stop nobody made."""
    _write_store(tmp_path, _row(stopped_reason="some_future_reason", next_due_ts=0.0))
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    try:
        with pytest.raises(MonitorUpdateConflict, match="stop reason: some_future_reason"):
            await svc.add(
                slot_key="chat-746-1788942058",
                message="re-arm",
                idle_secs=60,
                replace_existing=False,
                replace_stopped=True,
            )
    finally:
        svc.stop()
