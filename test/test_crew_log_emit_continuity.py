"""Live turns under eviction, resume, claims and teardown, and the `previous` edge.

It pins that eviction, a reconnect or a warm reuse never closes an open turn, that only a
resume repairs one, and that a resume leaves alone a turn this process is still running; how
a claim and a teardown hold or release the log's write ownership; what a dispatched child
records and how child pins are capped; that a refused turn writes no start; that call-index
numbering survives cache pressure; and which earlier store of the same slot a newly created
log names as `previous`. The repair of a superseded log's tail and the chain walk are pinned
in `test_crew_log_emit.py`, and the writer in `test_crew_log_emit_write_path.py`.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import threading

import pytest
from crew_log_emit_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    SESSION,
    SUCCESSOR,
    _body,
    _can_take_ownership,
    _isolated_home,
    _log_path,
    _open_session,
    _open_successor,
    _owned_elsewhere,
    _remove_as_retention_does,
)

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import emit
from kiro_crew.crew_log import projection as crew_log_projection

# --- cache pressure -------------------------------------------------------


def test_cache_pressure_never_closes_a_live_turn(monkeypatch):
    """What a bounded LRU of open handles must not do to a running turn.

    With more concurrent crew logs than the cache holds, evicting a live session's
    handle makes the next emit in that same turn reopen the crew log -- and an open
    that repaired would append a false `turn/completed {interrupted}` into a turn
    that then keeps writing. Two things prevent it: repair is opt-in and only a
    resume asks for it, and a session with a turn in flight is not evicted at all.
    """
    _open_session()
    emit.on_turn_started(SESSION, 7, "user")
    emit.on_tool_called(SESSION, 7, name="fs_read", call_id="tc-1")

    # Enough other sessions to overrun the cache several times over.
    for n in range(emit._MAX_OPEN_CREW_LOGS + 1):
        other = f"filler-{n:04d}"
        emit.on_session_opened(other, agent="kirocrew")
        emit.on_turn_started(other, 1, "user")
        emit.on_turn_completed(other, 1, stop_reason="end_turn")
    assert emit.flush()

    # The live session survived the pressure ...
    assert SESSION in emit._open
    # ... nothing closed its turn ...
    types = [e["type"] for e in _body()]
    assert "turn/completed" not in types
    assert types.count("tool/called") == 1
    # ... and its next entry still names the turn it belongs to.
    emit.on_tool_completed(SESSION, 7, status="completed", call_id="tc-1")
    assert emit.flush()
    last = _body()[-1]
    assert last["type"] == "tool/completed"
    assert last["data"]["turn"] == 7


def test_a_finished_session_is_evicted_so_the_cache_stays_bounded():
    # Pinning must not turn the cap into a leak: a session whose turn ended is
    # evictable again, which is what keeps a long-lived gateway bounded.
    for n in range(emit._MAX_OPEN_CREW_LOGS + 8):
        other = f"done-{n:04d}"
        emit.on_session_opened(other, agent="kirocrew")
        emit.on_turn_started(other, 1, "user")
        emit.on_turn_completed(other, 1, stop_reason="end_turn")
    assert emit.flush()
    assert len(emit._open) <= emit._MAX_OPEN_CREW_LOGS


def test_a_reconnect_after_an_eviction_does_not_repair(monkeypatch):
    # The reconnect path directly: a handle absent from the cache says nothing
    # about the writer's health, so reopening must not close anything.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.flush()
    with emit._lock:
        emit._open.clear()  # simulate the eviction the cap would have made
    emit.on_tool_completed(SESSION, 1, status="completed", call_id="tc-1")
    assert emit.flush()
    types = [e["type"] for e in _body()]
    assert "turn/completed" not in types
    assert types[-1] == "tool/completed"


def test_a_resume_closes_a_child_the_registry_no_longer_lists():
    # The reachability the registration exists for. A crashed gateway's successor
    # has an empty registry, so a child that never reported is gone and its opener
    # is closed. Without a registered probe the store closes nothing, which is what
    # the next test pins -- so this asserts the wiring, not just the store.
    emit.set_child_liveness(lambda agent_id: False)
    try:
        _open_session()
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1")
        assert emit.flush()
        emit.reset_caches()
        emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
        assert emit.flush()
        closers = [e for e in _body() if e["type"] == "subagent/failed"]
        assert [e["data"]["agent_id"] for e in closers] == ["sub-1"]
        assert closers[0]["data"]["outcome"] == "unknown"
    finally:
        emit.set_child_liveness(None)


def test_a_resume_leaves_a_child_the_registry_still_lists_alone():
    # The same-process case that made an earlier revision corrupt the file: idle
    # teardown, reset, resume -- while the child is still running and will file its
    # own terminal. The registry still lists it, so the repair must not invent one.
    emit.set_child_liveness(lambda agent_id: agent_id == "sub-1")
    try:
        _open_session()
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1")
        assert emit.flush()
        emit.reset_caches()
        emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
        assert emit.flush()
        assert [e for e in _body() if e["type"] == "subagent/failed"] == []
    finally:
        emit.set_child_liveness(None)


def _closer(kind: str) -> dict:
    """The one closer of *kind* in the log, so a test reads its data directly."""
    closers = [e for e in _body() if e["type"] == kind]
    assert len(closers) == 1, f"expected exactly one {kind}, saw {len(closers)}"
    return closers[0]["data"]


def _opener(kind: str) -> dict:
    """The one opener of *kind* in the log, so a test reads its data directly."""
    openers = [e for e in _body() if e["type"] == kind]
    assert len(openers) == 1, f"expected exactly one {kind}, saw {len(openers)}"
    return openers[0]["data"]


def test_a_dispatch_records_the_task_the_child_was_asked_to_do():
    """The one field a card cannot rebuild from anything else in the entry.

    A surface drawing a finished child after the dispatching process is gone reads
    the task from the log or from nowhere.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1", task="audit the retry path")
    assert emit.flush()
    assert _opener("subagent/spawned")["task"] == "audit the retry path"


