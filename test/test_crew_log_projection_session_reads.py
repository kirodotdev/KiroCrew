"""Reading a session's crew log: ``fold_session``, bundle reuse and unit order.

One pass serves every projection, an incremental read refuses a log that moved
backward or was rewritten under it, a bundle is reused only for the file it was
folded from, and a slot's units fold in succession order rather than by the header
clock. Shared logs, entries and fixture writers live in
``crew_log_projection_helpers.py``.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest
from crew_log_projection_helpers import (  # noqa: F401 -- fixtures are used by name
    GATEWAY,
    SESSION,
    _busy_log,
    _entries,
    _isolated_home,
    _log,
    _opened,
    _turn,
)

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, CrewLogError, Ref
from kiro_crew.crew_log import projection as crew_log


@pytest.fixture
def owned_log():
    """Refusal tracebacks can retain the writer beyond the test's lifetime."""
    handle = _log()
    try:
        yield handle
    finally:
        handle.release_ownership()


# --- reading a session ----------------------------------------------------


def test_a_session_with_no_crew_log_folds_to_the_empty_projections():
    bundle = crew_log.fold_session("s-absent")
    assert bundle.last_seq == 0
    assert set(bundle.checkpoints) == set(crew_log.PROJECTION_NAMES)
    assert bundle.projection("status").value["lifecycle"] == "unknown"
    assert crew_log.read_projection("s-absent", "usage").seq == 0


def test_fold_session_serves_every_projection_from_one_pass():
    _busy_log()
    bundle = crew_log.fold_session(SESSION)
    assert set(bundle.checkpoints) == set(crew_log.PROJECTION_NAMES)
    for name in crew_log.PROJECTION_NAMES:
        assert bundle.projection(name).value == crew_log.read_projection(SESSION, name).value


def test_fold_session_continues_from_an_earlier_bundle():
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)
    first = crew_log.fold_session(SESSION)
    _turn(handle, 2, credits=2.0)
    second = crew_log.fold_session(SESSION, since=first)
    assert second.last_seq > first.last_seq
    assert second.projection("usage").value == crew_log.fold_usage(_entries(handle))


def test_fold_session_incremental_refuses_a_backward_seq_like_a_fold_from_the_start(owned_log):
    """A non-advancing seq in the tail refuses BOTH ways -- never one way only.

    The append-only writer cannot produce a duplicate or backward seq, so one
    in the file is external damage. A fold from the start refuses it in
    ``advance``. Without the walked-entry guard, an incremental read drops the
    record below its resume seq unexamined and folds on -- two reads of the
    same bytes disagree and a reader cannot tell which answer it is getting.
    The guard in ``iter_from`` makes the incremental read refuse the same
    damage with the same code.
    """
    handle = owned_log
    _opened(handle)
    _turn(handle, 1)
    bundle = crew_log.fold_session(SESSION)

    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    last_line = path.read_bytes().splitlines(keepends=True)[-1]
    with open(path, "ab") as damaged:
        damaged.write(last_line)  # byte-identical copy: the last seq appears twice
    _turn(handle, 2)  # a real turn past the damage, so the incremental read walks over it

    with pytest.raises(CrewLogError) as from_start:
        crew_log.fold_session(SESSION)
    with pytest.raises(CrewLogError) as incremental:
        crew_log.fold_session(SESSION, since=bundle)

    assert from_start.value.code == lg.CODE_BAD_DATA
    assert incremental.value.code == lg.CODE_BAD_DATA


def test_fold_session_incremental_refuses_a_backward_tail_after_real_growth(owned_log):
    """A regressed physical tail cannot hide valid growth from a cached fold."""
    handle = owned_log
    _opened(handle)
    _turn(handle, 1)
    bundle = crew_log.fold_session(SESSION)

    _turn(handle, 2)
    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    earlier_line = next(
        line
        for line in path.read_bytes().splitlines(keepends=True)
        if json.loads(line).get("seq") == bundle.last_seq
    )
    with open(path, "ab") as damaged:
        damaged.write(earlier_line)

    with pytest.raises(CrewLogError) as from_start:
        crew_log.fold_session(SESSION)
    with pytest.raises(CrewLogError) as incremental:
        crew_log.fold_session(SESSION, since=bundle)

    assert from_start.value.code == lg.CODE_BAD_DATA
    assert incremental.value.code == lg.CODE_BAD_DATA


