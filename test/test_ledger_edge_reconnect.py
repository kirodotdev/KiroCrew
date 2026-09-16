"""Edge-case tests: ACP disconnect, reconnect and resume racing a live writer.

Covers the contract from ledger-core.md:

    Repair is opt-in, and only a RESUME may ask for it. A live writer
    RECONNECTING to its own ledger must NOT have its open turn closed,
    because its turn is still running and a `turn/completed {interrupted}`
    landing mid-turn would claim an outcome the turn never had and then
    be followed by more of that turn's entries.

These tests DO NOT overlap with the two existing tests:
  - test_a_reconnect_after_an_eviction_does_not_repair
  - test_a_resume_is_the_one_path_that_closes_an_open_turn
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import ledger as lg
from kiro_crew import session_ledger_emit as emit

SESSION = "reconnect-edge-001"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "1")
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    emit.reset_caches()
    yield
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()


def _ledger_path(session_id: str = SESSION) -> Path:
    return lg.ledger_path("session", session_id)


def _entries(session_id: str = SESSION) -> list[dict]:
    path = _ledger_path(session_id)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _body(session_id: str = SESSION) -> list[dict]:
    return _entries(session_id)[1:]


def _open_session(session_id: str = SESSION) -> None:
    emit.on_session_opened(
        session_id,
        agent="kirocrew",
        slot="chat-7",
        model="claude-opus-5",
        cwd="/home/dev/project",
        owner="default",
    )


# ---------------------------------------------------------------------------
# 1. Reconnect in the MIDDLE of a live turn: no closers, contiguous seq.
# ---------------------------------------------------------------------------


def test_reconnect_mid_turn_writes_no_closers_and_seq_stays_contiguous():
    """A reconnect happens because a handle was evicted from the bounded cache.

    Entries were already written for this turn, and more are coming. The reconnect
    must not produce a turn/completed or tool/completed{unknown} -- those would
    claim an outcome the turn never had. And the entries that follow must carry
    contiguous seq, proving no gap was introduced by the reopen.
    """
    _open_session()
    emit.on_turn_started(SESSION, 5, "user")
    emit.on_tool_called(SESSION, 5, name="fs_read", call_id="tc-1")
    emit.on_tool_called(SESSION, 5, name="fs_write", call_id="tc-2")
    assert emit.flush()

    pre_reconnect = _body()
    pre_seqs = [e["seq"] for e in pre_reconnect]

    # Simulate a cache eviction -- the handle disappears, forcing a reopen.
    with emit._lock:
        emit._open.pop(SESSION, None)

    # More entries for the same turn, post-reconnect.
    emit.on_tool_completed(SESSION, 5, status="completed", call_id="tc-1")
    emit.on_tool_completed(SESSION, 5, status="completed", call_id="tc-2")
    emit.on_turn_completed(SESSION, 5, stop_reason="end_turn")
    assert emit.flush()

    body = _body()

    # No spurious closers were injected at the reconnect point.
    interrupted = [
        e
        for e in body
        if e["type"] == "turn/completed" and e["data"].get("stop_reason") == "interrupted"
    ]
    assert not interrupted, f"reconnect injected turn/completed{{interrupted}}: {interrupted}"

    # Seq is contiguous ACROSS the reconnect: the reopen continued the run rather
    # than restarting it or leaving a gap.
    seqs = [e["seq"] for e in body]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"seq has a gap: {seqs}"
    assert seqs[: len(pre_seqs)] == pre_seqs, "the reconnect rewrote entries already on disk"

    # Exactly one turn/completed, the one we explicitly wrote.
    completed = [e for e in body if e["type"] == "turn/completed"]
    assert len(completed) == 1
    assert completed[0]["data"]["stop_reason"] == "end_turn"

    # seq is contiguous from 1 through the whole body.
    seqs = [e["seq"] for e in body]
    assert seqs == list(range(1, len(body) + 1)), f"seq gap after reconnect: {seqs}"

    # The tool completions name the right turn.
    for e in body:
        if e["type"] in ("tool/called", "tool/completed", "turn/completed"):
            assert e["data"]["turn"] == 5, f"{e['type']} names wrong turn: {e['data']}"


# ---------------------------------------------------------------------------
# 2. Resume with repair=True while the writer is still live.
#    The design admits nothing in the file can distinguish these.
# ---------------------------------------------------------------------------


def test_resume_with_repair_while_writer_still_live_produces_closers():
    """The design says nothing in the file distinguishes an open turn from a dead
    one; only the caller's situation can. A resume passes repair=True because it
    BELIEVES the writer is gone. If it is wrong -- the live writer is still active
    -- the repair will close the open turn, and the live writer's next entries
    land AFTER the turn/completed{interrupted}.

    This test documents what actually happens in that race. A reader folding the
    log would see a turn that completed (interrupted) and then MORE entries for
    that same turn ordinal with no new turn/started -- a sequence no live writer
    produces. This is a REAL HAZARD the design admits as an accepted residual:
    only the caller's situation can tell the two apart.
    """
    _open_session()
    emit.on_turn_started(SESSION, 3, "user")
    emit.on_tool_called(SESSION, 3, name="fs_read", call_id="tc-1")
    assert emit.flush()

    # A second process resumes the same session, believing the writer is gone.
    # This is the only code path that passes repair=True.
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", resumed=True)
    assert emit.flush()

    # The repair DID close the open turn and the open tool call.
    body = _body()
    interrupted = [
        e
        for e in body
        if e["type"] == "turn/completed" and e["data"].get("stop_reason") == "interrupted"
    ]
    assert len(interrupted) == 1, "resume did not repair the open turn"

    tool_closers = [
        e for e in body if e["type"] == "tool/completed" and e["data"].get("status") == "unknown"
    ]
    assert len(tool_closers) == 1, "resume did not close the open tool call"

    # Now simulate the "live writer" continuing to emit entries for turn 3.
    emit.on_tool_completed(SESSION, 3, status="completed", call_id="tc-1")
    emit.on_turn_completed(SESSION, 3, stop_reason="end_turn")
    assert emit.flush()

    body = _body()
    # HAZARD: turn 3 now has a turn/completed{interrupted} followed by more
    # entries and then a turn/completed{end_turn} -- a turn that completed twice.
    turn3_completions = [
        e for e in body if e["type"] == "turn/completed" and e["data"]["turn"] == 3
    ]
    assert len(turn3_completions) == 2, (
        f"expected the doubly-completed turn; got {len(turn3_completions)} "
        f"turn/completed entries for turn 3"
    )
    reasons = [e["data"]["stop_reason"] for e in turn3_completions]
    assert "interrupted" in reasons
    assert "end_turn" in reasons

    # The tool/completed{unknown} from repair AND the tool/completed{completed}
    # from the live writer both appear -- same call_id, two different statuses.
    tc1_completions = [
        e for e in body if e["type"] == "tool/completed" and e["data"].get("call_id") == "tc-1"
    ]
    assert (
        len(tc1_completions) == 2
    ), f"expected two completions for tc-1; got {len(tc1_completions)}"
    statuses = sorted(e["data"]["status"] for e in tc1_completions)
    assert statuses == ["completed", "unknown"], f"unexpected statuses for tc-1: {statuses}"

    # seq is still contiguous -- the repair's entries and the live writer's
    # entries are both valid appends.
    seqs = [e["seq"] for e in body]
    assert seqs == list(range(1, len(body) + 1)), f"seq gap: {seqs}"


# ---------------------------------------------------------------------------
# 3. Eviction from the emitter's bounded cache mid-turn: entries still land,
#    call_index does not restart, no closer appears.
# ---------------------------------------------------------------------------


def test_eviction_mid_turn_entries_land_and_call_index_continues():
    """A handle evicted from the bounded cache does not lose the turn's state.

    After eviction the turn's entries must still land (no handle = reopen, not
    drop), the call_index must not restart (it lives in _live, not _open), and
    no closer must appear.
    """
    _open_session()
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_tool_called(SESSION, 2, name="a", call_id="c1")
    emit.on_tool_called(SESSION, 2, name="b", call_id="c2")
    assert emit.flush()

    # Evict only the handle, not the live-turn state.
    with emit._lock:
        emit._open.pop(SESSION, None)

    # More entries for the same turn.
    emit.on_tool_called(SESSION, 2, name="c", call_id="c3")
    emit.on_tool_completed(SESSION, 2, status="completed", call_id="c1")
    emit.on_tool_completed(SESSION, 2, status="completed", call_id="c2")
    emit.on_tool_completed(SESSION, 2, status="completed", call_id="c3")
    emit.on_turn_completed(SESSION, 2, stop_reason="end_turn")
    assert emit.flush()

    body = _body()

    # No interrupted closer was injected.
    interrupted = [
        e
        for e in body
        if e["type"] == "turn/completed" and e["data"].get("stop_reason") == "interrupted"
    ]
    assert not interrupted, f"eviction injected a closer: {interrupted}"

    # call_index is contiguous 1, 2, 3 -- no restart at the reconnect boundary.
    indices = [e["data"]["call_index"] for e in body if e["type"] == "tool/called"]
    assert indices == [1, 2, 3], f"call_index restarted after eviction: {indices}"

    # Every entry names turn 2.
    for e in body:
        if "turn" in e.get("data", {}):
            assert (
                e["data"]["turn"] == 2 or e["type"] == "session/opened"
            ), f"{e['type']} has wrong turn: {e['data']}"


# ---------------------------------------------------------------------------
# 4. Repeated reconnects (evict/reopen several times in one turn):
#    no duplicate closers accumulate, seq stays contiguous.
# ---------------------------------------------------------------------------


def test_repeated_reconnects_in_one_turn_no_duplicate_closers():
    """Evict and reopen SEVERAL times inside one turn.

    Each reconnect goes through _handle -> Ledger.open(repair=False). None of
    them must produce a closer, and the final seq must be contiguous.
    """
    _open_session()
    emit.on_turn_started(SESSION, 10, "user")
    assert emit.flush()

    for n in range(5):
        with emit._lock:
            emit._open.pop(SESSION, None)  # simulate eviction
        emit.on_tool_called(SESSION, 10, name=f"tool-{n}", call_id=f"tc-{n}")
        assert emit.flush()

    emit.on_turn_completed(SESSION, 10, stop_reason="end_turn")
    assert emit.flush()

    body = _body()

    # No turn/completed{interrupted} was injected by any reconnect.
    interrupted = [
        e
        for e in body
        if e["type"] == "turn/completed" and e["data"].get("stop_reason") == "interrupted"
    ]
    assert not interrupted, f"{len(interrupted)} spurious closer(s) from {5} reconnects"

    # Exactly one turn/completed, our explicit one.
    completions = [e for e in body if e["type"] == "turn/completed"]
    assert len(completions) == 1
    assert completions[0]["data"]["stop_reason"] == "end_turn"

    # seq is contiguous.
    seqs = [e["seq"] for e in body]
    assert seqs == list(range(1, len(body) + 1)), f"seq has a gap: {seqs}"

    # call_index increments across all reconnects.
    indices = [e["data"]["call_index"] for e in body if e["type"] == "tool/called"]
    assert indices == list(range(1, 6)), f"call_index: {indices}"


# ---------------------------------------------------------------------------
# 5. Reconnect AFTER the file was repaired by a resume: does the live writer's
#    next entry land coherently after the closers, or produce a double-complete?
# ---------------------------------------------------------------------------


def test_reconnect_after_resume_repair_produces_coherent_new_turn():
    """After a resume repairs an interrupted turn, the file has closers at the
    end. A new turn started by the resumed process and then reconnected (cache
    eviction) must land its entries coherently AFTER those closers, with
    contiguous seq and no second set of closers.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.flush()

    # Resume: repairs the interrupted turn.
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", resumed=True)
    assert emit.flush()

    # The repair wrote closers.
    body_after_repair = _body()
    assert any(
        e["type"] == "turn/completed" and e["data"].get("stop_reason") == "interrupted"
        for e in body_after_repair
    ), "resume did not repair"

    # Now the resumed process starts a new turn.
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_tool_called(SESSION, 2, name="fs_write", call_id="tc-2")
    assert emit.flush()

    # Reconnect mid-turn (eviction).
    with emit._lock:
        emit._open.pop(SESSION, None)

    emit.on_tool_completed(SESSION, 2, status="completed", call_id="tc-2")
    emit.on_turn_completed(SESSION, 2, stop_reason="end_turn")
    assert emit.flush()

    body = _body()

    # The old turn's repair closer and the new turn's entries are all present.
    turn1_completed = [e for e in body if e["type"] == "turn/completed" and e["data"]["turn"] == 1]
    turn2_completed = [e for e in body if e["type"] == "turn/completed" and e["data"]["turn"] == 2]
    assert len(turn1_completed) == 1, "turn 1 closer missing or duplicated"
    assert turn1_completed[0]["data"]["stop_reason"] == "interrupted"
    assert len(turn2_completed) == 1, "turn 2 closer missing or duplicated"
    assert turn2_completed[0]["data"]["stop_reason"] == "end_turn"

    # The reconnect in turn 2 added no extra closers.
    all_interrupted = [
        e
        for e in body
        if e["type"] == "turn/completed" and e["data"].get("stop_reason") == "interrupted"
    ]
    assert (
        len(all_interrupted) == 1
    ), f"reconnect after repair added extra closer(s): {len(all_interrupted)}"

    # seq is contiguous over the whole file.
    seqs = [e["seq"] for e in body]
    assert seqs == list(range(1, len(body) + 1)), f"seq gap: {seqs}"


