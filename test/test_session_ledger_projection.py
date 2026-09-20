"""The session ledger as a PROJECTION of the session's crew log.

One test per property the change has to keep true. The load-bearing ones are
:func:`test_a_slot_folds_across_every_unit_it_ran_under` -- a slot owns one ACP
session id at a time, so a record that did not join those units would answer with
whichever part of the workstream happened to land in the newest session -- and
:func:`test_the_injected_snapshot_is_byte_identical_to_the_stored_document_s`,
which pins that the block a nudge cycle carries did not change shape when the
record stopped being a file.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger as sl
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store

SLOT = "chat-1"
SESSION = "acp-1"
LATER_SESSION = "acp-2"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own data home, crew log on, and no writer state carried between tests."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    crew_log_emit.reset_caches()
    sl._fold_cache.clear()
    yield
    crew_log_emit.reset_caches()
    sl._fold_cache.clear()


def _unit(unit_id: str = SESSION, *, slot: str = SLOT) -> None:
    """Create one session crew log, then drop the handle so it holds no lease.

    The emitter opens its own handle when the ledger appends, and a handle this
    test kept would own the write lease it needs.
    """
    CrewLog.create(lg.KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=slot)


def _entries(unit_id: str = SESSION) -> list:
    handle = CrewLog.open(lg.KIND_SESSION, unit_id)
    try:
        return list(handle.iter_from(1))
    finally:
        del handle


# --------------------------------------------------------------------------- #
# record -> fold
# --------------------------------------------------------------------------- #


def test_record_then_fold_is_a_round_trip():
    """Every field a call sets comes back out of the fold that reads the entries."""
    _unit()
    sl.record(
        SLOT,
        session_id=SESSION,
        goal="ship the ledger fold",
        next_step="write the spec",
        artifacts={"pr": "12345"},
        event="opened the worktree",
        event_kind="progress",
    )

    state = sl.read_state(SLOT)
    assert state["goal"] == "ship the ledger fold"
    assert state["next"] == "write the spec"
    assert state["artifacts"] == {"pr": "12345"}
    assert [(e["kind"], e["text"]) for e in state["events"]] == [
        ("progress", "opened the worktree")
    ]
    assert state["created_at"] and state["last_progress_at"]
    assert state["finished_at"] == ""


def test_one_call_appends_exactly_one_entry():
    """The whole update is ONE line, which is what makes it crash-atomic."""
    _unit()
    sl.record(
        SLOT,
        session_id=SESSION,
        goal="g",
        phase="implementing",
        next_step="n",
        tried_approach="a",
        tried_rejected_because="slow",
        artifacts={"branch": "b"},
        event="started",
        event_kind="phase",
    )

    ledger_entries = [e for e in _entries() if e.type == sl.LEDGER_ENTRY_TYPE]
    assert len(ledger_entries) == 1
    data = ledger_entries[0].data
    assert data["slot"] == SLOT
    assert data["phase"] == "implementing"
    assert data["event"] == "started"
    assert data["event_kind"] == "phase"
    assert data["tried"] == {"approach": "a", "rejected_because": "slow"}


def test_record_returns_the_record_the_appended_entry_produces():
    """The answer is the fold applied to the pending entry, not a read-back.

    The writer is asynchronous, so a read-back would race the drain and could
    report a phase the caller just set as still unset.

    Every field a caller can act on has to agree with what the log then reports. The
    DERIVED timestamps deliberately do not: the entry this record is folded from is
    never written, so its stamp is the caller's own sample while the log's is the
    store's, and no position for either sample makes them one value. They are pinned
    by the relationship that IS a property -- the returned record never claims a time
    later than the entry the log holds -- rather than by an equality that holds only
    while both samples happen to land inside the same second.
    """
    _unit()
    returned = sl.record(
        SLOT,
        session_id=SESSION,
        phase="awaiting-ci",
        event="pushed",
        event_kind="progress",
    )
    assert returned["phase"] == "awaiting-ci"
    assert crew_log_emit.flush(timeout=5.0)
    settled = sl.read_state(SLOT)

    # The field SET is compared whole, so a field added to the record in future is
    # covered by one of the two branches below rather than silently by neither.
    assert set(settled) == set(returned)
    stamped = {"created_at", "last_progress_at", "finished_at", "events"}
    assert {k: v for k, v in settled.items() if k not in stamped} == {
        k: v for k, v in returned.items() if k not in stamped
    }
    # Events carry a stamp each, so they are compared by what the caller wrote.
    assert [(e["kind"], e["text"]) for e in settled["events"]] == [
        (e["kind"], e["text"]) for e in returned["events"]
    ]
    assert returned["finished_at"] == settled["finished_at"] == ""
    for field in ("created_at", "last_progress_at"):
        assert returned[field] and settled[field]
        assert returned[field] <= settled[field]


def test_an_omitted_field_leaves_the_stored_value_alone():
    """A partial update is the norm, so absence has to mean unchanged."""
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="keep me", next_step="first")
    sl.record(SLOT, session_id=SESSION, next_step="second")

    state = sl.read_state(SLOT)
    assert state["goal"] == "keep me"
    assert state["next"] == "second"


def test_a_field_set_to_empty_is_applied_rather_than_ignored():
    """Clearing a field is a real update, so presence and not truth decides."""
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="temporary")
    sl.record(SLOT, session_id=SESSION, goal="")
    assert sl.read_state(SLOT)["goal"] == ""


# --------------------------------------------------------------------------- #
# the discipline
# --------------------------------------------------------------------------- #


def test_a_phase_without_an_event_is_refused_and_writes_nothing():
    """A phase never moves without a logged reason, and the refusal is total."""
    _unit()
    with pytest.raises(ValueError, match="phase change requires an event"):
        sl.record(SLOT, session_id=SESSION, phase="implementing")
    assert [e for e in _entries() if e.type == sl.LEDGER_ENTRY_TYPE] == []


def test_a_phase_without_a_recognized_event_kind_is_refused():
    _unit()
    with pytest.raises(ValueError, match="event_kind"):
        sl.record(
            SLOT, session_id=SESSION, phase="implementing", event="why", event_kind="nonsense"
        )
    assert [e for e in _entries() if e.type == sl.LEDGER_ENTRY_TYPE] == []


def test_a_phase_and_its_reason_are_the_same_entry():
    """No ordering exists in which a reader sees one without the other."""
    _unit()
    sl.record(
        SLOT, session_id=SESSION, phase="blocked", event="waiting on review", event_kind="blocked"
    )
    (entry,) = [e for e in _entries() if e.type == sl.LEDGER_ENTRY_TYPE]
    assert entry.data["phase"] == "blocked"
    assert entry.data["event"] == "waiting on review"


def test_an_unrecognized_event_kind_without_a_phase_degrades_to_note():
    """The kind is a filter over the text, so it never costs the event itself."""
    _unit()
    sl.record(SLOT, session_id=SESSION, event="something happened", event_kind="made-up")
    assert sl.read_state(SLOT)["events"][-1]["kind"] == "note"


# --------------------------------------------------------------------------- #
# the slot join
# --------------------------------------------------------------------------- #


def test_a_slot_folds_across_every_unit_it_ran_under():
    """A reset gives the slot a new ACP session; its ledger is still one record.

    The seqs restart in the second unit, so a fold that did not re-base them
    would refuse the whole second file as a re-fold of the first.
    """
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="one goal", next_step="before the reset")
    _unit(LATER_SESSION)
    sl.record(
        SLOT,
        session_id=LATER_SESSION,
        next_step="after the reset",
        event="resumed",
        event_kind="progress",
    )

    state = sl.read_state(SLOT)
    # Carried across the boundary from the older unit ...
    assert state["goal"] == "one goal"
    # ... and the newer unit's update wins.
    assert state["next"] == "after the reset"
    assert [e["text"] for e in state["events"]] == ["resumed"]
    assert store.session_units_for_slot(SLOT) == (SESSION, LATER_SESSION)


def test_units_of_another_slot_are_not_folded_in():
    """A record is one slot's own; a neighbour's entries never reach it."""
    _unit(SESSION, slot=SLOT)
    _unit(LATER_SESSION, slot="chat-other")
    sl.record(SLOT, session_id=SESSION, goal="mine")
    sl.record("chat-other", session_id=LATER_SESSION, goal="theirs")

    assert sl.read_state(SLOT)["goal"] == "mine"
    assert sl.read_state("chat-other")["goal"] == "theirs"


def test_a_unit_whose_header_names_no_slot_is_not_attributed_to_one():
    """A session that never ran on a slot cannot be folded into any slot's record."""
    CrewLog.create(lg.KIND_SESSION, "acp-orphan", owner="owner", agent="kirocrew")
    assert store.session_units_for_slot(SLOT) == ()


def test_the_slot_index_notices_a_unit_that_appears_after_a_read():
    """The scan is cached against the root's identity, so a new unit invalidates it."""
    _unit(SESSION)
    assert store.session_units_for_slot(SLOT) == (SESSION,)
    _unit(LATER_SESSION)
    assert store.session_units_for_slot(SLOT) == (SESSION, LATER_SESSION)


# --------------------------------------------------------------------------- #
# the record's shape
# --------------------------------------------------------------------------- #


def test_the_folded_record_carries_exactly_the_fields_the_document_carried():
    """Readers of the ledger did not have to learn a new shape."""
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="g")
    assert set(sl.read_state(SLOT)) == {
        "schema",
        "goal",
        "phase",
        "next",
        "tried",
        "artifacts",
        "events",
        "created_at",
        "last_progress_at",
        "finished_at",
    }


def test_the_injected_snapshot_is_byte_identical_to_the_stored_document_s():
    """The block a nudge cycle carries every wake, pinned line by line."""
    _unit()
    sl.record(
        SLOT,
        session_id=SESSION,
        goal="ship it",
        phase="implementing",
        next_step="add the fold test",
        tried_approach="a second document",
        tried_rejected_because="it can disagree with the log",
        artifacts={"worktree": "/w"},
        event="started",
        event_kind="phase",
    )

    assert sl.render_snapshot(SLOT).splitlines() == [
        "[work ledger — durable state for this session; authoritative over memory of prior cycles]",
        "goal: ship it",
        "phase: implementing",
        "next: add the fold test",
        "tried: a second document (rejected: it can disagree with the log)",
        "artifact worktree: /w",
    ]


def test_a_terminal_phase_stops_the_snapshot():
    """A finished workstream has nothing left to steer a resumed cycle with."""
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="g", next_step="n")
    assert sl.render_snapshot(SLOT)
    sl.record(SLOT, session_id=SESSION, phase="done", event="merged", event_kind="phase")
    assert sl.render_snapshot(SLOT) == ""


def test_leaving_a_terminal_phase_brings_the_snapshot_back():
    """``finished_at`` is re-derived on every phase write, never latched."""
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="g", phase="done", event="d", event_kind="phase")
    assert sl.render_snapshot(SLOT) == ""
    sl.record(SLOT, session_id=SESSION, phase="implementing", event="reopened", event_kind="phase")
    assert sl.read_state(SLOT)["finished_at"] == ""
    assert sl.render_snapshot(SLOT)


def test_a_slot_that_recorded_nothing_reads_as_the_empty_record():
    assert sl.read_state(SLOT) == crew_log.fold("ledger", [])
    assert sl.has_ledger(SLOT) is False
    assert sl.render_snapshot(SLOT) == ""


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #


def test_record_refuses_when_the_session_has_no_crew_log():
    """The update has nowhere to go, and a write that went nowhere is the one
    outcome a durable record must never produce."""
    with pytest.raises(sl.LedgerUnavailable, match="no crew log"):
        sl.record(SLOT, session_id=SESSION, goal="g")


def test_record_refuses_when_the_crew_log_is_switched_off(monkeypatch):
    _unit()
    monkeypatch.delenv("KIROCREW_CREW_LOG", raising=False)
    with pytest.raises(sl.LedgerUnavailable, match="KIROCREW_CREW_LOG"):
        sl.record(SLOT, session_id=SESSION, goal="g")


def test_record_refuses_a_session_with_no_live_acp_unit():
    """An update filed under a guessed session is worse than one that is refused."""
    _unit()
    with pytest.raises(sl.LedgerUnavailable, match="no live crew log"):
        sl.record(SLOT, session_id="", goal="g")


def test_record_refuses_an_empty_slot_key():
    _unit()
    with pytest.raises(ValueError, match="Invalid slot key"):
        sl.record("", session_id=SESSION, goal="g")


# --------------------------------------------------------------------------- #
# bounds and damage
# --------------------------------------------------------------------------- #


def test_the_fold_reclamps_a_field_the_writer_would_have_clamped():
    """A writer's clamp binds the writer; a planted line ignores it."""
    _unit()
    handle = CrewLog.open(lg.KIND_SESSION, SESSION)
    handle.append(
        sl.LEDGER_ENTRY_TYPE,
        {"slot": SLOT, "goal": "g" * (sl._MAX_TEXT + 500)},
        src="gateway",
    )
    del handle
    assert len(sl.read_state(SLOT)["goal"]) == sl._MAX_TEXT


def test_the_event_tail_is_bounded():
    _unit()
    handle = CrewLog.open(lg.KIND_SESSION, SESSION)
    handle.append_many(
        [
            {
                "type": sl.LEDGER_ENTRY_TYPE,
                "data": {"slot": SLOT, "event": f"e{n}", "event_kind": "progress"},
            }
            for n in range(sl._MAX_EVENTS + 25)
        ],
        src="gateway",
    )
    del handle
    events = sl.read_state(SLOT)["events"]
    assert len(events) == sl._MAX_EVENTS
    # The OLDEST age out, so the newest step is always the one a resume reads.
    assert events[-1]["text"] == f"e{sl._MAX_EVENTS + 24}"


def test_updating_the_oldest_artifact_does_not_age_it_out():
    """A plain dict update keeps the key's original position; the fold re-inserts."""
    _unit()
    handle = CrewLog.open(lg.KIND_SESSION, SESSION)
    handle.append_many(
        [
            {
                "type": sl.LEDGER_ENTRY_TYPE,
                "data": {"slot": SLOT, "artifacts": {f"k{n}": str(n)}},
            }
            for n in range(sl._MAX_ARTIFACTS)
        ]
        + [{"type": sl.LEDGER_ENTRY_TYPE, "data": {"slot": SLOT, "artifacts": {"k0": "fresh"}}}]
        + [{"type": sl.LEDGER_ENTRY_TYPE, "data": {"slot": SLOT, "artifacts": {"new": "1"}}}],
        src="gateway",
    )
    del handle
    artifacts = sl.read_state(SLOT)["artifacts"]
    assert len(artifacts) == sl._MAX_ARTIFACTS
    assert artifacts["k0"] == "fresh"
    assert "k1" not in artifacts


def test_a_ledger_entry_with_a_wrong_shape_is_refused_at_the_append():
    """The declaration is enforced on the way in, so no fold has to repair it."""
    _unit()
    handle = CrewLog.open(lg.KIND_SESSION, SESSION)
    with pytest.raises(lg.CrewLogError):
        handle.append(sl.LEDGER_ENTRY_TYPE, {"goal": "no slot"}, src="gateway")
    with pytest.raises(lg.CrewLogError):
        handle.append(
            sl.LEDGER_ENTRY_TYPE,
            {"slot": SLOT, "event": "x", "event_kind": "not-a-kind"},
            src="gateway",
        )
    del handle


# --------------------------------------------------------------------------- #
# the registry and the writer agree
# --------------------------------------------------------------------------- #


def test_the_writer_and_the_fold_name_the_same_entry_type():
    assert sl.LEDGER_ENTRY_TYPE in crew_log.KNOWN_TYPES
    assert sl._FOLD_NAME in crew_log.FOLD_NAMES


def test_the_ledger_fold_is_not_one_of_the_pushed_panel_folds():
    """It is slot-wide, and pushing it under one session's id would understate it."""
    assert sl._FOLD_NAME not in crew_log.PROJECTION_NAMES
    assert sl._FOLD_NAME in crew_log.SLOT_PROJECTION_NAMES


def test_the_synthetic_entry_carries_the_src_the_emitter_writes():
    """``record`` folds a pending entry, so its src must be the writer's own."""
    assert sl._ENTRY_SRC == crew_log_emit._SRC_GATEWAY


def test_the_declared_event_kinds_are_the_writers_own():
    from kiro_crew.crew_log.entry_types import SESSION_ENTRY_TYPES

    declared = SESSION_ENTRY_TYPES[sl.LEDGER_ENTRY_TYPE]
    (kind_field,) = [f for f in declared.fields if f.name == "event_kind"]
    assert set(kind_field.enum) == sl.EVENT_KINDS
    assert kind_field.enum_closed is True


def test_the_route_payload_is_json_serializable():
    """The record is handed to a JSON response and to the MCP tool verbatim."""
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="g", event="e", event_kind="progress")
    state = sl.read_state(SLOT)
    assert json.loads(json.dumps(state)) == state


# --------------------------------------------------------------------------- #
# the upgrade carry-forward
# --------------------------------------------------------------------------- #


def _legacy_document(slot: str, **fields) -> None:
    """Write a pre-projection ``state.json`` for *slot*, as the old writer did."""
    directory = sl.ledger_dir(slot)
    directory.mkdir(parents=True, exist_ok=True)
    state = sl._empty_state()
    state.update(fields)
    (directory / sl._STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    (directory / sl._KEY_FILE).write_text(slot + "\n", encoding="utf-8")


def test_state_written_before_the_fold_is_carried_into_the_log():
    """A workstream in flight across the upgrade keeps the state a resume needs."""
    _legacy_document(
        SLOT,
        goal="finish the migration",
        phase="implementing",
        next="carry the document forward",
        artifacts={"pr": "12027"},
    )
    _unit()
    sl.record(SLOT, session_id=SESSION, next_step="then record normally")

    state = sl.read_state(SLOT)
    assert state["goal"] == "finish the migration"
    assert state["phase"] == "implementing"
    # The carried entry lands BEFORE the update, so the update still wins.
    assert state["next"] == "then record normally"
    assert state["artifacts"] == {"pr": "12027"}


def test_the_carried_entry_brings_its_phase_with_a_reason():
    """The invariant the record rests on holds for the carry too."""
    _legacy_document(SLOT, phase="awaiting-ci", goal="g")
    _unit()
    sl.record(SLOT, session_id=SESSION, next_step="n")

    carried = [e for e in _entries() if e.type == sl.LEDGER_ENTRY_TYPE][0]
    assert carried.data["phase"] == "awaiting-ci"
    assert "carried forward" in carried.data["event"]
    assert carried.data["event_kind"] == "note"


def test_the_carry_names_what_it_could_not_bring():
    """An entry holds one rejected approach, so the rest are counted, not dropped silently."""
    _legacy_document(
        SLOT,
        goal="g",
        tried=[
            {"approach": "first", "rejected_because": "slow", "at": ""},
            {"approach": "second", "rejected_because": "wrong", "at": ""},
        ],
        events=[{"ts": "", "kind": "note", "text": "old"}],
    )
    _unit()
    sl.record(SLOT, session_id=SESSION, next_step="n")

    state = sl.read_state(SLOT)
    assert [row["approach"] for row in state["tried"]] == ["second"]
    note = state["events"][0]["text"]
    assert "1 earlier rejected approach(es)" in note
    assert "1 event(s)" in note


def test_the_carry_happens_once_and_never_again_for_that_slot():
    """It is consumed, not consulted: a second update adds no second carry."""
    _legacy_document(SLOT, goal="carried")
    _unit()
    sl.record(SLOT, session_id=SESSION, next_step="one")
    sl.record(SLOT, session_id=SESSION, next_step="two")

    carries = [
        e
        for e in _entries()
        if e.type == sl.LEDGER_ENTRY_TYPE and "carried forward" in e.data.get("event", "")
    ]
    assert len(carries) == 1


def test_a_slot_with_entries_already_carries_nothing():
    """The carry is the upgrade path only, never a merge into a live record."""
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="live")
    _legacy_document(SLOT, goal="stale residue")
    sl.record(SLOT, session_id=SESSION, next_step="n")
    assert sl.read_state(SLOT)["goal"] == "live"


def test_a_carried_document_is_not_resurrected_after_a_permanent_delete():
    """A permanent delete removes the crew log and PRESERVES the legacy store.

    So a fresh session on the same slot key finds an empty record. Without the carry
    being a consumption, it would import the deleted conversation's goal and phase
    back into a new log.
    """
    _legacy_document(SLOT, goal="deleted conversation", phase="implementing")
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, next_step="first life")
    assert sl.read_state(SLOT)["goal"] == "deleted conversation"

    # The permanent delete: the session's crew log unit goes, the legacy store stays.
    # The emitter caches this unit's open handle, which HOLDS its `.lease`. Windows
    # refuses to unlink an open file, so the test drops the handle it caused to be
    # opened before standing in for a delete. POSIX would allow the unlink; the
    # cleanup is the test's either way.
    crew_log_emit.reset_caches()
    shutil.rmtree(store.crew_log_dir(lg.KIND_SESSION, SESSION))
    crew_log_emit.reset_caches()
    sl._fold_cache.clear()
    assert sl.ledger_dir(SLOT).exists(), "the legacy store is preserved by the funnel"

    # A successor on the same recycled slot key starts clean.
    _unit(LATER_SESSION)
    state = sl.record(SLOT, session_id=LATER_SESSION, next_step="second life")
    assert state["goal"] == ""
    assert state["phase"] == ""
    assert sl.read_state(SLOT)["goal"] == ""


def test_a_delete_with_no_units_still_tombstones_the_legacy_document():
    """The slot's legacy document outlives a delete that had no crew log to remove.

    A conversation that never recorded has no unit to exclude, so the exclusion has
    nothing to write -- but the pre-projection document it would have carried is still
    on disk, and the age-based sweep is the only thing that ever removes it. Returning
    early here leaves that document claimable by the next session on the recycled slot
    key, which is the resurrection this file exists to prevent.
    """
    _legacy_document(SLOT, goal="deleted conversation", phase="implementing")

    # The permanent delete of a slot with NO units: nothing to exclude, everything to
    # tombstone.
    recorded = sl.exclude_units(SLOT, ())
    assert recorded.added == ()
    assert recorded.carry_tombstoned is True

    _unit(LATER_SESSION)
    state = sl.record(SLOT, session_id=LATER_SESSION, next_step="second life")
    assert state["goal"] == ""
    assert state["phase"] == ""


