"""Unit tests for the append-only session log emitter.

It pins how a crew log opens -- the header and the opening entry, the model asked for and
the one serving, the session that created it -- and how a store that supersedes another
closes the predecessor's interrupted tail and walks the whole predecessor chain. It also
pins which earlier store a new one names as its predecessor and where that id comes from:
every slot allocation site latches it first, the turn path reads the session map without
pruning it, and a cleared sid, a stale prune and a chain of three stores still name the
right one. That every failure report names a literal operation is pinned here too. Turn
entries live in `test_crew_log_emit_entries.py`; redaction, the flag and the boot path in
`test_crew_log_emit_redaction_and_flag.py`; cache pressure, resume, claims and the
`previous` edge a new store writes in `test_crew_log_emit_continuity.py`; and the writer in
`test_crew_log_emit_write_path.py`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import unittest.mock
from pathlib import Path

import pytest
from crew_log_emit_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    SESSION,
    SUCCESSOR,
    _assert_drained_for_shutdown,
    _body,
    _entries,
    _isolated_home,
    _log_path,
    _open_session,
    _open_successor,
    _remove_as_retention_does,
)

from kiro_crew import crew_log as lg
from kiro_crew import session_map
from kiro_crew.crew_log import emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.crew_log.writer import DEFAULT_LIMITS, WriterLimits

# --- creation and header ---------------------------------------------------


def test_first_open_creates_the_log_file():
    assert not _log_path().exists()
    _open_session()
    assert _log_path().is_file()


def test_the_header_is_line_one_and_carries_owner_agent_slot_cwd():
    _open_session()
    header = _entries()[0]
    assert header["type"] == "session"
    assert header["id"] == SESSION
    assert header["owner"] == "default"
    assert header["agent"] == "kirocrew"
    assert header["slot"] == "chat-7"
    assert header["cwd"] == "/home/dev/project"
    assert isinstance(header["createdAt"], int)


def test_opened_entry_echoes_the_header_and_adds_the_model():
    _open_session()
    opened = [e for e in _body() if e["type"] == "session/opened"]
    assert len(opened) == 1
    entry = opened[0]
    assert entry["src"] == "gateway"
    assert isinstance(entry["time"], int)
    data = entry["data"]
    assert data["agent"] == "kirocrew"
    assert data["slot"] == "chat-7"
    assert data["cwd"] == "/home/dev/project"
    assert data["owner"] == "default"
    assert data["resumed"] is False
    # The storage header has no model field, so the entry carries it.
    assert data["model"] == "claude-opus-5"


def test_an_agentless_call_site_still_produces_a_valid_header():
    emit.on_session_opened(SESSION, slot="chat-1")
    assert _entries()[0]["agent"] == "kirocrew"


# --- the model: what serves, and what was asked for ------------------------


@pytest.mark.parametrize(
    ("agent", "slot", "served"),
    [
        # The dispatched-worker shape: the gateway resolved a model (a crew pin) and
        # the backend confirmed none. Without the request the entry is
        # indistinguishable from a session deliberately left on the backend default.
        pytest.param(
            "kirocrew-worker",
            "chat-42",
            "",
            id="test_the_request_is_recorded_when_the_backend_confirmed_nothing",
        ),
        # A refused pin does not have to leave `model` empty: the backend can report
        # a concrete default of its own instead of the auto sentinel. Conditioning
        # the request on an unknown `model` dropped exactly this case, and the entry
        # is append-only, so the requested/served pair was lost with no way back.
        pytest.param(
            "kirocrew-worker",
            "chat-42",
            "backend-default-7",
            id="test_a_concrete_served_default_does_not_hide_the_request",
        ),
        # The backend serves the spelling IT resolved, so an APPLIED request can come
        # back as a different string. The entry records both and claims nothing about
        # which happened -- a reader must not read the difference as a refusal.
        pytest.param(
            "kirocrew",
            "chat-7",
            "kiro::claude-opus-5-v1",
            id="test_a_request_served_under_another_spelling_keeps_both_ids",
        ),
    ],
)
def test_the_request_is_recorded_beside_whatever_model_served(agent, slot, served):
    emit.on_session_opened(
        SESSION,
        agent=agent,
        slot=slot,
        model=served,
        model_requested="claude-opus-5",
    )
    data = _opened_data()
    assert data["model"] == served
    assert data["model_requested"] == "claude-opus-5"


def test_asking_for_nothing_writes_no_request_field():
    # Every tier deferred to the backend, so there is no request to record and an
    # empty field would read as a resolved model of "".
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", model="")
    data = _opened_data()
    assert data["model"] == ""
    assert "model_requested" not in data


# --- lineage: who made this session ----------------------------------------


def _opened_data() -> dict:
    opened = [e for e in _body() if e["type"] == "session/opened"]
    assert len(opened) == 1
    return opened[0]["data"]


def test_a_created_session_records_its_creator_on_the_opened_entry():
    # Both halves of the edge, from the one side that holds both: the creator's
    # key and ACP session id captured on the slot when session_create minted the
    # child. The sid is absent when the creator had no live handle at mint.
    emit.on_session_opened(
        SESSION,
        agent="kirocrew-worker",
        slot="chat-42",
        parent_slot="chat-7",
        parent_sid="acp-sess-creator",
    )
    assert _opened_data()["parent"] == {"slot": "chat-7", "sid": "acp-sess-creator"}


def test_a_creator_whose_handle_is_gone_is_recorded_by_slot_alone():
    # An empty sid is not written as "": a reader would take an empty string for
    # a creator with an empty name, where an absent key says the handle was down.
    emit.on_session_opened(SESSION, agent="kirocrew-worker", slot="chat-42", parent_slot="chat-7")
    assert _opened_data()["parent"] == {"slot": "chat-7"}


def test_a_session_nobody_created_carries_no_parent_at_all():
    # A person's own tab and a fork have no creator. The key is absent rather
    # than null or empty, so a fold can tell "no creator" from "creator unknown".
    _open_session()
    assert "parent" not in _opened_data()


def test_a_sid_without_a_slot_names_no_creator():
    # The slot is the tree key; a sid alone is a citation with nowhere to hang.
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-42", parent_sid="acp-sess-creator")
    assert "parent" not in _opened_data()


def test_the_session_class_is_recorded_as_facts_on_the_opened_entry():
    # What kind of session this log belongs to, recorded here because the reader
    # that asks -- may another session read this log -- asks it about sessions that
    # have closed, and a closed session has no slot left to ask.
    emit.on_session_opened(
        SESSION,
        agent="kirocrew",
        slot="chat-42",
        memory="persistent",
        app="travel-desk",
        channel=True,
    )
    assert _opened_data()["class"] == {
        "memory": "persistent",
        "app": "travel-desk",
        "channel": True,
    }


def test_a_session_with_nothing_to_declare_still_records_its_class():
    # MUTATION-SENSITIVE: this is what makes "recorded, and nothing applies"
    # distinguishable from "not recorded". The object is never empty, because
    # ``memory`` is always known for a live slot, so its presence is the witness --
    # and a reader deciding an authorization question refuses on the absence.
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-42", memory="persistent")
    assert _opened_data()["class"] == {"memory": "persistent"}


def test_a_caller_that_names_no_memory_mode_records_no_class_at_all():
    # A partial record would read as complete, so the emitter writes none. The key
    # is absent rather than empty, which is the same distinction ``parent`` keeps.
    _open_session()
    assert "class" not in _opened_data()


def test_an_absent_app_or_channel_is_absent_rather_than_falsy():
    # An empty app would read as an app with an empty name, and ``channel: false``
    # would claim the gateway checked and found none -- which is true here, but it
    # is not true on a log written before the field existed, and a reader must not
    # have to tell those two apart by the shape of a falsy value.
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-42", memory="incognito")
    recorded = _opened_data()["class"]
    assert recorded == {"memory": "incognito"}
    assert "app" not in recorded and "channel" not in recorded


def test_the_creator_is_written_again_on_a_re_attach():
    # A resume re-announces the header facts, and the creator is one of them: the
    # slot's attribution outlives the ACP session, so a gateway taking the child
    # over records who made it in the entry that says it re-attached. The emitter
    # writes whatever the caller froze on the slot; it never looks the sid up.
    emit.on_session_opened(SESSION, agent="kirocrew-worker", slot="chat-42", parent_slot="chat-7")
    emit.reset_caches()
    emit.on_session_opened(
        SESSION,
        agent="kirocrew-worker",
        slot="chat-42",
        resumed=True,
        parent_slot="chat-7",
        parent_sid="acp-sess-creator",
    )
    opened = [e for e in _body() if e["type"] == "session/opened"]
    assert [e["data"]["resumed"] for e in opened] == [False, True]
    assert opened[-1]["data"]["parent"] == {"slot": "chat-7", "sid": "acp-sess-creator"}


def test_reopening_appends_instead_of_truncating():
    _open_session()
    first = len(_entries())
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", resumed=True)
    entries = _entries()
    assert len(entries) == first + 1
    assert entries[-1]["data"]["resumed"] is True
    assert sum(1 for e in entries if e["type"] == "session") == 1


def test_a_warm_reuse_of_the_same_session_adds_no_second_opened_entry():
    """The claim runs every turn; only a create or a re-attach is worth an entry."""
    _open_session()
    _open_session()
    _open_session()
    assert sum(1 for e in _body() if e["type"] == "session/opened") == 1


def test_a_warm_reuse_does_not_change_which_turn_a_later_entry_names():
    # A re-claim of the same session has no cached anchor to clear: with the turn
    # carried in data, the next entry names the turn its caller passed.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    _open_session()
    emit.on_tool_called(SESSION, 2, name="fs_read", call_id="tc-1")
    last = _body()[-1]
    assert "thread" not in last
    assert last["data"]["turn"] == 2


def test_owner_is_written_once_and_a_reopen_cannot_change_it():
    emit.on_session_opened(SESSION, agent="kirocrew", owner="raymond")
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", owner="somebody-else")
    assert _entries()[0]["owner"] == "raymond"


async def _open_successor_on_a_loop() -> None:
    """Supersede ``SESSION`` from an event loop, where nothing is written inline."""
    _open_successor(previous_sid=SESSION)


# --------------------------------------------------------------------------- #
# A superseded crew log: closing its interrupted tail
# --------------------------------------------------------------------------- #


def _dangling_predecessor() -> None:
    """Leave ``SESSION`` with an open turn and an open tool call, then flush.

    The exact shape a torn-down ACP session leaves behind: a ``turn/started`` with
    no ``turn/completed`` and a ``tool/called`` with no result. Both are what a
    fold cannot tell from a turn still running.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.flush()