def test_a_dispatch_with_no_task_text_writes_no_task_key_at_all():
    """Absent means "this log does not say", and an empty string would not.

    It is also how every log written before the field reads, so the two are the
    same case to a reader: a card draws no task line rather than a blank one.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1", task="")
    assert emit.flush()
    assert "task" not in _opener("subagent/spawned")


def test_the_task_text_is_clipped_on_the_same_terms_as_a_plan_item():
    """A person's words, bounded so one field cannot dominate the line.

    Same helper and same cap ``plan/updated``'s item text uses -- the other place
    this module records text a person wrote -- and the ellipsis is part of the
    value, so a reader can tell a task that ends there from one that was cut.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1", task="t" * (emit._MAX_SHORT_TEXT + 50))
    assert emit.flush()
    written = _opener("subagent/spawned")["task"]
    assert len(written) == emit._MAX_SHORT_TEXT
    assert written.endswith("\u2026")


def test_a_credential_in_the_task_text_never_reaches_the_log():
    """Redacted at this boundary, not trusted from the call site.

    The task is raw input a person just typed, so the rule has to hold here: a
    site added later cannot forget it.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(
        SESSION, 1, agent_id="sub-1", task="deploy with AKIAIOSFODNN7EXAMPLE now"
    )
    assert emit.flush()
    assert "AKIAIOSFODNN7EXAMPLE" not in _opener("subagent/spawned")["task"]


def test_a_completed_child_records_the_credits_it_billed():
    """A child's spend is measured, so the parent's log carries it.

    ``SubagentInfo.credits`` accumulates the charge across every attempted turn, and
    the parent's log is the durable record of it: the runtime's own copy dies with
    the process.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1")
    emit.on_subagent_completed(SESSION, agent_id="sub-1", duration_ms=7, credits=1.5)
    assert emit.flush()
    assert _closer("subagent/completed")["credits"] == 1.5