def test_a_delete_that_did_not_happen_gives_the_tombstone_back():
    """The session still exists, so its earlier state is still owed to it.

    A committed marker is permanent for every other reader, so a tombstone left behind
    by a rolled-back delete would silence that live session's pre-projection goal and
    phase for good -- and the sweep will not clear it, because the session is not
    finished. This is the delete that HAD a unit: the rollback empties the exclusion
    file and the tombstone goes with the last id out of it.
    """
    _legacy_document(SLOT, goal="still mine", phase="implementing")
    recorded = sl.exclude_units(SLOT, ("u-old",))
    assert recorded.added == ("u-old",)
    assert recorded.carry_tombstoned is True

    sl.unexclude_units(SLOT, recorded.added, restore_carry=recorded.carry_tombstoned)
    assert sl._excluded_units(SLOT) == frozenset()

    _unit(SESSION)
    state = sl.record(SLOT, session_id=SESSION, next_step="carry on")
    assert state["goal"] == "still mine"
    assert state["phase"] == "implementing"


def test_a_rolled_back_empty_unit_delete_gives_the_tombstone_back_too():
    """The same rule where the delete recorded no ids, so there is no file to empty.

    Its own branch, because an exclusion that added nothing never wrote the file: the
    rollback has to reach the tombstone without one to read.
    """
    _legacy_document(SLOT, goal="still mine", phase="implementing")
    recorded = sl.exclude_units(SLOT, ())
    assert recorded.added == ()
    assert recorded.carry_tombstoned is True

    sl.unexclude_units(SLOT, recorded.added, restore_carry=recorded.carry_tombstoned)

    _unit(SESSION)
    assert sl.record(SLOT, session_id=SESSION, next_step="carry on")["goal"] == "still mine"


def test_another_deletes_exclusion_keeps_the_tombstone_standing():
    """An id still recorded is evidence that some delete of this slot DID proceed.

    The legacy document is the slot's rather than any one conversation's, so it stays
    tombstoned while any delete's exclusion stands, even though this rollback wrote the
    tombstone itself.
    """
    _legacy_document(SLOT, goal="a deleted conversation's")
    first = sl.exclude_units(SLOT, ("u-first",))
    assert first.carry_tombstoned is True
    second = sl.exclude_units(SLOT, ("u-second",))
    assert second.carry_tombstoned is False, "already tombstoned, so not the second's"

    # The FIRST delete rolls back; the second's exclusion is still recorded.
    sl.unexclude_units(SLOT, first.added, restore_carry=first.carry_tombstoned)

    marker = sl.control_dir(SLOT) / sl._CARRIED_FILE
    assert marker.read_text(encoding="utf-8").strip() == sl._CARRY_COMMITTED
    _unit(LATER_SESSION)
    assert sl.record(SLOT, session_id=LATER_SESSION, next_step="n")["goal"] == ""


def test_the_carry_marks_the_document_consumed():
    _legacy_document(SLOT, goal="carried once")
    _unit()
    sl.record(SLOT, session_id=SESSION, next_step="n")
    marker = sl.control_dir(SLOT) / sl._CARRIED_FILE
    assert marker.read_text(encoding="utf-8").strip() == sl._CARRY_COMMITTED


def test_the_control_files_live_outside_the_sweepable_store():
    """A store is collectable residue; the files governing its fold are not.

    ``purge_matching`` removes a whole store by breadcrumb and the sweep proposes a
    finished one for purge by age, so an exclusion list inside a store is removable by
    documented maintenance -- and losing it is what lets a recycled slot key fold a
    deleted conversation's units.
    """
    _legacy_document(SLOT, goal="g")
    _unit()
    sl.record(SLOT, session_id=SESSION, next_step="n")
    sl.exclude_units(SLOT, ("some-other-unit",))

    store = sl.ledger_dir(SLOT)
    control = sl.control_dir(SLOT)
    for name in (sl._CARRIED_FILE, sl._DELETED_UNITS_FILE, sl._UNIT_ORDER_FILE):
        assert (control / name).exists(), name
        assert not (store / name).exists(), name
    assert store.parent == control.parent.parent


def test_a_store_purge_leaves_the_slot_exclusions_standing():
    """The purge that makes a conversation unreadable must not erase what excludes it."""
    _legacy_document(SLOT, goal="g")
    (sl.ledger_dir(SLOT) / sl._LOCK_FILE).touch()  # purge enters a store by its own lock
    _unit()
    sl.record(SLOT, session_id=SESSION, next_step="n")
    sl.exclude_units(SLOT, ("deleted-unit",))

    assert sl.purge_matching({SLOT}, guard=lambda _dir: True) == 1
    assert not sl.ledger_dir(SLOT).exists()
    assert sl._excluded_units(SLOT) == frozenset({"deleted-unit"})


def test_an_abandoned_claim_is_taken_over_so_a_crashed_carry_retries():
    """A claim published before the carry lands would make a crash permanent.

    The holder's window is bounded by its own append wait, so a ``pending`` marker
    older than that is abandoned rather than in flight.
    """
    _legacy_document(SLOT, goal="carried after a crash")
    assert sl._claim_carry(SLOT) == sl._CLAIM_TAKEN  # claims, then dies before appending
    marker = sl.control_dir(SLOT) / sl._CARRIED_FILE
    assert marker.read_text(encoding="utf-8").strip() == sl._CARRY_PENDING

    old = time.time() - sl._CARRY_STALE_SECS - 1
    os.utime(marker, (old, old))  # the holder is gone, not in flight
    _unit()
    sl.record(SLOT, session_id=SESSION, next_step="n")
    assert sl.read_state(SLOT)["goal"] == "carried after a crash"
    assert marker.read_text(encoding="utf-8").strip() == sl._CARRY_COMMITTED


def test_a_committed_claim_is_never_taken_over():
    """Take-over is for a carry that did not land; a landed one stays consumed.

    Otherwise a permanent delete's preserved document comes back on a recycled key.
    """
    _legacy_document(SLOT, goal="deleted conversation")
    _unit()
    sl.record(SLOT, session_id=SESSION, next_step="n")
    marker = sl.control_dir(SLOT) / sl._CARRIED_FILE
    old = time.time() - sl._CARRY_STALE_SECS * 100
    os.utime(marker, (old, old))
    assert sl._claim_carry(SLOT) == sl._CLAIM_DONE


def test_a_carry_whose_append_may_still_be_queued_keeps_its_claim():
    """Releasing here lets the retry carry a SECOND time, and both entries are kept.

    A flush that ran out of budget leaves work in the queue, so the append may still
    land. The retry reads the same empty fold, so a released claim lets it carry again
    -- and the fold applies both carried entries, with nothing that dedups them. The
    claim therefore stays ``pending`` and the retry is refused instead.
    """
    _legacy_document(SLOT, goal="owed")
    _unit()
    from kiro_crew.crew_log import emit as crew_log_emit

    real_flush = crew_log_emit.flush
    real_seq = sl._unit_last_seq
    # The exact hazard: the flush budget expires AND this unit's seq has not moved, so
    # the append is neither proved landed nor proved lost.
    crew_log_emit.flush = lambda timeout=0.0: False  # type: ignore[assignment]
    sl._unit_last_seq = lambda session_id: 0  # type: ignore[assignment]
    try:
        with pytest.raises(sl.LedgerUnavailable):
            sl.record(SLOT, session_id=SESSION, next_step="n")
        marker = sl.control_dir(SLOT) / sl._CARRIED_FILE
        assert marker.exists()
        assert marker.read_text(encoding="utf-8").strip() == sl._CARRY_PENDING
        # That claim is what answers the retry, so it cannot carry a second time.
        assert sl._claim_carry(SLOT) == sl._CLAIM_BUSY
    finally:
        crew_log_emit.flush = real_flush  # type: ignore[assignment]
        sl._unit_last_seq = real_seq  # type: ignore[assignment]

    # And the state is not lost by holding the claim: the queued append is the one that
    # lands, so the record reads it and exactly one carried entry exists.
    sl._fold_cache.clear()
    assert sl.read_state(SLOT)["goal"] == "owed"
    carried = [
        e
        for e in _entries()
        if e.type == sl.LEDGER_ENTRY_TYPE and "carried forward" in e.data.get("event", "")
    ]
    assert len(carried) == 1


def test_a_carry_the_queue_drained_without_releases_its_claim():
    """A drained queue that left this unit's seq unmoved proves the append is gone.

    Nothing is in flight, so holding the claim would refuse updates for the whole
    staleness window and gain nothing; releasing lets the retry carry at once.
    """
    _legacy_document(SLOT, goal="owed")
    _unit()
    from kiro_crew.crew_log import emit as crew_log_emit

    real = crew_log_emit.on_ledger_recorded
    crew_log_emit.on_ledger_recorded = lambda *a, **kw: None  # type: ignore[assignment]
    try:
        with pytest.raises(sl.LedgerUnavailable):
            sl.record(SLOT, session_id=SESSION, next_step="n")
    finally:
        crew_log_emit.on_ledger_recorded = real  # type: ignore[assignment]
    assert not (sl.control_dir(SLOT) / sl._CARRIED_FILE).exists()

    sl._fold_cache.clear()
    sl.record(SLOT, session_id=SESSION, next_step="n")
    assert sl.read_state(SLOT)["goal"] == "owed"


def test_two_concurrent_first_records_carry_the_document_once():
    """The claim is an exclusive create, so only one of them wins.

    Checking a marker and then carrying is a race two first records pass together --
    a resumed loop and its own dashboard tab produce exactly that -- and both would
    append the legacy goal.
    """
    _legacy_document(SLOT, goal="carried once")
    assert sl._claim_carry(SLOT) == sl._CLAIM_TAKEN
    assert sl._claim_carry(SLOT) == sl._CLAIM_BUSY


def test_a_slot_with_no_legacy_directory_claims_nothing():
    """The claim must not create a directory for a document that does not exist."""
    assert sl._claim_carry("chat-fresh") == sl._CLAIM_DONE
    assert not sl.ledger_dir("chat-fresh").exists()


def test_the_recorded_order_beats_a_backward_header_clock():
    """Append order is causal; a header's wall clock is not.

    An NTP correction or a manual set moves the clock backward, and a unit created
    after that sorts before its predecessor -- applying a retired session's goal over
    a later one's. The order the slot actually recorded in is what the fold uses.
    """
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="first recorded")
    _unit(LATER_SESSION)
    sl.record(SLOT, session_id=LATER_SESSION, goal="second recorded")
    assert sl._recorded_unit_order(SLOT) == (SESSION, LATER_SESSION)

    real = store.session_units_for_slot
    store.session_units_for_slot = lambda slot: tuple(reversed(real(slot)))  # type: ignore[assignment]
    sl._fold_cache.clear()
    try:
        # No live session id, so ordering is all the fold has to go on.
        assert sl.read_state(SLOT)["goal"] == "second recorded"
    finally:
        store.session_units_for_slot = real  # type: ignore[assignment]


def test_the_public_slot_fold_applies_the_same_exclusions():
    """The HTTP projection route must not serve a different answer from read_state."""
    from kiro_crew.crew_log import projection as proj

    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="deleted conversation")
    assert proj.read_slot_projection(SLOT, sl._FOLD_NAME).value["goal"] == ("deleted conversation")

    sl.exclude_units(SLOT, sl.crew_log_units(SLOT))
    sl._fold_cache.clear()
    assert proj.read_slot_projection(SLOT, sl._FOLD_NAME).value["goal"] == ""


