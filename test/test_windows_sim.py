"""Self-tests for the Windows-condition simulators in ``windows_sim``.

These also serve as worked usage examples: each simulator is exercised both in
isolation and against the real code path it is meant to guard.
"""

from __future__ import annotations

import os
import shutil
import stat

import pytest
from windows_sim import (
    builtin_open_sharing_violation,
    colliding_clock,
    increasing_clock,
    nonatomic_write,
    open_sharing_violation,
    read_sharing_violation,
    readonly_unlink_refused,
    replace_sharing_violation,
    same_tick_file_stat,
    unlink_sharing_violation,
    windows_text_mode_write,
)


class TestCollidingClock:
    def test_now_is_frozen(self):
        import kiro_crew.history as h

        with colliding_clock("kiro_crew.history") as at:
            a = h.datetime.now()
            b = h.datetime.now()
        assert a == b == at
        assert a.isoformat() == b.isoformat()

    def test_inherited_classmethods_still_work(self):
        # The stub subclasses real datetime, so fromisoformat/strptime keep
        # working for code under test that uses them next to now().
        import kiro_crew.history as h

        with colliding_clock("kiro_crew.history"):
            parsed = h.datetime.fromisoformat("2020-05-01T12:00:00+00:00")
        assert (parsed.year, parsed.month) == (2020, 5)

    def test_tz_aware_now_respects_tz(self):
        import datetime as dt

        import kiro_crew.history as h

        with colliding_clock("kiro_crew.history"):
            aware = h.datetime.now(dt.timezone.utc)
        assert aware.tzinfo is not None

    def test_real_appends_survive_the_collision(self, tmp_path):
        from kiro_crew.history import ConversationLog, transcript_sort_key

        log = ConversationLog(base_dir=tmp_path)
        with colliding_clock("kiro_crew.history"):
            log.append("t", "user", "a")
            log.append("t", "user", "b")
            log.append("t", "user", "c")
        ts = [m["ts"] for m in log.read_messages("t")]
        # The simulator still hands every call one instant (test_now_is_frozen
        # proves that in isolation). ``append`` does not let it reach the file:
        # it stamps each row strictly after the one before, so the coarse-clock
        # collision Windows hits cannot collapse a turn onto one stamp.
        assert len(set(ts)) == 3
        keys = [transcript_sort_key(t) for t in ts]
        assert keys == sorted(keys)


class TestIncreasingClock:
    def test_now_strictly_increases(self):
        import kiro_crew.history as h

        with increasing_clock("kiro_crew.history"):
            first = h.datetime.now()
            second = h.datetime.now()
        assert second > first

    def test_real_appends_are_distinct_and_ordered(self, tmp_path):
        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        with increasing_clock("kiro_crew.history"):
            log.append("t", "user", "a")
            log.append("t", "user", "b")
        ts = [m["ts"] for m in log.read_messages("t")]
        assert len(set(ts)) == len(ts)  # distinct
        assert ts == sorted(ts)  # chronological


class TestReadSharingViolation:
    def test_first_read_raises_then_succeeds(self, tmp_path):
        f = tmp_path / "cred"
        f.write_bytes(b"data")
        with read_sharing_violation(match="cred", times=1) as state:
            with pytest.raises(PermissionError):
                f.read_bytes()
            assert f.read_bytes() == b"data"  # the retry sees the real bytes
        assert state["n"] >= 2

    def test_non_matching_path_unaffected(self, tmp_path):
        other = tmp_path / "other"
        other.write_bytes(b"x")
        with read_sharing_violation(match="cred"):
            assert other.read_bytes() == b"x"  # different name — never faults

    def test_times_zero_never_faults(self, tmp_path):
        f = tmp_path / "cred"
        f.write_bytes(b"data")
        with read_sharing_violation(match="cred", times=0):
            assert f.read_bytes() == b"data"


class TestReplaceSharingViolation:
    def test_first_replace_raises_then_succeeds(self, tmp_path):
        src = tmp_path / "src"
        src.write_bytes(b"v2")
        dst = tmp_path / "dst"
        dst.write_bytes(b"v1")
        with replace_sharing_violation(match="dst", times=1):
            with pytest.raises(PermissionError):
                os.replace(src, dst)
            os.replace(src, dst)  # retry succeeds
        assert dst.read_bytes() == b"v2"
        assert not src.exists()

    def test_non_matching_dest_unaffected(self, tmp_path):
        src = tmp_path / "src"
        src.write_bytes(b"v2")
        dst = tmp_path / "keep"
        dst.write_bytes(b"v1")
        with replace_sharing_violation(match="dst"):
            os.replace(src, dst)  # dest name != "dst" — not faulted
        assert dst.read_bytes() == b"v2"