def test_a_child_billed_nothing_writes_no_credits_key_at_all():
    """Absent means unmetered, and zero would claim a measurement of zero.

    A provider that does not bill in credits reports 0.0 through the shared
    ``TurnUsage`` contract, which is indistinguishable at this seam from a run that
    was genuinely free. Writing the zero would present one as the other, so the key
    is dropped -- the same posture ``background/completed`` takes.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1")
    emit.on_subagent_completed(SESSION, agent_id="sub-1", duration_ms=7, credits=0.0)
    assert emit.flush()
    assert "credits" not in _closer("subagent/completed")


@pytest.mark.parametrize("outcome", ["failed", "stopped"])
def test_a_child_that_did_not_finish_still_records_what_it_spent(outcome):
    """A failed or stopped run billed for the turns it did attempt.

    The credits are cumulative across attempts, including billed retries that failed
    before the last one, so the non-success closer is exactly where the charge would
    otherwise be lost.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1")
    emit.on_subagent_failed(
        SESSION, agent_id="sub-1", reason="boom", outcome=outcome, duration_ms=3, credits=0.75
    )
    assert emit.flush()
    data = _closer("subagent/failed")
    assert data["outcome"] == outcome
    assert data["credits"] == 0.75


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_charge_is_not_written_at_all(bad):
    """``inf > 0`` is True, so the positive-only gate alone would write it.

    The charge arrives from a provider usage report. A non-finite one is not a
    measurement, and a reader that sums it has no way back: every later total is
    non-finite too. The writer refuses it rather than handing the fold a value the
    fold must then defend against.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1")
    emit.on_subagent_completed(SESSION, agent_id="sub-1", duration_ms=7, credits=bad)
    assert emit.flush()
    assert "credits" not in _closer("subagent/completed")


@pytest.mark.parametrize("outcome", ["failed", "stopped"])
def test_a_child_that_did_not_finish_and_billed_nothing_writes_no_credits(outcome):
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1")
    emit.on_subagent_failed(
        SESSION, agent_id="sub-1", reason="boom", outcome=outcome, duration_ms=3, credits=0.0
    )
    assert emit.flush()
    assert "credits" not in _closer("subagent/failed")


def test_the_child_probe_reports_present_while_this_session_owes_an_entry():
    # The window the registry alone cannot answer. A child's terminal closer goes
    # through the writer and `_submit` returns before it lands, so the child leaves
    # the running set while its own outcome is still owed: the registry says gone
    # and the file shows an unmatched opener. Closing then puts a synthesised
    # `unknown` ahead of the real outcome, and both stand in a file nothing
    # rewrites. The debt is keyed by session, so another session's backlog must not
    # hold this one's children open.
    #
    # The one writer thread is parked FIRST, inside a third session's append, so every
    # entry submitted afterwards waits in its bucket for as long as the test looks --
    # no batching window, backoff or failure decides what is owed.
    other = "other-session"
    held = "held-session"
    _open_session()
    emit.on_session_opened(other, agent="kirocrew", slot="chat-8")
    emit.on_session_opened(held, agent="kirocrew", slot="chat-9")
    assert emit.flush()
    real_append = lg.CrewLog.append
    holding = threading.Event()
    release = threading.Event()

    def _park_on_the_held_turn(self, entry_type, *args, **kwargs):
        if entry_type == "turn/started" and args and args[0].get("turn") == 9:
            holding.set()
            release.wait(timeout=10.0)
        return real_append(self, entry_type, *args, **kwargs)

    async def _owe(session_id: str, turn: int = 1) -> None:
        emit.on_turn_started(session_id, turn, "user")

    emit.set_child_liveness(lambda agent_id: False)
    try:
        gone = emit._child_gone_probe(SESSION)
        assert gone is not None
        assert gone("sub-1") is True
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(lg.CrewLog, "append", _park_on_the_held_turn)
            asyncio.run(_owe(held, 9))
            assert holding.wait(timeout=10.0), "the writer never reached the held entry"
            asyncio.run(_owe(other))
            assert gone("sub-1") is True, "another session's debt held this one's child open"
            asyncio.run(_owe(SESSION))
            assert gone("sub-1") is False, "a child was reported gone while its session owed"
            release.set()
            assert emit.flush(timeout=10.0)
        assert gone("sub-1") is True, "the debt outlived the entry that landed"
    finally:
        release.set()
        emit.set_child_liveness(None)


def test_the_pin_cap_drops_a_finished_child_before_a_running_one():
    # A pin is released by its child's terminal entry, so pins for children that
    # never reported one collect at the old end -- and they are what makes this cap
    # reachable by accumulation rather than by that many children genuinely running.
    # The finished one goes first, and no running child's attribution pays for it.
    emit.set_child_liveness(lambda agent_id: agent_id != "gone-1")
    try:
        emit.remember_child_origin("gone-1", SESSION, 1)
        for i in range(2, emit._MAX_CHILD_ORIGINS + 2):
            emit.remember_child_origin(f"live-{i}", SESSION, i)
        assert "gone-1" not in emit._child_origin, "the finished child's pin is the one dropped"
        assert "live-2" in emit._child_origin, "the oldest RUNNING child keeps its pin"
        assert emit.lost_child_origins() == 0, "so nothing live was spent"
    finally:
        emit.set_child_liveness(None)


def test_a_cap_full_of_running_children_drops_the_oldest_and_counts_the_loss():
    # The residual this policy leaves: with every pin belonging to a child that is
    # still running there is nothing free to drop, so the oldest goes. That child's
    # remaining entries will be absent, which is exactly why the drop is counted
    # instead of left for a reader to fail to notice.
    emit.set_child_liveness(lambda agent_id: True)
    try:
        for i in range(emit._MAX_CHILD_ORIGINS + 1):
            emit.remember_child_origin(f"live-{i}", SESSION, i + 1)
        assert emit.lost_child_origins() == 1
        assert "live-0" not in emit._child_origin
        assert "live-1" in emit._child_origin
    finally:
        emit.set_child_liveness(None)


def test_the_cap_counts_what_it_drops_even_with_no_probe_registered():
    # With nothing able to say whether a child finished, the cap cannot prefer a
    # dead pin. That is a reason to drop the oldest, not a reason to do it silently.
    for i in range(emit._MAX_CHILD_ORIGINS + 1):
        emit.remember_child_origin(f"c-{i}", SESSION, i + 1)
    assert emit.lost_child_origins() == 1
    assert "c-0" not in emit._child_origin


def test_an_over_long_session_id_is_refused_and_counted():
    # The count cap bounds memory only if a pin's own fields are bounded, and the
    # session id comes from the provider. It is refused rather than shortened,
    # because an identity that has been cut down names a different unit -- and the
    # refusal is counted, because that child's entries are absent either way.
    emit.remember_child_origin("huge-1", "s" * (emit._MAX_SESSION_ID_CHARS + 1), 1)
    assert "huge-1" not in emit._child_origin, "an unbounded id is not retained"
    assert emit.lost_child_origins() == 1, "and the refusal is not silent"
    # Exactly at the bound is still retained, so it rejects nothing legitimate.
    emit.remember_child_origin("ok-1", "s" * emit._MAX_SESSION_ID_CHARS, 1)
    assert "ok-1" in emit._child_origin
    assert emit.lost_child_origins() == 1


def test_a_resume_with_no_registered_probe_closes_no_child():
    # The default every embedder and test gets. Nothing answers "is this child
    # alive", so the opener stands and a reader treats it as unknown.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1")
    assert emit.flush()
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
    assert emit.flush()
    assert [e for e in _body() if e["type"] == "subagent/failed"] == []


def test_a_resume_is_the_one_path_that_closes_an_open_turn():
    # `resumed=True` means this claim re-attached to a conversation a different
    # gateway process was writing, so a turn left open there belongs to a writer
    # that is gone -- the one situation where closing it records what happened.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
    assert emit.flush()
    closers = [e for e in _body() if e["type"] == "turn/completed"]
    assert len(closers) == 1
    assert closers[0]["data"] == {"turn": 1, "stop_reason": "interrupted"}


def test_a_resume_does_not_close_a_turn_this_process_is_still_running(caplog):
    """The resume flag is a belief; a live turn of our own is evidence against it.

    `resumed=True` says the writer of this file is gone. When this process is itself
    still running a turn for that id, that is false -- the writer is us. Repairing
    anyway closes a turn that is still producing entries, and the file then says the
    turn completed as interrupted and, further down, completed again for real, with
    the same tool call closed both `unknown` and `completed`. A fold reads two
    outcomes for one turn with no way to tell which happened.

    The legitimate resume is unaffected: a claim re-attaching to a conversation whose
    process is gone holds no live record, so it still repairs (see
    `test_a_resume_is_the_one_path_that_closes_an_open_turn`, which clears the caches
    to model exactly that).

    Mutation guard: repairing on the flag alone writes the interrupted closer here and
    reddens the count below.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.flush()
    # NO reset_caches: this process still holds turn 1 as live, which is the whole
    # point -- the claim below is contradicted by our own state.
    with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.emit"):
        emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
        assert emit.flush()

    body = _body()
    assert "turn/completed" not in [e["type"] for e in body], (
        "a turn this process is still running was closed by a resume; its remaining "
        "entries will follow an outcome it never had"
    )
    assert "tool/completed" not in [
        e["type"] for e in body
    ], "the live turn's open tool call was closed as unknown while it was still running"
    assert any(
        "is still running in this process" in r.getMessage() for r in caplog.records
    ), "the refusal to repair was silent"

    # And the turn still ends exactly once, on its own terms.
    emit.on_tool_completed(SESSION, 1, name="fs_read", call_id="tc-1", status="completed")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn", duration_ms=5)
    assert emit.flush()
    closers = [e for e in _body() if e["type"] == "turn/completed"]
    assert len(closers) == 1, f"the turn completed {len(closers)} times: {closers}"
    assert closers[0]["data"]["stop_reason"] == "end_turn"