def test_a_unit_evicted_from_the_order_log_applies_before_the_kept_tail(monkeypatch):
    """The log keeps the NEWEST ids, so anything missing from it is OLDER than all of them.

    Folding an evicted unit after the kept tail replays a slot's oldest state last,
    writing its stale goal over the current one.
    """
    monkeypatch.setattr(sl, "_MAX_ORDERED_UNITS", 2)
    third = "acp-3"
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="oldest")
    sl._fold_cache.clear()
    _unit(LATER_SESSION)
    sl.record(SLOT, session_id=LATER_SESSION, next_step="n")
    sl._fold_cache.clear()
    _unit(third)
    sl.record(SLOT, session_id=third, goal="newest")
    sl._fold_cache.clear()

    assert sl._recorded_unit_order(SLOT) == (LATER_SESSION, third)
    assert sl.crew_log_units(SLOT) == (SESSION, LATER_SESSION, third)
    assert sl.read_state(SLOT)["goal"] == "newest"


def test_a_repeated_id_in_the_order_log_folds_its_unit_once():
    """The write's own is-it-known check is not atomic, so an id can be appended twice.

    A repeated id would put one unit in the fold's order twice, replaying entries the
    fold has already consumed.
    """
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="once")
    path = sl.control_dir(SLOT) / sl._UNIT_ORDER_FILE
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{SESSION}\n")

    assert sl._recorded_unit_order(SLOT) == (SESSION,)
    assert sl.crew_log_units(SLOT) == (SESSION,)
    sl._fold_cache.clear()
    assert sl.read_state(SLOT)["goal"] == "once"


def test_a_refusal_during_the_append_is_not_reported_durable():
    """The refusal count brackets the append, so an INLINE refusal is visible.

    A second gateway owning the unit appends inline rather than queueing, so the
    counter moves during the append; a sample taken afterwards folds that move into
    the baseline and calls a refused write durable.
    """
    _unit(SESSION)
    refused = [0]
    real_emit = crew_log_emit.on_ledger_recorded

    def _refusing(session_id, data):
        refused[0] += 1  # the append is refused as it is made, not queued

    crew_log_emit.on_ledger_recorded = _refusing  # type: ignore[assignment]
    real_dropped = crew_log_emit.dropped_writes
    crew_log_emit.dropped_writes = lambda: refused[0]  # type: ignore[assignment]
    try:
        _state, durable = sl.record_update(SLOT, session_id=SESSION, goal="g")
    finally:
        crew_log_emit.on_ledger_recorded = real_emit  # type: ignore[assignment]
        crew_log_emit.dropped_writes = real_dropped  # type: ignore[assignment]
    assert durable is False


def test_a_caller_whose_key_is_not_the_slot_key_reads_its_own_record():
    """The write lands in the unit; the read must look up the identity the unit NAMES.

    A cron-injected, hook, task-runner or ACP subagent session is keyed `cron:<id>`
    and the like, which `ledger_key` leaves alone, while the unit header records the
    real slot key. Without resolving that, every read after the write is empty.
    """
    _unit(SESSION, slot="chat-real-slot")
    caller = "cron:42"
    sl.record(caller, session_id=SESSION, goal="recorded by a cron session")
    sl._fold_cache.clear()
    assert sl.read_state(caller, SESSION)["goal"] == "recorded by a cron session"
    assert sl.canonical_slot(caller, SESSION) == "chat-real-slot"


def test_every_control_file_keys_off_the_canonical_slot():
    """One slot, one exclusion list and one order log, however a caller spells its key.

    The delete funnel records exclusions under the HEADER's slot, so a caller whose
    control files sat under its own spelling would miss them and fold units a delete
    excluded.
    """
    _unit(SESSION, slot="chat-real-slot")
    caller = "cron:42"
    sl.record(caller, session_id=SESSION, goal="g")

    assert (sl.control_dir("chat-real-slot") / sl._UNIT_ORDER_FILE).exists()
    assert not (sl.control_dir(caller) / sl._UNIT_ORDER_FILE).exists()

    # The funnel's own spelling, and the cron caller's read must honour it.
    sl.exclude_units("chat-real-slot", (SESSION,))
    sl._fold_cache.clear()
    assert sl.read_state(caller, SESSION)["goal"] == ""


def test_a_record_written_under_the_caller_key_keeps_reading():
    """The caller's spelling is joined as an ALIAS, so nothing already written goes dark.

    A record written before the canonical resolution sits in a unit whose header names
    the caller's own key, and dropping that unit from the join would lose it.
    """
    _unit(SESSION, slot="cron:42")  # a unit whose header names the caller's own key
    sl.record("cron:42", session_id=SESSION, goal="written under the alias")
    sl._fold_cache.clear()
    _unit(LATER_SESSION, slot="chat-real-slot")  # the live unit, canonically named
    sl.record("cron:42", session_id=LATER_SESSION, next_step="written canonically")

    state = sl.read_state("cron:42", LATER_SESSION)
    assert state["goal"] == "written under the alias"
    assert state["next"] == "written canonically"


def test_a_caller_that_is_not_the_carrier_refuses_instead_of_recording_first():
    """Recording past someone else's live carry lets the legacy state apply LAST.

    The non-carrier's own update would be appended while the carry is still queued,
    and the carry would then overwrite it with the legacy goal and phase.
    """
    _legacy_document(SLOT, goal="legacy")
    _unit()
    assert sl._claim_carry(SLOT) == sl._CLAIM_TAKEN  # another caller holds the claim
    with pytest.raises(sl.LedgerUnavailable):
        sl.record(SLOT, session_id=SESSION, next_step="mine")


def test_durability_is_answered_from_this_units_own_growth():
    """A process-wide counter cannot say whether THIS append landed.

    An append that never reaches the file leaves the unit's newest seq where it was,
    which is evidence about this write rather than about every session in the process.
    """
    _unit(SESSION)
    real = crew_log_emit.on_ledger_recorded
    crew_log_emit.on_ledger_recorded = lambda session_id, data: None  # never written
    try:
        _state, durable = sl.record_update(SLOT, session_id=SESSION, goal="g")
    finally:
        crew_log_emit.on_ledger_recorded = real  # type: ignore[assignment]
    assert durable is False

    sl._fold_cache.clear()
    _state, durable = sl.record_update(SLOT, session_id=SESSION, goal="g2")
    assert durable is True


def test_the_order_file_is_compacted_rather_than_grown_forever(monkeypatch):
    """A bound that bounds only what a read returns leaves the file unbounded.

    Past the window the dedup check sees only the newest ids, so a slot re-appends the
    older ones and the file grows without limit.
    """
    monkeypatch.setattr(sl, "_MAX_ORDERED_UNITS", 3)
    monkeypatch.setattr(sl, "_MAX_ORDER_BYTES", 40)
    path = sl.control_dir(SLOT) / sl._UNIT_ORDER_FILE
    for n in range(12):
        unit = f"acp-{n}"
        _unit(unit)
        sl.record(SLOT, session_id=unit, next_step=f"n{n}")
        sl._fold_cache.clear()
        assert path.stat().st_size <= sl._MAX_ORDER_BYTES + 32, n

    kept = sl._recorded_unit_order(SLOT)
    assert kept == ("acp-9", "acp-10", "acp-11")
    # Bounded, not exact: compaction fires when the file crosses the size bound, so the
    # tail can hold one append made after the last rewrite.
    assert len(path.read_text(encoding="utf-8").splitlines()) <= sl._MAX_ORDERED_UNITS + 1


def test_an_unreadable_legacy_document_refuses_rather_than_reading_as_empty():
    """ "Nothing to carry" lands the update, and the carry never fires again.

    The trigger is an EMPTY folded record, so one transient read failure would orphan
    the legacy goal and phase for good.
    """
    _legacy_document(SLOT, goal="must survive a transient error")
    _unit()
    real = Path.stat

    def _boom(self, *a, **kw):
        if self.name == sl._STATE_FILE:
            raise OSError("transient")
        return real(self, *a, **kw)

    Path.stat = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(sl.LedgerUnavailable):
            sl.record(SLOT, session_id=SESSION, next_step="n")
    finally:
        Path.stat = real  # type: ignore[assignment]

    sl._fold_cache.clear()
    sl.record(SLOT, session_id=SESSION, next_step="n")
    assert sl.read_state(SLOT)["goal"] == "must survive a transient error"


def test_damaged_legacy_json_is_consumed_rather_than_retried_forever():
    """A file no retry can parse is the empty record, not a transient failure."""
    directory = sl.ledger_dir(SLOT)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / sl._STATE_FILE).write_text("{not json", encoding="utf-8")
    (directory / sl._KEY_FILE).write_text(SLOT + "\n", encoding="utf-8")
    _unit()
    sl.record(SLOT, session_id=SESSION, next_step="n")
    assert sl.read_state(SLOT)["next"] == "n"


def test_the_carry_commits_only_when_its_own_unit_grew():
    """Committing on a weaker signal marks the document consumed without carrying it."""
    _legacy_document(SLOT, goal="owed")
    _unit()
    real = sl._unit_last_seq
    sl._unit_last_seq = lambda unit_id: 0  # never grows
    try:
        with pytest.raises(sl.LedgerUnavailable):
            sl.record(SLOT, session_id=SESSION, next_step="n")
    finally:
        sl._unit_last_seq = real  # type: ignore[assignment]
    # The claim was released, so the retry carries rather than finding it consumed.
    assert not (sl.control_dir(SLOT) / sl._CARRIED_FILE).exists()


def test_a_delete_consumes_the_carry_claim_so_no_later_session_re_carries():
    """A delete removes the evidence the take-over rule rests on, so it settles the claim.

    Take-over is safe only because a landed carry leaves the folded record non-empty. A
    permanent delete removes the unit holding that entry, so the record reads empty
    again -- and a marker left `pending` by a crash between the carry append and its
    commit would let the next session on the recycled slot key carry the preserved
    document back in.
    """
    _legacy_document(SLOT, goal="deleted conversation")
    _unit(SESSION)
    assert sl._claim_carry(SLOT) == sl._CLAIM_TAKEN  # claimed, then the process dies
    marker = sl.control_dir(SLOT) / sl._CARRIED_FILE
    old = time.time() - sl._CARRY_STALE_SECS - 1
    os.utime(marker, (old, old))

    sl.exclude_units(SLOT, (SESSION,))  # the permanent delete
    assert marker.read_text(encoding="utf-8").strip() == sl._CARRY_COMMITTED

    # A successor on the same recycled slot key reads empty rather than the deleted goal.
    _unit(LATER_SESSION)
    sl._fold_cache.clear()
    sl.record(SLOT, session_id=LATER_SESSION, next_step="fresh")
    assert sl.read_state(SLOT, LATER_SESSION)["goal"] == ""