def test_fold_session_incremental_refuses_same_size_rewrite_of_folded_seq(owned_log):
    """A same-size rewrite behind the resume point must invalidate the cache."""
    handle = owned_log
    _opened(handle)
    _turn(handle, 1)
    bundle = crew_log.fold_session(SESSION)

    path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    before = path.stat()
    lines = path.read_bytes().splitlines(keepends=True)
    damaged = json.loads(lines[-2])
    damaged["seq"] = json.loads(lines[-3])["seq"]
    replacement = (
        json.dumps(damaged, ensure_ascii=True, separators=(",", ":"), sort_keys=False).encode()
        + b"\n"
    )
    assert len(replacement) == len(lines[-2])
    lines[-2] = replacement
    path.write_bytes(b"".join(lines))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    assert path.stat().st_size == before.st_size

    with pytest.raises(CrewLogError) as incremental:
        crew_log.fold_session(SESSION, since=bundle)

    assert incremental.value.code == lg.CODE_BAD_DATA


def test_fold_session_incremental_refuses_older_segment_seq_regression():
    """A seq regression in a NON-newest segment must invalidate the fast path.

    ``handle.path`` names only the newest segment, so a fingerprint taken from
    it alone never sees an older segment move -- the fast return would serve the
    cached bundle while a cold fold refuses the log. The fingerprint covers the
    whole segment set for exactly this case.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    _turn(handle, 2)
    del handle

    # Split the single-segment log into two valid segments: the head keeps the
    # early records, ``log.<first_seq>.jsonl`` carries the tail. Each segment
    # begins with the header line, as the raw walks expect.
    head = lg.crew_log_path(lg.KIND_SESSION, SESSION)
    lines = head.read_bytes().splitlines(keepends=True)
    header, records = lines[0], lines[1:]
    keep, moved = records[: len(records) // 2], records[len(records) // 2 :]
    first_moved_seq = json.loads(moved[0])["seq"]
    head.write_bytes(header + b"".join(keep))
    (head.parent / f"log.{first_moved_seq}.jsonl").write_bytes(header + b"".join(moved))

    handle = CrewLog.open(lg.KIND_SESSION, SESSION)
    bundle = crew_log.fold_session(SESSION, log=handle)

    # Regress a seq INSIDE the older segment, same byte length, and touch only
    # that older file -- the newest segment never changes, which is what the
    # newest-only fingerprint could not see.
    before = head.stat()
    old_lines = head.read_bytes().splitlines(keepends=True)
    damaged = json.loads(old_lines[-1])
    damaged["seq"] = json.loads(old_lines[-2])["seq"] if len(old_lines) > 2 else damaged["seq"]
    replacement = (
        json.dumps(damaged, ensure_ascii=True, separators=(",", ":"), sort_keys=False).encode()
        + b"\n"
    )
    assert len(replacement) == len(old_lines[-1])
    old_lines[-1] = replacement
    head.write_bytes(b"".join(old_lines))
    os.utime(head, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

    with pytest.raises(CrewLogError) as incremental:
        crew_log.fold_session(SESSION, since=bundle)

    assert incremental.value.code == lg.CODE_BAD_DATA


def test_fold_session_unchanged_fast_path_does_not_walk_entries(monkeypatch):
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    bundle = crew_log.fold_session(SESSION)
    calls = 0
    original = CrewLog.iter_from

    def counting(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CrewLog, "iter_from", counting)
    unchanged = crew_log.fold_session(SESSION, since=bundle)

    assert unchanged == bundle
    assert calls == 0


def test_fold_session_old_bundle_without_size_walks_once(monkeypatch):
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    bundle = crew_log.fold_session(SESSION)
    old_bundle = replace(bundle, size=None)
    calls = 0
    original = CrewLog.iter_from

    def counting(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CrewLog, "iter_from", counting)
    refreshed = crew_log.fold_session(SESSION, since=old_bundle)

    assert refreshed.projection("usage") == bundle.projection("usage")
    assert refreshed.size is not None
    assert refreshed.mtime_ns is not None
    assert calls == 1


def test_fold_session_old_bundle_without_mtime_walks_once(monkeypatch):
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    bundle = crew_log.fold_session(SESSION)
    old_bundle = replace(bundle, mtime_ns=None)
    calls = 0
    original = CrewLog.iter_from

    def counting(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CrewLog, "iter_from", counting)
    refreshed = crew_log.fold_session(SESSION, since=old_bundle)

    assert refreshed.projection("usage") == bundle.projection("usage")
    assert refreshed.mtime_ns is not None
    assert calls == 1


def test_fold_session_rebuilds_when_the_log_is_shorter_than_the_bundle():
    """A recreated unit restarts seq, so continuing would swallow the new log.

    The stale bundle carries a DIFFERENT session's state under this session's id
    and a seq beyond this file's end, which is the shape a cache holds after the
    unit it was folded from is removed and recreated. Reusing it returns the other
    session's totals and reports them at this file's seq.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)

    busier = _log("s-busier")
    _opened(busier)
    for turn in (1, 2, 3):
        _turn(busier, turn, credits=5.0)
    foreign = crew_log.fold_session("s-busier")
    ahead = handle.last_seq + 100
    stale = crew_log.SessionProjections(
        session_id=SESSION,
        last_seq=ahead,
        checkpoints={
            name: crew_log.Checkpoint(name=name, last_seq=ahead, state=checkpoint.state)
            for name, checkpoint in foreign.checkpoints.items()
        },
    )

    rebuilt = crew_log.fold_session(SESSION, since=stale)
    honest = crew_log.fold_usage(_entries(handle))
    assert rebuilt.last_seq == handle.last_seq
    assert rebuilt.projection("usage").value == honest
    assert honest["turns"]["completed"] == 1