def test_a_claim_that_cannot_own_the_log_writes_nothing_and_counts_the_loss():
    """A second gateway on a live session loses its own entries, not the log.

    A claim that cannot take a unit's write ownership has two honest options: write
    into a file whose owner is still producing entries, or write nothing and say
    so. This pins the second. The open turn is left as its owner has it, the
    entries this process could not write are counted in `dropped_writes`, and none
    of them is retried -- ownership is held for the life of the owning process,
    which no retry budget outlasts, and a retained batch would hold the rest of
    that session's log behind an entry that cannot land.

    The caches are cleared first so this process holds no live turn and no handle,
    which is the state a fresh gateway starts in: the same-process guard cannot
    answer here, and the kernel lock is the only thing that can.

    Mutation guard: dropping the claim from the store's write paths lets the repair
    close the owner's turn and reddens the first assertion.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.flush()
    before = [e["type"] for e in _body()]
    emit.reset_caches()

    with _owned_elsewhere():
        emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
        emit.on_message_received(SESSION, 1, role="user", text="hello")
        assert emit.flush()
        after = [e["type"] for e in _body()]
        assert after == before, f"a process that owns nothing wrote into the log: {after}"
        assert (
            emit.dropped_writes() == 2
        ), "the entries this process could not write are not counted"

    # Ownership released: the same emitter writes again, so the refusal was about
    # the moment rather than a state it latched.
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn", duration_ms=5)
    assert emit.flush()
    assert [e["type"] for e in _body()][-1] == "turn/completed"


def test_a_claim_does_not_take_back_a_pin_a_queued_closer_still_owes(monkeypatch):
    """A re-claim must leave a turn whose terminal event is still in flight.

    A terminal event is QUEUED, not written, so the pin that keeps its handle --
    and the log's write ownership with it -- is owed to the write job. A claim
    arriving in that window reads the live map, and a claim that closes every
    record it finds drops the pin while the file still shows the turn open:
    capacity eviction then takes the handle, the lease goes with the descriptor,
    and a successor process repairs a turn whose real completion is still on its
    way to disk. A record with no terminal handed over is different -- nothing
    else will ever close it -- and the claim still releases that one.

    The writer is held for the whole window, so the queued state is arranged
    rather than raced for: without that the background writer can land the closer
    and run its release before the claim is even made.

    Mutation guard: releasing every record regardless of an owed closer reddens
    the first assertion.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()

    release = threading.Event()
    original = lg.CrewLog.append

    def _held(self, *args, **kwargs):
        assert release.wait(20.0)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _held)

    # The terminal is handed over and cannot reach disk; the claim lands in between.
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn", duration_ms=5)
    _open_session()
    assert emit.live_turn(SESSION) == 1, (
        "a re-claim took back a pin its queued closer still owes, so eviction can "
        "release the log while the file still shows that turn open"
    )

    release.set()
    assert emit.flush(timeout=20.0)
    assert "turn/completed" in [e["type"] for e in _body()]
    assert emit.live_turn(SESSION) == 0, "the write job never released the pin it owed"


