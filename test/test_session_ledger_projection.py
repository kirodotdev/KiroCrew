"""The session ledger as a PROJECTION of the session's crew log.

One test per property the change has to keep true. The load-bearing ones are
:func:`test_a_slot_folds_across_every_unit_it_ran_under` -- a slot owns one ACP
session id at a time, so a record that did not join those units would answer with
whichever part of the workstream happened to land in the newest session -- and
:func:`test_the_injected_snapshot_is_byte_identical_to_the_stored_document_s`,
which pins that the block a nudge cycle carries did not change shape when the
record stopped being a file.

Two sibling files hold the rest of the projection's tests.
``test_session_ledger_projection_carry_forward.py`` covers the upgrade carry-forward and
the slot control files around it: the carry claim, the delete exclusions and tombstone,
and the recorded unit order. ``test_session_ledger_projection_durability_and_cache.py``
covers the durability answer an update gives and the slot-map and fold caches a read
goes through. The slot and session constants, the autouse home fixture, ``_unit`` and
``_entries`` that all three files use live in ``session_ledger_projection_helpers.py``.
"""

from __future__ import annotations

import json

import pytest
from session_ledger_projection_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    LATER_SESSION,
    SESSION,
    SLOT,
    _entries,
    _isolated_home,
    _unit,
)

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger as sl
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store

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
    monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
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


def test_a_subagent_entry_does_not_stop_the_fold(caplog):
    """A log that recorded a dispatched child still folds.

    The regression this pins: every entry the emitter writes reaches the fold through
    ``known=KNOWN_TYPES``, which refuses a type it does not know and that is not
    marked ignorable. ``subagent/spawned`` was written non-ignorable and never
    declared, so one dispatched child made this slot's record unreadable FOREVER --
    the fold stopped at that entry and ``read_state`` answered with the empty record
    for the rest of the session's life, while the log itself was perfectly intact.

    Ordered so the READ is what fails: the ledger entry lands first and the child
    entry after it, so the fold has to walk past the child to finish, and a refusal
    there loses an entry it had already read. That is the shape the failure takes in
    the field. The same refusal also makes ``record`` raise, since it folds to
    compute the state it returns; this asserts the quieter half, because a read that
    answers empty reports nothing to the caller.

    Driven through ``read_state`` rather than over ``iter_from`` directly: the refusal
    is caught there and converted to the empty record, so a test that called the
    reader itself would see an exception where a real caller sees a blank record.
    """
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="ship the declared types")
    crew_log_emit.on_subagent_spawned(
        SESSION,
        3,
        agent_id="sub-9",
        agent="kirocrew-worker",
        model="claude",
        scope={"memory": True, "lessons": True, "project": False},
    )
    crew_log_emit.drain_for_shutdown(timeout=5.0)
    # Controls: both entries are on disk and the child is AFTER the ledger entry, so
    # the fold cannot reach the goal without passing it. Without this the test could
    # pass on a log that never held a child at all.
    types = [entry.type for entry in _entries()]
    assert sl.LEDGER_ENTRY_TYPE in types and "subagent/spawned" in types
    assert types.index("subagent/spawned") > types.index(sl.LEDGER_ENTRY_TYPE)

    crew_log.forget_slot_folds()
    with caplog.at_level("WARNING", logger="kiro_crew.session_ledger"):
        state = sl.read_state(SLOT)
    assert state.get("goal") == "ship the declared types"
    assert "folding this slot's crew logs failed" not in caplog.text


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