def test_reconnect_after_resume_repair_live_writer_continues_old_turn():
    """The darker variant of test 5: the live writer's OLD turn was repaired by
    the resume, and then the live writer (which never stopped) emits MORE entries
    for that same turn.

    This is test 2's scenario (resume-while-live) followed by a reconnect. The
    live writer's entries land after the repair's closers, and a reconnect does
    not add a third set.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.flush()

    # Resume repairs the open turn.
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", resumed=True)
    assert emit.flush()

    # Live writer continues its turn (it never stopped), then gets evicted.
    emit.on_tool_completed(SESSION, 1, status="completed", call_id="tc-1")
    assert emit.flush()

    with emit._lock:
        emit._open.pop(SESSION, None)

    # Reconnect, then finish the turn.
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush()

    body = _body()

    # Turn 1 has both the repair closer and the live writer's closer.
    turn1_completed = [e for e in body if e["type"] == "turn/completed" and e["data"]["turn"] == 1]
    assert (
        len(turn1_completed) == 2
    ), f"expected doubly-completed turn 1; got {len(turn1_completed)}"
    reasons = sorted(e["data"]["stop_reason"] for e in turn1_completed)
    assert reasons == ["end_turn", "interrupted"]

    # The reconnect did NOT add a third closer.
    all_completed = [e for e in body if e["type"] == "turn/completed"]
    assert len(all_completed) == 2, f"reconnect added extra closer(s): {len(all_completed)}"

    # seq contiguous.
    seqs = [e["seq"] for e in body]
    assert seqs == list(range(1, len(body) + 1)), f"seq gap: {seqs}"