def _closers(session_id: str) -> list[dict]:
    return [e for e in _body(session_id) if e["type"] == "turn/completed"]


def test_a_supersede_closes_the_predecessors_interrupted_tail():
    """The repair half: the successor names the crew log AND closes its open tail.

    A slot outlives its ACP session, so every gateway restart creates a second crew
    log for one slot and leaves the first with a `turn/started` that has no
    `turn/completed` and a `tool/called` that has no result. Any fold over that file
    then reports a turn that never ended and a call that never returned, so the
    turn's cost, duration and outcome are permanently absent and a reader cannot
    tell an interrupted turn from one still running.

    Mutation guard: dropping the `_submit(_repair_superseded...)` call from the
    `session/opened` job leaves both entries below open and reddens this test.
    """
    _dangling_predecessor()
    # The predecessor's ACP session is GONE, so this process holds none of its
    # in-memory state -- the shape a gateway restart guarantees, and the one the
    # issue reports. Without this the fixture would leave turn 1's live record
    # standing, which in production means a turn still RUNNING, and the repair
    # would correctly stand down on a state no superseded session is ever in.
    emit.reset_caches()
    assert _closers(SESSION) == [], "the predecessor's turn is open before the supersede"

    _open_successor(previous_sid=SESSION)
    assert emit.flush()

    closers = _closers(SESSION)
    assert len(closers) == 1, "the superseded turn was not closed exactly once"
    assert closers[0]["data"] == {"turn": 1, "stop_reason": "interrupted"}
    results = [e for e in _body(SESSION) if e["type"] == "tool/completed"]
    assert len(results) == 1
    assert results[0]["data"]["status"] == "unknown"
    assert results[0]["data"]["call_id"] == "tc-1"
    # The repair writes into the PREDECESSOR only. The successor's own turn was
    # never started, so a closer there would be an outcome for a turn that is not
    # in the file at all.
    assert _closers(SUCCESSOR) == []


def test_the_repair_refuses_a_candidate_whose_header_names_another_slot():
    """The one place an outcome is authored into a unit that is not this session's.

    The edge already refuses to NAME a crew log whose own header does not name this
    slot, so this is the guarantee stated where the WRITE happens rather than
    inherited from a caller two decisions away: whatever reaches this function, a
    store belonging to another slot is never written into. Driven directly because
    the edge is what makes it unreachable through `on_session_opened` -- which is
    the point, not a gap.

    Mutation guard: removing the `_candidate_is_same_slot` branch from
    `_repair_superseded` closes the bystander's turn and reddens the count below.
    """
    bystander = "acp-sess-other-slot"
    emit.on_session_opened(bystander, agent="kirocrew", slot="chat-99")
    emit.on_turn_started(bystander, 1, "user")
    assert emit.flush()
    # Nothing is live for it any more: a fresh claim ends every turn no terminal is
    # coming for, so a stale pin cannot stand the repair down for a reason other than
    # the one under test.
    emit.on_session_opened(bystander, agent="kirocrew", slot="chat-99")
    assert emit.flush()
    assert emit.live_turn(bystander) == 0

    emit._repair_superseded(bystander, "chat-7")

    assert _closers(bystander) == [], "a crew log from another slot was written into"


def test_the_repair_stands_down_for_a_turn_still_running_in_this_process(caplog):
    """A running turn closes its own tail truthfully; a synthesised one would lie.

    Same rule as the resume path. Closing a turn that is still producing entries
    records an outcome it never had and is then followed by the rest of that turn,
    so the file states two outcomes for one turn with nothing able to say which
    happened.

    Mutation guard: removing the stand-down branch writes the interrupted closer
    here and reddens the count below.
    """
    _dangling_predecessor()
    assert emit.live_turn(SESSION) == 1

    with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.emit"):
        emit._repair_superseded(SESSION, "chat-7")

    assert _closers(SESSION) == [], "a turn still running in this process was closed"
    assert any("still running for it in this process" in r.message for r in caplog.records)


def test_a_live_record_with_no_closer_owed_stands_the_repair_down():
    """A turn mid-flight looks exactly like a leaked record, so neither is touched.

    `closer_owed` is set where a terminal is HANDED OVER, so a turn that is still
    running has not set it. A forced reset -- the model-switch route with
    `skip_running` false -- tears a session down in that state and lets the turn go
    on; its closer is queued later, by its own `finally`. Reading liveness after
    releasing records on `closer_owed` would drop exactly that turn's record, report
    0 running, and let this job write `interrupted` ahead of the real
    `turn/completed` still to come: two outcomes for one turn, in a file nothing
    rewrites.

    `on_session_closed` preserves those records for this reason, and this asserts the
    repair does not undo that.

    Mutation guard: releasing the session's unclosed turns ahead of the liveness read
    releases turn 1 (no terminal was handed over for it), the stand-down stops firing,
    and the closer count below goes from 0 to 1.
    """
    _dangling_predecessor()
    # Turn 1 is live with NO terminal handed over -- the state a reset-mid-turn leaves,
    # and the state a release keyed on `closer_owed` alone reads as certainly dead.
    assert emit.live_turn(SESSION) == 1

    emit._repair_superseded(SESSION, "chat-7")

    assert _closers(SESSION) == [], "a turn that may still be running was closed"


def test_the_repair_leaves_an_unmatched_child_open():
    """A repairing process cannot answer for another session's children.

    A child outlives the turn that asked for it and can still file its own real
    terminal. A synthesised `unknown` ahead of that would leave two outcomes for one
    `agent_id` in a file nothing rewrites, so no `child_gone` predicate is passed
    and the opener stands: a reader is left one fact short rather than wrong.

    Mutation guard: passing `child_gone=_child_gone_probe(previous_sid)` writes a
    `subagent/failed` here. The probe below is what makes that mutation observable:
    with no probe registered -- the default every test gets -- the store closes no
    child whatever the caller passes, so the assertion would hold against the
    mutation and pin nothing.
    """
    emit.set_child_liveness(lambda agent_id: False)
    try:
        _open_session()
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1")
        assert emit.flush()
        # The predecessor's ACP session is gone, so this process holds none of its
        # live state. Left standing, turn 1's record means a turn still RUNNING and
        # the repair correctly stands down, which is a state no superseded session
        # is in. This does not clear the child-liveness probe set above.
        emit.reset_caches()

        _open_successor(previous_sid=SESSION)
        assert emit.flush()
    finally:
        emit.set_child_liveness(None)

    assert [e for e in _body(SESSION) if e["type"] == "subagent/failed"] == []
    # The turn itself IS closed, so this is not a repair that did nothing.
    assert len(_closers(SESSION)) == 1