def test_fold_session_ignores_a_bundle_belonging_to_another_session():
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)
    other = _log("s-other")
    _opened(other)
    foreign = crew_log.fold_session("s-other")
    folded = crew_log.fold_session(SESSION, since=foreign)
    assert folded.projection("usage").value == crew_log.fold_usage(_entries(handle))


def test_fold_session_rebuilds_when_the_bundle_lacks_a_name_it_is_asked_for():
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)
    partial = crew_log.fold_session(SESSION, ("status",))
    full = crew_log.fold_session(SESSION, crew_log.PROJECTION_NAMES, since=partial)
    assert full.projection("usage").value == crew_log.fold_usage(_entries(handle))


def test_fold_session_refuses_an_entry_type_it_cannot_interpret():
    """A required unknown type stops the fold rather than skewing a total."""
    handle = _log()
    _opened(handle)
    path = handle.path
    line = json.dumps(
        {
            "type": "turn/teleported",
            "seq": handle.last_seq + 1,
            "time": 1789000000000,
            "src": GATEWAY,
            "data": {"turn": 1},
        }
    )
    with path.open("a", encoding="utf-8") as sink:
        sink.write(line + "\n")
    with pytest.raises(CrewLogError) as excinfo:
        crew_log.fold_session(SESSION)
    assert excinfo.value.code == lg.CODE_UNKNOWN_ENTRY_TYPE


def test_fold_session_skips_an_ignorable_type_it_cannot_interpret():
    handle = _log()
    _opened(handle)
    line = json.dumps(
        {
            "type": "sample/taken",
            "seq": handle.last_seq + 1,
            "time": 1789000000000,
            "src": GATEWAY,
            "ignorable": True,
            "data": {},
        }
    )
    with handle.path.open("a", encoding="utf-8") as sink:
        sink.write(line + "\n")
    assert crew_log.fold_session(SESSION).projection("status").value["lifecycle"] == "open"


def test_open_session_log_reads_without_claiming_write_ownership():
    """A read must not refuse just because the live writer holds the lease."""
    writer = _log()
    _opened(writer)
    reader = crew_log.open_session_log(SESSION)
    assert reader is not None
    assert reader.last_seq == writer.last_seq
    # The writer still owns its unit: the read took nothing away from it.
    writer.append("session/closed", {"reason": "reset"}, src=GATEWAY)


def test_open_session_log_is_none_for_a_session_that_has_none():
    assert crew_log.open_session_log("s-nothing") is None


