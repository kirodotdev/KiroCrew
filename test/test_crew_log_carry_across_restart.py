"""A carry an earlier process wrote still decides where a late write lands.

The writer job learns that a newer unit carried a slot from this process's cache
(``late_writes.note_carried``) or from the older unit's ``superseded`` close. A restart
clears the cache, and a unit a session-map key still maps is never closed, so a late
write naming it has neither. It must still land in the carrier, or the older unit folds
after the carry and replays its whole history over it: the job reads the carry off the
newer unit's log (``late_writes._read_carries``). ``emit.reset_caches()`` stands in for
the restart.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from crew_log_drain import assert_drained, settle

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger
from kiro_crew.crew_log import emit, emit_runtime, late_writes, projection
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log.entry_types import CUSTOM_FOLD_NAME
from kiro_crew.crew_log.writer import JobKind
from kiro_crew.session_ledger import _store_name
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

SLOT = "chat-restart"
#: The session key SLOT's own dashboard turns run under.
SLOT_SESSION_KEY = f"dashboard:{SLOT}"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture
def carrying(monkeypatch):
    """The live emitter, the carry's fold inline, and a probe whose map is the yielded dict."""
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    stp.reset_for_tests()
    monkeypatch.setattr(emit, "_submit_carry_fold", lambda job: job())
    mapped: dict[str, str] = {}
    emit.set_session_key_probe(mapped.get)
    try:
        yield mapped
    finally:
        emit.set_session_key_probe(None)
        settle()
        tree = stp.projection()
        stp.reset_for_tests()
        wait_until(
            lambda: not tree._write_scheduled,
            describe=lambda: "the session tree's debounced checkpoint is still sleeping",
        )


def _entries(unit_id: str) -> "list[dict]":
    path = lg.crew_log_root(lg.KIND_SESSION) / _store_name(unit_id) / "log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _events(unit_id: str) -> "list[str]":
    return [e["data"].get("event", "") for e in _entries(unit_id) if e["type"] == "ledger/recorded"]


def _customs(unit_id: str) -> "list[str]":
    return [
        e["data"]["data"]["value"]
        for e in _entries(unit_id)
        if e["type"] == "dashboard/custom_value"
    ]


def _view(state: dict) -> tuple:
    return (
        state["phase"],
        [event["text"] for event in state["events"]],
        [row.get("approach") for row in state["tried"]],
    )


def _custom(value: str) -> dict:
    return {
        "type": "uptime",
        "data": {"value": value},
        "writer": "cron:restart",
        "member": SLOT,
        "crew_key": "a" * 40,
    }


def _carry_with_the_older_unit_still_mapped(carrying: "dict[str, str]") -> None:
    """``u1`` records, ``u2`` carries and records a newer phase; a key still maps ``u1``."""
    carrying["u1"] = "dashboard:imported-copy"
    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(
        SLOT, session_id="u1", goal="ship it", phase="design", event="planned", event_kind="phase"
    )
    session_ledger.record_update(
        SLOT,
        session_id="u1",
        tried_approach="patch the line",
        tried_rejected_because="wrong altitude",
        event="tried",
    )
    assert emit.on_dashboard_custom("u1", _custom("first"), timeout=FALLBACK_TIMEOUT_SECS)
    assert_drained()
    emit.on_session_opened(
        "u2", agent="kirocrew", slot=SLOT, previous_sid="u1", session_key=SLOT_SESSION_KEY
    )
    assert_drained()
    assert any(e["type"] == "session/carried" for e in _entries("u2"))
    assert [e["type"] for e in _entries("u1")][-1] != "session/closed"
    session_ledger.record_update(
        SLOT, session_id="u2", phase="build", event="building", event_kind="phase"
    )
    assert_drained()


@pytest.mark.parametrize("restart", [False, True], ids=["same-process", "after-restart"])
def test_a_late_ledger_update_naming_the_older_unit_lands_in_its_carrier(carrying, restart):
    _carry_with_the_older_unit_still_mapped(carrying)
    if restart:
        emit.reset_caches()
    projection.forget_slot_folds()

    _record, durable = session_ledger.record_update(SLOT, session_id="u1", event="late")
    assert_drained()
    projection.forget_slot_folds()
    after_late = _view(session_ledger.read_state(SLOT))
    _record, next_durable = session_ledger.record_update(SLOT, session_id="u2", event="next")
    assert_drained()
    projection.forget_slot_folds()
    after_next = _view(session_ledger.read_state(SLOT))

    assert durable is True
    assert next_durable is True
    assert "late" not in _events("u1")
    assert "late" in _events("u2")
    assert after_late == (
        "build",
        ["planned", "tried", "building", "late"],
        ["patch the line"],
    )
    assert after_next == (
        "build",
        ["planned", "tried", "building", "late", "next"],
        ["patch the line"],
    )