def test_a_predecessor_collected_during_the_deferral_costs_no_dropped_write():
    """Retention can collect the unit while the repair waits behind its writes.

    The window is as long as the predecessor's outstanding writes, and a crew log
    can be collected inside it. Opening a unit that is gone raises `no_ledger`,
    which is a permanent refusal: the writer drops the job and counts it in
    `dropped_writes`, reporting a hole in a file that is gone.

    Queued directly rather than through `on_session_opened`, because the EDGE
    refuses a candidate that is already gone -- so a collected unit reaches this
    branch only through the race the branch exists for: collected AFTER the edge
    decision and BEFORE this job runs. Reaching it any other way would be a
    different test wearing this name.

    Mutation guard: removing the `_candidate_is_same_slot` branch lets the open
    raise, which drops the append and reddens the count below. A separate `exists`
    check ahead of that branch can change no outcome, because the branch returns
    before the open.
    """
    _dangling_predecessor()
    emit.reset_caches()
    before = emit.dropped_writes()
    _remove_as_retention_does(lambda: shutil.rmtree(_log_path(SESSION).parent))

    emit._submit(
        lambda: emit._repair_superseded(SESSION, "chat-7"),
        "closing a superseded crew log's interrupted tail",
        SESSION,
    )
    assert emit.flush()

    assert emit.dropped_writes() == before, "a collected predecessor was counted as a loss"


def test_the_repair_runs_after_an_abandoned_outcome_and_closes_the_tail():
    """Finding 1: a debt abandoned AFTER the supersede must still end closed.

    The repair must not append a synthesised `interrupted` while a real
    `turn/completed` for the same turn is still queued or retrying, or the file
    carries two contradictory outcomes for one turn. Deciding that once at create
    time -- standing down while the process still owed the predecessor an outcome --
    left nothing to re-run it: a superseded id is never resumed and nothing maps to
    it once the successor takes over, so an owed append that then exhausted its
    retries left the tail open for good, for precisely the crew log that most
    needed repairing.

    The deferral here is the WRITE QUEUE: the repair is submitted into the
    predecessor's own bucket, so it runs after that unit's outstanding writes have
    been attempted, whether they landed or were abandoned. This drives the
    abandoned half -- the predecessor's real `turn/completed` fails transiently
    until its attempt budget is spent, so it is dropped and admitted in a
    `write/dropped` marker, and the tail still ends closed.

    Mutation guard: deciding at create time and standing down on
    whether the predecessor still owes entries leaves the turn below open.
    """
    _dangling_predecessor()
    emit.reset_caches()

    # The predecessor's real outcome, queued and then abandoned: a TRANSIENT failure
    # is retained and retried, and the batch is dropped once its attempt budget is
    # spent -- the exact path finding 1 names. The fixture flattens the backoff to
    # zero, so the budget is spent without waiting on a clock.
    attempts = 0

    def _doomed() -> None:
        nonlocal attempts
        attempts += 1
        raise OSError("the filesystem stopped answering")

    emit._submit(_doomed, "the predecessor's real turn/completed", SESSION)
    _open_successor(previous_sid=SESSION)
    assert emit.flush()

    assert (
        attempts == DEFAULT_LIMITS.max_write_attempts
    ), "the append did not spend its whole budget"
    # The abandoned outcome is admitted as a loss, and the tail is closed anyway.
    assert emit.dropped_writes() >= 1, "the doomed append was not given up on"
    closers = _closers(SESSION)
    assert len(closers) == 1, "the tail stayed open after its real outcome was abandoned"
    assert closers[0]["data"] == {"turn": 1, "stop_reason": "interrupted"}


def test_a_real_outcome_that_lands_leaves_the_repair_nothing_to_close():
    """The other half of the same ordering: no second outcome for one turn.

    A real `turn/completed` queued at supersede time is written BEFORE the repair
    runs, because both sit in the predecessor's bucket and the writer keeps a
    session's jobs in submission order. The repair then finds no open turn and
    writes nothing, so the file carries exactly one outcome -- the true one.

    This pins an ASSUMPTION rather than a branch of this change: the repair appends
    closers only for entries still open, which `_close_interrupted_tail` decides.
    The day that stops holding, a supersede starts writing a second outcome over a
    real one, and this reddens instead of a reader discovering it. Deliberately NOT
    claimed as a mutation guard for the queueing: swapping the repair onto the
    successor's bucket leaves this green, because the predecessor's bucket was
    filled first and drains first regardless. The queueing is pinned on its own
    axis by `test_the_repair_is_queued_under_the_predecessor_not_the_successor`.
    """
    _dangling_predecessor()
    emit.on_tool_completed(SESSION, 1, call_id="tc-1", status="completed")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")

    _open_successor(previous_sid=SESSION)
    assert emit.flush()

    closers = _closers(SESSION)
    assert len(closers) == 1, "the repair added a second outcome for one turn"
    assert closers[0]["data"]["stop_reason"] == "end_turn", "the real outcome was overwritten"
    statuses = [e["data"]["status"] for e in _body(SESSION) if e["type"] == "tool/completed"]
    assert statuses == ["completed"], "the repair added a second result for one call"


def test_the_repair_is_queued_under_the_predecessor_not_the_successor(monkeypatch):
    """The deferral IS the queue, so which bucket the job enters is the mechanism.

    The writer runs a session's jobs in submission order and orders nothing across
    sessions, so the repair waits for the predecessor's outstanding writes only
    while it sits in the predecessor's own bucket. Queued anywhere else it races
    them, and a synthesised `interrupted` can land ahead of a real
    `turn/completed` that was already on its way -- two outcomes for one turn, in a
    file nothing rewrites.

    So the predecessor here still OWES its real outcome when the successor opens: the
    `turn/completed` is retained behind a long backoff, and the shutdown drain lands
    it. A repair queued behind it then finds nothing open.

    Mutation guard: submitting under `session_id` instead of `superseded` runs the
    repair ahead of the owed outcome, and the turn ends with two outcomes.
    """
    _dangling_predecessor()
    # The predecessor's process is gone, so no live record stands the repair down.
    emit.reset_caches()
    delays = {"secs": 3600.0}
    attempts = {"n": 0}

    def _real_outcome() -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("the outcome is on its way, not yet on disk")
        handle = emit._handle(SESSION)
        assert handle is not None
        handle.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")

    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: delays["secs"])
    emit._submit(_real_outcome, "the predecessor's real turn/completed", SESSION)
    assert attempts["n"] == 1, "the outcome was not attempted and retained"
    # From an event loop, so the opening is handed to the parked writer rather than
    # waiting on it for an inline turn.
    asyncio.run(_open_successor_on_a_loop())
    delays["secs"] = 0.0
    _assert_drained_for_shutdown("the retained outcome and the stood-down repair")

    stops = [closer["data"]["stop_reason"] for closer in _closers(SESSION)]
    assert stops == ["end_turn"], f"the repair ran ahead of the outcome still owed: {stops}"