def test_a_claim_still_closes_a_turn_no_closer_is_coming_for():
    """The stale-record case the claim exists for is unchanged.

    A turn whose terminal event was never emitted has nothing that will release
    it, so a fresh claim -- a new gateway taking over the same session id -- must
    close it. Skipping this one too would leak the pin for the life of the
    process and pin write ownership to a turn no one is writing.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()

    _open_session()
    assert emit.live_turn(SESSION) == 0, "a claim left a turn nothing will ever close"


def test_a_teardown_mid_turn_keeps_the_write_ownership_that_turn_needs():
    """A forced reset must not hand the log away while a turn is still writing.

    The reset route tears a session down with a turn still running, and that turn's
    closers are written later by its own `finally`. The cached handle carries the
    unit's write ownership, so dropping it at teardown releases the log to whichever
    process asks next -- and a second gateway's resume then closes a turn that
    completes for real a moment later, which is the two-outcome record ownership
    exists to prevent. Asserted through the kernel: another owner must not be able
    to take the lock while the turn is live.

    Mutation guard: dropping the handle unconditionally at teardown lets the foreign
    owner take it and reddens the first assertion.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.flush()

    emit.on_session_closed(SESSION, "reset")
    assert emit.flush()
    assert not _can_take_ownership(), "a live turn's write ownership was released by its teardown"

    # And the turn still closes on its own terms, through the handle it kept.
    emit.on_tool_completed(SESSION, 1, name="fs_read", call_id="tc-1", status="completed")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn", duration_ms=5)
    assert emit.flush()
    types = [e["type"] for e in _body()]
    assert (
        types.count("turn/completed") == 1
    ), f"the turn completed {types.count('turn/completed')}x"
    assert types.index("session/closed") < types.index("turn/completed")


