"""Size-rotation tests for kiro_crew.sel, and the writers and readers that race it.

Pins rotation at the size cap and segment retention, the cross-process rotation lock,
which threads may rotate, genesis handling in a fresh log, the collision retry's
unconditional re-anchor, and a prune or a foreign process racing a rotation. It also
holds the session-directory ratchet, which judges every module listed in
``_SEL_FAMILY``.
``test_sel.py`` holds the core contract and names the other topic modules; the shared
fixtures and helpers live in ``sel_helpers.py``.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sel_helpers import (  # noqa: F401 -- fixtures are requested by name
    _fill,
    _make_event,
    reset_singleton,
    sel_dir_fixture,
    small_segments_fixture,
)

import kiro_crew.sel as sel_mod
from kiro_crew import platform_compat
from kiro_crew import sel as kiro_crew_sel
from kiro_crew.sel import SecurityEventLog


def _lock_is_held(lock_path: Path) -> bool:
    """Whether *lock_path* is exclusively locked, observed from another thread.

    ``fcntl.flock`` is per open-file-description, so a fresh ``open`` in this
    same process still contends with the holder's fd -- which is what lets a test
    assert that a critical section really runs under the cross-process lock. On
    Windows ``msvcrt.locking`` is per-fd in the same way. Returns False when the
    lock file does not exist yet (nothing has been serialized).
    """
    if not lock_path.exists():
        return False
    with open(lock_path, "a+b") as probe:
        if platform_compat.try_acquire_lock(probe.fileno(), exclusive=True):
            platform_compat.release_lock(probe.fileno())
            return False
    return True


#: Iteration cap for the bounded poll loops below. Generous against a healthy
#: writer (the rotation loop needs 1-2 batches; a healthy top-up usually needs
#: zero), tight enough that a genuinely broken one fails fast without flooding
#: the shared session dir -- that dir lives under pytest's basetemp, which is
#: retained across runs, so an unbounded failure loop would persist tens of MB
#: per red run.
_POLL_ITERATION_CAP = 40


def _fill_until_over_cap(log: SecurityEventLog, *, deadline: float, start: int) -> int:
    """Top up through the async writer until the live log is over the size cap.

    Two mechanisms can leave the live log BELOW the cap after a large fill even
    with a perfectly healthy writer: ``flush()`` waits on the pending-event
    counter with a bounded timeout and RETURNS on expiry, so under runner load
    the tail of the fill may not be on disk yet; and a mid-fill batch boundary
    can itself enter the rotation window and rotate, resetting the live log to
    nearly empty. Both are harness properties, not defects, so the over-the-cap
    precondition is established by topping up in small batches rather than
    asserted after one fixed fill. Bounded by *deadline* and by
    ``_POLL_ITERATION_CAP``; the diagnostic reports the pending-queue depth so
    a wedged writer is distinguishable from a rotation problem.

    Returns the next unused ``seq=`` number for the caller's own fills.
    """
    seq = start
    iterations = 0
    while log._live_size() <= sel_mod._SEGMENT_MAX_BYTES:
        iterations += 1
        # flake-ok: pre-existing 30 s wall-clock ceiling on a bounded top-up loop -- moved verbatim; tracked for a de-flake pass
        assert iterations <= _POLL_ITERATION_CAP and time.monotonic() < deadline, (
            f"precondition never reached: live log stayed at {log._live_size()} "
            f"bytes (cap {sel_mod._SEGMENT_MAX_BYTES}) after {iterations - 1} "
            f"top-up batches, {log._pending} events still pending in the writer"
        )
        _fill(log, 5, start=seq)
        seq += 5
        log.flush()
    return seq


class TestSizeRotation:
    """The log is closed at a size cap and retained as N segments.

    Before this, ``security_events.jsonl`` was a single file with no cap: a
    long-running install measured 4.09 GB, which made the only sanctioned reader
    impractical and made every append and read pay the size.
    """

    def test_live_log_is_closed_at_the_size_cap(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        live = sel_dir / "security_events.jsonl"
        segments = log._segments_oldest_first()
        assert segments, "no rotation happened; the log grew unbounded"
        assert live.stat().st_size < sel_mod._SEGMENT_MAX_BYTES * 2

    def test_retention_keeps_only_the_newest_segments(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 600)
        segments = log._segments_oldest_first()
        assert len(segments) == sel_mod._SEGMENT_KEEP
        # The bound that matters: total on-disk size cannot exceed the cap times
        # the segments kept plus the live log.
        total = sum(p.stat().st_size for p in segments)
        total += (sel_dir / "security_events.jsonl").stat().st_size
        ceiling = sel_mod._SEGMENT_MAX_BYTES * (sel_mod._SEGMENT_KEEP + 2)
        assert total < ceiling, f"{total} bytes exceeds the retention ceiling {ceiling}"

    def test_retention_deletes_the_oldest_not_the_newest(self, sel_dir, small_segments):
        """The sequence must keep rising across deletions.

        A name reused after retention freed it would sort as the OLDEST segment,
        so the next sweep would delete the log that was just closed and keep the
        stale ones — silently discarding the newest audit history.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 600)
        seqs = [sel_mod._segment_seq(p) for p in log._segments_oldest_first()]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        # Newest surviving segment is the immediate predecessor of the live log.
        claimed = json.loads(
            (sel_dir / "security_events.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert claimed["metadata"]["previous_segment"] == log._segments_oldest_first()[-1].name

    def test_each_segment_is_an_independent_chain(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        for path in log._segments_oldest_first():
            first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            assert first["prev_hash"] == "", f"{path.name} chains across the boundary"
            if sel_mod._segment_seq(path) > 1:
                # Only the very first segment ever closed has no predecessor to
                # name; every later one opens with the boundary record.
                assert first["event_type"] == "sel_rotation"

    def test_rotation_record_names_the_closed_segment(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        closed = log._segments_oldest_first()[-1]
        opener = json.loads(
            (sel_dir / "security_events.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert opener["metadata"]["previous_segment"] == closed.name
        assert opener["metadata"]["previous_bytes"] > 0

    def test_the_rotation_record_makes_no_predecessor_hash_claim(self, sel_dir, small_segments):
        """A claim about the closed segment's tip cannot be kept true.

        A segment is not immutable the instant it is renamed: another process may
        still hold a writable fd to that inode and land a record after the tip is
        read. Any hash captured would then be stale, and verification would report
        an untampered log as compromised -- the worst failure mode available. The
        name is enough to walk the sequence, and forgery is caught per record.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        opener = json.loads(
            (sel_dir / "security_events.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert "previous_entry_hash" not in opener["metadata"]

    def test_a_late_append_into_the_closed_segment_still_verifies(self, sel_dir, small_segments):
        """The exact race the dropped claim could not survive.

        An appender holding an fd to the live log writes AFTER the rotator renamed
        it, so the record lands in the segment -- correctly chained, because the fd
        guard let it through precisely when the identity matched. Nothing about that
        may read as corruption.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segment = log._segments_oldest_first()[-1]
        tip = json.loads(segment.read_text(encoding="utf-8").strip().splitlines()[-1])
        # Stand in for the appender that was mid-write across the rename: one more
        # correctly chained record appended to the already-closed segment.
        late = _make_event(event_id="late0", resources="late")
        late.prev_hash = tip["entry_hash"]
        late.entry_hash = log._compute_hash(late)
        with open(segment, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(late)) + "\n")

        total, valid = log.verify_integrity()
        assert (
            total == valid
        ), f"{total - valid} entries reported as compromised by a benign late append"

    def test_verify_integrity_spans_segments(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        segment_lines = sum(
            len(p.read_text(encoding="utf-8").strip().splitlines())
            for p in log._segments_oldest_first()
        )
        total, valid = log.verify_integrity()
        assert total > segment_lines, "verification ignored the rotated segments"
        assert total == valid, f"{total - valid} entries failed verification"

    def test_deleting_an_aged_out_segment_leaves_survivors_verifiable(
        self, sel_dir, small_segments
    ):
        """The property rotation had to preserve.

        A retention deletion must not look like tampering. With one chain across
        the whole log, dropping the oldest file would orphan the next file's
        first ``prev_hash`` and report a break for every sweep.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        log._segments_oldest_first()[0].unlink()
        total, valid = log.verify_integrity()
        assert total == valid, f"retention deletion produced {total - valid} invalid entries"

    def test_tampering_inside_a_segment_is_still_caught(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        victim = log._segments_oldest_first()[-1]
        lines = victim.read_text(encoding="utf-8").strip().splitlines()
        record = json.loads(lines[2])
        record["outcome"] = "allowed"
        lines[2] = json.dumps(record)
        victim.write_text("\n".join(lines) + "\n", encoding="utf-8")
        total, valid = log.verify_integrity()
        assert valid < total, "an edited record inside a rotated segment verified clean"

    def test_a_rewritten_segment_is_reported_per_record(self, sel_dir, small_segments):
        """Substituted content is caught by signatures, not by a boundary claim.

        The claim a previous revision carried is gone (see
        ``_rotation_event``), so this is the property that has to hold on its own:
        a segment whose content was replaced cannot produce valid per-record HMACs,
        because the key lives outside the log directory.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        victim = log._segments_oldest_first()[-1]
        rows = [json.loads(line) for line in victim.read_text(encoding="utf-8").splitlines()]
        # Rewrite the segment with plausible-looking but unsigned records.
        forged = []
        for row in rows:
            row["outcome"] = "allowed"
            forged.append(json.dumps(row))
        victim.write_text("\n".join(forged) + "\n", encoding="utf-8")
        total, valid = log.verify_integrity()
        assert valid < total, "a rewritten segment verified clean"

    def test_a_truncated_segment_does_not_read_as_corruption(self, sel_dir, small_segments):
        """Losing the TAIL of a segment leaves the surviving records verifiable.

        Retention and a crash both produce this shape, and neither is tampering of
        the surviving records. With the old boundary claim a dropped final record
        made the SUCCESSOR's rotation record read as invalid; nothing here may.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        predecessor = log._segments_oldest_first()[-1]
        kept = predecessor.read_text(encoding="utf-8").strip().splitlines()
        predecessor.write_text("\n".join(kept[:-1]) + "\n", encoding="utf-8")
        total, valid = log.verify_integrity()
        assert total == valid, f"{total - valid} surviving entries reported as compromised"

    def test_only_matching_names_are_treated_as_segments(self, sel_dir, small_segments):
        """Retention must never delete a file an operator parked in the dir."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 600)
        stray = sel_dir / "security_events.d" / "operator-notes.txt"
        stray.write_text("keep me", encoding="utf-8")
        _fill(log, 600, start=600)
        assert stray.exists(), "retention deleted a non-segment file"
        assert stray not in log._segments_oldest_first()

    def test_rotation_failure_still_writes_the_event(self, sel_dir, small_segments, caplog):
        """An audit record must never be lost to a rotation that cannot happen."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        before = len(log.recent(limit=10_000))
        with patch("kiro_crew.sel.os.replace", side_effect=OSError("boom")):
            _fill(log, 300, start=1000)
        after = log.recent(limit=10_000)
        assert len(after) > before, "events were dropped when rotation failed"
        assert after[0]["resources"] == "seq=1299"

    def test_segment_dir_is_owner_only(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segment_dir = sel_dir / "security_events.d"
        assert segment_dir.is_dir()
        if os.name != "nt":  # Windows uses the DACL, not a POSIX mode
            assert oct(segment_dir.stat().st_mode & 0o777) == "0o700"

    def test_prune_ages_out_whole_segments(self, sel_dir, small_segments):
        """Age retention must reach the segments, not just the live log.

        Otherwise a rotated segment could outlive the retention window
        indefinitely, because only the live file was ever rewritten.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        base = datetime(2020, 1, 1, tzinfo=timezone.utc)
        for i in range(200):
            log.log(
                _make_event(
                    event_id=f"old{i:04d}",
                    timestamp=(base + timedelta(seconds=i)).isoformat(),
                    resources=f"old={i}",
                )
            )
        assert log._segments_oldest_first(), "precondition: rotation happened"
        removed = log.prune(keep_days=365)
        assert removed > 0
        assert log._segments_oldest_first() == [], "aged-out segments survived the sweep"

    def test_prune_keeps_a_segment_that_straddles_the_cutoff(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)  # 2026 timestamps — inside any sane retention window
        segments = log._segments_oldest_first()
        assert segments, "precondition: rotation happened"
        log.prune(keep_days=365 * 100)
        assert log._segments_oldest_first() == segments


class TestRotationIsSerializedAcrossProcesses:
    """Rotation runs under a cross-process lock, taken without blocking.

    ``_lock`` is a thread lock, but several processes share one data home (the
    gateway, the CLI, cron) and append to the same file, so they reach the size
    cap at the same moment. Unserialized, two of them pick the same target name
    and the second ``os.replace`` moves the freshly recreated live log onto the
    segment the first just closed.
    """

    def test_the_acquire_never_blocks(self, sel_dir, small_segments):
        """A critical audit is written inline, sometimes on the event loop.

        Parking that caller on another process's rotation is the
        no-blocking-call-on-event-loop hazard, and rotation is deferrable while an
        audit write is not -- so the blocking lock helper must not be used for
        ROTATION. The cross-process CHAIN lock is the one sanctioned exception:
        off the event loop it blocks by design (the background writer absorbs
        contention there), and its on-loop path is a single nonblocking attempt
        covered by TestCrossProcessSafety -- so the guard here permits exactly
        the chain-lock sidecar's fd and nothing else.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        sidecar = sel_dir / kiro_crew_sel._TRUST_SUBDIR / kiro_crew_sel._SEL_LOCK_FILE
        real_file_lock = platform_compat.file_lock

        def rotation_must_not_block(fd, *, exclusive):
            st = os.fstat(fd)
            try:
                side = sidecar.stat()
            except OSError:
                side = None
            if side is None or (st.st_dev, st.st_ino) != (side.st_dev, side.st_ino):
                raise AssertionError("blocking lock helper used on the audit path")
            return real_file_lock(fd, exclusive=exclusive)

        with patch(
            "kiro_crew.sel.platform_compat.file_lock",
            side_effect=rotation_must_not_block,
        ):
            _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: rotation happened"

    def test_the_rotation_step_runs_under_the_lock(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        lock_path = sel_dir / "security_events.d" / ".rotate.lock"
        held: list[bool] = []
        real_rotate = SecurityEventLog._rotate_under_lock

        def recording_rotate(inner_self):
            held.append(_lock_is_held(lock_path))
            return real_rotate(inner_self)

        with patch.object(SecurityEventLog, "_rotate_under_lock", recording_rotate):
            _fill(log, 200)
        assert held and all(held), "rotation ran without holding the cross-process lock"

    def test_below_the_cap_no_cross_process_lock_is_taken(self, sel_dir, small_segments):
        """The audit hot path must not pay for a lock it does not need."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        with patch("kiro_crew.sel.platform_compat.try_acquire_lock") as locker:
            _fill(log, 3)  # nowhere near the cap
        locker.assert_not_called()

    def test_contention_defers_rotation_without_a_stale_tip(self, sel_dir, small_segments):
        """Losing the lock must not mean appending from a pre-rotation tip.

        This is what makes skipping safe instead of merely fast: the contended
        path re-reads the live log's identity and re-anchors before the caller
        chains, so a rotation that already happened cannot orphan our record.

        The interleaving has to be exact, or the assertion passes for the wrong
        reason: at the top of the window we must still see the OLD file at the cap
        (the winner has not renamed yet, so no replacement is detectable there),
        and only the CONTENDED re-check may observe the swap. The identities are
        therefore synthesized -- first call same inode grown to the cap, second
        call a new inode -- so the top-of-window check cannot fire.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        stale_tip = log._last_hash
        live = sel_dir / "security_events.jsonl"
        pre_dev, pre_ino, _pre_size = log._live_seen

        # A sibling has rotated and written its own first record (see the
        # foreign-rotation suite for why this is built by hand).
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        os.replace(live, segment_dir / "security_events-000001-20260821T000000Z.jsonl")
        sibling_event = _make_event(event_id="sib1", resources="sibling")
        sibling_event.prev_hash = ""
        sibling_event.entry_hash = log._compute_hash(sibling_event)
        live.write_text(json.dumps(asdict(sibling_event)) + "\n", encoding="utf-8")
        assert sibling_event.entry_hash != stale_tip

        log._live_seen = (pre_dev, pre_ino, _pre_size)
        log._last_hash = stale_tip
        with patch("kiro_crew.sel.platform_compat.try_acquire_lock", return_value=False):
            with patch.object(
                SecurityEventLog,
                "_live_identity",
                side_effect=[
                    # Top of window: same file, grown to the cap. NOT a replacement,
                    # so the fast-path re-anchor must not fire here.
                    (pre_dev, pre_ino, sel_mod._SEGMENT_MAX_BYTES),
                    # Contended re-check: the winner's rename is now visible.
                    (pre_dev, pre_ino + 1, 512),
                ],
            ):
                with log._rotation_window():
                    pass
        assert (
            log._last_hash == sibling_event.entry_hash
        ), "kept a pre-rotation tip after deferring on contention"

    def test_a_serialization_failure_still_writes_the_event(self, sel_dir, small_segments):
        """An unusable lock must not cost an audit record."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        before = log._segments_oldest_first()
        with patch.object(SecurityEventLog, "_open_rotation_lock", return_value=None):
            _fill(log, 50, start=3000)
        assert log._segments_oldest_first() == before, "rotated without the lock"
        assert log.recent(limit=1)[0]["resources"] == "seq=3049"

    def test_the_lock_file_is_never_treated_as_a_segment(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 600)
        lock = sel_dir / "security_events.d" / ".rotate.lock"
        assert lock.exists(), "rotation did not take the cross-process lock"
        assert lock not in log._segments_oldest_first()
        total, valid = log.verify_integrity()
        assert total == valid, "the lock file was verified as an audit segment"

    def test_the_rotation_lock_is_never_written_through(self, sel_dir, small_segments):
        """Same contract as the chain-lock sidecar, at the rotation mutex.

        A Windows byte-range lock is mandatory, so a byte written on the way to
        the lock fails with ``EACCES`` against a sibling that acquired first —
        which declines a rotation over a lock that is working.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 600)
        lock = sel_dir / "security_events.d" / ".rotate.lock"
        assert lock.stat().st_size == 0, "a byte was written on the way to the lock"

    def test_a_planted_lock_link_is_not_opened_through(self, sel_dir, small_segments):
        """Opening the mutex CREATES and chmods it, so a link must not be followed."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        protected = sel_dir / "protected.json"
        protected.write_text('{"keep": "me"}\n', encoding="utf-8")
        before = protected.read_bytes()
        planted = segment_dir / ".rotate.lock"
        planted.unlink(missing_ok=True)
        try:
            planted.symlink_to(protected)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/filesystem")

        _fill(log, 200, start=500)

        assert protected.read_bytes() == before, "rotation wrote through the planted link"
        assert not planted.is_symlink(), "the planted link survived"

    def test_an_unremovable_lock_link_declines_to_rotate(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        before = log._segments_oldest_first()
        with patch("kiro_crew.sel.platform_compat.is_link_or_junction") as is_link:
            # The segment DIR check must still pass; only the lock path is linked.
            is_link.side_effect = lambda p: Path(p).name == ".rotate.lock"
            with patch(
                "kiro_crew.sel.platform_compat.unlink_link_or_junction",
                side_effect=OSError("read-only"),
            ):
                _fill(log, 300, start=4000)
        assert log._segments_oldest_first() == before, "rotated through a linked lock"
        assert log.recent(limit=1)[0]["resources"] == "seq=4299"

    def test_a_rotation_lost_to_another_process_re_anchors_the_chain(self, sel_dir, small_segments):
        """Winning the lock but finding the log already rotated must re-anchor.

        The cached tip points at a record that a sibling process has just moved
        into a closed segment; appending from it would leave the new live log's
        first record naming a ``prev_hash`` that is not in the same file.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        stale_tip = log._last_hash

        # Stand in for the sibling process: force one real rotation, so the live
        # log is a fresh file whose tip differs from what we cached.
        with patch.object(SecurityEventLog, "_live_size", return_value=sel_mod._SEGMENT_MAX_BYTES):
            log._rotate_under_lock()
        fresh_tip = log._last_hash
        assert fresh_tip not in ("", stale_tip)
        assert fresh_tip == log._read_last_hash()

        # Our process still holds the pre-rotation tip and, holding the lock,
        # finds the log already small — the lost-the-race branch.
        log._last_hash = stale_tip
        with patch.object(SecurityEventLog, "_live_size", return_value=0):
            log._rotate_under_lock()
        assert log._last_hash == fresh_tip, "kept a chain tip from a closed segment"


class TestRetentionStopsAtAnUndeletableSegment:
    def test_a_failed_oldest_delete_does_not_eat_newer_history(
        self, sel_dir, small_segments, monkeypatch
    ):
        """Skipping past it would trade newer evidence for stuck older evidence.

        The un-deletable segment keeps occupying a retention slot either way; what
        must not happen is deleting the segments BEHIND it to make room.

        Rotation enforces retention as it goes, so a plain fill leaves exactly
        ``_SEGMENT_KEEP`` segments and no excess to sweep -- the branch under test
        would never run. Accumulate under a LARGER keep, then tighten it, so the
        sweep has real excess and a real choice about which files to delete.
        """
        monkeypatch.setattr(sel_mod, "_SEGMENT_KEEP", 6)
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 700)
        accumulated = log._segments_oldest_first()
        assert len(accumulated) == 6, f"precondition: expected 6 segments, got {len(accumulated)}"

        monkeypatch.setattr(sel_mod, "_SEGMENT_KEEP", 3)
        assert len(accumulated) - sel_mod._SEGMENT_KEEP == 3, "precondition: 3 to sweep"
        oldest = accumulated[0]
        real_unlink = Path.unlink

        def refuse_oldest(inner_self, *args, **kwargs):
            if inner_self == oldest:
                raise OSError("permission denied")
            return real_unlink(inner_self, *args, **kwargs)

        with patch.object(Path, "unlink", refuse_oldest):
            deleted = log._enforce_segment_retention_locked()

        assert deleted == 0, "deleted something despite the oldest being stuck"
        survivors = log._segments_oldest_first()
        for kept in accumulated:
            assert kept in survivors, (
                f"{kept.name} was deleted to make room for a segment that could not "
                "be removed -- newer audit history traded for older"
            )


class TestASiblingsAppendDoesNotReadAsCorruption:
    """Another process appending is exactly HOW the log crosses the cap.

    An append makes the file GROW, which is legitimate, so the replacement check
    stays deliberately silent while our cached tip goes stale. An earlier revision
    put a predecessor-hash claim in the rotation record and that stale tip made
    verification report an untampered log as compromised; the claim is gone (see
    ``_rotation_event``), and this pins that the benign case stays clean.
    """

    def test_rotating_after_a_sibling_append_verifies_clean(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        our_cached_tip = log._last_hash
        live = sel_dir / "security_events.jsonl"

        # A sibling appends one correctly-chained record. Written directly: going
        # through the API would update OUR view of the file.
        sibling = _make_event(event_id="sibtip", resources="sibling")
        sibling.prev_hash = our_cached_tip
        sibling.entry_hash = log._compute_hash(sibling)
        with open(live, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(sibling)) + "\n")
        assert log._last_hash == our_cached_tip, "precondition: our tip is now stale"

        # _rotate_under_lock is called directly, so create the dir its caller
        # would normally have ensured.
        (sel_dir / "security_events.d").mkdir(parents=True, exist_ok=True)
        with patch.object(SecurityEventLog, "_live_size", return_value=sel_mod._SEGMENT_MAX_BYTES):
            log._rotate_under_lock()

        opener = json.loads(live.read_text(encoding="utf-8").splitlines()[0])
        assert opener["event_type"] == "sel_rotation"
        total, valid = log.verify_integrity()
        assert total == valid, f"{total - valid} entries reported as compromised"


class TestCriticalWritesDoNotRotateInline:
    """A critical audit runs on its caller's thread, sometimes the event loop.

    Doing rotation's filesystem work there -- a directory scan, a rename, a
    retention sweep -- would stall the loop. Rotation is deferrable; an audit write
    is not, so the inline path writes and leaves rotation to the background writer.

    These are the only tests here that exercise the ASYNC writer, so they omit
    ``base_dir`` and use the session-scoped SEL directory from the rootdir
    ``_isolate_sel_default_dir`` fixture. That is the repo convention for a reason
    specific to this class: the writer is a daemon thread on a process singleton,
    and against a per-test ``tmp_path`` it outlives the test and RE-CREATES the
    directory on its next flush (``_flush_batch`` mkdirs), so a stray directory
    reappears after the test's own cleanup removed it. Assertions here are all
    relative to what the shared directory already holds.
    """

    def test_a_critical_write_skips_the_rotation_window(self, small_segments):
        log = SecurityEventLog(sync=False)
        # Fill past the cap through the background writer, then flush so the log
        # is genuinely over budget when the critical write arrives.
        _fill(log, 200)
        log.flush()
        segments_before = log._segments_oldest_first()

        entered: list[str] = []
        real_window = SecurityEventLog._rotation_window

        def recording_window(inner_self):
            entered.append("yes")
            return real_window(inner_self)

        with patch.object(SecurityEventLog, "_rotation_window", recording_window):
            log.log(_make_event(event_id="crit0", resources="critical"), critical=True)
        assert entered == [], "a critical inline write entered the rotation window"
        # The record still landed -- that is the half that is not deferrable.
        assert any(e["resources"] == "critical" for e in log.recent(limit=20))
        assert log._segments_oldest_first() == segments_before

    def test_the_background_writer_still_rotates(self, small_segments):
        """Deferring must not mean never.

        Batches, not one shot, for two reasons that are both harness properties
        of the async writer: the window is entered once per batch and reads the
        size ALREADY on disk -- a batch sees the file below the cap and correctly
        does not rotate, which is the documented one-batch overshoot -- and
        ``flush()`` waits on the pending counter with a bounded timeout and
        returns on expiry, so under load a batch may not be on disk when the
        test looks. The test therefore polls for the condition with a bounded
        budget instead of asserting after a fixed number of batches
        (testing-conventions § Determinism).

        The observable is the highest segment SEQUENCE NUMBER, not the segment
        count. Rotation ends with a retention sweep, and this class runs against
        the shared session dir, so how many segments it already holds depends on
        which tests ran earlier on this worker: at ``_SEGMENT_KEEP`` segments a
        successful rotation adds one and the sweep deletes the oldest in the same
        breath, leaving the COUNT flat -- a real rotation that a count comparison
        calls "never rotated". The sequence only ever rises
        (``_next_segment_path`` continues from the highest segment still on
        disk, precisely so retention cannot make it go backwards), so it
        observes the rotation no matter what the sweep did.
        """
        log = SecurityEventLog(sync=False)
        _fill(log, 200)
        log.flush()

        def highest_seq() -> int:
            segments = log._segments_oldest_first()
            return sel_mod._segment_seq(segments[-1]) if segments else 0

        # flake-ok: pre-existing 30 s wall-clock ceiling on a bounded top-up loop -- moved verbatim; tracked for a de-flake pass
        seq = _fill_until_over_cap(log, deadline=time.monotonic() + 30.0, start=500)

        # Fresh budget for the phase actually under test, so a slow precondition
        # cannot eat the writer's rotation window and misattribute the failure.
        # flake-ok: pre-existing 30 s wall-clock ceiling on a bounded top-up loop -- moved verbatim; tracked for a de-flake pass
        deadline = time.monotonic() + 30.0
        before = highest_seq()
        iterations = 0
        while highest_seq() <= before:
            iterations += 1
            # flake-ok: pre-existing 30 s wall-clock ceiling on a bounded top-up loop -- moved verbatim; tracked for a de-flake pass
            assert iterations <= _POLL_ITERATION_CAP and time.monotonic() < deadline, (
                f"background writer never rotated: highest segment sequence "
                f"stayed at {before} after {iterations - 1} batches, with the "
                f"live log at {log._live_size()} bytes (cap "
                f"{sel_mod._SEGMENT_MAX_BYTES}), "
                f"{len(log._segments_oldest_first())} segments on disk, "
                f"{log._pending} events still pending in the writer"
            )
            _fill(log, 5, start=seq)
            seq += 5
            log.flush()

    def test_a_critical_write_still_fails_closed(self, sel_dir, small_segments):
        """Skipping rotation must not weaken audit-or-deny.

        Keeps ``base_dir``: the append is patched to raise, so no event is ever
        enqueued and the daemon writer is never started -- nothing outlives this
        test to re-create the directory.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=False)
        with patch.object(
            SecurityEventLog, "_append_lines_locked", side_effect=OSError("full disk")
        ):
            with pytest.raises(OSError):
                log.log(_make_event(event_id="crit1"), critical=True)
        assert log._writer is None, "a writer thread was started after all"


class TestTwoWritersCannotBothClaimGenesis:
    """The rotation record must not assume it lands first in the fresh log.

    After the rename the replacement is absent, and another process appending sees
    a small file -- so it never enters the rotation window and never takes the
    rotation lock. If the rotator then wrote a genesis record blindly, the new log
    would hold TWO records claiming an empty prev_hash and its chain would be
    broken from the second line.
    """

    def test_a_writer_that_beat_the_rotator_is_chained_off(self, sel_dir, small_segments):
        """Driven through _rotate_under_lock, not through its helpers.

        The sibling's write is injected at the real interleaving point -- inside the
        rename -- so the rotator's own boundary-record path is what is under test.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        live = sel_dir / "security_events.jsonl"
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)

        real_replace = os.replace
        raced: list[str] = []

        def replace_then_race(src, dst, *args, **kwargs):
            result = real_replace(src, dst, *args, **kwargs)
            if not raced:
                raced.append("yes")
                # A sibling process creates the replacement and puts the FIRST
                # record in it, before the rotator can write its own.
                first = _make_event(event_id="first0", resources="beat-the-rotator")
                first.prev_hash = ""
                first.entry_hash = log._compute_hash(first)
                live.write_text(json.dumps(asdict(first)) + "\n", encoding="utf-8")
            return result

        with patch("kiro_crew.sel.os.replace", replace_then_race):
            with patch.object(
                SecurityEventLog, "_live_size", return_value=sel_mod._SEGMENT_MAX_BYTES
            ):
                log._rotate_under_lock()

        assert raced, "precondition: the injected sibling write fired"
        rows = [json.loads(line) for line in live.read_text(encoding="utf-8").splitlines()]
        assert [r["event_type"] for r in rows] == ["tool_invocation", "sel_rotation"]
        assert rows[0]["prev_hash"] == ""
        assert (
            rows[1]["prev_hash"] == rows[0]["entry_hash"]
        ), "the rotation record claimed genesis in a log that already had a record"
        total, valid = log.verify_integrity()
        assert total == valid, f"{total - valid} entries failed verification"

    def test_uncontended_rotation_still_puts_the_record_first(self, sel_dir, small_segments):
        """The common case is unchanged: nothing raced, so the record opens the log."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        opener = json.loads(
            (sel_dir / "security_events.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert opener["event_type"] == "sel_rotation"
        assert opener["prev_hash"] == ""


class TestCollisionRetryReanchorsUnconditionally:
    """A collision has already proved the file moved on.

    The retry must not re-ask a predicate to decide whether to refresh the tip. An
    earlier revision routed it through the conditional check, and because that check
    reacted only to a shrink while the guard fired on growth too, a foreign APPEND
    was detected and then not corrected -- the retry re-chained from the very tip
    the collision had just invalidated and wrote it.
    """

    def test_a_foreign_append_is_re_chained_not_re_written_stale(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        live = sel_dir / "security_events.jsonl"
        stale_tip = log._last_hash

        # A sibling appends one correctly-chained record: the file GROWS, which is
        # what the old shrink-only predicate refused to react to.
        sibling = _make_event(event_id="grow0", resources="sibling-grew")
        sibling.prev_hash = stale_tip
        sibling.entry_hash = log._compute_hash(sibling)
        with open(live, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(sibling)) + "\n")
        assert log._last_hash == stale_tip, "precondition: our cached tip is stale"

        _fill(log, 1, start=6000)

        rows = [json.loads(line) for line in live.read_text(encoding="utf-8").splitlines()]
        ours = rows[-1]
        assert ours["resources"] == "seq=6000"
        assert (
            ours["prev_hash"] == sibling.entry_hash
        ), "wrote a stale prev_hash after the collision was detected"
        total, valid = log.verify_integrity()
        assert total == valid, f"{total - valid} entries failed verification"

    def test_reanchor_now_refreshes_without_asking(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        real_tip = log._last_hash
        log._last_hash = "0" * 64  # pretend we hold something stale
        log._reanchor_now()
        assert log._last_hash == real_tip
        assert log._live_seen == log._live_identity()


class TestOnlyTheWriterThreadRotates:
    """Rotation's filesystem work must never land on an inline caller's thread.

    A critical audit and the writer-start fallback both reach ``_flush_batch``
    inline, and for an async handler that thread is the event loop. The permission
    is derived from the running thread so a future inline caller cannot reintroduce
    the stall by forgetting a flag.
    """

    def test_sync_mode_may_rotate(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        assert log._may_rotate() is True

    def test_an_inline_caller_may_not_rotate(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=False)
        # No writer thread started yet, and this is the test's own thread.
        assert log._may_rotate() is False

    def test_the_writer_thread_may_rotate(self, small_segments):
        """Verified from INSIDE the writer, not by reasoning about it."""
        log = SecurityEventLog(sync=False)
        seen: list[bool] = []
        real_window = SecurityEventLog._rotation_window

        def recording_window(inner_self):
            seen.append(inner_self._may_rotate())
            return real_window(inner_self)

        with patch.object(SecurityEventLog, "_rotation_window", recording_window):
            _fill(log, 5)
            log.flush()
        assert seen and all(seen), "the writer thread was denied rotation"

    def test_the_writer_start_fallback_does_not_rotate(self, small_segments):
        """The path this finding was about: _ensure_writer fails, we write inline.

        That inline write happens on whatever thread called log() -- possibly the
        event loop -- so it must not scan, rename and sweep.

        Omits ``base_dir`` for the session-scoped SEL directory: ``_fill`` starts
        the daemon writer, and against a per-test ``tmp_path`` that thread outlives
        the test and re-creates the directory after cleanup. Assertions are relative
        to whatever the shared directory already holds.
        """
        log = SecurityEventLog(sync=False)
        # Get the log over the cap first, through a path that is allowed to
        # rotate. Topped up rather than asserted after one fixed fill: the same
        # two harness properties that broke the rotation test -- a
        # bounded flush() and a mid-fill batch-boundary rotation -- can leave
        # the live log below the cap here too.
        _fill(log, 200)
        log.flush()
        # flake-ok: pre-existing 30 s wall-clock ceiling on a bounded top-up loop -- moved verbatim; tracked for a de-flake pass
        _fill_until_over_cap(log, deadline=time.monotonic() + 30.0, start=700)
        before = log._segments_oldest_first()

        entered: list[str] = []
        real_window = SecurityEventLog._rotation_window

        def recording_window(inner_self):
            entered.append("yes")
            return real_window(inner_self)

        with patch.object(
            SecurityEventLog, "_ensure_writer", side_effect=RuntimeError("no thread")
        ):
            with patch.object(SecurityEventLog, "_rotation_window", recording_window):
                log.log(_make_event(event_id="fb0", resources="fallback"))

        assert entered == [], "the writer-start fallback rotated on the caller's thread"
        assert log._segments_oldest_first() == before
        # The record still landed: that is the half that is not deferrable.
        assert any(e["resources"] == "fallback" for e in log.recent(limit=20))


#: The test_sel.py family by file name: the module, its topic modules and sel_helpers.py. The
#: session-directory ratchet below judges them all, because a function that starts the async
#: writer can live in any of them; the other test_sel_*.py modules are outside it.
_SEL_FAMILY = (
    "test_sel.py",
    "test_sel_chain_hardening.py",
    "test_sel_edge_cases.py",
    "test_sel_redaction.py",
    "test_sel_rotation.py",
    "test_sel_segment_files.py",
    "sel_helpers.py",
)


def _sel_family_source() -> str:
    """The text of every module in ``_SEL_FAMILY``, joined, so one AST walk reads them all."""
    return "\n".join(
        Path(__file__).with_name(name).read_text(encoding="utf-8") for name in _SEL_FAMILY
    )


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ratchet below reads every module in ``_SEL_FAMILY``, so a leaf-only diff must run it."""
    root = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    # The join with the test directory on the next line is load-bearing: corpus_gates()
    # counts this file as a gate because of it, so spelling this path any other way drops
    # the file from the gates and fails this test.
    this_file = (root / "test" / Path(__file__).name).relative_to(root).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(root)


class TestAsyncTestsUseTheSessionScopedDirectory:
    """Ratchet for a convention I have now broken twice on this PR.

    A ``sync=False`` log started by ``_fill`` runs a DAEMON writer thread on a
    process singleton. Bound to a per-test ``tmp_path`` it outlives the test and
    re-creates the directory on its next flush, so a stray directory reappears
    after cleanup (see the rootdir ``_isolate_sel_default_dir`` fixture). A test
    that starts the writer must therefore omit ``base_dir``.

    Asserted structurally rather than by discipline, because discipline already
    failed: it was applied in one round and violated in the next.
    """

    def test_no_writer_starting_test_pins_a_per_test_dir(self):
        import ast

        source = _sel_family_source()
        tree = ast.parse(source)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(source, node) or ""
            constructs_async = "sync=False" in body and "base_dir=" in body
            starts_writer = "_fill(" in body or ".log(" in body
            if constructs_async and starts_writer:
                # A test may keep base_dir if it proves no writer was started.
                if "_writer is None" in body:
                    continue
                offenders.append(node.name)
        assert offenders == [], (
            "these tests build an async SecurityEventLog on a per-test dir AND write "
            f"through it, leaking a daemon writer bound to that dir: {offenders}. "
            "Omit base_dir to use the session-scoped SEL directory."
        )


class TestPruneCannotClobberAConcurrentRotation:
    """prune's read-then-replace is the one path that can lose persisted events.

    ``_lock`` is a thread lock, so it says nothing about a sibling process. If one
    rotates while prune is streaming, prune's ``os.replace`` drops a snapshot of the
    OLD file over the fresh live log -- discarding its rotation record and every
    event appended since.
    """

    def test_prune_takes_the_rotation_lock(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        base = datetime(2020, 1, 1, tzinfo=timezone.utc)
        for i in range(20):
            log.log(
                _make_event(
                    event_id=f"old{i:04d}",
                    timestamp=(base + timedelta(seconds=i)).isoformat(),
                    resources=f"old={i}",
                )
            )
        _fill(log, 5, start=700)  # recent entries that must survive
        lock_path = sel_dir / "security_events.d" / ".rotate.lock"

        held: list[bool] = []
        real_prune_live = SecurityEventLog._prune_live_locked

        def recording(inner_self, cutoff, keep_days):
            held.append(_lock_is_held(lock_path))
            return real_prune_live(inner_self, cutoff, keep_days)

        with patch.object(SecurityEventLog, "_prune_live_locked", recording):
            removed = log.prune(keep_days=365)
        assert removed > 0, "precondition: something was pruned"
        assert held and all(held), "prune rewrote the live log without the rotation lock"

    def test_prune_skips_the_live_sweep_when_it_cannot_serialize(self, sel_dir, small_segments):
        """Skipping beats an unserialized rewrite: the latter loses events."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        base = datetime(2020, 1, 1, tzinfo=timezone.utc)
        for i in range(20):
            log.log(
                _make_event(
                    event_id=f"old{i:04d}",
                    timestamp=(base + timedelta(seconds=i)).isoformat(),
                )
            )
        live = sel_dir / "security_events.jsonl"
        before = live.read_text(encoding="utf-8")
        with patch.object(SecurityEventLog, "_open_rotation_lock", return_value=None):
            log.prune(keep_days=365)
        assert live.read_text(encoding="utf-8") == before, "swept without the lock"

    def test_prune_re_anchors_after_replacing_the_live_log(self, sel_dir, small_segments):
        """The replace changes the inode, so the cached identity must follow.

        Note what this does NOT assert. Rewriting the live log orphans the first
        SURVIVING record's ``prev_hash`` -- it names a record prune just deleted --
        so a pruned log carries exactly one chain break. That is pre-existing
        behaviour of the in-place rewrite, not something rotation introduced, and it
        cannot be repaired by editing the survivor: ``prev_hash`` is covered by the
        record's own HMAC, so changing it would mean re-signing audit records. The
        property that must hold is that prune introduces NO FURTHER break -- our
        next append chains off the rewritten file's real tip.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        base = datetime(2020, 1, 1, tzinfo=timezone.utc)
        for i in range(10):
            log.log(
                _make_event(
                    event_id=f"old{i:04d}",
                    timestamp=(base + timedelta(seconds=i)).isoformat(),
                )
            )
        _fill(log, 3, start=800)
        assert log.prune(keep_days=365) > 0
        assert log._live_seen == log._live_identity()
        assert log._last_hash == log._read_last_hash()

        total_before, valid_before = log.verify_integrity()
        orphaned = total_before - valid_before
        assert (
            orphaned == 1
        ), f"expected exactly the known orphaned-first-survivor break, got {orphaned}"

        _fill(log, 1, start=900)
        total_after, valid_after = log.verify_integrity()
        assert (
            total_after - valid_after == orphaned
        ), "the post-prune append introduced a NEW chain break"
        last = json.loads(
            (sel_dir / "security_events.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        assert last["resources"] == "seq=900"


class TestForeignRotationDoesNotBreakOurChain:
    """Another process's rotation must not leave us chaining into a closed segment.

    Several processes share one data home and append to this file, each caching
    its own tip. A process whose next append comes AFTER a foreign rotation does
    not see the cap at all, so it never enters the rotation window -- and would
    chain off a tip that has moved into the closed segment. Unlike the
    pre-existing interleaving race, that break is guaranteed for every process
    after every foreign rotation.
    """

    def test_a_replaced_live_log_re_anchors_before_the_next_append(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        stale_tip = log._last_hash
        live = sel_dir / "security_events.jsonl"
        # What WE last saw. The sibling's work below must not update it: the whole
        # point is that this process never observed the replacement.
        pre_rotation_identity = log._live_seen

        # Stand in for the sibling process: rotate the log out of band and write
        # one genuine record (correctly signed, chained from genesis) into the
        # fresh live log. Written directly rather than through the SEL API because
        # SecurityEventLog is a singleton -- a second construction would hand back
        # THIS instance and update its view of the file.
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        os.replace(live, segment_dir / "security_events-000001-20260821T000000Z.jsonl")
        sibling_event = _make_event(event_id="sib0", resources="sibling")
        sibling_event.prev_hash = ""
        sibling_event.entry_hash = log._compute_hash(sibling_event)
        live.write_text(json.dumps(asdict(sibling_event)) + "\n", encoding="utf-8")
        sibling_tip = sibling_event.entry_hash
        assert sibling_tip != stale_tip

        # Our process still holds the pre-rotation tip and is nowhere near the cap,
        # so it never enters the rotation window at all.
        log._live_seen = pre_rotation_identity
        log._last_hash = stale_tip
        _fill(log, 1, start=9000)

        lines = live.read_text(encoding="utf-8").strip().splitlines()
        ours = json.loads(lines[-1])
        assert ours["resources"] == "seq=9000"
        assert (
            ours["prev_hash"] == sibling_tip
        ), "chained off a tip that now lives in the closed segment"
        total, valid = log.verify_integrity()
        assert total == valid, f"{total - valid} entries failed after a foreign rotation"

    def test_an_append_only_log_that_merely_grew_is_not_treated_as_replaced(self, sel_dir):
        """Growth is the normal case; re-reading the tip on it would be pure cost.

        Deliberately NOT using the shrunken-cap fixture: under the production cap
        these appends cannot trigger a rotation, so the only thing that could read
        the tip back is a spurious replacement verdict.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        assert log._segments_oldest_first() == [], "precondition: no rotation"
        with patch.object(
            SecurityEventLog,
            "_read_last_hash",
            side_effect=AssertionError("re-anchored on a plain append"),
        ):
            _fill(log, 5, start=100)
        total, valid = log.verify_integrity()
        assert total == valid