def test_a_reattach_recovers_the_durable_edge_after_a_crash_lost_the_repair():
    """The edge is durable, the repair job is not, so a re-attach re-queues it.

    The window the recovery closes: the opening entry carrying `previous.sid` is an
    append, while the repair rides the in-memory buffer, so a crash between the two
    loses the repair and a superseded id is never resumed to re-queue it. The
    predecessor's tail would then read as open for the life of the file.

    The crash is modelled as what a crash actually costs -- the queued repair never
    runs and the process's memory is gone -- rather than by faking a file state: the
    supersede is driven for real with the repair never queued, then `reset_caches`
    drops the in-memory state a restart would not have, and the successor re-attaches
    with `resumed=True`, which is what a later process does to a conversation an
    earlier one was writing.

    Mutation guard: dropping the `announce.setdefault("recovered_previous", ...)`
    recovery, or reading the caller's `previous_sid` instead of this log's own entry,
    leaves the closer count at 0.
    """
    _dangling_predecessor()
    emit.reset_caches()

    # The supersede, with the repair job LOST exactly as a crash loses it: the
    # opening entry lands durably, the follow-on job is never queued.
    with unittest.mock.patch.object(emit, "_queue_tail_repair", lambda previous_sid, slot: None):
        _open_successor(previous_sid=SESSION)
        assert emit.flush()

    assert _closers(SESSION) == [], "the repair was not actually lost, so this proves nothing"
    # The edge IS durable, which is what makes recovery possible at all.
    opened = [e for e in _body(SUCCESSOR) if e["type"] == "session/opened"]
    assert opened and opened[0]["data"]["previous"] == {"sid": SESSION}

    # The restart: no in-memory state survives it.
    emit.reset_caches()
    emit.on_session_opened(SUCCESSOR, agent="kirocrew", slot="chat-7", resumed=True)
    assert emit.flush()

    closers = _closers(SESSION)
    assert len(closers) == 1, "the re-attach did not recover the lost repair"
    assert closers[0]["data"] == {"turn": 1, "stop_reason": "interrupted"}