def test_an_oversized_exclusion_file_is_rejected_rather_than_truncated():
    """A truncated read rewritten as the whole set drops the newest exclusions for good.

    The exclusion file is bounded by COUNT, and that bound is what is meant to fail
    closed; a byte cap borrowed from a smaller file would silently cut the tail, which
    is the most recently deleted units.
    """
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="g")
    path = sl.control_dir(SLOT) / sl._DELETED_UNITS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("u\n" * (sl._MAX_EXCLUDED_BYTES // 2 + 8), encoding="utf-8")

    # The fold FAILS CLOSED to the empty record rather than folding units it cannot
    # prove are still included...
    sl._fold_cache.clear()
    assert sl.read_state(SLOT) == sl._empty_state()
    # ...and the write refuses outright rather than rewriting what it could read.
    with pytest.raises(sl.LedgerExclusionError):
        sl.exclude_units(SLOT, ("another",))


def test_an_unreadable_exclusion_list_reads_empty_rather_than_deleted_state():
    """Saying "nothing is excluded" is the one wrong answer: it serves deleted state.

    An empty record tells the person nothing and is recovered by the next read that can
    see the list; folding the units tells them someone else's goal and phase, and nothing
    later takes that back.
    """
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="deleted conversation")
    assert sl.read_state(SLOT)["goal"] == "deleted conversation"

    real = sl._read_lines

    def _unreadable(path, **kw):
        if path.name == sl._DELETED_UNITS_FILE:
            raise OSError("unreadable")
        return real(path, **kw)

    sl._read_lines = _unreadable  # type: ignore[assignment]
    sl._fold_cache.clear()
    try:
        assert sl.read_state(SLOT) == sl._empty_state()
    finally:
        sl._read_lines = real  # type: ignore[assignment]


def test_the_exclusion_read_bound_is_sized_for_its_own_count_bound():
    """Sharing the order file's smaller cap is what truncated a valid set."""
    assert sl._MAX_EXCLUDED_BYTES > sl._MAX_ORDER_READ_BYTES
    assert sl._MAX_EXCLUDED_BYTES >= sl._MAX_EXCLUDED_UNITS * 2


def test_a_claim_error_refuses_the_update_instead_of_skipping_the_carry():
    """Answering "done" on a transient error loses the legacy state permanently.

    The update would be appended, the folded record would stop being empty, and the
    carry is only ever attempted while it is empty.
    """
    _legacy_document(SLOT, goal="must not be lost")
    _unit()
    real = sl._control_file

    def _boom(slot_key, name, *, create=False):
        if name == sl._CARRIED_FILE:
            raise OSError("transient")
        return real(slot_key, name, create=create)

    sl._control_file = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(sl.LedgerUnavailable):
            sl.record(SLOT, session_id=SESSION, next_step="n")
    finally:
        sl._control_file = real  # type: ignore[assignment]

    # Nothing was appended, so the retry still finds an empty record and carries.
    sl._fold_cache.clear()
    sl.record(SLOT, session_id=SESSION, next_step="n")
    assert sl.read_state(SLOT)["goal"] == "must not be lost"


def test_a_failed_rollback_is_reported_rather_than_swallowed(caplog):
    """A request that answers cleanly while a live record reads empty has lied."""
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="g")
    added = sl.exclude_units(SLOT, ("some-unit",)).added
    real = sl._rewrite_lines
    sl._rewrite_lines = lambda path, lines: (_ for _ in ()).throw(OSError("full disk"))

    try:
        with caplog.at_level("ERROR"), pytest.raises(sl.LedgerExclusionError):
            sl.unexclude_units(SLOT, added)
    finally:
        sl._rewrite_lines = real  # type: ignore[assignment]
    assert any("could NOT roll back" in r.message for r in caplog.records)


def test_an_exclusion_that_cannot_be_read_back_refuses_the_delete(caplog):
    """A silent exclusion failure would leave deleted state foldable by a successor.

    Raising is what aborts the delete funnel before a unit is removed: an undeleted
    conversation is visible and can be deleted again, while its state appearing in a
    stranger's session cannot be undone.
    """
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="g")
    real = sl._read_lines
    sl._read_lines = lambda path, **kw: ()  # type: ignore[assignment]
    try:
        with caplog.at_level("ERROR"), pytest.raises(sl.LedgerExclusionError):
            sl.exclude_units(SLOT, (SESSION,))
    finally:
        sl._read_lines = real  # type: ignore[assignment]
    assert any("could NOT record" in r.message for r in caplog.records)


def test_an_exclusion_over_the_bound_refuses_rather_than_dropping_one(monkeypatch):
    """Dropping an id to stay under a bound resurrects what the file exists to hide."""
    monkeypatch.setattr(sl, "_MAX_EXCLUDED_UNITS", 6)
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="g")
    sl.exclude_units(SLOT, tuple(f"old-{n}" for n in range(5)))
    with pytest.raises(sl.LedgerExclusionError):
        sl.exclude_units(SLOT, tuple(f"new-{n}" for n in range(5)))
    # The refusal changed nothing: the ids already recorded are still there.
    assert "old-0" in sl._excluded_units(SLOT)
    assert not any(unit.startswith("new-") for unit in sl._excluded_units(SLOT))


def test_a_rollback_takes_back_only_the_ids_that_call_added():
    """A rollback that dropped every id it was asked about would undo another delete's.

    Two deletes of one slot share the ids of the units they both see; the loser must
    not take away the winner's exclusions.
    """
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="g")
    first = sl.exclude_units(SLOT, ("shared-unit",))
    assert first.added == ("shared-unit",)
    second = sl.exclude_units(SLOT, ("shared-unit", "its-own-unit"))
    assert second.added == ("its-own-unit",)

    sl.unexclude_units(SLOT, second.added)  # the second delete did not proceed
    assert sl._excluded_units(SLOT) == frozenset({"shared-unit"})


def test_the_exclusion_file_holds_each_id_once():
    """A file that already holds repeats is COMPACTED, not carried forward.

    The write path cannot add a repeat, so the dedup is for a file written by hand or
    by a build without this bound: repeats would otherwise count against the bound and
    grow the read without meaning anything.
    """
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="g")
    path = sl.control_dir(SLOT) / sl._DELETED_UNITS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("u1\nu1\nu1\nu2\n", encoding="utf-8")

    assert sl._excluded_units(SLOT) == frozenset({"u1", "u2"})
    sl.exclude_units(SLOT, ("u3",))
    assert path.read_text(encoding="utf-8").split() == ["u1", "u2", "u3"]


def test_an_excluded_unit_is_never_folded_again():
    """A permanent delete removes one unit; a slot's EARLIER units survive it.

    Without the exclusion a fresh session on the same recycled slot key folds those
    survivors and reads a deleted conversation's goal and phase.
    """
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="deleted conversation")
    _unit(LATER_SESSION)
    sl.record(SLOT, session_id=LATER_SESSION, next_step="also deleted")
    assert sl.read_state(SLOT)["goal"] == "deleted conversation"

    sl.exclude_units(SLOT, sl.crew_log_units(SLOT))
    sl._fold_cache.clear()
    state = sl.read_state(SLOT)
    assert state["goal"] == ""
    assert state["next"] == ""


def test_an_exclusion_does_not_reach_a_unit_created_afterwards():
    """The ids are recorded at delete time, so a successor's unit is not among them."""
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="deleted")
    sl.exclude_units(SLOT, sl.crew_log_units(SLOT))
    sl._fold_cache.clear()

    _unit(LATER_SESSION)
    state = sl.record(SLOT, session_id=LATER_SESSION, goal="the successor's own goal")
    assert state["goal"] == "the successor's own goal"


def test_the_live_unit_applies_last_even_when_the_clock_went_backwards():
    """Units are ordered by header wall clock, which can inverse; the live one wins.

    A clock that moves backward before a replacement unit is created sorts that
    replacement before its predecessor, so a retired session's state would apply over
    the current session's. The unit being written is pinned last.
    """
    _unit(SESSION)
    sl.record(
        SLOT,
        session_id=SESSION,
        phase="implementing",
        next_step="retired step",
        event="retired",
        event_kind="progress",
    )
    _unit(LATER_SESSION)
    sl.record(
        SLOT,
        session_id=LATER_SESSION,
        phase="verifying",
        next_step="live step",
        event="live",
        event_kind="progress",
    )

    # Report the units in the inverted order a backward clock produces.
    real = store.session_units_for_slot

    def _inverted(slot: str) -> "tuple[str, ...]":
        return tuple(reversed(real(slot)))

    store.session_units_for_slot = _inverted  # type: ignore[assignment]
    sl._fold_cache.clear()
    try:
        # A read that names the live session still applies it last.
        state = sl.record(SLOT, session_id=LATER_SESSION, event="probe", event_kind="progress")
    finally:
        store.session_units_for_slot = real  # type: ignore[assignment]

    assert state["phase"] == "verifying"
    assert state["next"] == "live step"


def test_a_fold_racing_an_append_is_not_cached_with_a_stale_seq():
    """A pre-sampled seq beside a newer checkpoint would make the next read empty.

    The sample is taken before the fold, so an append landing while it runs is in the
    checkpoint but not in the sample. Caching that pair would have the next read
    advance from a seq already folded, which ``advance`` refuses.
    """
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="before the race")
    sl._fold_cache.clear()

    real_last_seq = sl._unit_last_seq
    calls = {"n": 0}

    def _sampling_that_lands_an_append(unit_id: str) -> int:
        calls["n"] += 1
        seq = real_last_seq(unit_id)
        if calls["n"] == 1:
            # Between the sample and the fold, one more entry lands.
            handle = CrewLog.open(lg.KIND_SESSION, SESSION)
            handle.append(
                sl.LEDGER_ENTRY_TYPE,
                {"slot": SLOT, "next": "landed during the fold"},
                src="gateway",
            )
            del handle
        return seq

    sl._unit_last_seq = _sampling_that_lands_an_append  # type: ignore[assignment]
    try:
        first = sl.read_state(SLOT)
    finally:
        sl._unit_last_seq = real_last_seq  # type: ignore[assignment]

    # The racing entry is folded in, and the pair was not cached ...
    assert first["next"] == "landed during the fold"
    assert (str(sl.data_home()), SLOT) not in sl._fold_cache
    # ... so the next read still answers correctly rather than emptily.
    assert sl.read_state(SLOT)["next"] == "landed during the fold"
    assert sl.read_state(SLOT)["goal"] == "before the race"


def test_an_empty_legacy_document_is_not_carried():
    _legacy_document(SLOT)
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="fresh")
    assert [
        e
        for e in _entries()
        if e.type == sl.LEDGER_ENTRY_TYPE and "carried forward" in e.data.get("event", "")
    ] == []


def test_a_legacy_document_over_the_size_ceiling_is_not_parsed():
    """A damaged or hostile file cannot make an upgrade allocate its size."""
    directory = sl.ledger_dir(SLOT)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / sl._STATE_FILE).write_text("x" * (sl._MAX_STATE_BYTES + 10), encoding="utf-8")
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="fresh")
    assert sl.read_state(SLOT)["goal"] == "fresh"


# --------------------------------------------------------------------------- #
# the append is durable before it is acknowledged
# --------------------------------------------------------------------------- #


def test_the_entry_is_on_disk_before_record_returns():
    """An acknowledgement a caller acts on has to mean the entry landed."""
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="durable", event="e", event_kind="progress")
    # Read the file directly rather than through the fold, and take no flush of our
    # own: if the append were still queued this would see nothing.
    assert [e for e in _entries() if e.type == sl.LEDGER_ENTRY_TYPE]


# --------------------------------------------------------------------------- #
# the slot cache cannot serve a stale map
# --------------------------------------------------------------------------- #


