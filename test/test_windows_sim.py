"""Self-tests for the Windows-condition simulators in ``windows_sim``.

These also serve as worked usage examples: each simulator is exercised both in
isolation and against the real code path it is meant to guard.
"""

from __future__ import annotations

import os

import pytest
from windows_sim import (
    colliding_clock,
    increasing_clock,
    open_sharing_violation,
    read_sharing_violation,
    replace_sharing_violation,
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

    def test_real_appends_collide(self, tmp_path):
        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        with colliding_clock("kiro_crew.history"):
            log.append("t", "user", "a")
            log.append("t", "user", "b")
            log.append("t", "user", "c")
        ts = [m["ts"] for m in log.read_messages("t")]
        assert len(set(ts)) == 1  # the exact coarse-clock collision Windows hits


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