def test_the_recovery_reads_this_logs_own_edge_not_the_callers(caplog):
    """A re-attach must not repair the unit the CALLER names -- it may still be live.

    The latch refuses the caller's `previous_sid` on a re-attach for a stated reason:
    the unit it names is either this same one or an unrelated one that may still be
    running, and repairing that is how a live turn gets an outcome it never had. The
    recovery does not weaken it, because it reads the predecessor from THIS log's own
    opening entry -- a value an earlier attempt verified against the slot before
    writing it.

    Here the successor's file carries NO `previous`, and the caller names a live
    third session on the re-attach. Nothing may be closed.

    Mutation guard: recovering from `previous_sid` rather than from the file closes
    the third session's open turn and reddens the count below.
    """
    third = "sid-a-live-third-session"
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    # A successor opened with NO predecessor, so its file records no edge.
    emit.on_session_opened(SUCCESSOR, agent="kirocrew", slot="chat-7")
    assert emit.flush()
    opened = [e for e in _body(SUCCESSOR) if e["type"] == "session/opened"]
    assert opened and "previous" not in opened[0]["data"], "the fixture wrote an edge"

    emit.reset_caches()
    with caplog.at_level(logging.INFO, logger="kiro_crew.crew_log.emit"):
        emit.on_session_opened(
            SUCCESSOR, agent="kirocrew", slot="chat-7", resumed=True, previous_sid=third
        )
        assert emit.flush()

    assert _closers(third) == [], "the caller's named unit was repaired on a re-attach"
    assert _closers(SESSION) == [], "a unit this log never names was repaired"
    assert not any("recovered the superseded crew log" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# A superseded crew log: walking the whole predecessor chain
# --------------------------------------------------------------------------- #


def _open_on_chat_7(session_id: str, **kwargs) -> None:
    """Open *session_id* on the slot ``_open_session`` uses."""
    emit.on_session_opened(
        session_id,
        agent="kirocrew",
        slot="chat-7",
        model="claude-opus-5",
        cwd="/home/dev/project",
        owner="default",
        **kwargs,
    )


def _lose_the_repair():
    """The supersede's follow-on repair, lost exactly as a crash loses it.

    The opening entry carrying ``previous.sid`` still lands durably; only the job
    queued behind it never runs.
    """
    return unittest.mock.patch.object(emit, "_queue_tail_repair", lambda *_a, **_k: None)


def _chain_of_open_tails(sids: "list[str]") -> None:
    """Build one slot's chain ``sids[0] -> sids[1] -> ...``, every tail left OPEN.

    Each log after the first supersedes the one before it with the repair lost, and
    each process is gone before the next one starts (``reset_caches``), so no live
    record of any turn survives: a crash loop that never got as far as one repair.
    """
    for index, sid in enumerate(sids):
        if index == 0:
            _open_on_chat_7(sid)
        else:
            with _lose_the_repair():
                _open_on_chat_7(sid, previous_sid=sids[index - 1])
        emit.on_turn_started(sid, 1, "user")
        assert emit.flush()
        emit.reset_caches()
    for sid in sids:
        assert _closers(sid) == [], f"{sid} was closed while building the chain"


def test_a_cold_start_closes_a_tail_two_hops_back_in_the_chain():
    """A to B to C, and a crash after B named A but before A was repaired.

    The repair of A is a job on the in-memory write buffer and the edge naming A is
    a durable append, so a crash between them loses only the repair. B is never
    resumed after it is superseded, so the re-attach recovery never re-queues it,
    and C's supersede repaired B alone: A's ``turn/started`` stayed open for the life
    of a file nothing rewrites.

    B's own turn ENDS normally here, so B's tail needs nothing. That is the case a
    walk that stops at the first hop with nothing to close would get wrong: a hop's
    tail being closed says nothing about whether the repair of the hop BEFORE it ran.

    Mutation guard: dropping the walk from `_repair_superseded` (closing only the hop
    it was queued for) leaves A's closer count at 0.
    """
    third = "acp-sess-0003"
    _dangling_predecessor()
    emit.reset_caches()
    with _lose_the_repair():
        _open_successor(previous_sid=SESSION)
        emit.on_turn_started(SUCCESSOR, 1, "user")
        emit.on_turn_completed(SUCCESSOR, 1, stop_reason="end_turn")
        assert emit.flush()
    emit.reset_caches()
    assert _closers(SESSION) == [], "A's repair was not actually lost, so this proves nothing"

    _open_on_chat_7(third, previous_sid=SUCCESSOR)
    assert emit.flush()

    closers = _closers(SESSION)
    assert len(closers) == 1, "the tail two hops back stayed open after the cold start"
    assert closers[0]["data"] == {"turn": 1, "stop_reason": "interrupted"}
    results = [e for e in _body(SESSION) if e["type"] == "tool/completed"]
    assert [r["data"]["status"] for r in results] == ["unknown"]
    # B's one real outcome is left as it is: no second outcome for one turn.
    assert [c["data"]["stop_reason"] for c in _closers(SUCCESSOR)] == ["end_turn"]


def test_a_cold_start_closes_every_open_tail_of_the_chain():
    """The whole durable chain, not a fixed depth: three open tails, one cold start.

    Mutation guard: walking a fixed number of extra hops instead of following each
    log's own ``previous`` to the chain's start leaves the oldest tail open.
    """
    chain = ["acp-sess-0001", "acp-sess-0002", "acp-sess-0003", "acp-sess-0004"]
    _chain_of_open_tails(chain[:3])

    _open_on_chat_7(chain[3], previous_sid=chain[2])
    assert emit.flush()

    for sid in chain[:3]:
        closers = _closers(sid)
        assert len(closers) == 1, f"{sid}'s tail was not closed exactly once"
        assert closers[0]["data"] == {"turn": 1, "stop_reason": "interrupted"}
    assert _closers(chain[3]) == [], "the newest log's own turn never started"


def test_a_reattach_recovery_walks_the_chain_too():
    """The re-attach recovery queues the same repair, so it walks the same chain.

    A crash loop: A's repair and B's are both lost, then C re-attaches. C's own
    opening entry names B, the recovery closes B, and the walk goes on to A.

    Mutation guard: walking only from the supersede site, not from inside the
    repair every caller queues, leaves A open here.
    """
    chain = ["acp-sess-0001", "acp-sess-0002", "acp-sess-0003"]
    _chain_of_open_tails(chain)

    _open_on_chat_7(chain[2], resumed=True)
    assert emit.flush()

    assert len(_closers(chain[1])) == 1, "the recovered hop was not closed"
    assert len(_closers(chain[0])) == 1, "the recovery did not walk past the first hop"


@pytest.mark.parametrize(
    "edges",
    [
        pytest.param({"acp-sess-0001": "acp-sess-0001"}, id="self-loop"),
        pytest.param(
            {"acp-sess-0001": "acp-sess-0002", "acp-sess-0002": "acp-sess-0001"},
            id="two-log-loop",
        ),
    ],
)
def test_a_cyclic_predecessor_pointer_ends_the_walk_with_a_logged_stop(edges, monkeypatch, caplog):
    """A looping ``previous`` ends the walk, says so, and closes each tail once.

    The emitter refuses to WRITE such an edge, but the walk reads files written by
    any earlier build, and a damaged or hand-edited opening entry can name anything.
    Each log of the loop is a real log of this slot, so the same-slot check passes
    every step and only the walk's own guard can stop it.

    Mutation guard: removing the already-walked check stops the walk only at the
    process's repair memo (silently, so the warning below is missing) or at the hop
    bound (after closing nothing more, but logging the wrong reason).
    """
    for sid in sorted(set(edges) | set(edges.values())):
        _open_on_chat_7(sid)
        emit.on_turn_started(sid, 1, "user")
        assert emit.flush()
    emit.reset_caches()
    monkeypatch.setattr(emit, "_durable_previous", lambda sid: edges.get(sid, ""))

    with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.emit"):
        emit._queue_tail_repair("acp-sess-0001", "chat-7")
        assert emit.flush(), "the walk did not terminate"

    for sid in edges:
        assert len(_closers(sid)) == 1, f"{sid}'s tail was not closed exactly once"
    stops = [r.message for r in caplog.records if "loops back on itself" in r.message]
    assert len(stops) == 1, f"the cycle stop was not logged exactly once: {stops}"


def test_the_walk_stops_at_its_hop_bound_with_a_logged_stop(monkeypatch, caplog):
    """A chain longer than the bound is walked to the bound and no further.

    Each hop opens and scans a whole crew log on the writer thread, so the bound is
    what keeps one repair walk's cost finite whatever the chain holds, and stopping
    at it is logged with the log it stopped before.

    Mutation guard: removing the bound check closes all four tails here.
    """
    chain = ["acp-sess-0001", "acp-sess-0002", "acp-sess-0003", "acp-sess-0004"]
    _chain_of_open_tails(chain)
    monkeypatch.setattr(emit, "_REPAIR_WALK_HOPS", 2)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.emit"):
        emit._queue_tail_repair(chain[3], "chat-7")
        assert emit.flush()

    assert [len(_closers(sid)) for sid in chain] == [0, 0, 1, 1]
    stops = [r.message for r in caplog.records if "the walk's bound" in r.message]
    assert len(stops) == 1, f"the bound stop was not logged exactly once: {stops}"
    assert chain[1] in stops[0], "the stop did not name the log it stopped before"


def test_a_walk_that_reaches_a_log_retention_removed_ends_quietly(caplog):
    """The ordinary end of an old chain is not reported as damage.

    Retention removes a long-lived slot's oldest crew logs, so the walk reaching a
    log that is gone is how most chains end, on every restart. The walk reads the
    hop's header before queueing it and stops there; queueing it anyway would have
    the repair warn that it is not closing a log that is already gone.

    Mutation guard: removing the header read before the hop is queued brings back
    the repair's "NOT closing" warning for the removed log.
    """
    chain = ["acp-sess-0001", "acp-sess-0002", "acp-sess-0003"]
    _chain_of_open_tails(chain[:2])
    _remove_as_retention_does(lambda: shutil.rmtree(_log_path(chain[0]).parent))

    with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.emit"):
        _open_on_chat_7(chain[2], previous_sid=chain[1])
        assert emit.flush()

    assert len(_closers(chain[1])) == 1, "the hop that still exists was not closed"
    warned = [r.getMessage() for r in caplog.records if chain[0] in r.getMessage()]
    assert warned == [], f"the removed log was reported as damage: {warned}"


def test_a_walk_does_not_follow_an_edge_to_another_slots_log(monkeypatch, caplog):
    """A hop whose own header names another slot ends the walk, and says so.

    The emitter refuses to write such an edge, but a damaged or older entry can
    carry one, and following it would author an outcome into another slot's log.

    Mutation guard: queueing the hop without reading its header leaves the other
    slot's log unclosed as well (the repair refuses it), but the walk's own warning
    naming the edge is missing.
    """
    foreign = "acp-sess-0009"
    emit.on_session_opened(foreign, agent="kirocrew", slot="chat-8", owner="default")
    emit.on_turn_started(foreign, 1, "user")
    assert emit.flush()
    _chain_of_open_tails(["acp-sess-0001"])
    monkeypatch.setattr(
        emit, "_durable_previous", lambda sid: foreign if sid == "acp-sess-0001" else ""
    )

    with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.emit"):
        emit._queue_tail_repair("acp-sess-0001", "chat-7")
        assert emit.flush()

    assert len(_closers("acp-sess-0001")) == 1
    assert _closers(foreign) == [], "another slot's log was closed"
    stops = [r.getMessage() for r in caplog.records if "names another slot" in r.getMessage()]
    assert len(stops) == 1, f"the foreign edge was not reported exactly once: {stops}"


def test_a_second_supersede_in_one_process_does_not_rewalk_the_chain(monkeypatch):
    """Each crew log is walked once per process, not once per supersede.

    A slot gets a new log on every compaction, reset or model switch, so its chain
    grows for as long as it lives, and every hop is a whole-file scan on the writer
    thread. Re-walking the whole chain on each supersede would make each one cost
    the slot's whole history. Within one process a log already queued for repair
    needs no second walk: a superseded log is never resumed, so nothing reopens a
    tail this process has closed.

    Mutation guard: dropping the already-queued check makes the second supersede
    repair B and A again, so their counts below read 2.
    """
    chain = ["acp-sess-0001", "acp-sess-0002", "acp-sess-0003", "acp-sess-0004"]
    _chain_of_open_tails(chain[:2])
    repaired: "list[str]" = []
    real_repair = emit._repair_superseded

    def _spy(previous_sid: str, slot: str, **kwargs) -> None:
        repaired.append(previous_sid)
        real_repair(previous_sid, slot, **kwargs)

    monkeypatch.setattr(emit, "_repair_superseded", _spy)
    _open_on_chat_7(chain[2], previous_sid=chain[1])
    assert emit.flush()
    _open_on_chat_7(chain[3], previous_sid=chain[2])
    assert emit.flush()

    assert sorted(repaired) == sorted(chain[:3]), f"a log was walked twice: {repaired}"
    assert [len(_closers(sid)) for sid in chain[:2]] == [1, 1]


def test_a_superseded_repair_survives_the_pending_ceiling():
    """Refusing only the FOLLOW-ON leaves the tail open with nothing to re-queue it.

    The opening entry that queues this repair is itself exempt from the ceiling, so
    under pressure the successor's crew log is created while a non-exempt follow-on
    is refused. Nothing re-queues a one-shot job for an id that is never resumed, so
    the predecessor's `turn/started` and `tool/called` would stay open for the life
    of a file nothing rewrites -- the exact state this job exists to remove.

    The ceiling has to be in force at the moment the repair is SUBMITTED, which is
    inside the opener's job body, so the writer is built with a ceiling of zero for
    the whole test and a non-exempt probe proves it is in force.
    `repair_interrupted_turn` writes through the store handle rather than this
    queue, so the closers themselves are never ceiling-gated.

    Mutation guard: queueing the repair as an ordinary append rather than a writer
    follow-on leaves the closer count below at 0.
    """
    _dangling_predecessor()
    emit.reset_caches(limits=WriterLimits(max_pending_count=0))

    async def _probe() -> None:
        emit.on_turn_started("ceiling-probe", 1, "user")

    before = emit.overflow_writes()
    asyncio.run(_probe())
    assert emit.overflow_writes() == before + 1, "the ceiling did not reject a non-exempt job"

    _open_successor(previous_sid=SESSION)
    assert emit.flush(timeout=20.0)

    closers = _closers(SESSION)
    assert len(closers) == 1, "the ceiling refused the repair and the tail stayed open"
    assert closers[0]["data"] == {"turn": 1, "stop_reason": "interrupted"}


def test_a_repair_dropped_with_the_batch_it_waited_in_comes_back(monkeypatch):
    """The repair is a writer follow-on: dropped with its batch, it is submitted again.

    The deferral that makes this job correct is also how it is lost: it waits in the
    predecessor's bucket BEHIND entries that crew log already owes, and a filesystem
    that keeps refusing that session's writes until the attempt budget is spent
    drops the entire retained batch -- the owed append and this job with it. Nothing
    re-queues a one-shot job for an id that is never resumed, so the predecessor's
    ``turn/started`` would stay open for the life of a file nothing rewrites.

    Two things are asserted, because the policy has two halves. The repair comes
    back, and it lands BEHIND the ``write/dropped`` marker, so the file states both
    that entries are missing and that the turn did not finish. And the marker counts
    ONE loss rather than two: the repair is not missing, it is being submitted again.

    The owed append is parked behind a long backoff while the successor opens, so the
    repair is queued into the batch BEFORE the budget is spent; the shutdown drain
    then spends the rest of the budget inside its window.

    Mutation guard: queueing the repair as an ordinary append leaves the closer count
    at 0; counting the re-submitted repair as lost makes the marker claim 2.
    """
    _dangling_predecessor()
    # A restart leaves no live record behind, and this repair only ever runs for a
    # predecessor no process is still writing.
    emit.reset_caches()
    delays = {"secs": 3600.0}

    def _fail() -> None:
        raise OSError("the predecessor's disk is wedged")

    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: delays["secs"])
    emit._submit(_fail, "the predecessor's owed append", SESSION, 23)
    asyncio.run(_open_successor_on_a_loop())
    delays["secs"] = 0.0
    _assert_drained_for_shutdown("the dropped batch and its re-queued repair")

    assert emit.dropped_writes() == 1, "the budget was not spent, or the repair was counted"
    closers = _closers(SESSION)
    assert len(closers) == 1, "the dropped repair was not re-queued, so the tail stayed open"
    assert closers[0]["data"] == {"turn": 1, "stop_reason": "interrupted"}

    body = _body(SESSION)
    kinds = [entry["type"] for entry in body]
    markers = [entry for entry in body if entry["type"] == "write/dropped"]
    assert len(markers) == 1, f"expected exactly one marker, saw {kinds}"
    assert markers[0]["data"] == {"dropped_count": 1, "dropped_bytes": 23}, markers[0]["data"]
    assert kinds.index("write/dropped") < kinds.index(
        "turn/completed"
    ), f"the closer landed ahead of the marker that explains it: {kinds}"