def test_a_teardown_between_turns_gives_the_write_ownership_back():
    """With no turn running there is nothing to protect, so the log is released.

    The other half of the rule: holding ownership past the teardown of a quiet
    session would refuse a successor gateway that has every right to the log.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn", duration_ms=5)
    assert emit.flush()

    emit.on_session_closed(SESSION, "reset")
    assert emit.flush()
    gc.collect()
    assert _can_take_ownership(), "a torn-down session with no live turn kept the log"


def test_a_warm_reuse_inside_this_process_closes_nothing():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew")  # resumed defaults False
    assert emit.flush()
    assert "turn/completed" not in [e["type"] for e in _body()]


def test_a_refused_turn_is_its_own_fact_and_writes_no_start():
    """A start asserts the turn RAN.

    A start written before the dispatch gates leaves an orphan for every refusal,
    and the interrupted-turn repair would later close it as though the turn had
    died mid-flight. The refusal is recorded instead, naming the gate.
    """
    _open_session()
    emit.on_turn_refused(SESSION, 4, "stopped_before_dispatch", "user")
    types = [e["type"] for e in _body()]
    assert "turn/started" not in types
    assert types[-1] == "turn/refused"
    assert _body()[-1]["data"] == {
        "turn": 4,
        "actor": "user",
        "reason": "stopped_before_dispatch",
        "depth": 0,
    }


def test_a_refusal_records_the_actor_it_would_have_run_as():
    _open_session()
    emit.on_turn_refused(SESSION, 1, "not_authorized", "cron")
    assert _body()[-1]["data"]["actor"] == "cron"


def test_a_refusal_leaves_nothing_pinned():
    # Nothing is in flight after a refusal, so the handle and the step ordinals
    # must become evictable again -- otherwise a refused turn leaks a pin.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.live_turn(SESSION) == 1
    emit.on_turn_refused(SESSION, 1, "gateway_closing", "user")
    assert emit.live_turn(SESSION) == 0


def test_call_index_of_a_live_turn_survives_cache_pressure():
    """A live turn's step counter is its own state, so pressure cannot reset it.

    Dropping it mid-turn restarts the numbering, so the turn's next tool call
    claims a position an earlier call in the same turn already holds.

    The pressure is built from ABORTED turns: a turn that ends releases its own
    record, so it can never crowd the map. A turn that minted a step and was then
    re-claimed is the residue a cap exists to trim -- and what must not take a
    live turn with it.
    """
    _open_session()
    emit.on_turn_started(SESSION, 3, "user")
    emit.on_tool_called(SESSION, 3, name="fs_read", call_id="tc-1")
    emit.on_tool_called(SESSION, 3, name="fs_read", call_id="tc-2")

    for n in range(emit._MAX_OPEN_CREW_LOGS + 1):
        other = f"stepfill-{n:04d}"
        emit.on_session_opened(other, agent="kirocrew")
        emit.on_turn_started(other, 1, "user")
        emit.on_tool_called(other, 1, name="fs_read", call_id="tc-x")
    assert emit.flush()
    live = sum(
        emit.live_turn(f"stepfill-{n:04d}") == 1 for n in range(emit._MAX_OPEN_CREW_LOGS + 1)
    )
    assert live > emit._MAX_OPEN_CREW_LOGS, "the map never came under pressure"

    assert emit.live_turn(SESSION) == 3
    emit.on_tool_called(SESSION, 3, name="fs_read", call_id="tc-3")
    assert emit.flush()
    steps = [e["data"]["call_index"] for e in _body() if e["type"] == "tool/called"]
    assert steps == [1, 2, 3], "the numbering restarted, so two calls share a step"


def test_call_index_stays_unique_when_many_turns_are_live_at_once():
    """More live turns than the handle cap, all still running.

    The oldest live turn is the one any oldest-first trim reaches first, so it is
    the one this drives: its step counter must survive, because a counter that
    restarts mid-turn hands two of that turn's calls the same ordinal. Keeping the
    counter in the same record as the pin is what makes that impossible -- one
    record cannot be half-dropped.
    """
    _open_session()
    emit.on_turn_started(SESSION, 7, "user")
    for call in range(3):
        emit.on_tool_called(SESSION, 7, name="fs_read", call_id=f"early-{call}")

    # Every one of these stays live: no completion, no re-claim.
    for n in range(emit._MAX_OPEN_CREW_LOGS * 2):
        other = f"livefill-{n:04d}"
        emit.on_session_opened(other, agent="kirocrew")
        emit.on_turn_started(other, 1, "user")
    assert emit.flush()
    assert emit.live_turn(SESSION) == 7, "the oldest live turn lost its pin"

    for call in range(3):
        emit.on_tool_called(SESSION, 7, name="fs_read", call_id=f"late-{call}")
    assert emit.flush()
    steps = [e["data"]["call_index"] for e in _body() if e["type"] == "tool/called"]
    assert steps == sorted(steps), f"ordinals went backwards: {steps}"
    assert len(steps) == len(set(steps)), f"two calls share an ordinal: {steps}"
    assert steps == [1, 2, 3, 4, 5, 6]


def test_call_index_is_released_when_the_turn_completes():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.live_turn(SESSION) == 1
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.live_turn(SESSION) == 0


# --- loss debt survives until its marker lands -----------------------------


# --- the live-turn cap never restarts a live turn's numbering --------------


# --- the cap overage is reported once, not per event -----------------------


# --------------------------------------------------------------------------- #
# A superseded crew log: the `previous` edge that names it
# --------------------------------------------------------------------------- #


def test_a_new_store_for_a_slot_that_had_one_names_it_as_previous():
    """The edge `resumed` cannot express, because the successor is a different unit.

    A slot outlives its ACP session. When the session is torn down and the
    successor cold-starts under a new id, `CrewLog.exists` is false for that id,
    so the emitter CREATES a second crew log for one slot. `resumed` is correctly
    false there -- nothing re-attached -- and without this edge no field in either
    file says the two belong to the same slot.
    """
    _open_session()
    assert emit.flush()
    _open_successor(previous_sid=SESSION)
    assert emit.flush()

    opened = [e for e in _body(SUCCESSOR) if e["type"] == "session/opened"]
    assert len(opened) == 1
    assert opened[0]["data"]["previous"] == {"sid": SESSION}
    # Not redefined: the successor did not re-attach to anything.
    assert opened[0]["data"]["resumed"] is False
    # The edge is written on the SUCCESSOR only. The superseded log was written by
    # a session that never learns its successor's id, the same asymmetry `parent`
    # is recorded on the child for.
    assert "previous" not in _body(SESSION)[0]["data"]


def test_a_slots_first_store_names_no_previous():
    """Absent, not empty: "first crew log" has to be readable as its own case.

    A `previous` carrying an empty sid would read as an earlier crew log with an
    empty name, and a chain walker would try to open it.
    """
    _open_session()
    assert emit.flush()
    assert "previous" not in _body(SESSION)[0]["data"]


def test_a_resume_of_the_same_store_writes_no_previous_edge():
    """A store cannot be its own predecessor, and the emitter decides that here.

    The caller passes the id the slot was MAPPED to, which on the resume path is
    this session itself. Normally that is harmless because a resume re-attaches
    and never creates. The case that needs the comparison is a resumed session
    whose crew log is GONE: retention removes whole crew logs, so `session/load`
    can succeed while `CrewLog.exists` is false, and the emitter then creates a
    second crew log under the SAME id it was handed as the predecessor. Taking the
    caller's value on trust writes `previous {sid: <self>}` there, and a chain
    walker revisits the store it started from.

    Mutation guard: dropping the `previous_sid != session_id` comparison writes a
    self-edge here.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()

    # Retention's effect on this unit, without waiting for retention.
    _remove_as_retention_does(_log_path(SESSION).unlink)
    emit.reset_caches()
    assert not lg.CrewLog.exists(lg.KIND_SESSION, SESSION)

    emit.on_session_opened(
        SESSION, agent="kirocrew", slot="chat-7", resumed=True, previous_sid=SESSION
    )
    assert emit.flush()

    opened = [e for e in _body(SESSION) if e["type"] == "session/opened"]
    assert len(opened) == 1, "the re-created crew log carries exactly one opener"
    assert "previous" not in opened[0]["data"]
    # And a reader stepping back from it is not sent to itself.
    assert crew_log_projection.read_projection(SESSION, "status").value["previous"] is None