class TestOpenSharingViolation:
    def test_first_create_raises_then_succeeds(self, tmp_path):
        target = tmp_path / "cred"
        with open_sharing_violation(match="cred", times=1) as state:
            with pytest.raises(PermissionError):
                os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)  # the retry succeeds
        assert target.exists()
        assert state["n"] >= 2

    def test_reads_not_faulted_when_create_only(self, tmp_path):
        f = tmp_path / "cred"
        f.write_bytes(b"x")
        with open_sharing_violation(match="cred", create_only=True):
            fd = os.open(str(f), os.O_RDONLY)  # no O_CREAT — not faulted
            os.close(fd)
        assert f.read_bytes() == b"x"


class TestBuiltinOpenSharingViolation:
    def test_first_open_raises_then_succeeds(self, tmp_path):
        f = tmp_path / "cred"
        f.write_text("data\n")
        with builtin_open_sharing_violation(match="cred", times=1) as state:
            with pytest.raises(PermissionError):
                open(f).close()
            with open(f) as fh:  # the retry sees the real content
                assert fh.readline() == "data\n"
        assert state["n"] >= 2

    def test_non_matching_path_unaffected(self, tmp_path):
        other = tmp_path / "other"
        other.write_text("x")
        with builtin_open_sharing_violation(match="cred"):
            with open(other) as fh:
                assert fh.read() == "x"  # different name — never faults

    def test_times_zero_never_faults(self, tmp_path):
        f = tmp_path / "cred"
        f.write_text("data")
        with builtin_open_sharing_violation(match="cred", times=0):
            with open(f) as fh:
                assert fh.read() == "data"

    def test_it_reaches_what_os_open_patching_cannot(self, tmp_path):
        """Why this simulator exists alongside ``open_sharing_violation``.

        CPython's builtin ``open()`` goes through the C ``_io`` layer and never
        consults the ``os.open`` Python attribute, so patching ``os.open`` cannot
        fault a plain read.
        """
        f = tmp_path / "cred"
        f.write_text("data")
        with open_sharing_violation(match="cred", times=1, create_only=False):
            with open(f) as fh:  # unaffected: os.open was never consulted
                assert fh.read() == "data"


class TestUnlinkSharingViolation:
    def test_path_unlink_raises_then_succeeds(self, tmp_path):
        f = tmp_path / "cred"
        f.write_bytes(b"data")
        with unlink_sharing_violation(match="cred", times=1) as state:
            with pytest.raises(PermissionError):
                f.unlink()
            assert f.exists()  # the faulted delete did NOT remove the file
            f.unlink()  # the retry succeeds
        assert not f.exists()
        assert state["n"] >= 2

    def test_os_unlink_also_faults(self, tmp_path):
        # Path.unlink routes through os.unlink, so patching os.unlink covers the
        # bare os.unlink entry point too — with a single shared counter.
        f = tmp_path / "cred"
        f.write_bytes(b"data")
        with unlink_sharing_violation(match="cred", times=1):
            with pytest.raises(PermissionError):
                os.unlink(str(f))
            os.unlink(str(f))
        assert not f.exists()

    def test_non_matching_path_unaffected(self, tmp_path):
        other = tmp_path / "other"
        other.write_bytes(b"x")
        with unlink_sharing_violation(match="cred"):
            other.unlink()  # different name — never faults
        assert not other.exists()

    def test_times_zero_never_faults(self, tmp_path):
        f = tmp_path / "cred"
        f.write_bytes(b"data")
        with unlink_sharing_violation(match="cred", times=0):
            f.unlink()
        assert not f.exists()


class TestReadonlyUnlinkRefused:
    @staticmethod
    def _read_only_tree(tmp_path):
        tree = tmp_path / "checkout"
        pack = tree / ".git" / "objects" / "pack"
        pack.mkdir(parents=True)
        blob = pack / "pack-0123456789abcdef0123456789abcdef01234567.pack"
        blob.write_bytes(b"PACK")
        os.chmod(blob, stat.S_IREAD)
        return tree, blob

    def test_a_read_only_file_cannot_be_unlinked(self, tmp_path):
        _tree, blob = self._read_only_tree(tmp_path)
        with readonly_unlink_refused() as state:
            with pytest.raises(PermissionError):
                os.unlink(blob)
            assert blob.exists()
        assert state["refused"] == [str(blob)]

    def test_clearing_the_bit_lets_the_delete_through(self, tmp_path):
        _tree, blob = self._read_only_tree(tmp_path)
        with readonly_unlink_refused():
            os.chmod(blob, stat.S_IREAD | stat.S_IWRITE)
            os.unlink(blob)
        assert not blob.exists()

    def test_a_writable_file_is_unaffected(self, tmp_path):
        plain = tmp_path / "plain"
        plain.write_bytes(b"x")
        with readonly_unlink_refused() as state:
            plain.unlink()
        assert not plain.exists()
        assert state["refused"] == []

    def test_ignore_errors_leaves_the_tree_behind(self, tmp_path):
        tree, blob = self._read_only_tree(tmp_path)
        with readonly_unlink_refused():
            shutil.rmtree(tree, ignore_errors=True)
        assert blob.exists()

    def test_is_posix_is_restored_on_exit(self):
        from kiro_crew import platform_compat

        before = platform_compat.IS_POSIX
        with readonly_unlink_refused():
            assert platform_compat.IS_POSIX is False
        assert platform_compat.IS_POSIX is before