def test_a_dropped_terminal_requeues_the_repair_that_stood_down_for_it():
    """The stand-down is right only while that terminal is still COMING.

    A forced reset tears a session down mid-turn, so its closer is handed over later
    from the turn's own `finally`, and until then the repair must not write
    `interrupted` ahead of it. But a terminal that spends its attempt budget is
    dropped, and then no outcome is coming at all: the predecessor's `turn/started`
    would stay open for the life of a file nothing rewrites, because the queue orders
    this job behind entries ALREADY queued, not behind one handed over after it drained.

    The terminal carries a permanent-drop hook for exactly that outcome. `after` cannot
    serve here: it runs when the append RESOLVES, written or given up on alike, so it
    cannot tell the two apart, while a permanent-drop hook fires only on the giving up.

    `reset_caches` is deliberately NOT called: the standing live record IS the state a
    forced reset mid-turn leaves behind, and it is what makes the repair stand down.

    Mutation guard: dropping `on_permanent_drop=_terminal_dropped(...)` from the
    terminal sites, or the debt the stand-down records, leaves the closer count at 0.
    """
    _dangling_predecessor()
    _open_successor(previous_sid=SESSION)
    assert emit.flush()
    assert _closers(SESSION) == [], "the repair did not stand down for the live turn"

    real_append = lg.CrewLog.append

    def _lose_the_real_terminal(self, entry_type, data, *args, **kwargs):
        if entry_type == "turn/completed" and data.get("stop_reason") == "end_turn":
            raise OSError("the terminal cannot be written")
        return real_append(self, entry_type, data, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _lose_the_real_terminal)
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
        assert emit.flush(timeout=20.0)

    assert emit.dropped_writes() == 1, "the terminal's attempt budget was never spent"
    closers = _closers(SESSION)
    assert len(closers) == 1, "the dropped terminal left the predecessor's tail open"
    assert closers[0]["data"] == {"turn": 1, "stop_reason": "interrupted"}
    assert emit.live_turn(SESSION) == 0


def test_a_ceiling_rejected_terminal_releases_its_pin_then_requeues_the_repair(monkeypatch):
    """A ceiling REJECTION is a permanent loss too, and the emitter settles it.

    A terminal that stood a repair down carries the trigger that re-queues it. The
    writer hands a rejected job back unsettled, and the emitter settles it: it reads
    the repair debt FIRST, then runs the terminal's release -- which frees the turn's
    pin and, the session having no live turn left, clears that debt -- and only then
    re-queues the repair from what it read. Read after the release, the debt is gone
    and nothing is re-queued; re-queued before the release, the repair can run against
    the still-live pin, stand down again, and have its renewed debt erased by the
    release. Either way the predecessor's tail stays open for the life of the file.

    Driven through the public terminal on an event loop, so the entry takes the
    buffered path a ceiling of zero refuses.

    Mutation guard: reading the debt after the release leaves `queued` empty and the
    closer count at 0; re-queueing before the release records the pin still live.
    """
    emit.reset_caches(limits=WriterLimits(max_pending_count=0))
    _dangling_predecessor()
    _open_successor(previous_sid=SESSION)
    assert emit.flush()
    assert _closers(SESSION) == [], "the repair did not stand down for the live turn"

    queued: list[tuple[str, str, int]] = []
    real_queue = emit._queue_tail_repair

    def _spy_queue(previous_sid: str, slot: str) -> None:
        queued.append((previous_sid, slot, emit.live_turn(previous_sid)))
        real_queue(previous_sid, slot)

    monkeypatch.setattr(emit, "_queue_tail_repair", _spy_queue)

    async def _terminal() -> None:
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")

    asyncio.run(_terminal())
    assert emit.overflow_writes(SESSION) == 1, "the ceiling did not reject the terminal"
    assert queued == [
        (SESSION, "chat-7", 0)
    ], f"the repair was not re-queued after the release: {queued}"
    assert emit.flush(timeout=20.0)

    closers = _closers(SESSION)
    assert len(closers) == 1, "a ceiling-rejected terminal left the predecessor's tail open"
    assert closers[0]["data"] == {"turn": 1, "stop_reason": "interrupted"}
    kinds = [e["type"] for e in _body(SESSION)]
    assert kinds.index("write/dropped") < kinds.index("turn/completed"), kinds


def test_a_landed_terminal_pays_the_debt_and_the_repair_writes_nothing():
    """The other half: a terminal that LANDS closes the tail truthfully.

    The stand-down deferred to this turn's real outcome, and here it arrives, so the
    debt is simply paid and nothing may be re-queued or synthesised. The turn must end
    with its OWN `end_turn`, not with an `interrupted` written beside it -- two outcomes
    for one turn is the corruption the stand-down exists to avoid. That the landed
    release pays the debt is pinned at the tracker's interface
    (`test_a_landed_terminal_pays_the_debt` in test_crew_log_turn_tracker.py).
    """
    _dangling_predecessor()
    _open_successor(previous_sid=SESSION)
    assert emit.flush()
    assert _closers(SESSION) == [], "the repair did not stand down for the live turn"

    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush(timeout=20.0)

    closers = _closers(SESSION)
    assert len(closers) == 1, f"the turn got {len(closers)} outcomes, not one"
    assert closers[0]["data"]["stop_reason"] == "end_turn", (
        "the real outcome was replaced or shadowed by a synthesised one: " f"{closers[0]['data']}"
    )


def test_a_cleared_sid_still_names_the_predecessor():
    """`clear_sid` drops the POINTER, not the history, so the edge survives it.

    A provider switch and the poisoned-conversation discard both empty `sid` and
    stash it as `discarded_sid`, saying so in the method's own docstring: the
    native conversation is still on disk. For the RESUME question the clear is the
    answer, which is why `get` must not see the stashed id. For the history
    question it is not: the cleared id is exactly "the id this key was last
    serving". Reading `sid` alone made a key that had served a session all day
    report having served none, so the successor spawned after the clear cited no
    predecessor and the slot's history was truncated with nothing recording it.

    Mutation guard: dropping the `discarded_sid` fallback answers "" here.
    """
    mapping = session_map.SessionMap()
    mapping.set("dashboard:chat-9", SESSION, provider="", cwd="")
    assert mapping.mapped_sid("dashboard:chat-9") == SESSION

    mapping.clear_sid("dashboard:chat-9")

    # The resume answer and the history answer diverge here, which is the point.
    assert mapping.get("dashboard:chat-9") in (None, "")
    assert mapping.mapped_sid("dashboard:chat-9") == SESSION