def test_a_warm_reuse_of_a_live_store_writes_nothing_at_all():
    """The silent case is still silent, edge or no edge.

    Every turn calls this, and a warm reuse of a crew log this process already
    holds has nothing new to say. A `previous_sid` arriving on such a turn (the
    map names this same session, which is the ordinary steady state) must not turn
    a silent claim into a second opener.
    """
    _open_session()
    assert emit.flush()
    before = len(_body(SESSION))

    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", previous_sid=SESSION)
    assert emit.flush()
    assert len(_body(SESSION)) == before, "a warm reuse appended something"


def test_a_re_attach_names_no_predecessor():
    """Only a CREATE may name a predecessor, because only a create superseded one.

    A re-attach already has its store, so a unit the caller names alongside it is
    either that same store or an unrelated one. Writing the edge there would claim
    a supersede that did not happen, and point a chain walker at a store this slot
    never wrote.

    Mutation guard: dropping `created` from the latch condition writes the edge
    here.
    """
    # A bystander with work in flight, on a different slot.
    bystander = "acp-sess-bystander"
    emit.on_session_opened(bystander, agent="kirocrew", slot="chat-9")
    emit.on_turn_started(bystander, 1, "user")
    assert emit.flush()

    _open_session()
    assert emit.flush()
    emit.reset_caches()
    # A re-attach to a store that EXISTS, handed an unrelated unit as predecessor.
    emit.on_session_opened(
        SESSION, agent="kirocrew", slot="chat-7", resumed=True, previous_sid=bystander
    )
    assert emit.flush()

    for entry in [e for e in _body(SESSION) if e["type"] == "session/opened"]:
        assert "previous" not in entry["data"]
    assert [
        e for e in _body(bystander) if e["type"] == "turn/completed"
    ] == [], "the bystander's live turn was closed by another session's re-attach"


