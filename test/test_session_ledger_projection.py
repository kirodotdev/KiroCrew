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
    assert sl.read_state(SLOT) == returned


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


def test_a_carry_that_does_not_land_releases_its_claim():
    """A refused carry must leave the claim open, or its state is lost for good."""
    _legacy_document(SLOT, goal="owed")
    _unit()
    from kiro_crew.crew_log import emit as crew_log_emit

    real = crew_log_emit.flush
    crew_log_emit.flush = lambda timeout=0.0: False  # type: ignore[assignment]
    try:
        with pytest.raises(sl.LedgerUnavailable):
            sl.record(SLOT, session_id=SESSION, next_step="n")
    finally:
        crew_log_emit.flush = real  # type: ignore[assignment]
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
    added = sl.exclude_units(SLOT, ("some-unit",))
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
    assert first == ("shared-unit",)
    second = sl.exclude_units(SLOT, ("shared-unit", "its-own-unit"))
    assert second == ("its-own-unit",)

    sl.unexclude_units(SLOT, second)  # the second delete did not proceed
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