def test_an_oversized_id_in_the_mapping_answers_as_no_predecessor():
    """This map is agent-writable JSON, so a value read back is input, not a fact.

    An id longer than the bound every other consumer applies would ride into
    `session/opened.previous`, and an entry over the maximum size is DROPPED rather
    than truncated -- so one oversized value in this file would cost a record that
    had nothing to do with it. Answering "" keeps the failure at the size of "no
    predecessor", which every caller already handles.

    Mutation guard: removing the bound returns the oversized id here.
    """
    mapping = session_map.SessionMap()
    mapping.set("dashboard:chat-big", "x" * 4096, provider="", cwd="")

    assert mapping.mapped_sid("dashboard:chat-big") == ""

    # At the bound it still answers: the rule is a ceiling, not a narrowing.
    mapping.set("dashboard:chat-fits", "y" * 128, provider="", cwd="")
    assert mapping.mapped_sid("dashboard:chat-fits") == "y" * 128


def test_a_key_that_never_served_a_session_names_no_predecessor():
    """The fallback must not invent one: absent is still "nothing to follow"."""
    mapping = session_map.SessionMap()
    assert mapping.mapped_sid("dashboard:chat-never") == ""


def test_a_stale_prune_records_the_id_it_dropped_as_the_keys_latest_store():
    """Clearing A then pruning B must not leave A standing as the last store.

    Three paths empty ``sid`` in place: the provider switch, the startup prune,
    and the per-read stale repair. All three must record what they dropped. When
    only one does, a prune leaves an OLDER id in ``discarded_sid`` and the history
    reader answers that -- naming a predecessor two links back and orphaning the store
    between them. Which path emptied the field is not a distinction any reader of
    it can use, so it cannot be written by only some of them.

    Mutation guard: returning the prune's clear to a bare ``sid = ""`` answers
    ``acp-sess-A`` here.
    """
    mapping = session_map.SessionMap()
    key = "dashboard:chat-42"

    mapping.set(key, "acp-sess-A", provider="", cwd="")
    mapping.clear_sid(key)
    mapping.set(key, "acp-sess-B", provider="", cwd="")
    assert mapping.mapped_sid(key) == "acp-sess-B"

    # A binding makes the entry outlive its session, so the stale path empties the
    # pointer in place instead of removing the whole entry.
    mapping._data[session_map.canonical_key(key)]["slack_thread_ts"] = "1700000000.1"

    # B's transcript does not exist here, which is what prune calls stale.
    mapping.prune()

    assert mapping.mapped_sid(key) == "acp-sess-B", (
        "the reader named the older cleared id, so a successor would cite a "
        "predecessor two links back and skip the store between them"
    )


def test_three_stores_of_one_slot_form_a_chain_with_no_store_skipped():
    """A to B to C, each citing the one before it rather than the one before that.

    What a reader needs from the edge: following it from the newest store reaches
    every earlier one exactly once. Each predecessor is handed in directly here,
    which is what the caller does -- it reads the slot's mapping and latches the
    answer -- so this pins the chain shape the emitter writes rather than where
    the caller found the id.
    """
    first, second, third = SESSION, SUCCESSOR, "acp-sess-0003"

    emit.on_session_opened(first, agent="kirocrew", slot="chat-7", cwd="/home/dev/project")
    assert emit.flush()

    emit.on_session_opened(
        second,
        agent="kirocrew",
        slot="chat-7",
        cwd="/home/dev/project",
        previous_sid=first,
    )
    assert emit.flush()

    emit.on_session_opened(
        third,
        agent="kirocrew",
        slot="chat-7",
        cwd="/home/dev/project",
        previous_sid=second,
    )
    assert emit.flush()

    def _previous(session_id: str) -> dict | None:
        opened = [e for e in _body(session_id) if e["type"] == "session/opened"]
        assert len(opened) == 1
        return opened[0]["data"].get("previous")

    assert _previous(first) is None
    assert _previous(second) == {"sid": first}
    assert _previous(third) == {"sid": second}, "the chain skipped the store between"


def test_every_slot_allocation_site_latches_the_predecessor_first():
    """Site COVERAGE, which the spelling ratchet above cannot give.

    That ratchet pins how the two known sites read the predecessor. It says nothing
    about a THIRD allocation site added later: such a site publishes its own id over
    the slot without ever latching, so the store it supersedes is cited by nobody
    and the chain has a gap a walker cannot see -- the same loss this edge exists to
    remove, reintroduced by addition rather than by edit, and silently.

    So this reads the allocation calls instead of the latch calls, and requires each
    one to be preceded by a latch. A new site reds this on the day it is written,
    which is the only moment anyone is in a position to know whether it should
    latch.

    The window is generous (the prefetch's latch sits nine lines above its call, the
    turn's two) and deliberately bounded: a latch hundreds of lines away is not
    evidence about this call.

    The exact count is asserted too, so a third site cannot arrive unnoticed even if
    it happens to sit below an unrelated latch.

    Mutation guard: deleting either latch call, or moving it below its allocation,
    reds this.
    """
    runner = Path(__file__).parent.parent / "src" / "kiro_crew" / "dashboard" / "chat_runner.py"
    lines = "\n".join(
        turn_file.read_text(encoding="utf-8")
        for turn_file in [*sorted(runner.with_name("chat_turn").glob("*.py")), runner]
        if turn_file.name != "__init__.py"
    ).splitlines()
    allocations = [i for i, line in enumerate(lines) if "get_or_create(" in line]
    assert len(allocations) == 2, (
        "chat_runner now allocates a slot's session at a different number of sites; "
        f"each one must latch the predecessor first: lines {[i + 1 for i in allocations]}"
    )
    window = 30
    for index in allocations:
        preceding = lines[max(0, index - window) : index]
        assert any("latch_crew_log_previous(" in line for line in preceding), (
            f"the allocation at line {index + 1} publishes a session id for the slot "
            "without latching the store it supersedes, so that store is left cited "
            "by nobody and a chain walker steps over it"
        )