def test_a_unit_whose_header_lands_after_a_scan_becomes_visible():
    """``create`` publishes the header AFTER its mkdir, and that lands inside the
    directory, so the root's mtime and child count do not move with it. A cache
    keyed on the root alone would hold a map missing this unit until unrelated
    churn happened to invalidate it."""
    directory = store.crew_log_dir(lg.KIND_SESSION, SESSION)
    directory.mkdir(parents=True, exist_ok=True)
    # Scan the window: the store exists, its header does not.
    assert store.session_units_for_slot(SLOT) == ()
    # Publish the header without touching the root.
    _unit()
    assert store.session_units_for_slot(SLOT) == (SESSION,)


def test_a_permanently_unprovable_child_does_not_break_the_scan():
    """A stray directory is re-checked and never proves; the real units still resolve."""
    (store.crew_log_root(lg.KIND_SESSION) / "not-a-unit").mkdir(parents=True, exist_ok=True)
    _unit()
    assert store.session_units_for_slot(SLOT) == (SESSION,)
    assert store.session_units_for_slot(SLOT) == (SESSION,)


# --------------------------------------------------------------------------- #
# the fold is continued, not re-walked
# --------------------------------------------------------------------------- #


def test_the_continued_fold_equals_a_cold_one_at_every_update():
    """The resumed answer and the from-scratch answer are one implementation.

    The record is read on every loop wake, so it is folded from a cached checkpoint
    rather than re-walked from seq 1. That is only safe while the two agree.
    """
    _unit()
    for n in range(6):
        sl.record(
            SLOT,
            session_id=SESSION,
            next_step=f"step {n}",
            event=f"e{n}",
            event_kind="progress",
        )
        warm = sl.read_state(SLOT)
        sl._fold_cache.clear()
        assert sl.read_state(SLOT) == warm, f"warm and cold folds disagree after {n + 1} updates"


def test_a_new_unit_for_the_slot_rebuilds_the_fold():
    """A reset gives the slot another session, so the cached unit list is stale."""
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="before")
    assert sl.read_state(SLOT)["goal"] == "before"
    _unit(LATER_SESSION)
    sl.record(SLOT, session_id=LATER_SESSION, next_step="after")
    state = sl.read_state(SLOT)
    assert state["goal"] == "before"
    assert state["next"] == "after"


def test_the_fold_cache_is_bounded_by_slot_count():
    """A gateway sees many slots over its life; the cache cannot grow with them."""
    _unit()
    for n in range(sl._FOLD_CACHE_SLOTS + 8):
        sl.read_state(f"chat-{n}")
    assert len(sl._fold_cache) <= sl._FOLD_CACHE_SLOTS


def test_growth_in_an_older_unit_is_not_hidden_by_the_cache():
    """An earlier unit is not closed to writes, so its seq is part of the cache key.

    A forced reset tears a session down while a turn is still running and that turn
    keeps appending through the handle it holds. Keying growth on the newest unit
    alone left those entries permanently outside the record: the unit list is
    unchanged and the newest seq is unchanged, so nothing would invalidate it.
    """
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="first")
    _unit(LATER_SESSION)
    sl.record(SLOT, session_id=LATER_SESSION, next_step="second")
    assert sl.read_state(SLOT)["next"] == "second"

    # Append to the OLDER unit, exactly as a turn that outlived its reset does.
    handle = CrewLog.open(lg.KIND_SESSION, SESSION)
    handle.append(
        sl.LEDGER_ENTRY_TYPE,
        {"slot": SLOT, "event": "late entry from the retired session", "event_kind": "progress"},
        src="gateway",
    )
    del handle

    events = [e["text"] for e in sl.read_state(SLOT)["events"]]
    assert "late entry from the retired session" in events


def test_a_durable_append_is_reported_as_durable():
    """The route tells a caller whether the update reached disk, rather than implying it."""
    _unit()
    _state, durable = sl.record_update(SLOT, session_id=SESSION, goal="g")
    assert durable is True


def test_the_plain_record_helper_answers_with_the_record_alone():
    """A caller that cannot act on durability keeps the record's ten fields.

    The durability rides beside the record rather than inside it, so the shape every
    reader already expected is unchanged.
    """
    _unit()
    state = sl.record(SLOT, session_id=SESSION, goal="g")
    assert state["goal"] == "g"
    assert "durable" not in state


# --------------------------------------------------------------------------- #
# damaged input cannot take the record down
# --------------------------------------------------------------------------- #


def test_a_timestamp_outside_the_datetime_range_does_not_crash_the_fold():
    """The line stays on disk, so a crash here would be permanent for that slot."""
    _unit()
    handle = CrewLog.open(lg.KIND_SESSION, SESSION)
    handle.append(sl.LEDGER_ENTRY_TYPE, {"slot": SLOT, "goal": "survives"}, src="gateway")
    del handle
    # Rewrite the entry's envelope time to a value no datetime can hold, the way a
    # damaged or planted line would carry it.
    path = store.crew_log_path(lg.KIND_SESSION, SESSION)
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    entry["time"] = 10**19
    lines[-1] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sl._fold_cache.clear()

    state = sl.read_state(SLOT)
    assert state["goal"] == "survives"
    assert state["created_at"] == ""
    # And the fold answers through the route's own entry point too, not only here.
    assert crew_log.read_slot_projection(SLOT, "ledger").value["goal"] == "survives"


def test_a_same_count_unit_swap_invalidates_the_slot_map():
    """A purge plus a create inside one mtime tick leaves the count and mtime equal.

    The names are in the fingerprint for that case: without them the replacement
    unit's record stays invisible for as long as the directory sits still.
    """
    _unit(SESSION)
    assert store.session_units_for_slot(SLOT) == (SESSION,)
    root = store.crew_log_root(lg.KIND_SESSION)
    before = root.stat().st_mtime_ns
    # Swap one unit for another and restore the root's mtime, so only the NAMES differ.
    # The emitter caches this unit's open handle, which HOLDS its `.lease`. Windows
    # refuses to unlink an open file, so the test drops the handle it caused to be
    # opened before standing in for a delete. POSIX would allow the unlink; the
    # cleanup is the test's either way.
    crew_log_emit.reset_caches()
    shutil.rmtree(store.crew_log_dir(lg.KIND_SESSION, SESSION))
    _unit(LATER_SESSION)
    os.utime(root, ns=(before, before))
    assert store.session_units_for_slot(SLOT) == (LATER_SESSION,)


def test_a_retried_delete_still_consumes_the_carry_claim():
    """The retry of a delete that already recorded its exclusions must settle the marker.

    A delete persists exclusions and then can still fail further on, so it is retried
    with the same units. That retry adds nothing -- and an early return on "nothing
    added" would skip the claim consumption entirely, leaving the `pending` marker of a
    crashed carry standing over a record the delete has since emptied. The next session
    on the recycled slot key would then take that claim over and carry the deleted
    document in, which is the resurrection the marker exists to prevent.
    """
    _legacy_document(SLOT, goal="deleted conversation")
    _unit(SESSION)
    assert sl._claim_carry(SLOT) == sl._CLAIM_TAKEN  # claimed, then the process dies
    marker = sl.control_dir(SLOT) / sl._CARRIED_FILE
    old = time.time() - sl._CARRY_STALE_SECS - 1
    os.utime(marker, (old, old))

    # The FIRST call records the exclusion; the marker settles here too.
    assert sl.exclude_units(SLOT, (SESSION,)).added == (SESSION,)
    marker.write_text(sl._CARRY_PENDING, encoding="utf-8")  # as a crash before it settled
    os.utime(marker, (old, old))

    # The RETRY adds nothing, and must still settle the claim.
    assert sl.exclude_units(SLOT, (SESSION,)).added == ()
    assert marker.read_text(encoding="utf-8").strip() == sl._CARRY_COMMITTED

    _unit(LATER_SESSION)
    sl._fold_cache.clear()
    sl.record(SLOT, session_id=LATER_SESSION, next_step="fresh")
    assert sl.read_state(SLOT, LATER_SESSION)["goal"] == ""


def test_a_failed_carry_does_not_withdraw_another_call_s_committed_marker():
    """Releasing a claim must never remove a COMMITTED marker.

    A marker that is absent is claimable, so a release that deleted the committed one
    would hand the next session on a recycled slot key a fresh claim over a deleted
    conversation's document. The committed marker is another call's proof that the
    carry landed, not this call's to withdraw.
    """
    _legacy_document(SLOT, goal="deleted conversation")
    _unit(SESSION)
    marker = sl.control_dir(SLOT) / sl._CARRIED_FILE
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(sl._CARRY_COMMITTED, encoding="utf-8")

    sl._finish_carry(SLOT, landed=False)  # a concurrent attempt gives up

    assert marker.exists(), "the committed marker was withdrawn by an unrelated release"
    assert marker.read_text(encoding="utf-8").strip() == sl._CARRY_COMMITTED
    assert sl._claim_carry(SLOT) == sl._CLAIM_DONE


def test_an_append_during_an_incremental_fold_is_not_cached_as_a_stale_checkpoint():
    """A checkpoint cached against a pre-read sample breaks the NEXT read.

    The incremental path samples seqs BEFORE advancing. An append landing during the
    advance is folded into the result but not into that sample, so caching the pair
    claims "state through N+1, seqs through N". The next read sees the seq move, judges
    itself continuable, and advances from an entry the checkpoint already folded --
    which ``advance`` REFUSES as at-or-below, and the read degrades to the empty record.
    """
    _unit(SESSION)
    for goal in ("first", "second", "third"):
        sl.record(SLOT, session_id=SESSION, goal=goal)
    # ``record`` folds to build its own answer, so the cache is already warm at the seq
    # BEFORE the newest entry. Two more appends are needed for a sample to sit between
    # that checkpoint and the truth, and they must not fold on the way in -- which is
    # why they go through the emitter rather than through ``record``.
    for goal in ("fourth", "fifth"):
        crew_log_emit.on_ledger_recorded(SESSION, {"slot": SLOT, "goal": goal})
    assert crew_log_emit.flush(timeout=5.0)

    # Stand in for an append landing DURING the advance: the pre-read sample misses the
    # newest seq, while ``iter_from`` streams to the end of the file and folds it.
    real = sl._unit_last_seq
    calls = {"n": 0}

    def _sampled_one_short(unit_id: str) -> int:
        calls["n"] += 1
        return real(unit_id) - 1 if calls["n"] == 1 else real(unit_id)

    sl._unit_last_seq = _sampled_one_short  # type: ignore[assignment]
    try:
        assert sl.read_state(SLOT, SESSION)["goal"] == "fifth"
    finally:
        sl._unit_last_seq = real  # type: ignore[assignment]

    # The read the stale pair would break: it must still answer, not fold from a seq the
    # cached checkpoint has already consumed and fall back to the empty record.
    assert sl.read_state(SLOT, SESSION)["goal"] == "fifth"