def test_a_late_dashboard_value_naming_the_older_unit_lands_in_its_carrier_after_a_restart(
    carrying,
):
    """The dashboard's record route resolved ``u1`` before the reset; the gateway restarted."""
    _carry_with_the_older_unit_still_mapped(carrying)
    emit.reset_caches()

    assert emit.on_dashboard_custom("u1", _custom("late"), timeout=FALLBACK_TIMEOUT_SECS)
    assert_drained()

    assert _customs("u1") == ["first"]
    assert _customs("u2") == ["late"]
    projection.forget_slot_folds()
    custom = projection.read_slot_projection(SLOT, CUSTOM_FOLD_NAME).value
    assert custom["latest"]["uptime"]["data"] == {"value": "late"}
    assert custom["wrote"] == 2


def test_a_dashboard_waiter_that_gave_up_after_a_restart_gets_nothing_landed(carrying):
    """``False`` stays final when the hand-over was decided off the newer unit's log."""
    _carry_with_the_older_unit_still_mapped(carrying)
    emit.reset_caches()
    held: "list[tuple[str, Any]]" = []
    real = emit_runtime._hand_over

    def _hold_follow_ons(unit: str, job: Any) -> None:
        if job.kind is JobKind.FOLLOW_ON:
            held.append((unit, job))
            return
        real(unit, job)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_hand_over", _hold_follow_ons)
        # The handed-on job is held, so nothing can wake this waiter: a 0 s wait gives up.
        answered = emit.on_dashboard_custom("u1", _custom("late"), timeout=0.0)

    assert [unit for unit, _job in held] == ["u2"], "the job was handed to the carrier"
    assert answered is False
    for unit, job in held:
        real(unit, job)
    assert_drained()

    assert _customs("u1") == ["first"]
    assert _customs("u2") == []


def test_a_later_unit_is_read_once_and_the_newest_opens_no_log(carrying, monkeypatch):
    """The read pays once per process; a write naming the newest unit reads nothing at all."""
    _carry_with_the_older_unit_still_mapped(carrying)
    emit.reset_caches()
    reads: "list[str]" = []
    real = late_writes._read_carries

    def _counted(slot: str, unit: str) -> "frozenset[str] | None":
        reads.append(unit)
        return real(slot, unit)

    monkeypatch.setattr(late_writes, "_read_carries", _counted)
    session_ledger.record_update(SLOT, session_id="u2", event="newest")
    assert_drained()
    assert reads == []

    session_ledger.record_update(SLOT, session_id="u1", event="late-1")
    session_ledger.record_update(SLOT, session_id="u1", event="late-2")
    assert_drained()

    assert _events("u2")[-3:] == ["newest", "late-1", "late-2"]
    assert reads == ["u2"]


def test_a_later_unit_holding_no_carry_is_read_once(carrying, monkeypatch):
    """Proving a unit holds no carry reads all of it, so that is paid once per process too.

    The writes still land in the older unit: no carry replaced it, and the one still to
    land reads them when it does.
    """
    carrying["u1"] = "dashboard:imported-copy"
    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(SLOT, session_id="u1", goal="ship it", event="planned")
    assert_drained()
    # The new unit's carry never folds, so it holds none.
    monkeypatch.setattr(emit, "_submit_carry_fold", lambda job: None)
    emit.on_session_opened(
        "u2", agent="kirocrew", slot=SLOT, previous_sid="u1", session_key=SLOT_SESSION_KEY
    )
    assert_drained()
    assert not any(e["type"] == "session/carried" for e in _entries("u2"))
    reads: "list[str]" = []
    real = late_writes._read_carries

    def _counted(slot: str, unit: str) -> "frozenset[str] | None":
        reads.append(unit)
        return real(slot, unit)

    monkeypatch.setattr(late_writes, "_read_carries", _counted)
    session_ledger.record_update(SLOT, session_id="u1", event="late-1")
    session_ledger.record_update(SLOT, session_id="u1", event="late-2")
    assert_drained()

    assert _events("u1")[-2:] == ["late-1", "late-2"]
    assert reads == ["u2"]


def test_a_carry_whose_read_failed_is_read_again_by_the_next_write(carrying, monkeypatch):
    """A failed read proves nothing: that write lands where it always went, the next reads."""
    _carry_with_the_older_unit_still_mapped(carrying)
    emit.reset_caches()
    failed: "list[str]" = []

    def _unreadable(raw: bytes, slot: str) -> "frozenset[str] | None":
        failed.append(slot)
        raise ValueError("a segment would not read")

    with pytest.MonkeyPatch.context() as patch:
        # Both reads of this write fail: the first guess off the lane and the job's own.
        patch.setattr(late_writes, "_carry_keys", _unreadable)
        session_ledger.record_update(SLOT, session_id="u1", event="unread")
        assert_drained()
    session_ledger.record_update(SLOT, session_id="u1", event="read")
    assert_drained()

    assert "unread" in _events("u1")
    assert "read" in _events("u2") and "read" not in _events("u1")
    assert failed and set(failed) == {SLOT}
