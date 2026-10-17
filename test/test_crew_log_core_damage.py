"""Append-only crew log core: what a read and an open do with a damaged log.

A torn tail is truncated, an append still inside its fsync is never read, damaged
interior lines are skipped and left on disk, seq recovers from the newest valid
line off a bounded tail window, and an interrupted turn is closed on open by
deterministic closers that never write past damage.
"""

from __future__ import annotations

import json
import logging
import threading

import crew_log_core_helpers
import pytest
from crew_log_core_helpers import CREW, SESSION, _code, _crew, _log_bytes, _raises, _session

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, store

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_isolated_home = crew_log_core_helpers._isolated_home


# --- damage tolerance -----------------------------------------------------


def test_a_torn_last_line_is_truncated_on_open():
    crew = _crew()
    crew.append("item/opened", {"kept": True}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    intact = path.read_bytes()
    path.write_bytes(intact + b'{"type":"item/opened","seq":2,"tim')

    reopened = CrewLog.open(lg.KIND_CREW, CREW)

    assert path.read_bytes() == intact  # the crash artifact is gone, history is not
    assert reopened.last_seq == 1
    assert reopened.append("item/opened", {}, src="gateway").seq == 2


def _append_stuck_in_fsync(monkeypatch, crew):
    """Start an append that has written and flushed its line and is inside a failing fsync.

    Returns the event that lets the fsync fail and the appender thread. Until the event
    is set the line is in the file and the append lock is held, which is the window a
    reader without the lock would see a line the rollback then removes.
    """
    in_fsync, fail = threading.Event(), threading.Event()
    real_fsync = store.os.fsync

    def _fsync(fd: int) -> None:
        # Only the append's own fsync fails; the rollback's fsync is real, so the
        # failure is the definite "the line is gone" outcome a rollback promises.
        if threading.current_thread().name != "stuck-appender" or in_fsync.is_set():
            return real_fsync(fd)
        in_fsync.set()
        fail.wait(5)
        raise OSError(5, "simulated fsync failure")

    monkeypatch.setattr(store.os, "fsync", _fsync)

    def _append() -> None:
        with pytest.raises(OSError):
            crew.append("item/opened", {"kept": False}, src="gateway")

    appender = threading.Thread(target=_append, name="stuck-appender", daemon=True)
    appender.start()
    assert in_fsync.wait(5)
    return fail, appender


def _read_in_thread(read):
    """Run *read* on a thread; return (finished event, result box)."""
    done, box = threading.Event(), {}

    def _run() -> None:
        try:
            box["value"] = read()
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    return done, box


def test_an_open_does_not_see_an_append_whose_fsync_has_not_returned(monkeypatch):
    # No reader folds bytes the writer has not yet fsynced under its lock: the open's
    # tail read waits for the append, and the failed fsync's rollback is what it sees.
    crew = _crew()
    crew.append("item/opened", {"kept": True}, src="gateway")
    fail, appender = _append_stuck_in_fsync(monkeypatch, crew)
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    assert path.read_bytes().count(b'"seq":2') == 1  # the in-flight line is on disk

    done, box = _read_in_thread(lambda: CrewLog.open(lg.KIND_CREW, CREW).last_seq)
    # flake-ok: pre-existing 300 ms absence window while the stuck append holds the lock; moved verbatim, tracked for a de-flake pass
    assert not done.wait(0.3), "the open read the tail while the append held the lock"
    fail.set()
    appender.join(5)
    assert done.wait(5)

    assert box["value"] == 1


def test_a_held_handle_does_not_read_an_append_whose_fsync_has_not_returned(monkeypatch):
    # The same rule for a handle opened earlier: each read measures the newest
    # segment's end under the lock, so an in-flight line is never yielded.
    crew = _crew()
    crew.append("item/opened", {"kept": True}, src="gateway")
    held = CrewLog.open(lg.KIND_CREW, CREW)
    fail, appender = _append_stuck_in_fsync(monkeypatch, crew)

    done, box = _read_in_thread(lambda: [entry.seq for entry in held.iter_from(1)])
    # flake-ok: pre-existing 300 ms absence window while the stuck append holds the lock; moved verbatim, tracked for a de-flake pass
    assert not done.wait(0.3), "the read walked the file while the append held the lock"
    fail.set()
    appender.join(5)
    assert done.wait(5)

    assert box["value"] == [1]


def test_a_torn_tail_still_waits_for_the_lock_before_truncating():
    # A torn tail may be an append in flight, so its open takes the lock and
    # reads again: the holder finishing the line means nothing is dropped.
    crew = _crew()
    crew.append("item/opened", {"kept": True}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    intact = path.read_bytes()
    finished = b'{"type":"item/opened","seq":2}\n'
    path.write_bytes(intact + finished[:12])
    taken, release = threading.Event(), threading.Event()

    def _finish_under_lock() -> None:
        with store._open_lock(store._lock_path(lg.KIND_CREW, CREW)):
            taken.set()
            release.wait(5)
            path.write_bytes(intact + finished)

    holder = threading.Thread(target=_finish_under_lock, daemon=True)
    holder.start()
    assert taken.wait(5)
    # flake-ok: pre-existing 200 ms timed release of the lock holder; moved verbatim, tracked for a de-flake pass
    threading.Timer(0.2, release.set).start()

    CrewLog.open(lg.KIND_CREW, CREW)
    holder.join(5)

    assert path.read_bytes() == intact + finished


def test_a_complete_last_line_missing_only_its_newline_is_kept():
    # Only the separator was lost, so the record is real; the next append
    # re-supplies the newline instead of rewriting the line.
    crew = _crew()
    crew.append("item/opened", {"kept": True}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))

    reopened = CrewLog.open(lg.KIND_CREW, CREW)

    assert reopened.last_seq == 1
    assert reopened.append("item/opened", {"next": True}, src="gateway").seq == 2
    assert [entry.seq for entry in CrewLog.open(lg.KIND_CREW, CREW).iter_from()] == [1, 2]


def test_a_malformed_interior_line_is_skipped_on_read_and_left_on_disk():
    crew = _crew()
    for index in range(3):
        crew.append("activity/tick", {"i": index}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = '{"type":"activity/tick","seq":'  # terminated, so NOT torn
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reopened = CrewLog.open(lg.KIND_CREW, CREW)

    assert [entry.seq for entry in reopened.iter_from()] == [1, 3]
    assert reopened.last_seq == 3
    assert '{"type":"activity/tick","seq":' in path.read_text(encoding="utf-8")


def test_a_forward_gap_inside_one_file_reads_without_raising():
    # Seq continuity is a boundary-only check by design: inside one file a
    # missing seq is a damaged line the reader skips on purpose, so a record
    # lost ENTIRELY must read the same way. This pins ``iter_from``'s
    # non-advancing-seq refusal against ever widening into a contiguity check.
    crew = _crew()
    for index in range(3):
        crew.append("activity/tick", {"i": index}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[2]  # the seq-2 record is gone entirely: a pure forward gap
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert [entry.seq for entry in CrewLog.open(lg.KIND_CREW, CREW).iter_from()] == [1, 3]


def test_a_duplicate_seq_is_refused_by_a_read_that_never_yields_it():
    # The walked-entry guard, below the requested seq: the duplicate sits
    # BEFORE the resume point, so the old code dropped it unexamined. The
    # refusal must fire on what the read walks, not only on what it yields.
    crew = _crew()
    for index in range(2):
        crew.append("activity/tick", {"i": index}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    raw = path.read_bytes()
    last_line = raw.splitlines(keepends=True)[-1]
    with open(path, "ab") as damaged:
        damaged.write(last_line)  # byte-identical copy: seq 2 appears twice
    crew.append("activity/tick", {"i": 2}, src="gateway")  # seq 3, past the damage

    with _raises(lg.CODE_BAD_DATA) as excinfo:
        list(CrewLog.open(lg.KIND_CREW, CREW).iter_from(3))

    assert excinfo.value.field == "seq"


def test_a_strictly_backward_seq_is_refused_not_only_a_duplicate():
    # The other half of "non-advancing": a copy of an OLD record at the tail,
    # so the walked seq goes 1,2,3,1. A guard narrowed to equality (the shape
    # "duplicate" invites) would let this through and reopen the silent-skip
    # divergence for the stale-tail-copy case.
    crew = _crew()
    for index in range(3):
        crew.append("activity/tick", {"i": index}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    first_record = path.read_bytes().splitlines(keepends=True)[1]  # [0] is the header
    with open(path, "ab") as damaged:
        damaged.write(first_record)  # seq 1 again, after seq 3: backward, not duplicate

    with _raises(lg.CODE_BAD_DATA) as excinfo:
        list(CrewLog.open(lg.KIND_CREW, CREW).iter_from(4))

    assert excinfo.value.field == "seq"


def test_a_page_read_still_renders_a_unit_with_a_non_advancing_seq():
    # Paging renders history for a human; refusing every intact line of a unit
    # because one damaged line exists elsewhere in it would take the history
    # away exactly when damage makes it most worth reading. ``strict_seq=False``
    # is the rendering caller's contract; folds keep the refusing default.
    crew = _crew()
    for index in range(2):
        crew.append("activity/tick", {"i": index}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    last_line = path.read_bytes().splitlines(keepends=True)[-1]
    with open(path, "ab") as damaged:
        damaged.write(last_line)  # seq 2 appears twice

    seqs = [e.seq for e in CrewLog.open(lg.KIND_CREW, CREW).iter_from(1, strict_seq=False)]

    assert seqs == [1, 2, 2]


def test_a_blank_interior_line_is_skipped():
    crew = _crew()
    crew.append("activity/tick", {"i": 0}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert [entry.seq for entry in CrewLog.open(lg.KIND_CREW, CREW).iter_from()] == [1]


def test_an_interior_line_whose_envelope_is_wrong_is_skipped():
    crew = _crew()
    crew.append("activity/tick", {"i": 0}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    path.write_text(
        path.read_text(encoding="utf-8")
        + json.dumps({"type": "activity/tick", "seq": 2, "time": 1, "src": "gateway"})
        + "\n"
        + json.dumps({"type": "activity/tick", "seq": 3, "time": 1, "src": "g", "data": 4})
        + "\n",
        encoding="utf-8",
    )
    assert [entry.seq for entry in CrewLog.open(lg.KIND_CREW, CREW).iter_from()] == [1]


def test_an_interior_line_whose_bytes_are_not_utf8_is_skipped_not_altered():
    # The dangerous shape: invalid bytes INSIDE a JSON string, where a
    # replacement decode would still yield valid JSON. Handing a consumer a
    # value with U+FFFD substituted into it is worse than skipping the line --
    # the record calls itself the authority, so an altered value is a lie a
    # reader cannot detect.
    crew = _crew()
    crew.append("activity/tick", {"note": "intact"}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    damaged = (
        b'{"type":"activity/tick","seq":2,"time":1,"src":"gateway",'
        b'"data":{"note":"caf\xe9 latte"}}\n'
    )
    with open(path, "ab") as handle:
        handle.write(damaged)
    crew_reopened = CrewLog.open(lg.KIND_CREW, CREW)

    entries = list(crew_reopened.iter_from())

    assert [entry.seq for entry in entries] == [1]
    assert all("\ufffd" not in str(entry.data) for entry in entries)
    assert damaged.strip() in path.read_bytes()  # skipped on read, kept on disk


def test_a_header_whose_bytes_are_not_utf8_refuses_the_open():
    _crew()
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    path.write_bytes(b'{"type":"crew","version":1,"id":"caf\xe9","createdAt":1}\n')
    with _raises(lg.CODE_BAD_HEADER) as exc:
        CrewLog.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_a_torn_tail_of_invalid_bytes_is_truncated_not_decoded():
    crew = _crew()
    crew.append("activity/tick", {"note": "intact"}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    intact = path.read_bytes()
    path.write_bytes(intact + b'{"type":"activity/tick","seq":2,"d\xff\xfe')

    reopened = CrewLog.open(lg.KIND_CREW, CREW)

    assert path.read_bytes() == intact
    assert reopened.last_seq == 1


def test_seq_recovers_from_the_newest_valid_line_when_the_tail_is_damaged():
    crew = _crew()
    for index in range(3):
        crew.append("activity/tick", {"i": index}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = "{damaged"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # The newest PARSEABLE seq is 2, so the next entry is 3 -- the damaged
    # line's number is not reused, because a reader cannot know it was 3.
    assert CrewLog.open(lg.KIND_CREW, CREW).append("activity/tick", {}, src="gateway").seq == 3


def test_a_torn_tail_is_repaired_by_an_append_too_not_only_by_a_reopen():
    # A long-lived writer holds its handle open across a crash somewhere else,
    # so the repair cannot live only in ``open``.
    crew = _crew()
    crew.append("item/opened", {"kept": True}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    path.write_bytes(path.read_bytes() + b'{"type":"item/opened","seq":2,"tim')

    assert crew.append("item/opened", {"next": True}, src="gateway").seq == 2
    assert [entry.seq for entry in crew.iter_from()] == [1, 2]


def test_an_interior_line_carrying_a_malformed_ref_is_skipped():
    crew = _crew()
    crew.append("item/opened", {"i": 0}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    path.write_text(
        path.read_text(encoding="utf-8")
        + json.dumps(
            {
                "type": "item/opened",
                "seq": 2,
                "time": 1,
                "src": "gateway",
                "ref": {"unit": "swarm", "id": "x", "from": 1},
                "data": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert [entry.seq for entry in CrewLog.open(lg.KIND_CREW, CREW).iter_from()] == [1]


def test_a_stored_session_thread_anchor_that_is_damaged_reads_as_absent():
    _session()
    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    header["thread"] = {"crew": CREW}
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    assert CrewLog.open(lg.KIND_SESSION, SESSION).header.thread is None


@pytest.mark.parametrize("key,bad", [("createdAt", "yesterday"), ("version", "1")])
def test_a_header_field_of_the_wrong_stored_type_refuses_the_open(key, bad):
    _crew()
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    header[key] = bad
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        CrewLog.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_a_session_header_missing_its_owner_refuses_the_open():
    _session()
    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    del header["owner"]
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        CrewLog.open(lg.KIND_SESSION, SESSION)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_a_log_of_one_kind_cannot_be_opened_as_the_other():
    # The two roots are separate, so this needs the file moved -- which is
    # exactly what a mistaken restore or a hand-edit does.
    _crew()
    target = lg.crew_log_dir(lg.KIND_SESSION, CREW)
    target.mkdir(parents=True, exist_ok=True)
    (target / lg.LOG_FILE).write_bytes(lg.crew_log_path(lg.KIND_CREW, CREW).read_bytes())
    with _raises(lg.CODE_BAD_HEADER) as exc:
        CrewLog.open(lg.KIND_SESSION, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


# --- large files: the bounded tail read -----------------------------------


def test_seq_comes_off_a_bounded_window_on_a_file_past_that_window():
    # The window can begin mid-line, so its first fragment is not a record. If
    # that fragment were trusted the seq would come from a partial line.
    crew = _crew()
    for _ in range(3):  # three ~40 KiB lines clear the ~72 KiB window
        crew.append("item/opened", {"blob": "x" * 40_000}, src="gateway")
    assert crew.append("item/opened", {}, src="gateway").seq == 4
    assert CrewLog.open(lg.KIND_CREW, CREW).last_seq == 4


def test_seq_falls_back_to_a_full_scan_when_the_window_holds_nothing_valid():
    crew = _crew()
    crew.append("item/opened", {"i": 0}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    # One terminated garbage line wider than the tail window, so the window
    # sees only garbage and the real seq is behind it.
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("{" + "z" * 80_000 + "\n")

    reopened = CrewLog.open(lg.KIND_CREW, CREW)

    assert reopened.last_seq == 1
    assert reopened.append("item/opened", {"i": 1}, src="gateway").seq == 2
    assert [entry.seq for entry in reopened.iter_from()] == [1, 2]


# --- interrupted-tail closers ---------------------------------------------


def _interrupted_session() -> CrewLog:
    """A session log whose newest turn was cut off mid-tool-call."""
    led = _session()
    led.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    led.append(
        "tool/called",
        {"turn": 1, "call_id": "tc-1", "name": "fs_read", "server": "", "kind": ""},
        src="acp",
    )
    led.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")
    led.append("turn/started", {"turn": 2, "actor": "user", "depth": 0}, src="gateway")
    led.append(
        "tool/called",
        {"turn": 2, "call_id": "tc-2", "name": "execute_bash", "server": "", "kind": ""},
        src="acp",
    )
    led.append(
        "tool/called",
        {"turn": 2, "call_id": "tc-3", "name": "fs_write", "server": "", "kind": ""},
        src="acp",
    )
    return led


def test_a_plain_open_never_touches_an_interrupted_tail():
    # The bug this split exists to prevent. A live writer reconnects to its own
    # log for reasons that say nothing about its health -- a handle dropped from
    # a bounded cache is enough -- so an open that repaired would close a turn that
    # is still running and then let it keep writing past its own completion.
    _interrupted_session()
    before = _log_bytes()
    reopened = CrewLog.open(lg.KIND_SESSION, SESSION)
    assert _log_bytes() == before
    assert [e.type for e in reopened.iter_from(1)][-1] == "tool/called"


def test_repair_is_reachable_from_a_handle_as_well_as_from_open():
    # The resume path may already hold a handle, so the same rule needs a method
    # form; a caller must not have to re-open just to ask for the repair.
    led = _interrupted_session()
    assert led.repair_interrupted_turn() == 3
    assert list(led.iter_from(1))[-1].data["stop_reason"] == "interrupted"
    # The handle's cached seq follows the closers, so its next append does not
    # collide with them.
    assert (
        led.append("turn/started", {"turn": 3, "actor": "user", "depth": 0}, src="gateway").seq
        == led.last_seq
    )


def test_an_interrupted_turn_is_closed_when_the_log_is_opened():
    # A crash leaves the newest turn open. Closing it in the record answers
    # "did this turn finish or did its writer die" once, instead of leaving every
    # reader to carry the same special case.
    _interrupted_session()
    reopened = CrewLog.open(lg.KIND_SESSION, SESSION, repair=True)
    body = list(reopened.iter_from(1))
    assert [e.type for e in body[-3:]] == [
        "tool/completed",
        "tool/completed",
        "turn/completed",
    ]
    assert body[-1].data == {"turn": 2, "stop_reason": "interrupted"}


def test_repair_refuses_to_close_past_a_damaged_real_completion(caplog):
    led = _session()
    led.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    led.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")
    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    lines = path.read_bytes().splitlines(keepends=True)
    lines[2] = b"{ damaged real completion\n"
    path.write_bytes(b"".join(lines))
    before = path.read_bytes()

    with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.store"):
        CrewLog.open(lg.KIND_SESSION, SESSION, repair=True)

    assert (
        path.read_bytes() == before
    ), "repair fabricated an interrupted outcome after skipping the damaged real completion"
    warnings = [
        record for record in caplog.records if "could fabricate an outcome" in record.getMessage()
    ]
    assert len(warnings) == 1, f"repair emitted {len(warnings)} refusal warnings, expected one"


def test_a_closer_names_every_unmatched_call_in_first_seen_order():
    _interrupted_session()
    body = list(CrewLog.open(lg.KIND_SESSION, SESSION, repair=True).iter_from(1))
    closers = [e for e in body if e.type == "tool/completed"]
    assert [e.data["call_id"] for e in closers] == ["tc-2", "tc-3"]
    assert all(e.data["status"] == "unknown" for e in closers)
    # The trusted identity the call frame carried rides along, so the closer
    # joins to its call without a reader having to fold backwards for it.
    assert closers[0].data["name"] == "execute_bash"


def test_an_unmatched_approval_is_closed_with_an_unknown_decision():
    # An approval is the one moment the agent stops and asks permission. A crash
    # between the request and the answer leaves a request nothing ever decided,
    # and a reader cannot tell that from a human who is still thinking.
    led = _session()
    led.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    led.append("approval/requested", {"turn": 1, "approval_id": "ap-1"}, src="gateway")
    led.append("approval/requested", {"turn": 1, "approval_id": "ap-2"}, src="gateway")
    led.append(
        "approval/decided",
        {"turn": 1, "approval_id": "ap-1", "decision": "approved"},
        src="gateway",
    )
    body = list(CrewLog.open(lg.KIND_SESSION, SESSION, repair=True).iter_from(1))
    closers = [e for e in body if e.type == "approval/decided" and e.data["decision"] == "unknown"]
    # Only the one still open. The answered request keeps its real decision.
    assert [e.data["approval_id"] for e in closers] == ["ap-2"]
    assert [
        e.data["decision"]
        for e in body
        if e.type == "approval/decided" and e.data.get("approval_id") == "ap-1"
    ] == ["approved"]
    # Decided before the turn is closed: that is the only order a live writer
    # could have produced.
    assert body.index(closers[0]) < next(
        i for i, e in enumerate(body) if e.type == "turn/completed"
    )


def _gone_unless(*live: str):
    """A stand-in for the repairing process's live subagent registry.

    The store never asks the file whether a child is finished -- an unmatched
    opener cannot answer that -- so every child-closing test has to say which
    children are still running, the way the real predicate reads the registry.
    """
    return lambda agent_id: agent_id not in live


def test_repair_appends_nothing_to_a_tail_whose_seq_goes_backward():
    # ``_scan_tail`` takes the newest line's seq, so a backward tail lowers the
    # closers' first seq onto records that already exist -- a repair acting on
    # such a file would AMPLIFY the damage (colliding seqs) before any reader
    # refuses it. The repair fold must treat a non-advancing seq like any other
    # record it cannot trust: refuse content repair, append zero bytes.
    led = _session()
    led.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    led.append(
        "tool/called",
        {"turn": 1, "call_id": "tc-1", "name": "execute_bash", "server": "", "kind": ""},
        src="acp",
    )
    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    opener_line = path.read_bytes().splitlines(keepends=True)[1]  # the seq-1 record
    with open(path, "ab") as damaged:
        damaged.write(opener_line)  # tail now reads 1,2,1 -- backward

    before = path.read_bytes()
    CrewLog.open(lg.KIND_SESSION, SESSION, repair=True)

    assert path.read_bytes() == before


def test_a_child_that_outlived_its_completed_turn_is_still_closed():
    # The ORDINARY dangling case, and the reason a child is matched across the
    # whole file rather than inside the open turn: a subagent routinely reports
    # long after the turn that asked for it ended, so scoping its closer to an
    # open turn would close only the rare child that died inside its own turn.
    led = _session()
    led.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    led.append("subagent/spawned", {"turn": 1, "agent_id": "sub-1"}, src="gateway")
    led.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")
    body = list(
        CrewLog.open(lg.KIND_SESSION, SESSION, repair=True, child_gone=_gone_unless()).iter_from(1)
    )
    closers = [e for e in body if e.type == "subagent/failed"]
    assert [e.data["agent_id"] for e in closers] == ["sub-1"]
    assert closers[0].data["outcome"] == "unknown"
    # No `turn`: a child's outcome is not an event of any one turn.
    assert "turn" not in closers[0].data


def test_a_dangling_child_alone_closes_no_turn():
    # The turn completed on its own. Appending a second `turn/completed` would
    # close a turn that already closed itself.
    led = _session()
    led.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    led.append("subagent/spawned", {"turn": 1, "agent_id": "sub-1"}, src="gateway")
    led.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")
    body = list(
        CrewLog.open(lg.KIND_SESSION, SESSION, repair=True, child_gone=_gone_unless()).iter_from(1)
    )
    assert [e.type for e in body if e.type == "turn/completed"] == ["turn/completed"]
    assert body[-1].type == "subagent/failed"


def test_a_child_whose_terminal_landed_is_not_closed_again():
    # Both real terminals must balance the opener, or a resume would append a
    # second outcome for a child that already reported one.
    for terminal, payload in (
        ("subagent/completed", {"agent_id": "sub-1", "ms": 12}),
        ("subagent/failed", {"agent_id": "sub-1", "outcome": "stopped"}),
    ):
        unit = f"balanced-{terminal.replace('/', '-')}"
        led = _session(unit)
        led.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
        led.append("subagent/spawned", {"turn": 1, "agent_id": "sub-1"}, src="gateway")
        led.append(terminal, payload, src="gateway")
        led.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")
        before = lg.crew_log_path(lg.KIND_SESSION, unit).read_bytes()
        CrewLog.open(lg.KIND_SESSION, unit, repair=True, child_gone=_gone_unless())
        assert (
            lg.crew_log_path(lg.KIND_SESSION, unit).read_bytes() == before
        ), f"repair appended a second outcome over a child already closed by {terminal}"


def test_a_child_the_registry_still_reports_running_is_left_alone():
    # The corruption this predicate exists to prevent. An unmatched opener does
    # NOT mean the child finished: a same-process teardown can leave one running,
    # and it will file its own real terminal later. Closing it here would put two
    # outcomes for one agent_id in a file nothing rewrites, so a child the
    # registry still lists is skipped even though its opener is unbalanced.
    led = _session()
    led.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    led.append("subagent/spawned", {"turn": 1, "agent_id": "sub-1"}, src="gateway")
    led.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")
    before = lg.crew_log_path(lg.KIND_SESSION, SESSION).read_bytes()
    CrewLog.open(lg.KIND_SESSION, SESSION, repair=True, child_gone=_gone_unless("sub-1"))
    assert lg.crew_log_path(lg.KIND_SESSION, SESSION).read_bytes() == before


def test_a_predicate_that_cannot_answer_closes_no_child():
    # A registry lookup that raises must not decide the child is gone. The repair
    # reads a failed answer as "still running" for the same reason it defaults to
    # it: an opener left standing is a reader behind, a wrong closer is a file
    # that cannot be corrected.
    def _raises(agent_id: str) -> bool:
        raise RuntimeError("registry unavailable")

    led = _session()
    led.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    led.append("subagent/spawned", {"turn": 1, "agent_id": "sub-1"}, src="gateway")
    led.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")
    before = lg.crew_log_path(lg.KIND_SESSION, SESSION).read_bytes()
    CrewLog.open(lg.KIND_SESSION, SESSION, repair=True, child_gone=_raises)
    assert lg.crew_log_path(lg.KIND_SESSION, SESSION).read_bytes() == before


def test_a_same_process_resume_leaves_a_child_opener_alone():
    # The default, and the asymmetry the predicate exists for. "The writer is gone"
    # closes a turn-scoped opener, because the turn died with its writer. It says
    # nothing about a CHILD, which outlives its asking turn by design -- so a
    # writer torn down inside a live process can leave one still running and still
    # able to file its own real terminal. Closing it here would put two outcomes
    # for one agent_id in a file nothing rewrites, so a resume that cannot rule
    # that out must leave the opener standing.
    led = _session()
    led.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    led.append("subagent/spawned", {"turn": 1, "agent_id": "sub-1"}, src="gateway")
    led.append("approval/requested", {"turn": 1, "approval_id": "ap-1"}, src="gateway")
    body = list(CrewLog.open(lg.KIND_SESSION, SESSION, repair=True).iter_from(1))
    assert [e.type for e in body if e.type == "subagent/failed"] == []
    # The turn-scoped opener IS still closed on the same repair: this narrows what
    # a repair may conclude about a child, it does not switch the repair off.
    assert [e.data["decision"] for e in body if e.type == "approval/decided"] == ["unknown"]
    assert body[-1].type == "turn/completed"


def test_closers_reuse_the_last_real_entrys_time_and_continue_seq():
    # A closer describes what happened when the writer STOPPED. Stamping it with
    # the clock at open time would put a gap of arbitrary length inside a turn
    # and make any duration computed off these entries a measure of downtime.
    led = _interrupted_session()
    last_real = list(led.iter_from(1))[-1]
    body = list(CrewLog.open(lg.KIND_SESSION, SESSION, repair=True).iter_from(1))
    closers = body[-3:]
    assert {e.time for e in closers} == {last_real.time}
    assert [e.seq for e in closers] == [last_real.seq + 1, last_real.seq + 2, last_real.seq + 3]


def test_closing_is_deterministic_and_a_balanced_tail_is_left_alone():
    _interrupted_session()
    closed = CrewLog.open(lg.KIND_SESSION, SESSION, repair=True)
    after_first = _log_bytes()
    # The tail is balanced now, so a second open must add nothing at all --
    # otherwise every open would grow the file.
    again = CrewLog.open(lg.KIND_SESSION, SESSION, repair=True)
    assert _log_bytes() == after_first
    assert closed.last_seq == again.last_seq
    assert list(again.iter_from(1))[-1].data["stop_reason"] == "interrupted"


def test_only_a_session_log_is_closed_this_way():
    # The closers encode the SESSION turn lifecycle, so the repair is gated on
    # kind rather than on what the tail happens to look like: a crew log's
    # types are its own, and its reader must not find turn entries it never wrote.
    # Asserted on the gate directly, because the ownership rule refuses to let a
    # crew log hold a `turn/started` in the first place.
    _interrupted_session()
    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    before = _log_bytes()
    assert store._close_interrupted_tail(lg.KIND_CREW, SESSION, path) == 0
    assert _log_bytes() == before
    assert store._close_interrupted_tail(lg.KIND_SESSION, SESSION, path) == 3


def test_a_completed_turns_unmatched_call_is_left_open():
    # Scoped to the OPEN turn on purpose: an unmatched call inside a turn that
    # did complete is a different anomaly, and inventing a result for it would be
    # this reader editing history it was not asked about.
    led = _session()
    led.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    led.append(
        "tool/called",
        {"turn": 1, "call_id": "tc-1", "name": "fs_read", "server": "", "kind": ""},
        src="acp",
    )
    led.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")
    before = _log_bytes()
    CrewLog.open(lg.KIND_SESSION, SESSION, repair=True)
    assert _log_bytes() == before