class TestNonatomicWrite:
    def test_empty_during_block_full_after(self, tmp_path):
        cred = tmp_path / "cred"  # does not exist yet
        with nonatomic_write(cred, b"secret-v1"):
            # Truncate phase: the file exists but is EMPTY — exactly the
            # transient a concurrent poller can observe as a spurious revision.
            assert cred.exists()
            assert cred.read_bytes() == b""
        # Completion phase: the full payload has landed.
        assert cred.read_bytes() == b"secret-v1"

    def test_full_payload_lands_even_if_block_raises(self, tmp_path):
        cred = tmp_path / "cred"
        with pytest.raises(RuntimeError):
            with nonatomic_write(cred, b"secret-v1"):
                raise RuntimeError("boom")
        assert cred.read_bytes() == b"secret-v1"  # finally-clause completes the write


class TestWindowsTextModeWrite:
    _PAYLOAD = bytes(range(32))  # contains 0x0A (LF) at index 10

    def _write_via_os(self, path, flags):
        fd = os.open(str(path), flags, 0o600)
        try:
            os.write(fd, self._PAYLOAD)
        finally:
            os.close(fd)

    def test_text_mode_fd_translates_newline(self, tmp_path):
        f = tmp_path / "cred"
        with windows_text_mode_write(match="cred") as state:
            # No os.O_BINARY -> simulated text mode -> 0x0A becomes 0x0D 0x0A.
            self._write_via_os(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        on_disk = f.read_bytes()
        assert on_disk != self._PAYLOAD
        assert len(on_disk) == len(self._PAYLOAD) + 1  # one '\n' -> '\r\n'
        assert b"\r\n" in on_disk
        assert state["translated"] == 1

    def test_binary_fd_is_not_translated(self, tmp_path):
        f = tmp_path / "cred"
        with windows_text_mode_write(match="cred") as state:
            # ORing the (simulated, non-zero) os.O_BINARY suppresses translation.
            self._write_via_os(
                f, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            )
        assert f.read_bytes() == self._PAYLOAD
        assert state["translated"] == 0

    def test_non_matching_path_unaffected(self, tmp_path):
        f = tmp_path / "other"
        with windows_text_mode_write(match="cred"):
            self._write_via_os(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        assert f.read_bytes() == self._PAYLOAD  # different name — never translated

    def test_os_write_returns_input_byte_count(self, tmp_path):
        # Windows reports the INPUT bytes consumed even though more land on disk;
        # a short-write loop relies on this to terminate correctly.
        f = tmp_path / "cred"
        with windows_text_mode_write(match="cred"):
            fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                n = os.write(fd, self._PAYLOAD)
            finally:
                os.close(fd)
        assert n == len(self._PAYLOAD)


class TestSameTickFileStat:
    def test_timestamps_pin_while_size_stays_live(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("a\n", encoding="utf-8", newline="\n")
        with same_tick_file_stat(match="t.jsonl") as seen:
            first = p.stat()
            os.utime(p, ns=(first.st_mtime_ns + 5_000_000_000,) * 2)
            p.write_text("a\nbb\n", encoding="utf-8", newline="\n")
            second = p.stat()
            assert (second.st_mtime_ns, second.st_ctime_ns) == (
                first.st_mtime_ns,
                first.st_ctime_ns,
            )
            assert second.st_mtime == first.st_mtime  # the float view pins with the ns one
            assert second.st_size == len("a\nbb\n") > first.st_size
            assert second.st_ino == first.st_ino
            assert seen["n"] == 2
            os.utime(p, ns=(first.st_mtime_ns + 7_000_000_000,) * 2)
        assert (
            p.stat().st_mtime_ns == first.st_mtime_ns + 7_000_000_000
        ), "the pin outlived the block"

    def test_non_matching_path_unaffected(self, tmp_path):
        other = tmp_path / "other.txt"
        other.write_text("x")
        with same_tick_file_stat(match="t.jsonl") as seen:
            before = other.stat().st_mtime_ns
            os.utime(other, ns=(before + 5_000_000_000,) * 2)
            assert other.stat().st_mtime_ns == before + 5_000_000_000
            assert seen["n"] == 0
