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

    Chunks appended one at a time could be left on disk with no citing entry after a hard
    kill. One write leaves either the whole group or a torn tail the next append truncates.
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
    """Repair drops chunks whose citing entry never landed.

    The citing entry is the last line, so a torn write leaves only uncited chunks; dropping
    them loses just the message that was mid-write.
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

    A newer format can fail any structural check (`unknown_entry_type`, `bad_header`), and a
    log that claims corruption at its successor invites deletion. So the version is checked
    before the other header checks and before any row is decoded.
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

    An `or {}` fallback would let a missing body or `None` write an entry with no data.
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

    Retention deletes whole segments; while a segment is on disk its entries are readable,
    and `None` must mean "no such entry".
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

    Reading only the newest file would stop at the boundary with `next_before` None, claiming
    the whole history was seen.
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

    The ``chunks`` are the only path from a message to its body, and the lock is
    cross-process. Seqs planned from an unlocked tail read would name the intruder's entries,
    which parse fine. A hook appends through a second handle just before this group takes
    the lock, so the boundary is real, not simulated.
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

    The orphan-group scan walks byte lengths. The skipping posture drops an over-cap record
    unseen, so later offsets come out short and truncation would cut valid entries. The scan
    fails closed instead: nothing is truncated and the warning says why.
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

    A bad entry shape repeats every time, so it must be a CrewLogError refusal. A raw
    TypeError would land in the retry class and block the session's later entries.
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
    """A failure has to be definite, or a retry cannot be safe.

    Bytes reach the file before fsync, so a failed fsync alone leaves an unknown outcome and
    a retry could write the fact twice. The append rolls back to its exact prior bytes; the
    lock is held throughout, so only its own failed bytes are removed.
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

    The rollback cuts to the size at this append's start, so an intruder's earlier entry
    survives; a later one cannot exist while the lock is held.
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
    """A failed group append rolls back the whole group.

    A group is the largest write and the likeliest to fail part-way; a duplicated group would
    bring its own citation, so a reader sees the same body twice.
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
    """When the rollback fails too, the caller is told the file may hold residue.

    The untracked tail is the shape the next open truncates, but reporting an ordinary
    OSError would claim the file is untouched.
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