def test_known_types_is_the_declared_session_vocabulary():
    assert crew_log.KNOWN_TYPES == frozenset(lg.SESSION_ENTRY_TYPES)


def test_the_fold_registry_and_the_public_name_list_agree():
    assert tuple(crew_log._FOLDS) == crew_log.FOLD_NAMES


def test_a_ref_on_a_session_entry_is_left_to_the_page_path():
    """A fold reads its own crew log only (FR-4), so a ref changes no fold's value."""
    handle = _log()
    _opened(handle)
    handle.append(
        "subagent/spawned",
        {"turn": 1, "agent_id": "sub-1", "agent": "worker", "model": "opus"},
        src=GATEWAY,
        ref=Ref(unit=lg.KIND_SESSION, id="s-child", from_seq=1, to_seq=4),
    )
    value = crew_log.fold_timeline(_entries(handle))
    assert value["moments"][-1]["type"] == "subagent/spawned"
    assert "ref" not in value["moments"][-1]


# --------------------------------------------------------------------------- #
# recreated-then-grown reuse (residual/crash-data-loss-corruption)
# --------------------------------------------------------------------------- #


def test_fold_session_rebuilds_when_the_bundle_origin_does_not_match_the_file():
    """A bundle whose origin differs from the current file is not reused.

    This is the removed-and-recreated case that has already grown PAST the cached
    seq: the seq guard (``since.last_seq <= last_seq``) passes because the file is
    longer than the stale bundle, so without the file-identity check the old
    file's state would be folded over the new file's bytes. A recreated log has a
    different origin (a fresh ``created_at`` and/or inode), so the reuse is
    refused and the fold rebuilds from the start. Filesystem-free so it behaves
    the same on every platform.
    """
    handle = _log()
    _opened(handle)
    for turn in range(1, 6):
        _turn(handle, turn, credits=1.0)

    # A stale bundle from a DIFFERENT file: real checkpoints (so a wrong reuse
    # would fold visibly wrong totals) but a foreign origin and a seq below the
    # current head, the exact recreated-then-grown shape.
    other = _log("s-other-origin")
    _opened(other)
    for turn in (1, 2):
        _turn(other, turn, credits=99.0)
    foreign = crew_log.fold_session("s-other-origin")
    stale = crew_log.SessionProjections(
        session_id=SESSION,
        last_seq=foreign.last_seq,
        checkpoints=foreign.checkpoints,
        origin="not-this-file",
    )
    assert stale.last_seq < handle.last_seq  # seq guard alone would pass

    rebuilt = crew_log.fold_session(SESSION, since=stale)
    honest = crew_log.fold_usage(_entries(handle))
    assert rebuilt.projection("usage").value == honest
    assert rebuilt.origin is not None and rebuilt.origin != stale.origin


def test_fold_session_reuses_the_bundle_when_the_file_is_the_same():
    """The origin guard does not defeat a legitimate incremental reuse."""
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)
    first = crew_log.fold_session(SESSION, log=handle)
    _turn(handle, 2, credits=1.0)
    second = crew_log.fold_session(SESSION, since=first, log=handle)
    assert second.origin == first.origin
    assert second.projection("usage").value == crew_log.fold_usage(_entries(handle))


def _rewrite_in_place(target, lines: list[str]) -> None:
    """Replace *target*'s whole content, keeping its device and inode.

    A removal and a create would hand the new file a new inode on most runs, and
    then device and inode refuse the reuse on their own -- so a test written that
    way is decided by whether the filesystem recycled the inode, and passes
    without the identity it means to exercise. Truncating in place is the
    recycled-inode case made deterministic: same device, same inode, different
    file. The assertion below is what keeps it that way.
    """
    before = target.stat()
    with target.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")
    after = target.stat()
    assert (after.st_dev, after.st_ino) == (
        before.st_dev,
        before.st_ino,
    ), "the rewrite moved the file, so device and inode alone would refuse it"