def test_a_tombstone_that_did_not_persist_refuses_the_delete(caplog):
    """A delete whose carry tombstone did not land leaves the slot CLAIMABLE.

    The legacy document outlives the delete -- only the age-based sweep removes it, and
    it may never run -- so an absent or still-pending marker lets the next session on
    the recycled slot key carry the DELETED conversation's goal into its own record.

    ``False`` cannot carry this: it already means "already committed by another call",
    which is a slot that IS tombstoned. Refusing the delete is the recoverable side of
    the trade, exactly as it is for an exclusion that cannot be read back.
    """
    _legacy_document(SLOT, goal="deleted conversation")
    real = sl._rewrite_lines

    def _lose_the_marker(path, lines):
        if path.name == sl._CARRIED_FILE:
            return  # a short write on a full filesystem: no error, nothing on disk
        real(path, lines)

    sl._rewrite_lines = _lose_the_marker  # type: ignore[assignment]
    try:
        # NO units: the delete still has to settle the marker, because the document it
        # tombstones belongs to the slot rather than to any one unit.
        with caplog.at_level("ERROR"), pytest.raises(sl.LedgerExclusionError):
            sl.exclude_units(SLOT, ())
    finally:
        sl._rewrite_lines = real  # type: ignore[assignment]

    assert not sl._carry_committed(sl.control_dir(SLOT) / sl._CARRIED_FILE)


def test_the_carry_path_tolerates_a_marker_it_could_not_commit(caplog):
    """The same failure must NOT refuse an update whose carry already landed.

    Here the append is proved on disk, so the folded record is non-empty and the
    take-over rule refuses a second carry on its own evidence; the claim left
    ``pending`` goes stale and is reclaimed. Refusing would fail a carry that worked.
    """
    _legacy_document(SLOT, goal="carried anyway")
    _unit()
    real = sl._rewrite_lines

    def _lose_the_marker(path, lines):
        if path.name == sl._CARRIED_FILE:
            return
        real(path, lines)

    sl._rewrite_lines = _lose_the_marker  # type: ignore[assignment]
    try:
        with caplog.at_level("WARNING"):
            sl.record(SLOT, session_id=SESSION, next_step="n")
    finally:
        sl._rewrite_lines = real  # type: ignore[assignment]

    assert sl.read_state(SLOT)["goal"] == "carried anyway"
    assert any("could not commit the marker" in r.message for r in caplog.records)

    # And the uncommitted marker cannot cause a SECOND carry: the record holds content,
    # which is the evidence the take-over rule actually rests on.
    sl._fold_cache.clear()
    sl.record(SLOT, session_id=SESSION, next_step="n2")
    assert sl.read_state(SLOT)["goal"] == "carried anyway"


# --------------------------------------------------------------------------- #
# the carry's READ half: the record between the upgrade and the first record
# --------------------------------------------------------------------------- #


def test_a_read_before_any_record_still_sees_the_document_it_will_carry():
    """The FIRST read after an upgrade is the one a resumed loop takes.

    It happens before the loop records anything, so a carry that only runs from a
    write leaves that turn with no goal, no phase and no next step -- and the later
    carry cannot undo a turn already taken. A slot that ran before the crew log
    existed has no unit at all, which is why an absent unit must not answer early.
    """
    _legacy_document(
        SLOT,
        goal="finish the migration",
        phase="implementing",
        next="carry the document forward",
        artifacts={"pr": "12027"},
    )
    assert sl.crew_log_units(SLOT, "") == (), "the upgrade case has no unit yet"

    state = sl.read_state(SLOT)
    assert state["goal"] == "finish the migration"
    assert state["phase"] == "implementing"
    assert state["next"] == "carry the document forward"
    assert state["artifacts"] == {"pr": "12027"}


def test_the_preview_is_the_record_the_carry_goes_on_to_append():
    """One construction, so the record does not change shape when the carry lands.

    The two answers are never compared in production -- the same loop sees them
    minutes apart -- so a second spelling of the payload would drift invisibly.
    """
    _legacy_document(SLOT, goal="g", phase="implementing", next="n", artifacts={"pr": "1"})
    _unit()
    previewed = sl.read_state(SLOT)

    sl.record(SLOT, session_id=SESSION, event="e", event_kind="note")
    carried = sl.read_state(SLOT)
    for field in ("goal", "phase", "next", "artifacts"):
        assert previewed[field] == carried[field], field


def test_the_preview_folds_onto_the_log_rather_than_replacing_it():
    """A slot that recorded a reason and no state keeps it.

    The trigger is an empty CONTENT record, and events are not content, so a preview
    that substituted its answer would drop a logged event the fold already had.
    """
    _unit()
    sl.record(SLOT, session_id=SESSION, event="logged before the upgrade", event_kind="note")
    _legacy_document(SLOT, goal="from the document")
    sl._fold_cache.clear()

    state = sl.read_state(SLOT)
    assert state["goal"] == "from the document"
    assert any("logged before the upgrade" in e.get("text", "") for e in state["events"])


def test_a_tombstoned_document_is_not_previewed_on_a_recycled_slot_key():
    """The read is bound by the same marker the carry is, or it undoes a delete.

    A permanent delete preserves the legacy store and tombstones the carry, so the
    document is on disk with nothing left that may consume it. A read that consulted
    it anyway would hand a successor on the recycled key the deleted conversation's
    goal -- exactly the resurrection the tombstone exists to prevent, arriving through
    the read instead of through the carry.
    """
    _legacy_document(SLOT, goal="deleted conversation", phase="implementing")
    assert sl.exclude_units(SLOT, ()).carry_tombstoned is True
    assert sl.ledger_dir(SLOT).exists(), "the funnel preserves the legacy store"
    sl._fold_cache.clear()

    state = sl.read_state(SLOT)
    assert state["goal"] == ""
    assert state["phase"] == ""


def test_an_unreadable_document_answers_a_read_empty_instead_of_raising():
    """A nudge cycle asks for this record on its way into a turn.

    Raising there stops the loop rather than telling anyone anything, so the read
    keeps its best-effort contract even though the same failure correctly REFUSES a
    write: the write can still carry later, the read has nothing to offer.
    """
    _legacy_document(SLOT, goal="unreadable right now")
    _unit()
    real = Path.stat

    def _boom(self, *a, **kw):
        if self.name == sl._STATE_FILE:
            raise OSError("transient")
        return real(self, *a, **kw)

    Path.stat = _boom  # type: ignore[assignment]
    try:
        state = sl.read_state(SLOT)
    finally:
        Path.stat = real  # type: ignore[assignment]
    assert state["goal"] == ""


def test_a_refused_delete_takes_back_the_exclusions_it_already_wrote(caplog):
    """A refusal has to leave the slot as it found it.

    The exclusion commits inside the hold and the tombstone is settled after it, so a
    tombstone that cannot be written refuses a delete whose exclusions HAVE landed --
    on a session that still exists, because nothing was unlinked. Leaving them would
    make a live conversation's record read empty with only a hand repair, and the
    caller answers 409 on the strength of "a refusal wrote nothing".
    """
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="a live conversation")
    _legacy_document(SLOT, goal="and an un-carried document")
    real = sl._rewrite_lines

    def _lose_the_marker(path, lines):
        if path.name == sl._CARRIED_FILE:
            return  # a short write on a full filesystem: no error, nothing on disk
        real(path, lines)

    sl._rewrite_lines = _lose_the_marker  # type: ignore[assignment]
    try:
        with caplog.at_level("ERROR"), pytest.raises(sl.LedgerExclusionError):
            sl.exclude_units(SLOT, (SESSION,))
    finally:
        sl._rewrite_lines = real  # type: ignore[assignment]

    assert sl._excluded_units(SLOT) == frozenset()
    sl._fold_cache.clear()
    assert sl.read_state(SLOT)["goal"] == "a live conversation"


def test_a_concurrent_deletes_exclusions_survive_a_refusal_rollback(caplog):
    """The rollback takes back only what THIS call added.

    A concurrent delete of the same slot proceeded on its own ids, and dropping those
    would leave its units foldable by the next session on the recycled slot key.
    """
    _unit(SESSION)
    _unit(LATER_SESSION)
    # The other delete's id, written straight into the file: going through
    # `exclude_units` would settle the carry marker too, and then this call's
    # `_finish_carry` answers "already committed" instead of failing.
    sl._rewrite_lines(sl._control_file(SLOT, sl._DELETED_UNITS_FILE, create=True), (SESSION,))
    _legacy_document(SLOT, goal="un-carried")
    real = sl._rewrite_lines

    def _lose_the_marker(path, lines):
        if path.name == sl._CARRIED_FILE:
            return
        real(path, lines)

    sl._rewrite_lines = _lose_the_marker  # type: ignore[assignment]
    try:
        with caplog.at_level("ERROR"), pytest.raises(sl.LedgerExclusionError):
            sl.exclude_units(SLOT, (LATER_SESSION,))
    finally:
        sl._rewrite_lines = real  # type: ignore[assignment]

    assert sl._excluded_units(SLOT) == frozenset({SESSION})


def test_a_late_first_record_from_a_retired_unit_does_not_freeze_the_order():
    """The order log orders units by their NEWEST record, not their first.

    A request from a session the slot has replaced can reach the recorder after the
    successor has already recorded. Ordering by FIRST record would put that retired
    unit last and leave it there for good, because a presence check never revisits a
    known id -- so its phase and next step would win every later fold. Moving the
    recording unit to the end makes the successor's next record correct it, and while
    the retired unit is last its entry genuinely IS the newest one recorded.
    """
    _unit(SESSION)  # the retired unit, which never recorded during its own life
    _unit(LATER_SESSION)
    sl.record(SLOT, session_id=LATER_SESSION, goal="the live conversation")

    # The delayed request from the retired session finally records.
    sl.record(SLOT, session_id=SESSION, goal="from the retired session")
    assert sl._recorded_unit_order(SLOT) == (LATER_SESSION, SESSION)

    # The live session's NEXT record puts it back at the end, and its state wins again.
    sl.record(SLOT, session_id=LATER_SESSION, next_step="still live")
    assert sl._recorded_unit_order(SLOT) == (SESSION, LATER_SESSION)
    sl._fold_cache.clear()
    # No live session id, so the recorded order is all the fold has to go on.
    assert sl.read_state(SLOT)["goal"] == "the live conversation"


def test_an_append_that_did_not_land_does_not_publish_the_units_precedence():
    """A refused or still-queued append must not pin its unit ahead of the successor.

    The order file's whole claim is that among units which have recorded, the one
    recording right now holds the newest entry. A unit whose append was refused, or is
    still queued, has NOT recorded, so publishing its precedence there asserts
    something untrue -- and the rewrite is fsynced immediately, so no crash is needed
    for the wrong order to survive. A successor resuming with no ledger content of its
    own would then fold the retired unit's stale goal and phase as current and act on
    it, which is the inversion the ordering machinery exists to prevent.

    The fault is injected on the OBSERVATION rather than on the append, which makes
    this strictly harder than the real case: the retired entry really is in its log
    here, so the fold has it available and still must not let it win.
    """
    _unit(SESSION)  # the unit the slot has since replaced
    _unit(LATER_SESSION)
    sl.record(SLOT, session_id=LATER_SESSION, goal="the live conversation")
    assert sl._recorded_unit_order(SLOT) == (LATER_SESSION,)

    # The delayed request from the retired session records, but its append is not
    # observed to land: this unit's own newest seq does not move.
    real = sl._unit_last_seq
    sl._unit_last_seq = lambda unit_id: 0  # type: ignore[assignment]
    try:
        sl.record(SLOT, session_id=SESSION, goal="from the retired session")
    finally:
        sl._unit_last_seq = real  # type: ignore[assignment]

    # Precedence was NOT published, so the successor keeps it.
    assert sl._recorded_unit_order(SLOT) == (LATER_SESSION,)
    sl._fold_cache.clear()
    assert sl.read_state(SLOT)["goal"] == "the live conversation"


