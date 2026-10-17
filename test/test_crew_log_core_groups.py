"""Append-only crew log core: groups, and an append that has to be rolled back.

A group whose members are meaningless apart is written contiguously with its
citing entry last, refused whole, and repaired whole; a failed append leaves the
file exactly as it was and never removes another writer's entries.
"""

from __future__ import annotations

import json
import logging
import os

import crew_log_core_helpers
import pytest
from crew_log_core_helpers import SESSION, _raises, _session, _split_into_segments

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, store

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_isolated_home = crew_log_core_helpers._isolated_home


# --- a group whose members are meaningless apart --------------------------


def test_a_group_is_written_contiguously_with_its_citing_entry_last():
    """`append_many` allocates one contiguous seq run and writes it once.

    A body too large for one line becomes chunk entries plus the entry that CITES
    their seqs. Appended one at a time, a hard kill between them leaves the chunks on
    disk with nothing pointing at them: the body is stored and unreachable, and the
    message it belongs to has no record at all. One write leaves either the whole
    group or a torn tail, which the next append truncates.
    """
    session = _session()
    written = session.append_many(
        [
            {"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}, "ignorable": True},
            {"type": "message/chunk", "data": {"turn": 1, "delta": "bb"}, "ignorable": True},
        ],
        src="acp",
        cite=lambda seqs: {"type": "message/sent", "data": {"turn": 1, "chunks": seqs, "chars": 4}},
    )

    assert [e.seq for e in written] == [written[0].seq + n for n in range(3)], "seqs have a gap"
    assert written[-1].type == "message/sent", "the citing entry is not last"
    assert written[-1].data["chunks"] == [written[0].seq, written[1].seq]
    # The chunks are skippable by a reader that does not know them; the citing entry
    # is not, because a fold that skipped it would lose the message.
    assert written[0].ignorable and written[1].ignorable
    assert not written[-1].ignorable
    assert session.last_seq == written[-1].seq


def test_a_group_is_refused_whole_and_leaves_the_file_identical():
    """Every check happens before a byte is written, as for a single append."""
    session = _session()
    session.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    before = lg.crew_log_path(lg.KIND_SESSION, SESSION).read_bytes()

    # The second entry is a crew type in a session log (rule 1), so the whole
    # group is refused as ``event_type_not_owned``. The old ``_raises`` helper
    # never compared the code, and this site named ``bad_src`` for years.
    with _raises(lg.CODE_EVENT_TYPE_NOT_OWNED):
        session.append_many(
            [
                {"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}},
                {"type": "item/opened", "data": {"turn": 1}},  # not a session type
            ],
            src="acp",
        )

    assert lg.crew_log_path(lg.KIND_SESSION, SESSION).read_bytes() == before


def test_repair_drops_a_chunk_group_whose_citing_entry_never_landed(caplog):
    """The torn-tail case the batch cannot rule out, and its bounded residual.

    One write still tears, and the citing entry is the last line -- so what a hard
    kill can leave is chunks nothing points at. Dropping them costs the one message
    that was mid-write, the same residual as any single entry lost the same way, and
    it leaves the file free of lines whose only purpose was to be cited by an entry
    that does not exist.
    """
    session = _session()
    session.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    # Exactly the shape a torn group leaves: chunks, and then nothing.
    orphans = session.append_many(
        [
            {"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}, "ignorable": True},
            {"type": "message/chunk", "data": {"turn": 1, "delta": "bb"}, "ignorable": True},
        ],
        src="acp",
    )
    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    assert "message/chunk" in path.read_text(encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.store"):
        CrewLog.open(lg.KIND_SESSION, SESSION, repair=True)

    body = path.read_text(encoding="utf-8")
    assert "message/chunk" not in body, "the unreachable chunks were left in the file"
    named = [
        r
        for r in caplog.records
        if "unreachable chunk group" in r.getMessage()
        and f"{orphans[0].seq}-{orphans[-1].seq}" in r.getMessage()
    ]
    assert len(named) == 1, "the drop was silent or did not name the seq range"
    # Still append-only, and with no hole: the repair's own closers reuse the seqs the
    # dropped chunks had held, so a fold sees a contiguous run rather than a gap.
    reopened = CrewLog.open(lg.KIND_SESSION, SESSION)
    seqs = [e.seq for e in reopened.iter_from(1)]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"seq is not contiguous: {seqs}"
    assert seqs[0] == 1 and orphans[0].seq in seqs, "the truncated range was not reused"


def test_repair_keeps_a_chunk_group_that_was_completed():
    """A group followed by any entry landed whole -- it is not an orphan."""
    session = _session()
    session.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    session.append_many(
        [
            {"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}, "ignorable": True},
        ],
        src="acp",
        cite=lambda seqs: {"type": "message/sent", "data": {"turn": 1, "chunks": seqs, "chars": 2}},
    )

    CrewLog.open(lg.KIND_SESSION, SESSION, repair=True)
    body = lg.crew_log_path(lg.KIND_SESSION, SESSION).read_text(encoding="utf-8")
    assert "message/chunk" in body, "a cited chunk was dropped"


def test_a_newer_format_version_says_upgrade_rather_than_corrupt():
    """A file this build cannot read is not a damaged file.

    A newer format need not satisfy any of this build's structural checks: its new
    required event type surfaces as `unknown_entry_type`, its header shape as
    `bad_header`. Both tell the reader the log is damaged when the truth is that
    the reader is old, and a durable record that cries corruption at its own
    successor is worse than useless -- someone will delete the file.

    Refused before the remaining header checks and before any row is decoded, which
    is the only ordering where the diagnosis can be right. Carrying a version is
    pointless otherwise: nothing else in the module reads it.

    Mutation guard: dropping the comparison makes this raise `bad_header` or
    `unknown_entry_type` instead.
    """
    session = _session()
    session.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    header = json.loads(lines[0])
    header["version"] = lg.SCHEMA_VERSION + 1
    # A field this build has never heard of, so the header would also fail a
    # structural check if the version were not consulted first.
    header["somethingNewer"] = {"whatever": 1}
    path.write_text(json.dumps(header) + "\n" + "".join(lines[1:]), encoding="utf-8")

    with _raises(lg.CODE_UNSUPPORTED_VERSION) as caught:
        CrewLog.open(lg.KIND_SESSION, SESSION)
    assert "upgrade" in str(caught.value)
    assert "not damaged" in str(caught.value)


def test_the_current_and_older_versions_still_open():
    """The refusal is for NEWER only -- an older file is what migration is for."""
    session = _session()
    session.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    assert CrewLog.open(lg.KIND_SESSION, SESSION).last_seq == 1

    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    header = json.loads(lines[0])
    header["version"] = 0
    path.write_text(json.dumps(header) + "\n" + "".join(lines[1:]), encoding="utf-8")
    assert CrewLog.open(lg.KIND_SESSION, SESSION).last_seq == 1, "an older file was refused"


def test_a_group_with_no_data_is_refused_rather_than_written_empty():
    """`data` is validated as itself, not defaulted into an empty object.

    Substituting `{}` for a missing or falsey value made two different mistakes
    validate: a caller that forgot the body, and one that passed `None`. Either
    would have had its entry written with no data at all -- a line claiming a fact
    and carrying none, in a file nothing rewrites.

    Mutation guard: restoring the `or {}` fallback makes both of these pass.
    """
    session = _session()
    session.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    before = path.read_bytes()

    for bad in ({"type": "message/chunk"}, {"type": "message/chunk", "data": None}):
        with _raises("bad_data"):
            session.append_many(
                [
                    {"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}},
                    bad,
                ],
                src="acp",
            )
        assert path.read_bytes() == before, "a refused group still wrote something"


def test_a_point_read_finds_an_entry_in_an_older_segment():
    """`get` walks segments, so a rotated seq is not reported as absent.

    Reading only the newest file made a point read of a rotated entry answer `None`,
    which a caller cannot tell from "no such entry" -- so a seq it had just been
    handed would come back missing. Retention is the one thing that legitimately
    removes an entry, and it deletes whole segments; while a segment is on disk its
    entries are readable.

    Mutation guard: reading only `self._path` makes seqs 1-8 answer None.
    """
    led = _session("seg-get")
    for n in range(1, 13):
        led.append("turn/started", {"turn": n, "actor": "user", "depth": 0}, src="gateway")
    _split_into_segments(lg.KIND_SESSION, "seg-get", 9)
    _split_into_segments(lg.KIND_SESSION, "seg-get", 5)

    reader = lg.CrewLog.open(lg.KIND_SESSION, "seg-get")
    for seq in range(1, 13):
        found = reader.get(seq)
        assert found is not None, f"seq {seq} was reported absent though its segment is on disk"
        assert found.seq == seq
        assert found.data["turn"] == seq
    assert reader.get(13) is None, "a seq past the end must still answer None"


def test_paging_walks_past_a_segment_boundary():
    """Paging spans segments, so history does not silently stop at a rotation.

    Reading only the newest file made a page stop at the boundary AND report
    `next_before` as None -- telling the caller it had seen the whole history when it
    had seen one segment. Paging renders history for a human, and truncating it
    silently is the one answer it must not give.

    Mutation guard: reading only `self._path` cuts the walk to the last segment.
    """
    led = _session("seg-page")
    for n in range(1, 13):
        led.append("turn/started", {"turn": n, "actor": "user", "depth": 0}, src="gateway")
    _split_into_segments(lg.KIND_SESSION, "seg-page", 9)
    _split_into_segments(lg.KIND_SESSION, "seg-page", 5)

    reader = lg.CrewLog.open(lg.KIND_SESSION, "seg-page")
    seen: list[int] = []
    cursor: int | None = None
    for _ in range(10):  # bounded: a cursor that never ends is the bug, not a hang
        page = reader.page(before=cursor, limit=5)
        seen.extend(e.seq for e in page.entries)
        cursor = page.next_before
        if cursor is None:
            break
    assert cursor is None, "paging never reached the end of the history"
    assert sorted(seen) == list(range(1, 13)), f"paging skipped or repeated entries: {seen}"


def test_a_group_cites_the_seqs_it_was_actually_allocated():
    """A writer that appends at the lock boundary must not shift a citation.

    The citing entry's ``chunks`` are the ONLY path from a message to its body, and
    the lock this store takes is a cross-process one -- it exists because a second
    handle can be appending to the same file. So the allocation a citation names has
    to be the allocation the write commits to. Deciding it from an earlier, unlocked
    read of the tail leaves a window: another handle appends, the run shifts, and the
    ``chunks`` name entries belonging to the intruder. Those seqs exist and parse, so
    no later read can tell -- the body a reader reconstructs is simply the wrong one.

    The intruder is interposed exactly at that window: a hook fires once, just before
    this group takes the lock, and appends through a SECOND handle to the same file.
    Nothing is held at that point, so the intruder is not blocked and the boundary is
    real rather than simulated.

    Mutation guard: allocating from a tail read before the lock (the shape
    ``plan_group_seqs`` served) makes the citation name the intruder's seq and reddens
    the equality below.
    """
    from kiro_crew.crew_log import store as store_mod

    session = _session()
    session.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")

    real_open_lock = store_mod._open_lock
    fired: list[int] = []

    def _intruding_lock(path):
        # Once, and only for this log's lock: a re-entrant fire would recurse
        # through the intruder's own append.
        if not fired:
            fired.append(1)
            intruder = CrewLog.open(lg.KIND_SESSION, SESSION)
            intruder.append("turn/started", {"turn": 99, "actor": "user", "depth": 0}, src="acp")
        return real_open_lock(path)

    # A scoped context, NOT the `monkeypatch` fixture: its `undo` would also revert
    # the autouse home isolation, and the reads below would then resolve against a
    # different data home than the file was written under.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store_mod, "_open_lock", _intruding_lock)
        written = session.append_many(
            [
                {"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}, "ignorable": True},
                {"type": "message/chunk", "data": {"turn": 1, "delta": "bb"}, "ignorable": True},
            ],
            src="acp",
            cite=lambda seqs: {
                "type": "message/sent",
                "data": {"turn": 1, "chunks": seqs, "chars": 4},
            },
        )
    assert fired, "the intruder never ran, so the boundary was not exercised"

    landed = [e.seq for e in written if e.type == "message/chunk"]
    cited = written[-1].data["chunks"]
    assert cited == landed, (
        f"the citation names {cited} but the chunks landed at {landed}; a citation "
        "decided before the lock names the intruder's entries"
    )
    # And the file agrees: every cited seq is a chunk of this message, not the
    # intruder's entry.
    by_seq = {e.seq: e for e in session.iter_from(1)}
    assert [by_seq[s].type for s in cited] == ["message/chunk", "message/chunk"]
    assert [by_seq[s].data["delta"] for s in cited] == ["aa", "bb"]


def test_repair_leaves_the_file_alone_when_a_record_cannot_be_accounted_for(caplog):
    """A byte offset is only usable if every byte before it was counted.

    The orphan-group scan walks byte lengths to find where to truncate. Read with the
    skipping posture, an over-cap record is dropped in full -- terminator included --
    without being yielded, so the walk stops matching the file at that record and
    every offset after it is short by its length. Truncating at a short offset cuts
    valid entries, which is the one thing an append-only file must never do to itself.

    Failing closed is the answer: a record that cannot be delivered intact aborts the
    scan, nothing is truncated, and the warning says why. The unreachable chunks stay
    -- which costs a reader one unusable body, against losing history that is fine.

    Mutation guard: reading this scan with the skipping posture again lets the
    truncation run at a short offset and drops the valid entry below.
    """
    session = _session()
    kept = session.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    # An over-cap record, then a chunk group with no citing entry: the orphan shape,
    # sitting behind a record the bounded reader would silently drop.
    with open(path, "ab") as handle:
        handle.write(b'{"type":"junk","data":"' + b"x" * (lg.MAX_ENTRY_BYTES + 64) + b'"}\n')
    orphans = session.append_many(
        [
            {"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}, "ignorable": True},
            {"type": "message/chunk", "data": {"turn": 1, "delta": "bb"}, "ignorable": True},
        ],
        src="acp",
    )
    before = path.read_bytes()

    with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.store"):
        CrewLog.open(lg.KIND_SESSION, SESSION, repair=True)

    body = path.read_bytes()
    assert body.startswith(before), (
        "the repair truncated a file whose bytes it could not account for; the "
        f"entry at seq {kept.seq} and the records after it are at risk"
    )
    reopened = CrewLog.open(lg.KIND_SESSION, SESSION)
    seqs = [e.seq for e in reopened.iter_from(1)]
    assert kept.seq in seqs, "the repair dropped a valid entry"
    assert orphans[0].seq in seqs, "the chunks were truncated on an offset that was short"
    assert any(
        "cannot be trusted" in r.getMessage() for r in caplog.records
    ), "the refusal to scan was silent"


def test_a_group_refuses_a_cite_that_does_not_return_an_entry():
    """A caller bug is refused, not retried.

    An entry shape the store cannot use will be produced identically every time, so
    it belongs in the refusal class -- a loss now, counted and named. Left as a raw
    TypeError it lands in the "might clear" class the write-behind retries, spending
    the whole attempt budget on a verdict that cannot change and holding every later
    entry of that session behind it.

    Mutation guard: dropping the isinstance check lets a list through to `.get` and
    raises AttributeError instead, which is not a CrewLogError and reddens this.
    """
    session = _session()
    session.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    before = lg.crew_log_path(lg.KIND_SESSION, SESSION).read_bytes()

    with _raises("bad_data") as caught:
        session.append_many(
            [{"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}, "ignorable": True}],
            src="acp",
            cite=lambda seqs: ["not", "an", "entry"],
        )
    assert caught.value.code == "bad_data", f"refused with the wrong code: {caught.value.code}"
    assert (
        lg.crew_log_path(lg.KIND_SESSION, SESSION).read_bytes() == before
    ), "a refused group wrote bytes"


def _fsync_failing_once(burned: list, real=os.fsync):
    """An fsync that fails the first time and works after."""

    def _fsync(fd):
        if not burned:
            burned.append(1)
            raise OSError(5, "EIO")
        return real(fd)

    return _fsync


def test_a_failed_append_leaves_the_file_exactly_as_it_was():
    """A failure has to be DEFINITE, or a retry cannot be safe.

    Bytes reach the file before the fsync runs, so a failing fsync alone leaves an
    outcome nobody knows: the entry may well be durable. The write-behind retains and
    retries a failure, and a retry against an unknown outcome writes the same fact
    twice under two seqs -- a duplicate no reader can tell from a real repeat, in a
    file nothing rewrites.

    So the append rolls itself back: the file returns to the exact bytes it held
    before the attempt, and the retry writes cleanly. Truncating this is not an
    exception to the never-rewrite rule -- the bytes removed are the caller's own
    failed append, and the same append lock is held throughout, so no other handle's
    entries can be inside the range.

    Mutation guard: dropping the rollback leaves the entry on disk and the retry adds
    a second copy, which reddens the count below.
    """
    session = _session()
    session.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    before = path.read_bytes()
    burned: list = []

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fsync", _fsync_failing_once(burned))
        with pytest.raises(OSError):
            session.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")
        assert (
            path.read_bytes() == before
        ), "the failed append left bytes behind; a retry would now duplicate the fact"
        retried = session.append(
            "turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp"
        )
    assert burned, "fsync never failed, so the rollback path was not exercised"

    completed = [e for e in session.iter_from(1) if e.type == "turn/completed"]
    assert len(completed) == 1, f"the fact is on disk twice: seqs {[e.seq for e in completed]}"
    assert retried.seq == completed[0].seq


def test_a_rollback_never_removes_another_writers_entries():
    """The truncation is bounded to this call's own bytes.

    A rollback cuts back to the size the file had when this append started. Another
    handle appending BEFORE that point is behind the cut and untouched; one appending
    after it cannot exist, because the append lock is held across the write and its
    rollback. This pins the first half -- an intruder's entry survives a rollback that
    happens after it.

    Mutation guard: rolling back to a size captured before the intruder's append (or
    truncating the whole tail) destroys its entry and reddens this.
    """
    session = _session()
    session.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    intruder = CrewLog.open(lg.KIND_SESSION, SESSION)
    landed = intruder.append("message/chunk", {"turn": 1, "delta": "keep me"}, src="acp")
    burned: list = []

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fsync", _fsync_failing_once(burned))
        with pytest.raises(OSError):
            session.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")
    assert burned

    survivors = [e.seq for e in CrewLog.open(lg.KIND_SESSION, SESSION).iter_from(1)]
    assert (
        landed.seq in survivors
    ), f"the rollback removed another writer's entry at seq {landed.seq}: {survivors}"


def test_a_failed_group_append_rolls_back_the_whole_group():
    """The group path needs it more than the single one.

    A group is the body-split write -- several chunks plus their citing entry in one
    ``write()`` -- so it is the largest write this format makes and the likeliest to
    be the one that fails part-way. A duplicated group is worse than a duplicated
    entry too: the second copy brings its own citation, and a reader folding the file
    finds the same message body twice under two seq runs.

    Mutation guard: dropping the rollback leaves the group on disk and the retry
    writes a second one, reddening the counts below.
    """
    session = _session()
    session.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    before = path.read_bytes()
    group = [
        {"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}, "ignorable": True},
        {"type": "message/chunk", "data": {"turn": 1, "delta": "bb"}, "ignorable": True},
    ]

    def _cite(seqs):
        return {"type": "message/sent", "data": {"turn": 1, "chunks": list(seqs), "chars": 4}}

    burned: list = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fsync", _fsync_failing_once(burned))
        with pytest.raises(OSError):
            session.append_many(group, src="acp", cite=_cite)
        assert path.read_bytes() == before, "the failed group left bytes behind"
        retried = session.append_many(group, src="acp", cite=_cite)
    assert burned

    entries = list(session.iter_from(1))
    chunks = [e for e in entries if e.type == "message/chunk"]
    citing = [e for e in entries if e.type == "message/sent"]
    assert len(chunks) == 2, f"the chunks are on disk twice: {[e.seq for e in chunks]}"
    assert len(citing) == 1, f"the citation is on disk twice: {[e.seq for e in citing]}"
    assert citing[0].data["chunks"] == [e.seq for e in chunks]
    assert [e.seq for e in retried if e.type == "message/chunk"] == [e.seq for e in chunks]


def test_an_append_whose_rollback_also_fails_says_so():
    """When the cleanup fails too, the caller is told the file may hold residue.

    Whatever broke the write is often still broken, so the rollback can fail as well.
    That leaves bytes no entry claims -- an unterminated or unparseable tail, which is
    the shape the next open truncates, so the recovery exists. What must not happen is
    reporting it as an ordinary failure, because that is a claim the file is untouched.

    Mutation guard: letting the rollback error propagate as a plain OSError makes this
    raise the wrong type and reddens.
    """
    session = _session()
    session.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    burned: list = []

    def _rollback_fails(path, size):
        raise OSError(28, "ENOSPC on the rollback too")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fsync", _fsync_failing_once(burned))
        patch.setattr(store, "_rollback_append", _rollback_fails)
        with _raises(None, lg.IndeterminateAppend) as caught:
            session.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")
    assert burned
    assert "could not be rolled back" in str(caught.value)
    assert caught.value.written, "the error does not carry what was written"