def test_the_file_identity_reads_the_creation_stamp_from_disk_not_from_the_handle():
    """A handle's own header describes the file that existed when it was opened.

    The identity exists to separate a recreated log from the one a reader started
    on. Taken from the open handle, the stamp cannot do that: it is parsed once at
    open and keeps answering for the replaced file, leaving device and inode as the
    only live signal -- and those agree whenever the new file landed on the freed
    inode, which is the common case. Here they are held IDENTICAL on purpose, so
    only a stamp read from disk can tell the two files apart.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)
    before = crew_log.log_origin(handle)
    assert before is not None

    lines = handle.path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["createdAt"] = header["createdAt"] + 1
    _rewrite_in_place(handle.path, [json.dumps(header), *lines[1:]])

    after = crew_log.log_origin(handle)
    assert after is not None, "a readable header is a provable identity"
    assert after != before


def test_a_log_recreated_under_a_held_handle_is_folded_again_rather_than_spliced():
    """The caller passes its handle back in, so the reuse must not trust it.

    ``fold_session`` takes ``log=`` from a caller that already holds one, which is
    what makes this reachable without any race: the handle was opened before the
    recreation, so a stamp read from it still names the retired file. The seq guard
    passes because the new file is longer, so nothing else stands between the old
    file's folded state and the new file's bytes -- the totals below are the old
    turn plus part of the new ones, a number no file on disk ever held.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)
    first = crew_log.fold_session(SESSION, log=handle)

    # A different, longer log, written over the first one's bytes: the same id, a
    # fresh creation stamp, and turns whose credits could not come from the file
    # the bundle was folded from.
    donor = _log("s-recreated")
    _opened(donor)
    for turn in (1, 2, 3):
        _turn(donor, turn, credits=99.0)
    body = donor.path.read_text(encoding="utf-8").splitlines()
    header = json.loads(body[0])
    header["id"] = SESSION
    retired = json.loads(handle.path.read_text(encoding="utf-8").splitlines()[0])
    header["createdAt"] = retired["createdAt"] + 1
    _rewrite_in_place(handle.path, [json.dumps(header), *body[1:]])

    settled = CrewLog.open(lg.KIND_SESSION, SESSION)
    assert first.last_seq <= settled.last_seq, "the seq guard alone would allow the reuse"
    honest = crew_log.fold_usage(_entries(settled))

    rebuilt = crew_log.fold_session(SESSION, since=first, log=handle)

    assert rebuilt.projection("usage").value == honest
    assert rebuilt.origin is not None and rebuilt.origin != first.origin


# --- the order a slot's units fold in -------------------------------------


def test_usage_units_order_by_succession_not_wall_clock(monkeypatch):
    """MUTATION-SENSITIVE: a clock rollback must not invert two units of one slot.

    Unit ``b`` REPLACED unit ``a`` (its ``session/opened`` names ``a`` as its
    predecessor), so ``b`` is the newer unit and its rows must fold LAST -- the fold
    applies a later unit over an earlier one. But the clock stepped backward before
    ``b`` was created, so ``b``'s header ``createdAt`` (100) is SMALLER than ``a``'s
    (200). A wall-clock sort would put ``b`` first, land its rows at the front of the
    per-turn window, and the front-trim would evict the newest unit's rows. Ordering
    by the durable ``previous_sid`` succession chain keeps ``b`` last regardless.
    """
    created = {"a": 200, "b": 100}
    previous = {"a": None, "b": "a"}
    monkeypatch.setattr(crew_log, "session_units_for_slot", lambda slot: ("a", "b"))
    monkeypatch.setattr(crew_log, "unit_header_created_at", lambda kind, uid: created.get(uid))
    monkeypatch.setattr(crew_log, "unit_opened_previous", lambda kind, uid: previous.get(uid))
    assert crew_log.units_in_succession("chat-1") == ("a", "b")


