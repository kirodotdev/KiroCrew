"""A crew's progress lines read back newest first, and a crew-level sweep names no issue.

This file pins how the ledger's lines read back: newest first, filtered by crew or by
phase entry, with ``phase`` stamped only on a line that moves the item; the crew-level
``sweep`` line, which carries no issue number and coalesces across an idle stretch, a
pause included; a numbered line's content-addressed id; and the phase classifications
and vocabularies an update is checked against. The update entry itself lives in
``test_issue_radar_crew_store.py`` and the pass a line can carry in
``test_issue_radar_crew_store_skip_index.py``.
"""

import hashlib
import inspect

import pytest
from issue_radar_crew_store_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    OWNER,
    REPO,
    _entries,
    _isolated_crew_log,
    _live_crew,
    _record,
    _sweep,
    _tick,
)

from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs

# ── the ledger: progress lines ──────────────────────────────────────────────


def test_events_read_newest_first_and_filter_by_crew(tmp_path):
    a, sid_a = _live_crew(tmp_path, name="Andromeda")
    b, sid_b = _live_crew(tmp_path, name="Whirlpool")
    _record(tmp_path, a["id"], sid_a, 1, {}, "claim", "claimed")
    _tick()
    _record(tmp_path, b["id"], sid_b, 2, {}, "ci", "CI round 3")
    all_events = cs.read_events(OWNER, REPO, tmp_path)
    assert [e["kind"] for e in all_events] == ["ci", "claim"]
    mine = cs.read_events(OWNER, REPO, tmp_path, crew_id=a["id"])
    assert [e["kind"] for e in mine] == ["claim"]


def test_require_phase_keeps_only_the_entries_into_a_phase(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "awaiting-ci"}, "ci", "round 1")
    _record(tmp_path, cid, sid, 7, {"ci_state": {"round": 2}}, "ci", "round 2")
    lines = cs.read_events(OWNER, REPO, tmp_path, crew_id=cid, require_phase=True)
    assert [(e["text"], e["phase"]) for e in lines] == [("round 1", "awaiting-ci")]
    assert cs.read_events(OWNER, REPO, tmp_path, crew_id=cid, limit=1)[0]["text"] == "round 2"


def test_unknown_event_kind_is_refused(tmp_path):
    crew, sid = _live_crew(tmp_path)
    with pytest.raises(cs.CrewStoreError, match="unknown event kind"):
        _record(tmp_path, crew["id"], sid, 1, {}, "vibes", "...")
    assert _entries(sid) == []


# ── crew-level lines (a step that belongs to no issue) ──────────────────────
#
# The invariant these protect is the one the feature exists for: a crew that
# checked the queue and took nothing must be able to SAY so. Before this, the
# only way to record the cycle was to attribute it to an issue the crew never
# acted on, so the ledger either lied or stayed silent. Both directions of the
# pairing are asserted, because a crew-level kind carrying a number is the same
# false attribution written the other way round.


def test_a_crew_level_line_omits_the_number_entirely(tmp_path):
    crew, sid = _live_crew(tmp_path)
    result = _sweep(tmp_path, crew["id"], sid)
    entry = result["event"]
    # Absent, NOT zero: a `0` is indistinguishable from a real issue number in
    # every filter and join that keys on this field.
    assert "number" not in entry
    stored = cs.read_events(OWNER, REPO, tmp_path)
    assert len(stored) == 1
    assert "number" not in stored[0]
    assert (stored[0]["kind"], stored[0]["text"]) == ("sweep", "queue empty")
    # And the appended entry itself carries no number, so no reader can invent one.
    (appended,) = _entries(sid)
    assert "number" not in appended.data


def test_a_crew_level_kind_with_a_number_is_refused(tmp_path):
    crew, sid = _live_crew(tmp_path)
    with pytest.raises(cs.CrewStoreError, match="takes no issue number"):
        _record(tmp_path, crew["id"], sid, 7, {}, "sweep", "queue empty")
    with pytest.raises(cs.CrewStoreError, match="takes no issue number"):
        _record(tmp_path, crew["id"], sid, 1, {}, cs.CREW_LEVEL_EVENT_KIND, "x")
    assert _entries(sid) == []


def test_a_numbered_line_keeps_its_historical_id(tmp_path):
    """The id formula for a NUMBERED line must not have changed.

    It is content-addressed and drives merge-on-read dedupe, so a new formula would
    give every existing line a fresh id and silently defeat the dedupe for the whole
    ledger. Asserted against the literal formula rather than against a golden string
    so the test does not need a frozen clock.
    """
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    entry = _record(tmp_path, cid, sid, 4, {}, "claim", "claimed")["event"]

    def _formula(ts, crew_id, number, kind, text):
        shown = "" if number is None else int(number)
        return hashlib.sha256(f"{ts}|{crew_id}|{shown}|{kind}|{text}".encode()).hexdigest()[:16]

    assert entry["id"] == _formula(entry["ts"], cid, 4, "claim", "claimed")
    # And the two families cannot collide: the numberless variant renders the
    # number as the empty string, which no real number produces.
    sweep = _sweep(tmp_path, cid, sid, "x")["event"]
    assert sweep["id"] == _formula(sweep["ts"], cid, None, "sweep", "x")
    assert _formula(sweep["ts"], cid, None, "sweep", "x") != _formula(
        sweep["ts"], cid, 0, "sweep", "x"
    )