def test_the_turn_path_reads_the_predecessor_through_the_non_pruning_accessor():
    """A source ratchet, because the sources are one identifier apart.

    Three spellings type-check here and only one is right. `mapped_sid` and
    `resumable_sid` differ by a filesystem stat and a prune, and at this call site
    that difference is a sync store read on the gateway loop plus the loss of the
    very edge being recorded. Feeding the latch from the mapping ALONE type-checks
    too, and is the defect this edge was moved off: the mapping is deliberately a
    generation behind while an allocation's replay is pending, so a gateway that
    restarts inside that window cites a generation back and the store between is
    cited by nobody. Every behavioural test of the emitter passes on any of the
    three, because the emitter is handed the value rather than choosing it. So the
    choice is pinned where it is made.

    It is pinned at EVERY site, not one: the slot has two allocation sites -- the
    eager prefetch and the first real turn -- and a spelling that is right at one
    and wrong at the other leaves the prefetched half of the fleet recording no
    edge, which is the shape that shipped broken once already. A site added later
    that feeds the latch from anything else reds this.

    Mutation guard: feeding either latch from `mapped_sid` directly, or swapping the
    resolver's own fallback to `resumable_sid`, reds this.
    """
    runner = Path(__file__).parent.parent / "src" / "kiro_crew" / "dashboard" / "chat_runner.py"
    source = "\n".join(
        turn_file.read_text(encoding="utf-8")
        for turn_file in [*sorted(runner.with_name("chat_turn").glob("*.py")), runner]
        if turn_file.name != "__init__.py"
    )
    # Joined because the call can be wrapped across lines; the whole expression is
    # what this pins, so a line-at-a-time read could not see it.
    flat = " ".join(source.split())
    found = re.findall(r"slot\.latch_crew_log_previous\([^)]*\)", flat)
    # Paren-adjacent spaces dropped, because whether a call fits on one line is the
    # formatter's business and this ratchet is about what feeds the latch.
    latches = [call.replace("( ", "(").replace(" )", ")") for call in found]
    # Both allocation sites, each latching ALL THREE parts of what the one resolver
    # answered. A site that passed only the sid would latch a slot with an
    # undetermined predecessor as one that has none; a site that dropped
    # `from_mapping` would cite the mapping inside the window where it is knowingly a
    # generation behind, because the slot cannot otherwise tell a mapped id from its
    # own record.
    assert latches == [
        "slot.latch_crew_log_previous(_previous.sid, undecided=_previous.undecided, "
        "from_mapping=_previous.from_mapping,)",
        "slot.latch_crew_log_previous(_previous.sid, undecided=_previous.undecided, "
        "from_mapping=_previous.from_mapping,)",
    ], f"the predecessor is latched somewhere unexpected: {latches}"
    # Each of those reads its pair from the one helper that consults the store first.
    # The `sessions` receiver differs because the prefetch is handed the boundary
    # directly and the turn reaches it through `state`.
    resolutions = re.findall(r"_previous = await _slot_predecessor_store\([^)]*\)", flat)
    assert resolutions == [
        "_previous = await _slot_predecessor_store(sessions, slot, session_key)",
        "_previous = await _slot_predecessor_store(state.sessions, slot, session_key)",
    ], f"the predecessor is resolved somewhere unexpected: {resolutions}"
    # The store is the authority and the mapping is the fallback, read non-pruning,
    # inside that one helper -- so this ratchet pins one resolution rather than one
    # per call site.
    resolver = source[source.index("async def _slot_predecessor_store(") :]
    resolver = resolver[: resolver.index("\ndef ")]
    assert "await asyncio.to_thread( crew_log_emit.slot_previous_store, slot.key )" in " ".join(
        resolver.split()
    )
    # The mapping serves a DECIDED empty only, and comes back FLAGGED rather than
    # cited: whether allocation is holding the prior resumable id back cannot be read
    # here, because the marker belongs to a session this runs before. The undecided
    # case names no store while SAYING so, rather than citing the source the store read
    # was preferred over or passing for a log that has no predecessor at all. And the
    # absence is STATED only when the store's answer is complete -- units it could not
    # rank make an empty mapping no finding about this slot.
    assert "undecided=False if complete else None," in resolver
    assert 'return CrewLogPrevious(sid="", undecided=True)' in resolver
    assert "provider_switch_replay_pending" not in resolver, (
        "the replay window is decided in the resolver again, which runs before this "
        "turn's session exists -- so a cold start reads 'no replay owed' from there "
        "being nobody to ask, and cites the generation the mapping is holding"
    )
    # Spent exactly once, at the emitter call, which is also where the slot is told
    # which store it is now on. A second consumer would hand the same edge to two
    # entries; none would leave it for the slot's next store. The record is what names
    # a store whose unit is still queued to the writer thread, so dropping it here
    # reopens that window.
    #
    # Read off the FLATTENED source for the same reason the latches are: whether the
    # call fits on one line is the formatter's business.
    takes = [
        call.replace("( ", "(").replace(" )", ")")
        for call in re.findall(r"slot\.take_crew_log_previous\([^)]*\)", flat)
    ]
    assert takes == [
        "slot.take_crew_log_previous(now_writing=_crew_log_sid, "
        "replay_pending=_crew_log_replay_owed)"
    ], f"the predecessor edge is consumed somewhere unexpected: {takes}"
    # The window is asked about HERE, where a session exists to answer. Asked at the
    # latch instead, a cold start is told "no replay owed" by the absence of anybody to
    # ask, and that is the case where the mapping is most likely a generation behind.
    assert (
        "_crew_log_replay_owed = state.sessions.provider_switch_replay_pending(session_key) "
        "is True" in flat
    )
    # BOTH halves reach the entry from that one handover. Feeding the emitter the sid
    # alone would write a log that claims to start the slot's chain while its
    # predecessor was merely undetermined.
    assert "previous_sid=_crew_log_edge.sid," in source
    assert "previous_undecided=_crew_log_edge.undecided," in source


def test_the_predecessor_is_read_without_pruning_the_mapping():
    """The turn path reads the mapped id, not the resumable one.

    `SessionMap.get` answers "can this id still be resumed", so it stats the ACP
    transcript and PRUNES the entry when that file is gone or empty. Both are
    wrong for a history citation: the stat is synchronous store work on the
    gateway loop, and the prune erases the id exactly when the two stores
    disagree -- a crew log unit can outlive a truncated ACP transcript, and that
    unit is the one whose tail most needs closing.

    Mutation guard: routing the turn path back through `resumable_sid` answers
    None here, so the successor records no edge and the predecessor's tail is
    never closed.
    """
    mapping = session_map.SessionMap()
    mapping.set("dashboard:chat-7", SESSION, provider="", cwd="")

    # The history read answers, and it is read FIRST because that is the order the
    # turn path uses -- nothing has pruned the entry yet.
    assert mapping.mapped_sid("dashboard:chat-7") == SESSION

    # The resumable read, on the same live entry, answers None AND removes it:
    # there is no ACP transcript on disk for that id.
    assert mapping.get("dashboard:chat-7") is None
    assert (
        mapping.has_hint("dashboard:chat-7") is False
    ), "get() pruned the entry, which is why the history read must not go through it"


def test_the_mapped_read_touches_no_file():
    """Safe on the gateway loop because it reads memory, not the store.

    An unmapped key answers "" rather than raising, since the emitter turns that
    into an absent edge: a slot's first ever session has nothing to name.
    """
    mapping = session_map.SessionMap()
    mapping.set("dashboard:chat-7", SESSION, provider="", cwd="")

    def _no_disk(*_args, **_kwargs):
        raise AssertionError("mapped_sid touched the filesystem")

    # Patched AFTER construction, which loads the map from disk by design.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "exists", _no_disk)
        patch.setattr(Path, "stat", _no_disk)
        assert mapping.mapped_sid("dashboard:chat-7") == SESSION
        assert mapping.mapped_sid("dashboard:never-seen") == ""


def test_a_store_whose_front_retention_removed_reports_no_edge():
    """`null` is "no edge to follow", never "there was no earlier crew log".

    The edge rides on the creating `session/opened`, which lives at the front of
    the file, and retention deletes whole segments off the front. A reader that
    read a missing edge as "this is the slot's first crew log" would silently
    claim a slot's history began at the oldest segment that survived.
    """
    _open_session()
    assert emit.flush()
    _open_successor(previous_sid=SESSION)
    assert emit.flush()
    assert crew_log_projection.read_projection(SUCCESSOR, "status").value["previous"] == SESSION

    _remove_as_retention_does(_log_path(SUCCESSOR).unlink)
    folded = crew_log_projection.read_projection(SUCCESSOR, "status").value
    assert folded["previous"] is None
    assert folded["lifecycle"] == "unknown", "and the fold says it could not read an opener"


# --- the failure warning budget -------------------------------------------


def test_every_report_call_site_names_a_literal_operation():
    """Enumerated from the source, so a new call site is covered by existing.

    ``op`` is the only part of a call that reaches the warning budget's key, which is
    what keeps the map bounded and keeps one cause across many stores to one warning.
    A site passing an f-string or a variable there would put a store name, a session
    id or an entry type into the key and hand every unit its own warning. The
    population is read out of the two modules that report -- the emitter's ``_report``
    calls and the writer's ``.report`` calls on its budget -- rather than listed here,
    because a listed set of sites goes stale the moment someone adds one.
    """
    import ast

    from kiro_crew.crew_log import writer as writer_mod

    sites = []
    for module, wanted in ((emit, ast.Name), (writer_mod, ast.Attribute)):
        glob = "emit*.py" if module is emit else Path(module.__file__).name
        paths = sorted(Path(module.__file__).parent.glob(glob))
        trees = [ast.parse(p.read_text(encoding="utf-8")) for p in paths]
        for node in (n for tree in trees for n in ast.walk(tree)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, wanted):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            if name in ("_report", "report"):
                sites.append((module.__name__, node))
    assert {m for m, _ in sites} == {
        emit.__name__,
        writer_mod.__name__,
    }, "a module's report sites were not found -- the check would pass vacuously"
    offenders = []
    for module_name, node in sites:
        passed = {kw.arg: kw.value for kw in node.keywords}
        op = passed.get("op")
        if not isinstance(op, ast.Constant) or not isinstance(op.value, str) or not op.value:
            offenders.append((module_name, node.lineno, ast.unparse(node)[:90]))
    assert (
        not offenders
    ), f"{len(offenders)} of {len(sites)} report sites do not name a literal op: {offenders}"