def test_usage_units_keep_disconnected_chains_contiguous(monkeypatch):
    """Two UNRELATED chains fold each as a contiguous run, ordered by root createdAt.

    A slot can hold two chains the ``previous_sid`` links never relate -- a session
    recreated after its predecessor's log was pruned, two logs whose link was never
    written. Chain B is b0->b1->b2 (three deep); chain A is a0->a1 (two deep) and its
    root was created FIRST. Ordering by depth alone would interleave them
    (a0,b0,a1,b1,b2) and split each chain's predecessor edges apart. The order must be
    chain-contiguous -- A's whole run then B's whole run -- with the runs ordered by
    their roots' ``createdAt``, so every ``previous_sid`` edge stays adjacent.
    """
    # a-root created before b-root; within each chain the header clock is irrelevant.
    created = {"a0": 100, "a1": 500, "b0": 200, "b1": 50, "b2": 300}
    previous = {"a0": None, "a1": "a0", "b0": None, "b1": "b0", "b2": "b1"}
    # Store listing order deliberately interleaves the two chains, to prove the resolver
    # regroups them rather than trusting the listing.
    monkeypatch.setattr(
        crew_log, "session_units_for_slot", lambda slot: ("a0", "b0", "a1", "b1", "b2")
    )
    monkeypatch.setattr(crew_log, "unit_header_created_at", lambda kind, uid: created.get(uid))
    monkeypatch.setattr(crew_log, "unit_opened_previous", lambda kind, uid: previous.get(uid))
    # A's run (root createdAt 100) precedes B's run (root createdAt 200); each run is
    # predecessor-first and unbroken.
    assert crew_log.units_in_succession("chat-1") == ("a0", "a1", "b0", "b1", "b2")


def test_usage_units_unrelated_roots_without_a_clock_keep_store_order(monkeypatch):
    """Two unrelated roots with no readable createdAt keep their store listing order.

    Neither unit names the other, and the header clock is unreadable for both, so the
    chain has nothing to say and neither does the clock. The resolver must fall back to
    the store's own listing order (the creation order it established), NOT an arbitrary
    id order that could sort a retired unit's rows ahead of a live one's.
    """
    monkeypatch.setattr(crew_log, "session_units_for_slot", lambda slot: ("older", "newer"))
    monkeypatch.setattr(crew_log, "unit_header_created_at", lambda kind, uid: None)
    monkeypatch.setattr(crew_log, "unit_opened_previous", lambda kind, uid: None)
    assert crew_log.units_in_succession("chat-1") == ("older", "newer")


def test_usage_units_single_unit_skips_the_chain_read(monkeypatch):
    """One unit has nothing to reorder, so the predecessor read is not even paid."""
    monkeypatch.setattr(crew_log, "session_units_for_slot", lambda slot: ("only",))

    def _boom(*_a, **_k):
        raise AssertionError("unit_opened_previous must not be read for a single unit")

    monkeypatch.setattr(crew_log, "unit_opened_previous", _boom)
    assert crew_log.units_in_succession("chat-1") == ("only",)


def test_usage_fold_uses_the_succession_order(monkeypatch):
    """The usage fold's unit resolver routes through the durable-succession helper."""
    monkeypatch.setattr(crew_log, "units_in_succession", lambda slot: ("x", "y"))
    assert crew_log._slot_units_for_fold("chat-1", "usage") == ("x", "y")


def test_agentic_fold_uses_the_succession_order(monkeypatch):
    """The agentic fold's unit resolver routes through the durable-succession helper.

    This fold keeps the LATEST value per field, so whichever unit is folded last wins
    each cell. Under the header clock a backward step between two units of one slot
    sorts the retired unit last, and its stale value would overwrite one the live
    session wrote and the write path accepted -- silently, because both are well-formed.
    """
    monkeypatch.setattr(crew_log, "units_in_succession", lambda slot: ("x", "y"))
    assert crew_log._slot_units_for_fold("chat-1", crew_log.DASHBOARD_FOLD_NAME) == ("x", "y")


def test_agentic_fold_does_not_fall_through_to_the_header_clock(monkeypatch):
    """The header fallthrough must not be what answers for this fold.

    Asserted by making the fallthrough the ONLY thing that could answer: if the
    resolver reached ``session_units_for_slot`` for the agentic fold it would return
    the clock order below, which is the inversion the succession read exists to undo.
    """
    monkeypatch.setattr(crew_log, "session_units_for_slot", lambda slot: ("retired", "live"))
    monkeypatch.setattr(crew_log, "units_in_succession", lambda slot: ("live", "retired"))
    assert crew_log._slot_units_for_fold("chat-1", crew_log.DASHBOARD_FOLD_NAME) == (
        "live",
        "retired",
    )