def test_a_candidate_from_another_slot_is_not_linked():
    """`previous` means the SAME slot, so the candidate's own header must say so.

    The id arrives from the slot-to-session mapping, a persisted file whose entry
    can be stale or recycled by the time a successor cold-starts. Comparing that
    mapping against itself proves nothing, so the check reads the candidate crew
    log's own header -- written once at create, never rewritten -- and links only a
    candidate that names this slot. Otherwise a reader following the edge lands in
    a crew log this slot never wrote.

    Mutation guard: removing the check links the other slot's crew log here.
    """
    # A crew log belonging to a DIFFERENT slot, fully written.
    other = "acp-sess-other-slot"
    emit.on_session_opened(other, agent="kirocrew", slot="chat-9", cwd="/home/dev/project")
    assert emit.flush()

    _open_successor(previous_sid=other)
    assert emit.flush()

    opened = [e for e in _body(SUCCESSOR) if e["type"] == "session/opened"]
    assert len(opened) == 1
    assert "previous" not in opened[0]["data"], "a crew log from another slot was linked"


def test_a_candidate_whose_crew_log_is_gone_is_not_linked_and_costs_no_entry():
    """Unverifiable is not the same as verified, and the entry still lands.

    Retention removes whole crew logs, so a successor can name a predecessor whose
    header is unreadable. That candidate is not KNOWN to be this slot's, so
    no edge is written -- ending the walk one link early beats sending a reader
    somewhere unverified. The successor's own record is what this job owes, and the
    refusal must not take it down too.
    """
    _open_successor(previous_sid="acp-sess-never-existed")
    assert emit.flush()

    opened = [e for e in _body(SUCCESSOR) if e["type"] == "session/opened"]
    assert len(opened) == 1
    assert "previous" not in opened[0]["data"]


def test_the_previous_edge_survives_a_retry_that_finds_the_header_already_written():
    """Latched with the create decision, for the same reason that one is.

    A retry after the header landed but the entry did not reads `exists` true and
    `created` false. An unlatched edge would be dropped exactly there, silently
    and permanently, while the entry it belongs to still gets written.

    Mutation guard: reading `created` instead of the latch drops `previous` here.
    """
    _open_session()
    assert emit.flush()

    calls = {"n": 0}
    real_append = lg.CrewLog.append

    def _fail_the_first_append(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1 and self.id == SUCCESSOR:
            raise OSError("the entry did not land, the header did")
        return real_append(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _fail_the_first_append)
        _open_successor(previous_sid=SESSION)
        assert emit.flush()
        assert emit.flush()

    opened = [e for e in _body(SUCCESSOR) if e["type"] == "session/opened"]
    assert len(opened) == 1, "the retried entry landed more or less than once"
    assert opened[0]["data"]["previous"] == {"sid": SESSION}


def test_a_reader_joins_both_stores_of_one_slot_through_the_status_fold():
    """The read side: one slot's history, joined across the supersede boundary.

    Each fold reads ONE crew log, which is what the routes and the crew-log MCP
    server address. `previous` is what lets a reader that wants the SLOT rather
    than the session step from the newest crew log to the one before it, one fold
    at a time, through the projection route that already exists.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    # The predecessor's ACP session is gone before the successor opens, so this
    # process holds none of its live state -- a gateway restart's shape. A live
    # record left standing would mean a turn still RUNNING, which the repair
    # correctly declines to close.
    emit.reset_caches()
    _open_successor(previous_sid=SESSION)
    emit.on_turn_started(SUCCESSOR, 1, "user")
    emit.on_turn_completed(SUCCESSOR, 1, stop_reason="end_turn")
    assert emit.flush()

    newest = crew_log_projection.read_projection(SUCCESSOR, "status").value
    assert newest["slot"] == "chat-7"
    assert newest["turn_open"] is False
    # The edge a reader follows to reach the rest of this slot's history.
    assert newest["previous"] == SESSION

    older = crew_log_projection.read_projection(newest["previous"], "status").value
    assert older["slot"] == "chat-7", "one slot, two crew logs"
    assert older["previous"] is None, "the oldest crew log ends the walk"
    # The superseded turn is CLOSED. The edge is a citation and the repair is a
    # separate job, queued into the predecessor's own write order -- so a reader
    # joining the slot sees both crew logs and an ended turn in each, and can tell
    # the older one's last turn was interrupted rather than still running.
    assert older["turn_open"] is False