def test_a_unit_recording_repeatedly_is_not_rewritten_into_the_log_twice():
    """The ordinary path stays a bare read: already last means nothing to write.

    Two properties, and dedup on READ hides both. A move that degraded into an append
    would put one unit in the file twice, and the fold would consume its entries twice
    the moment a reader without the dedup came along. And a record that rewrote the
    file every time would make the common case -- one live unit recording repeatedly --
    pay a file replace per update for an order that did not change.
    """
    _unit(SESSION)
    real = sl._rewrite_lines
    rewrites = []

    def _counted(path, lines):
        rewrites.append(path.name)
        real(path, lines)

    sl._rewrite_lines = _counted  # type: ignore[assignment]
    try:
        for n in range(4):
            sl.record(SLOT, session_id=SESSION, next_step=f"step {n}")
    finally:
        sl._rewrite_lines = real  # type: ignore[assignment]

    assert sl._recorded_unit_order(SLOT) == (SESSION,)
    raw = (sl.control_dir(SLOT) / sl._UNIT_ORDER_FILE).read_text(encoding="utf-8")
    assert raw.split() == [SESSION], raw
    assert sl._UNIT_ORDER_FILE not in rewrites, rewrites


def test_the_tombstone_is_settled_inside_the_exclusion_hold():
    """One transaction, or the take-back can withdraw another delete's evidence.

    Settling the marker after the hold is released publishes the exclusion first. A
    concurrent delete of the same slot then reads those ids as already present, adds
    none of its own, settles the marker, and UNLINKS its transcript -- on the strength
    of an exclusion this call is about to take back. Holding the lock across both is
    what makes there be no intermediate state to rely on.
    """
    from kiro_crew.platform_compat import try_acquire_lock

    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="g")
    real = sl._settle_carry_locked
    lock_was_held = []

    def _probe(path, *, landed):
        fd = os.open(sl.control_dir(SLOT) / sl._LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # A second flock on a new description fails while the hold is live, in this
            # process as much as any other -- the lock belongs to the description.
            lock_was_held.append(not try_acquire_lock(fd, exclusive=True))
        finally:
            os.close(fd)
        return real(path, landed=landed)

    sl._settle_carry_locked = _probe  # type: ignore[assignment]
    try:
        sl.exclude_units(SLOT, (SESSION,))
    finally:
        sl._settle_carry_locked = real  # type: ignore[assignment]

    assert lock_was_held == [True]


def test_a_refused_delete_cannot_withdraw_a_concurrent_deletes_exclusion():
    """The interleaving the transaction exists to make impossible.

    A's exclusion lands and its tombstone fails; B deletes the same slot's overlapping
    units and proceeds. If A's take-back ran outside the hold, B would have reused A's
    ids, added none of its own, and unlinked -- and A would then remove the only record
    keeping B's deleted state out of the recycled slot key. Inside the hold, B cannot
    read the file until A has already put it back, so B adds the id itself.
    """
    import threading

    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="deleted conversation")
    _legacy_document(SLOT, goal="un-carried")
    real_settle = sl._settle_carry_locked
    b_started = threading.Event()
    b_result: list[object] = []
    a_thread = threading.current_thread()

    def _fail_only_for_a(path, *, landed):
        # Keyed on the CALLING THREAD, not on call order: either delete may reach the
        # lock first, and faulting whichever arrives first would fault B in the runs
        # where B wins the race -- so the test would measure the interleaving instead
        # of the property. B must always take the real path.
        if threading.current_thread() is a_thread:
            b_started.wait(2.0)
            raise OSError("the carry marker did not persist as committed")
        return real_settle(path, landed=landed)

    def _b():
        b_started.set()
        try:
            b_result.append(sl.exclude_units(SLOT, (SESSION,)))
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
            b_result.append(exc)

    sl._settle_carry_locked = _fail_only_for_a  # type: ignore[assignment]
    worker = threading.Thread(target=_b, daemon=True)
    try:
        worker.start()
        with pytest.raises(sl.LedgerExclusionError):
            sl.exclude_units(SLOT, (SESSION,))
        worker.join(10.0)
    finally:
        sl._settle_carry_locked = real_settle  # type: ignore[assignment]

    assert b_result and isinstance(b_result[0], sl.SlotExclusion), b_result
    # B added the id ITSELF rather than inheriting A's, so its delete rests on its own
    # record and A's take-back could not have removed it.
    assert b_result[0].added == (SESSION,)
    assert sl._excluded_units(SLOT) == frozenset({SESSION})


def test_the_tombstone_withdrawal_runs_inside_the_rollback_hold():
    """ONE acquisition, or a rollback withdraws a concurrent delete's tombstone.

    A marker says `committed` and never says WHOSE, so a withdrawal cannot tell the
    tombstone it is undoing from one a concurrent delete committed in the gap between
    two holds. Removing that one carries the deleted conversation's legacy state into a
    recycled slot, silently, with nothing left to put it back.

    Counted rather than probed for "is a lock held right now": a withdrawal in its own
    second hold holds the lock too, so that question cannot tell the two structures
    apart. The number of acquisitions can, and it is what "one transaction" means.
    """
    _legacy_document(SLOT, goal="owed to the spared session")
    recorded = sl.exclude_units(SLOT, ())
    assert recorded.carry_tombstoned is True
    real_locked = sl._locked
    acquires: list[object] = []

    def _counting(dir_path, *, create=True):
        acquires.append(dir_path)
        return real_locked(dir_path, create=create)

    sl._locked = _counting  # type: ignore[assignment]
    try:
        sl.unexclude_units(SLOT, (), restore_carry=True)
    finally:
        sl._locked = real_locked  # type: ignore[assignment]

    assert len(acquires) == 1, acquires
    assert not sl._carry_committed(sl.control_dir(SLOT) / sl._CARRIED_FILE)


def test_a_withdrawal_that_did_not_persist_is_raised_not_swallowed(caplog):
    """The marker standing means the spared session's state is never carried.

    Only this path ever removes a committed marker, so swallowing the failure makes
    that loss permanent and silent. Raised instead, which the caller answers as
    retryable -- and a retry can actually succeed.
    """
    _legacy_document(SLOT, goal="owed to the spared session")
    assert sl.exclude_units(SLOT, ()).carry_tombstoned is True
    real = Path.unlink

    def _refuse(self, *a, **kw):
        if self.name == sl._CARRIED_FILE:
            return  # a removal the filesystem reports as done and did not do
        return real(self, *a, **kw)

    Path.unlink = _refuse  # type: ignore[assignment]
    try:
        with caplog.at_level("ERROR"), pytest.raises(sl.LedgerExclusionError):
            sl.unexclude_units(SLOT, (), restore_carry=True)
    finally:
        Path.unlink = real  # type: ignore[assignment]

    # The tombstone STANDS, which is the safe direction: the deleted document is not
    # carried anywhere, and the caller was told the rollback did not complete.
    assert sl._carry_committed(sl.control_dir(SLOT) / sl._CARRIED_FILE)


def test_an_update_too_large_for_one_entry_is_refused_before_the_append():
    """A refused append is permanent, so the update must not be reported as taken.

    The writer counts an over-ceiling entry and drops it, which reaches the caller as
    ``durable=False`` -- the very same value a writer that is merely still busy gives,
    and that update IS recorded. So a caller acting on ``durable`` alone cannot tell a
    record that will land from one that can never land, and the one that can never land
    would be reported recorded and then be absent from the slot at the next read.
    """
    _unit()
    # The CLAMPS' own worst case, not an arbitrary big value: each key and value is
    # exactly what ``record_update`` retains, so this is the largest entry the public
    # surface can be asked to write. 32 * (128 + 2000) is over the 64 KiB ceiling, so
    # the ceiling is reachable through clamped input alone.
    oversized = {
        ("k%03d" % i).ljust(sl._MAX_ARTIFACT_KEY, "x"): "v" * sl._MAX_TEXT
        for i in range(sl._MAX_ARTIFACTS)
    }
    assert not crew_log_emit.ledger_entry_fits({"slot": SLOT, "artifacts": oversized})

    with pytest.raises(sl.LedgerEntryTooLarge):
        sl.record_update(
            SLOT,
            session_id=SESSION,
            artifacts=oversized,
            event="too big",
            event_kind="progress",
        )

    # Refused BEFORE the append, so the log holds no entry for it and the record is
    # unchanged -- the refusal is not an entry that half happened.
    assert [e for e in _entries() if e.type == sl.LEDGER_ENTRY_TYPE] == []
    assert sl.read_state(SLOT)["artifacts"] == {}

    # And an ordinary update on the same slot still lands, so the refusal is scoped to
    # the entry that does not fit rather than to the slot.
    sl.record(SLOT, session_id=SESSION, phase="implementing", event="ok", event_kind="phase")
    assert sl.read_state(SLOT)["phase"] == "implementing"


def test_every_control_file_bound_is_derived_from_the_unit_id_bound():
    """The per-id bound is the only number; the file bounds follow from it.

    A hardcoded file bound is a bound on the file that says nothing about the ids in
    it: it holds only while every id happens to be far under the per-id bound, and the
    two drift apart with nothing to catch it. Derived, a full window of maximum-size
    ids fits the read by construction, which is what stops the order read coming back
    SHORT -- it does not reject an oversized file, and its caller rewrites what it read.
    """
    assert sl._MAX_ORDER_BYTES == sl._MAX_ORDERED_UNITS * (sl._MAX_UNIT_ID_BYTES + 1)
    assert sl._MAX_EXCLUDED_BYTES == sl._MAX_EXCLUDED_UNITS * (sl._MAX_UNIT_ID_BYTES + 1)
    # A whole compaction window of maximum-size ids is READ WHOLE, with a compaction's
    # worth of slack for a file caught between the threshold and the rewrite.
    assert sl._MAX_ORDERED_UNITS * (sl._MAX_UNIT_ID_BYTES + 1) <= sl._MAX_ORDER_READ_BYTES
    assert sl._MAX_ORDER_READ_BYTES >= 2 * sl._MAX_ORDER_BYTES


def test_a_unit_id_over_the_bound_is_refused_at_every_site_that_retains_it(caplog):
    """Enforced at the boundary, not assumed of the input.

    Unchecked, an id past the bound makes a control file outgrow the read sized for it.
    The order read then comes back short -- it truncates rather than rejecting -- and
    its caller rewrites what it read, so the NEWEST ids are lost permanently and older
    units fold afterward over the current record.
    """
    huge = "u" * (sl._MAX_UNIT_ID_BYTES + 1)

    # The exclusion refuses, which refuses the delete: the same direction that file's
    # count bound already fails in, and never a silently shortened exclusion set.
    with pytest.raises(sl.LedgerExclusionError):
        sl.exclude_units(SLOT, (huge,))
    assert sl._excluded_units(SLOT) == frozenset()

    # The order log declines the line and says so, which falls back to header order --
    # the same state a crash between the entry and the line leaves.
    _unit(huge)
    with caplog.at_level("WARNING"):
        sl._note_unit_order(SLOT, huge)
    assert huge not in sl._recorded_unit_order(SLOT)

    # An id INSIDE the bound is retained at both sites, so the check is a bound and not
    # a blanket refusal.
    _unit()
    assert sl.exclude_units(SLOT, (SESSION,)).added == (SESSION,)
    sl._note_unit_order(SLOT, SESSION)
    assert SESSION in sl._recorded_unit_order(SLOT)
