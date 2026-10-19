"""Segment-file tests for kiro_crew.sel: what the readers and the appender trust on disk.

Pins the segment directory a read pins, fd validation on append, bounded segment
enumeration, name probing and backward reads, the refusal of planted links and forged
segments, descriptor validation at open time, the live-log-moved predicate and
time-window reads.
``test_sel.py`` holds the core contract and names the other topic modules; the shared
fixtures and helpers live in ``sel_helpers.py``.
"""

from __future__ import annotations

import errno
import json
import os
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
from kiro_crew.sel import SecurityEventLog


class TestSegmentDirIsPinnedOnRead:
    """The segment DIRECTORY is pinned for the duration of a read.

    Validating the descriptor of each file the readers open closes the FINAL
    path component, but the directory itself could still be walked BY NAME.
    A ``security_events.d`` replaced with a link (planted ahead of time,
    or swapped while a read is in flight) would redirect enumeration —
    and every per-file open — into another tree, whose segment-shaped REGULAR
    files pass every per-file check, because they are regular files. The read
    paths pin the directory itself first and refuse a linked one, so a
    swapped dir fails closed instead of feeding another tree's files to
    ``recent()`` / ``verify_integrity()``. The rotation-time repair
    (``_ensure_segment_dir``) is the write-side guard, and the live log
    is unchanged: its writer follows an operator's symlink, so its readers
    must too.
    """

    def test_a_swapped_segment_dir_is_not_read_through(self, sel_dir, small_segments):
        """A linked ``security_events.d`` must fail closed, not follow.

        The observable is ``verify_integrity()``'s totals: without the pin, the
        decoy's unsigned lines inflate ``total`` while ``valid`` stays flat —
        the false tamper alarm — so ``total == valid`` here is exactly
        the property that must hold. Built with ``symlink_or_junction`` so the
        same attack runs on Windows junctions, which need no elevation.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: rotation happened"
        segment_dir = sel_dir / "security_events.d"
        decoy = sel_dir / "decoy.d"
        decoy.mkdir()
        (decoy / "security_events-000042-20260821T000000Z.jsonl").write_text(
            '{"planted": true}\n' * 3, encoding="utf-8"
        )
        os.rename(segment_dir, sel_dir / "aside.d")
        platform_compat.symlink_or_junction(str(decoy), str(segment_dir))

        total, valid = log.verify_integrity()
        assert total == valid, (
            f"a swapped segment dir surfaced {total - valid} decoy lines as "
            "audit history (false tamper alarm)"
        )
        live_lines = (
            (sel_dir / "security_events.jsonl").read_text(encoding="utf-8").strip().splitlines()
        )
        assert total == len(live_lines), "records beyond the live log were counted"
        # The swap is itself tampering, so the detailed result must not call
        # this run verifiable over the live log alone.
        result = log.verify_integrity(detailed=True)
        assert result.history_verifiable is False
        assert "refused" in result.reason

    def test_a_refusal_is_not_reclassified_by_a_later_repair(
        self, sel_dir, small_segments, monkeypatch
    ):
        """The refusal verdict must come from pin time, not a re-stat.

        A linked segment dir is refused, and rotation's repair concurrently
        removes the link before verify classifies the outcome: re-deriving
        the answer from the path would now see a real directory again and
        report the run verifiable while the rotated history went unchecked.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: rotation happened"
        segment_dir = sel_dir / "security_events.d"
        decoy = sel_dir / "decoy.d"
        decoy.mkdir()
        os.rename(segment_dir, sel_dir / "aside.d")
        platform_compat.symlink_or_junction(str(decoy), str(segment_dir))

        real_open = sel_mod._open_segment_dir

        def repairing_open(path):
            pin, absent = real_open(path)
            if pin is None and not absent and not segment_dir.exists():
                # The repair races in behind the refusal: the real directory
                # is back by the time verify would have re-stat'ed the path.
                # The exists() guard makes the race idempotent — the repair
                # fires exactly once, whatever later pin attempt arrives.
                os.rename(sel_dir / "aside.d", segment_dir)
            return pin, absent

        monkeypatch.setattr(sel_mod, "_open_segment_dir", repairing_open)
        result = log.verify_integrity(detailed=True)
        assert (
            result.history_verifiable is False
        ), "a concurrent repair reclassified the refusal as verifiable"

    def test_a_fresh_install_stays_verifiable(self, sel_dir):
        """No segment dir yet is not tampering — nothing to vouch for."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 3)
        result = log.verify_integrity(detailed=True)
        assert result.history_verifiable is True
        assert result.reason == ""
        assert result.total == result.valid

    def test_a_healthy_rotation_is_verifiable(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: rotation happened"
        result = log.verify_integrity(detailed=True)
        assert result.history_verifiable is True
        assert result.reason == ""
        assert result.total == result.valid

    @pytest.mark.skipif(os.name == "nt", reason="the fd branch that opens the dir is POSIX-only")
    def test_a_dir_vanishing_after_lstat_is_a_refusal(self, sel_dir, small_segments, monkeypatch):
        """Vanishing BETWEEN the lstat and the pin is interference, not absence.

        Fresh-install silence belongs to the directory that was NEVER there;
        one that was seen by lstat and then disappears before the open
        cannot make the rotated history read as unverifiable-but-
        absent.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: rotation happened"
        real_open = os.open

        def vanishing_open(path, *args, **kwargs):
            if getattr(path, "name", str(path)).endswith("security_events.d"):
                raise FileNotFoundError(errno.ENOENT, "gone", str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(sel_mod.os, "open", vanishing_open)
        result = log.verify_integrity(detailed=True)
        assert (
            result.history_verifiable is False
        ), "a directory that vanished after lstat read as absent"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode semantics")
    def test_an_unreadable_segment_dir_is_not_verifiable(self, sel_dir, small_segments):
        """A real directory the pin could not open is still unchecked history.

        Permissions changed under us, or the platform refused the open for
        any other reason: the rotated segments were skipped either way, and
        reporting the run as intact over the live log alone is the false
        negative the third outcome exists to prevent.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segment_dir = sel_dir / "security_events.d"
        assert log._segments_oldest_first(), "precondition: rotation happened"
        before = segment_dir.stat().st_mode
        segment_dir.chmod(0o000)
        try:
            result = log.verify_integrity(detailed=True)
        finally:
            segment_dir.chmod(before)
        assert result.history_verifiable is False
        assert "refused" in result.reason

    def test_a_mid_verification_swap_is_not_verifiable(self, sel_dir, small_segments, monkeypatch):
        """A directory replaced mid-run leaves totals from the pinned tree,
        but the tree on disk is not that tree — the detail must say so."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: rotation happened"
        monkeypatch.setattr(sel_mod._SegmentDirPin, "matches", lambda self, path: False)
        result = log.verify_integrity(detailed=True)
        assert result.history_verifiable is False
        assert "replaced during verification" in result.reason

    @pytest.mark.skipif(os.name == "nt", reason="dir-fd pinning is POSIX-only")
    def test_enumeration_is_immune_to_a_swapped_path_with_the_pin_alive(
        self, sel_dir, small_segments
    ):
        """POSIX: the walk goes through the pinned descriptor.

        Swapping the PATH (to a decoy link, and back again — the ABA shape:
        plant the decoy mid-read, restore the real directory before any
        recheck) cannot redirect, empty, or confuse the enumeration, because
        it reads RELATIVE to the fd. Every real name must survive.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segment_dir = sel_dir / "security_events.d"
        real_names = {p.name for p in log._segments_oldest_first()}
        assert real_names, "precondition: rotation happened"
        pin, absent = sel_mod._open_segment_dir(segment_dir)
        assert pin is not None, "the real segment dir must pin"
        assert absent is False, "a real segment dir is not absence"
        assert pin.fd is not None, "POSIX must pin by descriptor, not identity"
        try:
            decoy = sel_dir / "decoy.d"
            decoy.mkdir()
            (decoy / "security_events-000042-20260821T000000Z.jsonl").write_text(
                "", encoding="utf-8"
            )
            os.rename(segment_dir, sel_dir / "aside.d")
            (sel_dir / "security_events.d").symlink_to(decoy)
            names = {p.name for p in log._segments_oldest_first(pin=pin)}
            assert names == real_names, "enumeration leaked names through a swapped path"
            fd = sel_mod._open_segment(segment_dir / sorted(real_names)[0], pin=pin)
            assert fd is not None, "the pinned descriptor refused a real segment after the swap"
            os.close(fd)
        finally:
            pin.close()

    @pytest.mark.skipif(
        os.name == "nt", reason="identity pins are the Windows-only degenerate form"
    )
    def test_an_identity_pin_fails_closed_on_a_mid_read_swap(
        self, sel_dir, small_segments, monkeypatch
    ):
        """Without directory descriptors the walk is by name, so a swap that
        survives the post-walk identity revalidation must fail closed — the
        identity the read pinned is not the identity the path names anymore.

        Forces the identity branch the way Windows takes it naturally: the
        platform gate claims no descriptors, and the pin carries only an
        identity.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segment_dir = sel_dir / "security_events.d"
        assert log._segments_oldest_first(), "precondition: rotation happened"
        decoy = sel_dir / "decoy.d"
        decoy.mkdir()
        (decoy / "security_events-000042-20260821T000000Z.jsonl").write_text("", encoding="utf-8")
        pin = sel_mod._SegmentDirPin(fd=None, identity=(0, 0))
        monkeypatch.setattr(sel_mod, "_open_segment_dir", lambda path: (pin, False))
        os.rename(segment_dir, sel_dir / "aside.d")
        platform_compat.symlink_or_junction(str(decoy), str(segment_dir))
        assert (
            log._segments_oldest_first(pin=pin) == []
        ), "enumeration surfaced names through a swapped segment dir"
        result = log.verify_integrity(detailed=True)
        assert result.history_verifiable is False


class TestAppendValidatesTheFileByFd:
    """A lock-free append must notice a rotation that lands under it.

    The append is deliberately not serialized (a blocking cross-process acquire
    could park an event-loop caller writing a critical audit), so instead it
    validates the file it OPENED. A path stat cannot do this: whatever it observed
    may be replaced before the open that follows.
    """

    def test_a_rotation_between_chaining_and_opening_is_re_chained(self, sel_dir, small_segments):
        """The exact interleaving: we chain, a sibling renames, then we open."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        live = sel_dir / "security_events.jsonl"
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)

        real_open = os.open
        rotated: list[bool] = []

        def rotating_open(path, flags, *args, **kwargs):
            # Fire once, at the moment of the append's open: the sibling's rename
            # and its own first record land between our chaining and our write.
            if not rotated and str(path) == str(live):
                rotated.append(True)
                os.replace(live, segment_dir / "security_events-000001-20260821T000000Z.jsonl")
                sibling = _make_event(event_id="sibfd", resources="sibling")
                sibling.prev_hash = ""
                sibling.entry_hash = log._compute_hash(sibling)
                live.write_text(json.dumps(asdict(sibling)) + "\n", encoding="utf-8")
            return real_open(path, flags, *args, **kwargs)

        with patch("kiro_crew.sel.os.open", rotating_open):
            _fill(log, 1, start=7000)

        assert rotated, "precondition: the injected rotation fired"
        lines = live.read_text(encoding="utf-8").strip().splitlines()
        records = [json.loads(line) for line in lines]
        ours = [r for r in records if r.get("resources") == "seq=7000"]
        assert ours, "our record was lost"
        sibling_rec = next(r for r in records if r.get("resources") == "sibling")
        assert (
            ours[0]["prev_hash"] == sibling_rec["entry_hash"]
        ), "record chained off a tip that lives in the closed segment"
        total, valid = log.verify_integrity()
        assert total == valid, f"{total - valid} entries failed verification"

    def test_holding_the_pre_rename_inode_still_writes_there(self, sel_dir, small_segments):
        """Matching identity means our record rides into the segment, chained.

        The other correct outcome: the rename has not happened yet, so the fd we
        hold is the file we chained from and the write is valid whether or not a
        rename follows it.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        tip_before = log._last_hash
        _fill(log, 1, start=8000)
        line = (
            (sel_dir / "security_events.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        assert json.loads(line)["prev_hash"] == tip_before

    def test_sustained_contention_refuses_rather_than_writing_a_bad_link(
        self, sel_dir, small_segments
    ):
        """Every attempt is validated, including the last.

        Writing the final attempt unguarded would put a knowingly-broken link in
        the chain, and that does not read as one imperfect record -- it makes
        `security verify` report the log as COMPROMISED, which an investigator
        cannot tell apart from real tampering. Refusing is the behaviour this class
        already has for an unwritable audit.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        tip_before = log._last_hash
        live = sel_dir / "security_events.jsonl"
        before = live.read_text(encoding="utf-8")

        calls: list[int] = []

        def always_colliding(inner_self, lines, *, expect=None):
            calls.append(1)
            assert expect is not None, "an attempt was made without the fd guard"
            raise sel_mod._LiveLogReplaced("forced collision")

        with patch.object(SecurityEventLog, "_append_lines_locked", always_colliding):
            with pytest.raises(sel_mod.SelChainContention):
                log._append_chained_locked([_make_event(event_id="cont0")])

        assert len(calls) == sel_mod._APPEND_RETRIES + 1
        assert live.read_text(encoding="utf-8") == before, "a record was written anyway"
        assert log._last_hash == tip_before, "the chain tip was left advanced"

    def test_contention_is_an_oserror_so_critical_writes_fail_closed(self, sel_dir, small_segments):
        """A critical caller must be able to deny the action it could not audit.

        Keeps ``base_dir``: the critical path writes inline and never calls
        ``_ensure_writer``, so no daemon writer is bound to this per-test dir --
        asserted below rather than assumed.
        """
        assert issubclass(sel_mod.SelChainContention, OSError)
        log = SecurityEventLog(base_dir=sel_dir, sync=False)
        with patch.object(
            SecurityEventLog,
            "_append_lines_locked",
            side_effect=sel_mod._LiveLogReplaced("forced collision"),
        ):
            with pytest.raises(OSError):
                log.log(_make_event(event_id="cont1"), critical=True)
        assert log._writer is None, "a writer thread was started after all"

    def test_contention_does_not_kill_the_background_writer(self, small_segments):
        """Best-effort path: the batch is dropped with a warning, thread survives."""
        log = SecurityEventLog(sync=False)
        _fill(log, 2)
        log.flush()
        with patch.object(
            SecurityEventLog,
            "_append_lines_locked",
            side_effect=sel_mod._LiveLogReplaced("forced collision"),
        ):
            log.log(_make_event(event_id="cont2", resources="dropped"))
            log.flush()
        # Writer still alive and still serving later events.
        assert log._writer is not None and log._writer.is_alive()
        _fill(log, 1, start=950)
        log.flush()
        assert any(e["resources"] == "seq=950" for e in log.recent(limit=20))

    def test_the_guard_covers_both_replacement_and_a_foreign_append(self):
        """Inode catches a swap; size catches an append the inode cannot see.

        The size signal is what covers the fresh-log case: a rotator anchored on an
        ABSENT replacement has no inode to compare, so only the size reveals that
        another process already put a record there -- and without it both would
        write a record claiming genesis.
        """

        class _St:
            def __init__(self, dev, ino, size):
                self.st_dev = dev
                self.st_ino = ino
                self.st_size = size

        changed = sel_mod._identity_changed
        # Same file, same size: unchanged.
        assert changed((1, 5, 10), _St(1, 5, 10)) is False
        # Same file, grown by somebody else: changed.
        assert changed((1, 5, 10), _St(1, 5, 40)) is True
        # Replaced file: changed.
        assert changed((1, 5, 10), _St(1, 6, 10)) is True
        # No usable inode on either side -- size still decides.
        assert changed((1, 0, 10), _St(1, 0, 10)) is False
        assert changed((1, 0, 10), _St(1, 0, 40)) is True
        # Anchored on an absent replacement; another process wrote first.
        assert changed((0, 0, 0), _St(1, 7, 512)) is True
        # Anchored on an absent replacement; still absent/empty.
        assert changed((0, 0, 0), _St(1, 7, 0)) is False


class TestSegmentEnumerationIsBounded:
    def test_enumeration_stops_at_the_scan_cap(self, sel_dir, small_segments, monkeypatch):
        """This walk runs on the append path, where a critical audit may be inline.

        An unbounded listing of a directory somebody else filled would stall that
        caller (no-blocking-call-on-event-loop), so the cost must be capped rather
        than scale with the directory.
        """
        monkeypatch.setattr(sel_mod, "_SEGMENT_SCAN_CAP", 8)
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        for i in range(200):
            (segment_dir / f"planted-{i:04d}.txt").write_text("x", encoding="utf-8")

        examined = 0
        real_scandir = os.scandir

        class _CountingScandir:
            """Wraps os.scandir, counting entries the caller actually pulls."""

            def __init__(self, path):
                self._it = real_scandir(path)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._it.close()
                return False

            def __iter__(self):
                return self

            def __next__(self):
                nonlocal examined
                entry = next(self._it)
                examined += 1
                return entry

        with patch("kiro_crew.sel.os.scandir", _CountingScandir):
            log._segments_oldest_first()

        assert examined <= sel_mod._SEGMENT_SCAN_CAP + 1, (
            f"walked {examined} entries with a cap of {sel_mod._SEGMENT_SCAN_CAP}; "
            "enumeration scales with a directory an agent could have filled"
        )

    def test_the_cap_does_not_hide_the_real_segments(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        assert len(log._segments_oldest_first()) == sel_mod._SEGMENT_KEEP


class TestSegmentNameProbingIsBounded:
    def test_probing_stops_and_defers_rotation(self, sel_dir, small_segments, monkeypatch):
        """Each probe is a stat on the append path; a filled directory must not
        make rotation walk it."""
        monkeypatch.setattr(sel_mod, "_SEGMENT_NAME_PROBES", 4)
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)

        probes = 0
        real_exists = Path.exists

        def counting_exists(inner_self, *args, **kwargs):
            nonlocal probes
            if inner_self.parent == segment_dir and inner_self.name.startswith("security_events-"):
                probes += 1
                return True  # every candidate name is taken
            return real_exists(inner_self, *args, **kwargs)

        with patch.object(Path, "exists", counting_exists):
            assert log._next_segment_path() is None, "did not defer on an exhausted probe"
        assert probes == 4, f"probed {probes} times with a cap of 4"

    def test_an_exhausted_probe_still_writes_the_event(self, sel_dir, small_segments):
        """Deferring means the live log is NOT renamed and the record still lands.

        Asserted on the live log's identity rather than on the segment list: a
        rotation into a low-sequence name would be deleted by retention as the
        oldest segment in the same sweep, erasing its own evidence from the list
        while the live log had still been moved out from under us.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        live = sel_dir / "security_events.jsonl"
        identity_before = (live.stat().st_dev, live.stat().st_ino)
        with patch.object(SecurityEventLog, "_next_segment_path", return_value=None):
            _fill(log, 50, start=9500)
        assert (
            live.stat().st_dev,
            live.stat().st_ino,
        ) == identity_before, "the live log was renamed even though no free segment name was found"
        assert log.recent(limit=1)[0]["resources"] == "seq=9549"
        total, valid = log.verify_integrity()
        assert total == valid

    def test_a_free_name_is_still_found_normally(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "rotation never found a name"


class TestBackwardReadBufferIsBounded:
    def test_an_unterminated_file_does_not_accumulate_without_bound(
        self, sel_dir, small_segments, monkeypatch
    ):
        """A planted segment with no newline would otherwise be held in memory.

        This reader is pointed at attacker-influenced input: a segment planted
        before this release is read here by both the time-range read and segment
        admission.
        """
        monkeypatch.setattr(sel_mod, "_MAX_LINE_BYTES", 64 * 1024)
        monkeypatch.setattr(sel_mod, "_TAIL_CHUNK_BYTES", 8 * 1024)
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        planted = segment_dir / "security_events-000001-20200101T000000Z.jsonl"
        planted.write_bytes(b"A" * (1024 * 1024))  # 1 MiB, not one newline in it

        yielded = list(log._iter_lines_backward(planted))
        assert yielded == [], "an unterminated file yielded a line"
        # Admission must simply fail, not blow up.
        assert log._segment_is_signed_by_us(planted) is False
        pin, _absent = sel_mod._open_segment_dir(segment_dir)
        assert pin is not None, "the real segment dir must pin"
        try:
            assert planted not in list(log._read_sources_newest_first(pin=pin))
        finally:
            pin.close()

    def test_a_normal_log_is_read_completely(self, sel_dir, small_segments):
        """The cap must not truncate ordinary records."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 40)
        live = sel_dir / "security_events.jsonl"
        expected = len(live.read_text(encoding="utf-8").strip().splitlines())
        assert len(list(log._iter_lines_backward(live))) == expected


class TestSegmentDirIsNotFollowedThroughALink:
    """Neither the segment dir nor a segment name may be a planted link.

    Before this release ``security_events.d`` was not on the sensitive-path
    floor, so an agent could have pre-planted either. Same defense as the SEL
    trust dir: remove the LINK, never its target.
    """

    def test_a_planted_link_is_replaced_by_a_real_directory(self, sel_dir, small_segments):
        outside = sel_dir.parent / "agent-readable"
        outside.mkdir()
        link = sel_dir / "security_events.d"
        sel_dir.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/filesystem")
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert not link.is_symlink(), "rotation wrote through a planted link"
        assert log._segments_oldest_first(), "precondition: rotation happened"
        assert list(outside.iterdir()) == [], "audit segments landed outside the fence"

    def test_an_unremovable_link_refuses_to_rotate(self, sel_dir, small_segments):
        """Refusing beats writing audit records outside the fence.

        The log keeps growing, which is the failure this release bounds — but an
        oversized log inside the fence beats a bounded one outside it.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        # Answer "link" only for rotation's paths (the segment dir and its
        # rotation lock). A blanket True would also condemn the chain-lock
        # sidecar in trust/, whose refusal correctly drops best-effort appends
        # -- a different, deliberate behavior with its own test
        # (test_symlinked_sidecar_is_refused) -- and this test is about
        # rotation refusing while audit records still land.
        real_probe = platform_compat.is_link_or_junction
        segment_dir = log._segment_dir

        def rotation_paths_are_links(path):
            p = Path(path)
            if p == segment_dir or p.parent == segment_dir:
                return True
            return real_probe(path)

        with patch(
            "kiro_crew.sel.platform_compat.is_link_or_junction",
            side_effect=rotation_paths_are_links,
        ):
            with patch(
                "kiro_crew.sel.platform_compat.unlink_link_or_junction",
                side_effect=OSError("read-only"),
            ):
                before = log._segments_oldest_first()
                _fill(log, 300, start=2000)
                assert log._segments_oldest_first() == before, "rotated through the link"
        # The audit records still landed in the live log.
        assert log.recent(limit=1)[0]["resources"] == "seq=2299"

    def test_a_planted_segment_link_is_never_read_as_audit_history(self, sel_dir, small_segments):
        """A segment-shaped SYMLINK must not be surfaced as events.

        Every reader resolves segments by name, and `recent()` backs the
        dashboard's SEL events endpoint -- so a link planted before the upgrade
        pointing at, say, the refresh-chain store would have its JSON handed back
        as audit records.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)  # creates the real segment dir
        secret = sel_dir / "refresh_chains.json"
        secret.write_text(json.dumps({"chain": "s3cr3t-refresh-token"}) + "\n", encoding="utf-8")
        planted = sel_dir / "security_events.d" / "security_events-000001-20200101T000000Z.jsonl"
        try:
            planted.unlink(missing_ok=True)
            planted.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/filesystem")
        assert planted.is_symlink()

        assert planted not in log._segments_oldest_first(), "a planted link ranked as a segment"
        surfaced = json.dumps(log.recent(limit=10_000))
        assert "s3cr3t-refresh-token" not in surfaced, "linked file's content surfaced as events"
        # The link's target must survive: we ignore the entry, we do not chase it.
        assert secret.exists()

    def test_a_segment_shaped_directory_is_ignored(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        bogus = sel_dir / "security_events.d" / "security_events-000002-20200101T000000Z.jsonl"
        bogus.mkdir(parents=True, exist_ok=True)
        assert bogus not in log._segments_oldest_first()
        total, valid = log.verify_integrity()
        assert total == valid


class TestSegmentOpensValidateTheDescriptor:
    """The scan->open window is closed at OPEN time, not just at scan time.

    ``_segments_oldest_first`` judges the DIRENT; a link or FIFO planted after
    that judgment must still be refused by the open itself. ``_open_segment``
    opens ``O_NOFOLLOW | O_NONBLOCK``, requires the DESCRIPTOR to be a regular
    file, and requires the opened identity to be exactly what the name points
    at — so both consumers (``_verify_file`` for verify_integrity,
    ``_iter_lines_backward`` for recent) are exercised here with the planted
    path handed to them DIRECTLY, as if it had passed the scan. Hardlink
    ALIASES are deliberately not deduplicated (a cross-name decision keyed on
    attacker-controlled names lets a plant displace real history); the
    displacement test locks that invariant.
    """

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_a_symlink_is_refused_by_both_consumers(self, sel_dir, small_segments):
        """O_NOFOLLOW (and the lstat identity check where it is absent) is what
        catches this: a followed link to a regular file yields a perfectly
        regular descriptor, so fstat alone would pass it."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)  # rotation creates the real segment dir
        secret = sel_dir / "outside.jsonl"
        secret.write_text(json.dumps({"chain": "s3cr3t-outside"}) + "\n", encoding="utf-8")
        planted = sel_dir / "security_events.d" / "security_events-000001-20200101T000000Z.jsonl"
        planted.unlink(missing_ok=True)
        try:
            planted.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/filesystem")

        assert log._verify_file(planted) == (0, 0), "linked file counted as audit history"
        assert list(log._iter_lines_backward(planted)) == [], "linked file's lines surfaced"
        # End to end: nothing surfaced by the readers, nothing counted as valid.
        assert "s3cr3t-outside" not in json.dumps(log.recent(limit=10_000))
        total, valid = log.verify_integrity()
        assert total == valid
        # The link's target survives: the entry is ignored, never chased.
        assert secret.exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hardlink semantics")
    def test_an_alias_plant_cannot_displace_newer_history(self, sel_dir, small_segments):
        """A LOWER-sequence hardlink of the newest segment must not displace
        the real name from the read order. Aliases are deliberately NOT
        deduplicated — every key such a decision could use (which name sorts
        first, which survives) is attacker-controlled in this directory, so a
        dedupe hands the plant the power to hide or reorder real history. An
        alias's worst case without dedupe is repeating already-signed records;
        it can never displace, hide, or forge them."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segments = log._segments_oldest_first()
        assert len(segments) > 1, "precondition: several segments"
        newest = segments[-1]
        alias = sel_dir / "security_events.d" / "security_events-000000-20000101T000000Z.jsonl"
        alias.unlink(missing_ok=True)
        os.link(newest, alias)  # newest content under the lowest-sorting name

        listed = log._segments_oldest_first()
        assert newest in listed, "the real newest segment was displaced by its alias"
        alias_pin, _absent = sel_mod._open_segment_dir(sel_dir / "security_events.d")
        assert alias_pin is not None, "the real segment dir must pin"
        try:
            order = list(log._read_sources_newest_first(pin=alias_pin))
        finally:
            alias_pin.close()
        assert newest in order, "the real newest segment vanished from the read order"
        assert order.index(newest) < order.index(
            alias
        ), "the alias outranked the real newest segment in newest-first reads"
        total, valid = log.verify_integrity()
        assert total == valid, "alias duplication broke signature validity"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hardlink semantics")
    def test_a_backup_hardlink_does_not_suppress_audit_history(self, sel_dir, small_segments):
        """Backup tools (rsync --link-dest, cp -al, rsnapshot) hardlink audit
        files from OUTSIDE the segment dir. A link count above one must not
        refuse the file: history keeps reading and verifying, and retention
        keeps moving — refusing here silently blanked real audit history while
        verify_integrity still reported clean."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        before_recent = len(log.recent(limit=10_000))
        before = log.verify_integrity()
        backups = sel_dir / "backup"
        backups.mkdir()
        os.link(sel_dir / "security_events.jsonl", backups / "live.jsonl")
        for i, seg in enumerate(log._segments_oldest_first()):
            os.link(seg, backups / f"seg{i}.jsonl")

        assert len(log.recent(limit=10_000)) == before_recent, "backup links suppressed history"
        assert log.verify_integrity() == before

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
    def test_a_fifo_does_not_block_and_is_skipped(self, sel_dir, small_segments):
        """O_NONBLOCK is load-bearing: without it the read-side open of a FIFO
        with no writer blocks forever, BEFORE any descriptor check is reachable
        — this test hangs, loudly, if that flag is dropped."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        fifo = sel_dir / "security_events.d" / "security_events-000001-20200101T000000Z.jsonl"
        fifo.unlink(missing_ok=True)
        os.mkfifo(fifo)

        assert log._verify_file(fifo) == (0, 0), "a FIFO counted as audit history"
        assert list(log._iter_lines_backward(fifo)) == [], "a FIFO yielded lines"
        total, valid = log.verify_integrity()
        assert total == valid

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_a_symlinked_live_log_stays_readable(self, sel_dir, small_segments):
        """An operator may relocate the live log via symlink. The WRITER
        follows it (O_CREAT | O_APPEND open), so the readers must too — a
        reader that refuses the link turns the live log write-only: events
        keep landing while recent() reports empty and verify_integrity()
        counts a clean (0, 0)."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 20)
        live = sel_dir / "security_events.jsonl"
        relocated = sel_dir / "relocated.jsonl"
        os.replace(live, relocated)
        live.symlink_to(relocated)
        _fill(log, 5, start=1000)  # the writer keeps appending through the link

        recent = log.recent(limit=10_000)
        assert any(
            e.get("resources") == "seq=1004" for e in recent
        ), "reads went dark through the linked live log"
        total, valid = log.verify_integrity()
        assert total == valid
        assert total > 0

    def test_regular_segments_read_identically_through_the_funnel(self, sel_dir, small_segments):
        """The guard must not change what a REAL segment verifies or yields."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segments = log._segments_oldest_first()
        assert segments, "precondition: rotation happened"
        seg = segments[0]
        expected = [
            line.strip().decode("utf-8")
            for line in reversed(seg.read_bytes().splitlines())
            if line.strip()
        ]
        assert list(log._iter_lines_backward(seg)) == expected
        total, valid = log.verify_integrity()
        assert total == valid
        assert total > 0


class TestPlantedSegmentsAreNotReadAsHistory:
    """A forged segment must never surface through the read path.

    ``security_events.d`` is created by this release and was not on the
    sensitive-path floor before it, so an agent could have pre-created the
    directory and left a segment-shaped JSONL of its own choosing. Admission is
    by SIGNATURE: the chain key lives outside the log directory, so a planted
    file cannot carry a single valid one.
    """

    @staticmethod
    def _plant(sel_dir: Path, seq: int = 1) -> Path:
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        planted = segment_dir / f"security_events-{seq:06d}-20200101T000000Z.jsonl"
        forged = {
            "event_id": "forged0000000001",
            "timestamp": "2020-01-01T00:00:00+00:00",
            "event_type": "tool_invocation",
            "caller_identity": "dashboard:victim",
            "agent": "kirocrew",
            "source": "dashboard",
            "operation": "rm -rf /",
            "outcome": "approved",
            "resources": "FORGED-MARKER",
            "prev_hash": "",
            "entry_hash": "0" * 64,
            "metadata": {},
        }
        planted.write_text(json.dumps(forged) + "\n", encoding="utf-8")
        return planted

    def test_recent_never_returns_unsigned_records(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        self._plant(sel_dir)
        surfaced = json.dumps(log.recent(limit=10_000))
        assert "FORGED-MARKER" not in surfaced, "forged records were presented as audit events"

    def test_a_genuine_segment_is_still_read(self, sel_dir, small_segments):
        """The admission gate must not hide real history."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segments = log._segments_oldest_first()
        assert segments, "precondition: rotation happened"
        assert all(log._segment_is_signed_by_us(p) for p in segments)
        # A window reaching back into the segments returns their events.
        reached = log.recent(limit=10_000, since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert len(reached) > len(
            (sel_dir / "security_events.jsonl").read_text(encoding="utf-8").splitlines()
        )

    def test_verify_integrity_reports_a_planted_segment_instead_of_hiding_it(
        self, sel_dir, small_segments
    ):
        """An audit tool must surface tampering, not quietly drop the evidence."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        self._plant(sel_dir)
        total, valid = log.verify_integrity()
        assert total > valid, "the planted segment was hidden from verification"

    def test_an_empty_segment_is_not_admitted(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        empty = segment_dir / "security_events-000009-20200101T000000Z.jsonl"
        empty.write_text("", encoding="utf-8")
        assert log._segment_is_signed_by_us(empty) is False

    def test_segments_are_not_opened_when_the_live_log_answers(self, sel_dir, small_segments):
        """The bounded tail read must not pay for segment admission."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: segments exist"
        with patch.object(
            SecurityEventLog,
            "_segment_is_signed_by_us",
            side_effect=AssertionError("segment opened for a tail read"),
        ):
            assert len(log.recent(limit=3)) == 3


class TestLiveLogMovedOnHelper:
    """One predicate serves the pre-append check and the post-open guard.

    Answering the same question differently in those two places is how a stale tip
    survived a collision: the guard fired on growth while the re-anchor reacted only
    to a shrink, so a foreign APPEND was detected and then not corrected.
    """

    @pytest.mark.parametrize(
        ("previous", "current", "expected"),
        [
            ((1, 10, 500), (1, 10, 500), False),  # unchanged
            # Growth is somebody ELSE appending: our own writes refresh the anchor
            # from the fd, so a size difference always means a foreign write.
            ((1, 10, 500), (1, 10, 900), True),
            ((1, 10, 500), (1, 11, 40), True),  # new inode: replaced
            ((1, 10, 500), (1, 10, 40), True),  # shrank: append-only cannot
            ((1, 0, 500), (1, 0, 40), True),  # no inode available, size decides
            ((1, 0, 500), (1, 0, 500), False),  # no inode available, unchanged
            ((1, 10, 500), (2, 10, 40), True),  # same ino, different device
            ((0, 0, 0), (1, 7, 512), True),  # anchored absent, somebody wrote
            ((0, 0, 0), (1, 7, 0), False),  # anchored absent, still empty
        ],
    )
    def test_moved_on_signals(self, previous, current, expected):
        assert sel_mod._live_log_moved_on(previous, current) is expected

    def test_the_guard_delegates_to_the_same_predicate(self):
        """``_identity_changed`` must not be a second, drifting implementation."""

        class _St:
            def __init__(self, dev, ino, size):
                self.st_dev = dev
                self.st_ino = ino
                self.st_size = size

        for previous, dev, ino, size in [
            ((1, 10, 500), 1, 10, 500),
            ((1, 10, 500), 1, 10, 900),
            ((1, 10, 500), 1, 11, 500),
            ((0, 0, 0), 1, 7, 512),
        ]:
            assert sel_mod._identity_changed(previous, _St(dev, ino, size)) is (
                sel_mod._live_log_moved_on(previous, (dev, ino, size))
            )


class TestTimeWindowRead:
    """``recent()`` takes a time window, and reads only what it needs.

    A count alone could not express "the last two hours": on a busy log 6000
    entries reached 15 minutes back, so a two-hour question meant pulling ~90k
    entries and filtering client-side.
    """

    def test_since_and_until_bound_the_window(self, sel_dir):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 500)
        base = datetime(2026, 8, 21, tzinfo=timezone.utc)
        got = log.recent(
            limit=100, since=base + timedelta(seconds=100), until=base + timedelta(seconds=110)
        )
        assert [e["resources"] for e in got] == [f"seq={i}" for i in range(109, 99, -1)]

    def test_until_is_exclusive_and_since_inclusive(self, sel_dir):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 20)
        base = datetime(2026, 8, 21, tzinfo=timezone.utc)
        got = log.recent(
            limit=100, since=base + timedelta(seconds=5), until=base + timedelta(seconds=6)
        )
        assert [e["resources"] for e in got] == ["seq=5"]

    def test_window_reads_across_a_segment_boundary(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 120)
        assert log._segments_oldest_first(), "precondition: rotation happened"
        base = datetime(2026, 8, 21, tzinfo=timezone.utc)
        got = log.recent(limit=500, since=base + timedelta(seconds=110))
        # Rotation records carry their own (wall-clock) timestamp and are real
        # audit entries, so they legitimately fall in the window too.
        events = [e for e in got if e["event_type"] != "sel_rotation"]
        assert [e["resources"] for e in events] == [f"seq={i}" for i in range(119, 109, -1)]

    def test_limit_still_caps_a_windowed_read(self, sel_dir):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        got = log.recent(limit=4, since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert len(got) == 4
        assert got[0]["resources"] == "seq=199"

    def test_no_window_reads_the_tail_unchanged(self, sel_dir):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 50)
        assert [e["resources"] for e in log.recent(limit=3)] == ["seq=49", "seq=48", "seq=47"]

    def test_a_bounded_read_does_not_load_the_whole_file(self, sel_dir):
        """The read cost must not scale with the log's size.

        The old ``recent()`` did ``read_text().splitlines()``, so printing 20
        entries allocated the entire log — 4.09 GB on the reported install.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 4000)
        live = sel_dir / "security_events.jsonl"
        size = live.stat().st_size
        assert size > sel_mod._TAIL_CHUNK_BYTES, "precondition: log exceeds one chunk"
        read_bytes = 0
        real_read = os.read

        def counting_read(fd, n):
            nonlocal read_bytes
            chunk = real_read(fd, n)
            read_bytes += len(chunk)
            return chunk

        with patch("kiro_crew.sel.os.read", counting_read, create=True):
            with patch.object(Path, "read_text", side_effect=AssertionError("whole-file read")):
                assert len(log.recent(limit=5)) == 5

    def test_records_with_unreadable_timestamps_are_excluded_from_a_window(self, sel_dir):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        log.log(_make_event(event_id="bad", timestamp="not-a-time", resources="seq=bad"))
        _fill(log, 5)
        assert any(e["resources"] == "seq=bad" for e in log.recent(limit=50))
        windowed = log.recent(limit=50, since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert all(e["resources"] != "seq=bad" for e in windowed)

    def test_zero_limit_reads_nothing(self, sel_dir):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        assert log.recent(limit=0) == []