def test_record_crew_checkpoint_writes_one_line_and_no_item(tmp_path):
    crew, sid = _live_crew(tmp_path)
    result = _sweep(tmp_path, crew["id"], sid, "checked 42 open issues, took none")
    # Same envelope as commit_work_progress, so the write route answers one shape.
    assert {"item", "event", "skip"} <= set(result)
    assert result["item"] is None and result["skip"] is None
    assert result["event"]["kind"] == "sweep"
    assert result["coalesced"] is False
    assert set(result["event"]) == {"id", "ts", "crew_id", "kind", "text"}
    # No work item was created, so nothing consumes a work slot.
    assert cs.list_work_items(OWNER, REPO, crew["id"], tmp_path) == []
    assert len(_entries(sid)) == 1


def test_consecutive_sweeps_coalesce_instead_of_running_away(tmp_path):
    """An idle crew must not bury its own work log.

    A sweep is a recurring latest-value fact and the crew is nudged on a timer, so
    one line per cycle would be an unbounded run. Every ledger read is capped and
    drops the OLDEST line first, so a few hundred idle cycles would push the real
    history out of the log the feature exists to make honest.
    """
    crew, sid = _live_crew(tmp_path)
    first = _sweep(tmp_path, crew["id"], sid, "queue empty")
    _tick()
    again = _sweep(tmp_path, crew["id"], sid, "still empty")

    assert first["coalesced"] is False
    assert again["coalesced"] is True
    # The SECOND call is answered with the FIRST line, so the timestamp marks when
    # the idle stretch began rather than when it was last observed.
    assert again["event"]["id"] == first["event"]["id"]
    assert again["event"]["text"] == "queue empty"
    assert len(cs.read_events(OWNER, REPO, tmp_path)) == 1
    # Coalesced BEFORE the append: the second sweep wrote nothing at all.
    assert len(_entries(sid)) == 1


def test_a_sweep_after_real_work_is_written(tmp_path):
    # Coalescing keys on the crew's newest line, so the transition back into idle
    # is recorded -- otherwise the guard would swallow every sweep after the first.
    crew, sid = _live_crew(tmp_path)
    _sweep(tmp_path, crew["id"], sid, "queue empty")
    _tick()
    _record(tmp_path, crew["id"], sid, 7, {}, "claim", "took #7")
    _tick()
    third = _sweep(tmp_path, crew["id"], sid, "empty again")

    assert third["coalesced"] is False
    assert [e["kind"] for e in cs.read_events(OWNER, REPO, tmp_path)] == ["sweep", "claim", "sweep"]


def test_a_pause_does_not_break_the_fold_because_liveness_is_not_evidenced(tmp_path):
    """Coalescing is deliberately blind to pause state.

    An earlier revision stamped ``paused_at`` and refused to fold across it, to stop
    the page claiming an unbroken "checking since" run over a stop. That claim is
    gone: the row now renders as a past instant, so the fold has nothing to protect,
    and the field was removed rather than kept to cover one of the three stop modes
    while a crash and a lost timer stayed uncovered. This pins the simpler rule so
    it is not re-complicated without a reason.
    """
    crew, sid = _live_crew(tmp_path)
    first = _sweep(tmp_path, crew["id"], sid, "queue empty")
    cs.set_crew_paused(OWNER, REPO, crew["id"], True, "operator stopped it", tmp_path)
    cs.set_crew_paused(OWNER, REPO, crew["id"], False, "", tmp_path)
    _tick()
    after = _sweep(tmp_path, crew["id"], sid, "empty again")

    assert first["coalesced"] is False
    assert after["coalesced"] is True
    assert after["event"]["id"] == first["event"]["id"]
    assert len(cs.read_events(OWNER, REPO, tmp_path)) == 1
    # The record carries no pause timestamp: the field is gone, not merely unused.
    assert "paused_at" not in cs.read_crew(OWNER, REPO, crew["id"], tmp_path)


def test_one_crews_sweep_does_not_coalesce_anothers(tmp_path):
    # Each crew folds its own log, so one crew's idle line cannot swallow another's.
    a, sid_a = _live_crew(tmp_path, name="Andromeda")
    b, sid_b = _live_crew(tmp_path, name="Whirlpool")
    _sweep(tmp_path, a["id"], sid_a, "a is empty")
    theirs = _sweep(tmp_path, b["id"], sid_b, "b is empty")

    assert theirs["coalesced"] is False
    assert len(cs.read_events(OWNER, REPO, tmp_path)) == 2


def test_the_checkpoint_writer_takes_no_kind_so_a_wrong_one_is_unrepresentable(tmp_path):
    """The refusal is structural: the constant is written directly and a caller has
    nothing to get wrong. The number/kind pairing is still enforced, in
    ``commit_work_progress`` and again by the fold, which the tests above cover."""
    crew, sid = _live_crew(tmp_path)
    assert "event_kind" not in inspect.signature(cs.record_crew_checkpoint).parameters
    written = _sweep(tmp_path, crew["id"], sid)
    assert written["event"]["kind"] == cs.CREW_LEVEL_EVENT_KIND


# ── phase classification ────────────────────────────────────────────────────


def test_the_two_phase_classifications_do_not_coincide(tmp_path):
    """This is the point of keeping two separate sets rather than a flag."""
    # awaiting-ci: occupies a slot, is NOT ttl-active, is NOT editing.
    assert "awaiting-ci" not in cs.TTL_ACTIVE_PHASES
    assert "awaiting-ci" not in cs.EDITING_PHASES
    assert "awaiting-ci" not in cs.TERMINAL_PHASES
    # addressing-review: editing, but NOT ttl-active -- an open pull request stands
    # in for a heartbeat, so the claim does not age against it.
    assert "addressing-review" in cs.EDITING_PHASES
    assert "addressing-review" not in cs.TTL_ACTIVE_PHASES
    # implementing: both ttl-active and editing, and slot-occupying.
    assert "implementing" in cs.TTL_ACTIVE_PHASES
    assert "implementing" in cs.EDITING_PHASES


def test_the_vocabularies_are_the_ones_the_entry_type_declares(tmp_path):
    """The store re-exports the crew log's constants rather than owning a copy:
    the entry type's closed enums and the writer's refusals must be ONE set."""
    from kiro_crew.crew_log import entry_types as et

    assert cs.PHASES is et.RADAR_PHASES
    assert cs.EVENT_KINDS is et.RADAR_EVENT_KINDS
    assert cs.SKIP_SCOPES is et.RADAR_SKIP_SCOPES
    assert cs.CREW_LEVEL_EVENT_KIND == et.RADAR_CREW_LEVEL_EVENT_KIND == "sweep"
    assert cs.LEDGER_ENTRY_TYPE == et.RADAR_ENTRY_TYPE == "radar/recorded"


def test_a_write_that_does_not_move_the_item_leaves_phase_off_the_event_line(tmp_path):
    """`phase` on an event line means an ENTRY into that phase, so a no-move write
    must not carry one.

    The fabric measures an open dwell from the MOST RECENT entry into the current
    phase, which is what a review pass legitimately restarts. Stamping the
    phase on every write makes each CI round -- which lands `ci_state` while the
    item sits still in `awaiting-ci` -- look like a fresh entry, so an item parked
    for hours reads as minutes old and never surfaces as stalled. The item polled
    most often is the one whose stall would be hidden best, which is the exact
    inversion of what the view is for.
    """
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]

    entered = _record(tmp_path, cid, sid, 7301, {"phase": "awaiting-ci"}, "ci", "round 1")
    assert entered["event"].get("phase") == "awaiting-ci", "an entry must be recorded"

    for round_no in (2, 3):
        polled = _record(
            tmp_path,
            cid,
            sid,
            7301,
            {"ci_state": {"state": "pending", "round": round_no}},
            "ci",
            f"round {round_no}",
        )
        assert "phase" not in polled["event"], (
            "a write that did not move the item must not claim a phase entry "
            f"(round {round_no} did)"
        )

    moved = _record(tmp_path, cid, sid, 7301, {"phase": "implementing"}, "implement", "fix")
    assert moved["event"].get("phase") == "implementing", "a real move must be recorded"

    # And a genuine RE-entry still records, so a round-trip keeps restarting the clock.
    back = _record(tmp_path, cid, sid, 7301, {"phase": "awaiting-ci"}, "ci", "round 4")
    assert back["event"].get("phase") == "awaiting-ci", "a re-entry must be recorded"

    phases = [
        ev.get("phase")
        for ev in cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)
        if ev.get("number") == 7301
    ]
    assert [p for p in phases if p] == [
        "awaiting-ci",
        "implementing",
        "awaiting-ci",
    ], f"only entries should carry a phase, got {phases}"
    # The per-item phase history the pipeline view draws lanes from says the same.
    assert [
        row["phase"] for row in cs.read_ledger(OWNER, REPO, cid, tmp_path)["phase_lines"]["7301"]
    ] == [
        "awaiting-ci",
        "implementing",
        "awaiting-ci",
    ]
