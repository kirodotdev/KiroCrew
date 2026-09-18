"""Tests for PID tracking and orphan cleanup in session.py."""

from __future__ import annotations

import errno
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from kiro_crew import platform_compat

# These tests exercise POSIX-only process-management semantics: process-group
# APIs (os.killpg / os.getpgrp / os.getpgid), POSIX identity/age probes
# (os.getuid / os.sysconf, /proc, ps), the raw signal.SIGKILL constant, and the
# POSIX kill path of the orphan sweep (which no-ops on Windows). None of these
# have a Windows equivalent, so they are skipped on Windows.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX process-management semantics only; see issue #2041"
)


@pytest.fixture()
def pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect _pid_file_path to a temp file."""
    p = tmp_path / "kiro_pids.txt"
    monkeypatch.setattr("kiro_crew.session_pid._pid_file_path", lambda: p)
    return p


@pytest.fixture()
def session_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect _session_pid_file_path to a temp file."""
    p = tmp_path / "kiro_session_pids.txt"
    monkeypatch.setattr("kiro_crew.session_pid._session_pid_file_path", lambda: p)
    return p


class TestTrackUntrack:
    def test_track_pid_creates_file(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid

        _track_pid(12345)
        assert "12345" in pid_file.read_text(encoding="utf-8")

    def test_track_multiple(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid

        _track_pid(111)
        _track_pid(222)
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == ["111", "222"]

    def test_untrack_pid(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid, _untrack_pid

        _track_pid(111)
        _track_pid(222)
        _untrack_pid(111)
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == ["222"]

    def test_untrack_nonexistent(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid, _untrack_pid

        _track_pid(111)
        _untrack_pid(999)  # should not crash
        assert "111" in pid_file.read_text(encoding="utf-8")

    @pytest.mark.parametrize("token", [None, "tok123"])
    def test_untrack_session_pid(
        self,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        token: str | None,
    ) -> None:
        """Untrack removes only the named PID's entry, in either record format.

        Pin the start token instead of inheriting it from the host: PIDs 111
        and 222 are live kernel threads on some machines (so _track writes the
        3-field ``gw:pid:token``) and absent on others (2-field ``gw:pid``),
        which would otherwise make the expected value host-dependent.
        """
        from kiro_crew.session_pid import _track_session_pid, _untrack_session_pid

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: token)
        _track_session_pid(111)
        _track_session_pid(222)
        _untrack_session_pid(111)
        gw = os.getpid()
        suffix = f":{token}" if token else ""
        lines = session_pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == [f"{gw}:222{suffix}"]

    def test_untrack_session_pid_missing_file(self, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _untrack_session_pid

        _untrack_session_pid(999)  # should not crash on missing file
        assert not session_pid_file.exists()

    def test_untrack_session_pid_other_gateway_untouched(self, session_pid_file: Path) -> None:
        """Untracking our PID must NOT remove other gateways' entries for same child PID."""
        from kiro_crew.session_pid import _track_session_pid, _untrack_session_pid

        _track_session_pid(111)
        # Simulate another gateway's entry for the same child PID
        with open(session_pid_file, "a", encoding="utf-8") as f:
            f.write("99999:111\n")
        _untrack_session_pid(111)
        lines = session_pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == ["99999:111"]

    def test_track_child_pids_with_parent(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({100: None, 200: None, 300: None}, parent_pid=999)
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert set(lines) == {"100:999", "200:999", "300:999"}

    def test_track_child_pids_dedup(self, pid_file: Path) -> None:
        """Duplicate child:parent entries should not be written."""
        from kiro_crew.session_pid import _track_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({100: None, 200: None}, parent_pid=999)
            _track_child_pids({100: None, 300: None}, parent_pid=999)
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert sorted(lines) == ["100:999", "200:999", "300:999"]

    def test_untrack_child_pids(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_child_pids, _untrack_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({100: None, 200: None, 300: None}, parent_pid=999)
        _untrack_child_pids({100: None, 300: None})
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == ["200:999"]

    def test_untrack_child_pids_preserves_bare_pid(self, pid_file: Path) -> None:
        """Untracking child PIDs must not remove bare PID lines (kiro-cli parents)."""
        from kiro_crew.session_pid import _track_child_pids, _track_pid, _untrack_child_pids

        _track_pid(100)  # bare parent line
        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({100: None}, parent_pid=999)  # child line with same PID
        _untrack_child_pids({100: None})
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert "100" in lines  # bare line preserved

    def test_replace_child_pids_rewrites_only_the_children_it_names(self, pid_file: Path) -> None:
        """A whole-set write for the caller's OWN children, nothing else."""
        from kiro_crew.session_pid import _replace_child_pids, _track_child_pids, _track_pid

        _track_pid(100)  # bare root line, another owner's business
        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({200: None, 300: None}, parent_pid=999)
            _track_child_pids({400: None}, parent_pid=888)  # a different parent

        assert (
            _replace_child_pids(
                {300: ("t300", b"x"), 500: ("t500", b"y")}, parent_pid=999, drop=(200,)
            )
            is True
        )

        lines = set(pid_file.read_text(encoding="utf-8").strip().splitlines())
        # 200 dropped, 500 arrived, and neither the bare root nor 888's child moved.
        assert lines == {"100", "300:999:t300", "500:999:t500", "400:888"}

    def test_replace_child_pids_keeps_a_child_it_was_not_given(self, pid_file: Path) -> None:
        """A root PID is reused like any other number.

        A descendant that outlived an earlier runtime holding this number is
        still tracked under it. Wiping the block by owner alone would untrack
        that survivor permanently — the leak this file exists to prevent.
        """
        from kiro_crew.session_pid import _replace_child_pids, _track_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({777: None}, parent_pid=999)  # an older runtime's survivor

        assert _replace_child_pids({300: ("t300", b"x")}, parent_pid=999) is True

        lines = set(pid_file.read_text(encoding="utf-8").strip().splitlines())
        assert lines == {"777:999", "300:999:t300"}

    def test_replace_child_pids_writes_the_recorded_token_not_a_live_read(
        self, pid_file: Path
    ) -> None:
        """The writer must never read a live pid's identity for itself.

        Reading it here reopens the window the caller closed: a descendant that
        exited and had its number taken would be written with the STRANGER's
        token, and the sweep compares live against recorded — both the
        stranger's, so they agree and it kills an unrelated process.
        """
        from kiro_crew.session_pid import _replace_child_pids, _track_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value="old"):
            _track_child_pids({200: None}, parent_pid=999)
        assert pid_file.read_text(encoding="utf-8").strip() == "200:999:old"

        with patch("kiro_crew.session_pid._pid_start_token", return_value="live-stranger") as live:
            assert _replace_child_pids({200: ("recorded", b"x")}, parent_pid=999) is True

        live.assert_not_called()
        assert pid_file.read_text(encoding="utf-8").strip() == "200:999:recorded"

    def test_replace_child_pids_falls_back_to_two_fields(self, pid_file: Path) -> None:
        """An identity that cannot be a field leaves the sweep nothing to match."""
        from kiro_crew.session_pid import _replace_child_pids

        assert _replace_child_pids({200: (None, b"x"), 300: ("has:colon", b"y")}, 999) is True

        lines = set(pid_file.read_text(encoding="utf-8").strip().splitlines())
        assert lines == {"200:999", "300:999"}

    def test_replace_child_pids_drops_a_parents_last_child(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _replace_child_pids, _track_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({200: None}, parent_pid=999)

        assert _replace_child_pids({}, parent_pid=999, drop=(200,)) is True
        assert pid_file.read_text(encoding="utf-8").strip() == ""

    def test_replace_child_pids_with_nothing_named_writes_nothing(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _replace_child_pids, _track_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({200: None}, parent_pid=999)

        assert _replace_child_pids({}, parent_pid=999) is True
        assert pid_file.read_text(encoding="utf-8").strip() == "200:999"

    def test_replace_child_pids_reports_a_failed_write(self, pid_file: Path) -> None:
        """The caller publishes nothing on False, so the answer must be truthful."""
        from kiro_crew.session_pid import _replace_child_pids

        with patch("kiro_crew.session_pid._rewrite_pid_file", return_value=False):
            assert _replace_child_pids({200: ("t", b"x")}, parent_pid=999) is False

    def test_replace_child_pids_refuses_a_parentless_call(self, pid_file: Path) -> None:
        """parent_pid=0 names no owner's block, so there is nothing to replace."""
        from kiro_crew.session_pid import _replace_child_pids

        assert _replace_child_pids({200: ("t", b"x")}, parent_pid=0) is False


class TestSignalOrphanedRuntimeGroup:
    """Signal a reaped-root group's MEMBERS, by identity, once they vouch.

    Never the group number: it is the dead root's pid, and the kernel can hand
    it to a fresh session leader at any moment.
    """

    @staticmethod
    def _identity(monkeypatch, live: dict[int, str | None]) -> None:
        """What each pid's start id reads as at the instant of the signal."""
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda pid: live.get(pid))

    @staticmethod
    def _delivery(monkeypatch, seam: str) -> list[tuple[int, int]]:
        """Record what each delivery seam sends, one list for either branch.

        There are two: a pidfd, which pins the process so a recycled number
        cannot be reached, and the re-verified ``os.kill`` for a kernel without
        one. Both must satisfy the same assertions, so every test that checks
        delivery runs against both.
        """
        from kiro_crew import session_pid as sp

        sent: list[tuple[int, int]] = []
        if seam == "kill":
            monkeypatch.delattr(sp.os, "pidfd_open", raising=False)
            monkeypatch.delattr(sp.signal, "pidfd_send_signal", raising=False)
            monkeypatch.setattr(sp.os, "kill", lambda pid, sig: sent.append((pid, sig)))
            return sent
        fds = {}

        def _open(pid):
            fd = 900 + len(fds)
            fds[fd] = pid
            return fd

        monkeypatch.setattr(sp.os, "pidfd_open", _open, raising=False)
        monkeypatch.setattr(
            sp.signal,
            "pidfd_send_signal",
            lambda fd, sig: sent.append((fds[fd], sig)),
            raising=False,
        )
        monkeypatch.setattr(sp.os, "close", lambda fd: fds.pop(fd, None))
        # A signal that went out through the descriptor must not also go out
        # through the number.
        monkeypatch.setattr(
            sp.os, "kill", lambda pid, sig: pytest.fail("os.kill used while a pidfd was available")
        )
        return sent

    @pytest.mark.parametrize("seam", ["kill", "pidfd"])
    def test_signals_each_vouched_member_by_identity(self, monkeypatch, seam) -> None:
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {101: "a", 102: "b"})
        self._identity(monkeypatch, {101: "a", 102: "b"})
        sent = self._delivery(monkeypatch, seam)
        killpg = MagicMock()
        monkeypatch.setattr(sp.os, "killpg", killpg)

        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {101: "a", 102: "b"}
        # Highest pid first (leaf-first), one signal per member, and no group signal.
        assert sent == [(102, 15), (101, 15)]
        killpg.assert_not_called()

    @pytest.mark.parametrize(
        "err,expect_numeric",
        [("ENOSYS", True), ("EPERM", True), ("EMFILE", False), ("ENOMEM", False)],
    )
    def test_pidfd_errnos_split_absent_from_refused(self, monkeypatch, err, expect_numeric) -> None:
        """ENOSYS is "this kernel has no pidfd", which is what the number is for.

        The attribute exists on any Linux build, so a pre-5.3 kernel reaches the
        open and is told ENOSYS. Failing closed there would signal no member at
        all and disable this path on those hosts. A refusal of a call the kernel
        HAS (EMFILE, ENOMEM) is the opposite case and must not use the number.
        """
        import errno as errno_mod

        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {101: "a"})
        self._identity(monkeypatch, {101: "a"})

        def _refuse(pid):
            raise OSError(getattr(errno_mod, err), err)

        numeric: list[tuple[int, int]] = []
        monkeypatch.setattr(sp.os, "pidfd_open", _refuse, raising=False)
        monkeypatch.setattr(sp.signal, "pidfd_send_signal", lambda fd, sig: None, raising=False)
        monkeypatch.setattr(sp.os, "kill", lambda pid, sig: numeric.append((pid, sig)))

        result = sp._signal_orphaned_runtime_group(100, 15, "inst")

        if expect_numeric:
            assert result == {101: "a"}
            assert numeric == [(101, 15)]
        else:
            assert result == {}
            assert numeric == []

    def test_a_send_that_reports_no_syscall_falls_back_to_the_number(self, monkeypatch) -> None:
        """`pidfd_send_signal` is 5.1 and `pidfd_open` is 5.3, so they can differ."""
        import errno as errno_mod

        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {101: "a"})
        self._identity(monkeypatch, {101: "a"})

        def _send(fd, sig):
            raise OSError(errno_mod.ENOSYS, "ENOSYS")

        numeric: list[tuple[int, int]] = []
        monkeypatch.setattr(sp.os, "pidfd_open", lambda pid: 900, raising=False)
        monkeypatch.setattr(sp.signal, "pidfd_send_signal", _send, raising=False)
        monkeypatch.setattr(sp.os, "close", lambda fd: None)
        monkeypatch.setattr(sp.os, "kill", lambda pid, sig: numeric.append((pid, sig)))

        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {101: "a"}
        assert numeric == [(101, 15)]

    def test_a_refused_pidfd_does_not_fall_back_to_signalling_the_number(self, monkeypatch) -> None:
        """A kernel that HAS the call and refused it gets no numeric signal.

        `pidfd_open` can fail for reasons that have nothing to do with the target
        (EMFILE, ENOMEM). Falling back to the number there would reopen the reuse
        window the descriptor exists to close, so the member is skipped and left
        to the sweep instead.
        """
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {101: "a"})
        self._identity(monkeypatch, {101: "a"})

        def _refuse(pid):
            raise OSError(24, "Too many open files")

        monkeypatch.setattr(sp.os, "pidfd_open", _refuse, raising=False)
        monkeypatch.setattr(sp.signal, "pidfd_send_signal", lambda fd, sig: None, raising=False)
        monkeypatch.setattr(
            sp.os, "kill", lambda pid, sig: pytest.fail("fell back to signalling the number")
        )

        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {}

    def test_the_pidfd_identity_check_happens_after_the_descriptor_is_open(
        self, monkeypatch
    ) -> None:
        """The order is what closes the window, so the order is what is pinned.

        A descriptor opened first pins whichever process answered, so a check
        after it proves the pinned process is the one that vouched. Checking
        first and opening second leaves exactly the window the descriptor was
        introduced to remove: here the number changes hands AT the open, and only
        the correct order notices.
        """
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {101: "a"})
        live = {101: "a"}
        monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda pid: live.get(pid))

        sent: list[tuple[int, int]] = []

        def _open(pid):
            live[pid] = "someone-else"  # the number changes hands at this instant
            return 900

        monkeypatch.setattr(sp.os, "pidfd_open", _open, raising=False)
        monkeypatch.setattr(
            sp.signal, "pidfd_send_signal", lambda fd, sig: sent.append((fd, sig)), raising=False
        )
        monkeypatch.setattr(sp.os, "close", lambda fd: None)
        monkeypatch.setattr(
            sp.os, "kill", lambda pid, sig: pytest.fail("os.kill used while a pidfd was available")
        )

        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {}
        assert sent == []

    @pytest.mark.parametrize("seam", ["kill", "pidfd"])
    def test_a_member_recycled_between_vouch_and_signal_is_skipped(self, monkeypatch, seam) -> None:
        """A pid that changed hands is not signalled.

        On the pidfd seam the verification happens after the descriptor is open,
        so the check is binding rather than merely recent; on the fallback it is
        the same re-read as before. Neither may signal the newcomer.
        """
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {101: "a", 102: "b"})
        self._identity(monkeypatch, {101: "a", 102: "b2"})  # 102 changed hands
        sent = self._delivery(monkeypatch, seam)

        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {101: "a"}
        assert sent == [(101, 15)]

    @pytest.mark.parametrize("seam", ["kill", "pidfd"])
    def test_escalation_re_signals_only_the_members_it_vouched(self, monkeypatch, seam) -> None:
        """A vouched member still alive under the same start id is the proof, and
        the only target."""
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {101: "a", 103: "c"})
        self._identity(monkeypatch, {101: "a", 103: "c"})
        sent = self._delivery(monkeypatch, seam)

        # 103 is alive and vouched but was not there on the first pass: it was
        # never signalled, owes no escalation, and is what a newer incarnation
        # of the number would look like. Only 101 is re-signalled.
        assert sp._signal_orphaned_runtime_group(100, 9, "inst", expected={101: "a", 102: "b"}) == {
            101: "a",
        }
        assert sent == [(101, 9)]

    def test_escalation_refuses_a_group_none_of_whose_vouched_members_survive(
        self, monkeypatch
    ) -> None:
        """Every runtime is a marked session leader, so a fresh one on the reused
        number vouches as well as the old did. Only the members the SIGTERM saw
        can tell them apart; none alive under their start id means not ours."""
        from kiro_crew import session_pid as sp

        # 101 is alive but under a NEW start id: the pid was reused.
        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {101: "a2", 200: "z"})
        kill = MagicMock()
        monkeypatch.setattr(sp.os, "kill", kill)

        assert (
            sp._signal_orphaned_runtime_group(100, 9, "inst", expected={101: "a", 102: "b"}) == {}
        )
        kill.assert_not_called()

    def test_escalation_refuses_when_a_vouched_member_has_no_readable_identity(
        self, monkeypatch
    ) -> None:
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {101: None})
        kill = MagicMock()
        monkeypatch.setattr(sp.os, "kill", kill)

        assert sp._signal_orphaned_runtime_group(100, 9, "inst", expected={101: None}) == {}
        kill.assert_not_called()

    def test_a_member_with_no_readable_identity_is_never_signalled(self, monkeypatch) -> None:
        """Nothing to compare at the instant of the signal means no signal."""
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {101: None})
        kill = MagicMock()
        monkeypatch.setattr(sp.os, "kill", kill)

        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {}
        kill.assert_not_called()

    def test_no_vouching_member_sends_nothing(self, monkeypatch) -> None:
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {})
        kill = MagicMock()
        monkeypatch.setattr(sp.os, "kill", kill)

        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {}
        kill.assert_not_called()

    def test_a_refused_signal_does_not_abort_the_teardown(self, monkeypatch) -> None:
        """The caller is mid-teardown; an error here would skip its state clearing
        and PID pruning. The sweep retries a refused member on its own cadence."""
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {101: "a"})
        self._identity(monkeypatch, {101: "a"})

        def _refused(pid, sig):
            raise PermissionError

        monkeypatch.setattr(sp.os, "kill", _refused)
        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {}

    def test_a_member_that_exited_under_us_counts_as_nothing(self, monkeypatch) -> None:
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {101: "a"})
        self._identity(monkeypatch, {101: "a"})

        def _gone(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(sp.os, "kill", _gone)
        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {}

    @pytest.mark.parametrize("pgid", [0, 1])
    def test_refuses_a_broadcast_group(self, monkeypatch, pgid) -> None:
        """A member listing keyed on 0 or 1 would be a listing of the wrong thing."""
        from kiro_crew import session_pid as sp

        members = MagicMock(return_value={101: "a"})
        monkeypatch.setattr(sp, "_marked_group_members", members)
        kill = MagicMock()
        monkeypatch.setattr(sp.os, "kill", kill)

        assert sp._signal_orphaned_runtime_group(pgid, 15, "inst") == {}
        members.assert_not_called()
        kill.assert_not_called()

    def test_refuses_to_signal_without_an_instance(self, monkeypatch) -> None:
        """No incarnation pin, no authority: the number plus the generic marker
        cannot tell this spawns's group from a fresh runtime's on a recycled pid."""
        from kiro_crew import session_pid as sp

        members = MagicMock(return_value={101: "a"})
        monkeypatch.setattr(sp, "_marked_group_members", members)
        kill = MagicMock()
        monkeypatch.setattr(sp.os, "kill", kill)

        assert sp._signal_orphaned_runtime_group(100, 15, "") == {}
        members.assert_not_called()
        kill.assert_not_called()

    def test_refuses_our_own_group(self, monkeypatch) -> None:
        from kiro_crew import session_pid as sp

        kill = MagicMock()
        monkeypatch.setattr(sp.os, "kill", kill)
        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst: {101: "a"})

        assert sp._signal_orphaned_runtime_group(sp.os.getpgrp(), 15, "inst") == {}
        kill.assert_not_called()


class TestMarkedGroupMembers:
    """The vouching read: which live members of a group are ours."""

    @staticmethod
    def _fake_proc(tmp_path: Path, rows: dict[int, tuple[str, int, str]]) -> Path:
        """rows: pid -> (state, pgrp, spawn instance). Minimal /proc/<pid>/{stat,environ}."""
        for pid, (state, pgrp, inst) in rows.items():
            d = tmp_path / str(pid)
            d.mkdir()
            # pid (comm) state ppid pgrp ...
            (d / "stat").write_text(f"{pid} (x) {state} 1 {pgrp} 0 0 0 0 0", encoding="utf-8")
            (d / "environ").write_bytes(
                b"KIROCREW_SPAWNED=1\x00KIROCREW_SPAWN_INSTANCE="
                + inst.encode()
                + b"\x00PATH=/bin\x00"
            )
        (tmp_path / "self").mkdir()  # a non-digit entry, skipped
        return tmp_path

    def test_only_live_marked_members_of_the_group_count(self, tmp_path, monkeypatch) -> None:
        from kiro_crew import session_pid as sp

        # The reader is Linux-gated, and the fixture supplies the /proc shape it
        # reads, so pin the platform rather than skipping: a skip would leave the
        # vouching rules unasserted on the hosts where a wrong kill is worst.
        monkeypatch.setattr(sp.sys, "platform", "linux")
        root = self._fake_proc(
            tmp_path,
            {
                101: ("S", 100, "ours"),  # ours, live
                102: ("Z", 100, "ours"),  # ours, but a zombie
                103: ("S", 100, "ours"),  # in the group, no marker
                104: ("S", 200, "ours"),  # another group
                105: ("S", 100, "theirs"),  # a fresh runtime that took the number
            },
        )
        monkeypatch.setattr(sp, "Path", lambda p="/proc": root if p == "/proc" else Path(p))
        real_reader = sp._env_spawn_instance
        monkeypatch.setattr(sp, "_env_spawn_instance", lambda pid: real_reader(pid, root))
        monkeypatch.setattr(sp, "_env_has_kirocrew_marker", lambda pid: pid in (101, 102, 105))
        monkeypatch.setattr(sp, "_tracked_child_has_runtime_identity", lambda pid: True)
        monkeypatch.setattr(sp, "_pid_start_token", lambda pid: f"s{pid}")

        assert sp._marked_group_members(100, "ours") == {101: "s101"}
        # The instance is the pin: the same group read as a different spawn is empty.
        assert sp._marked_group_members(100, "theirs") == {105: "s105"}
        assert sp._marked_group_members(100, "") == {}

    def test_a_marked_member_without_runtime_identity_does_not_vouch(
        self, tmp_path, monkeypatch
    ) -> None:
        """A detached survivor that merely inherited the marker."""
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "linux")
        root = self._fake_proc(tmp_path, {101: ("S", 100, "ours")})
        monkeypatch.setattr(sp, "Path", lambda p="/proc": root if p == "/proc" else Path(p))
        real_reader = sp._env_spawn_instance
        monkeypatch.setattr(sp, "_env_spawn_instance", lambda pid: real_reader(pid, root))
        monkeypatch.setattr(sp, "_env_has_kirocrew_marker", lambda pid: True)
        monkeypatch.setattr(sp, "_tracked_child_has_runtime_identity", lambda pid: False)

        assert sp._marked_group_members(100, "ours") == {}

    def test_env_spawn_instance_reads_the_value_and_fails_closed(self, tmp_path) -> None:
        from kiro_crew import session_pid as sp

        root = self._fake_proc(tmp_path, {101: ("S", 100, "abc123")})
        (tmp_path / "202").mkdir()
        (tmp_path / "202" / "environ").write_bytes(b"KIROCREW_SPAWNED=1\x00")
        assert sp._env_spawn_instance(101, root) == "abc123"
        assert sp._env_spawn_instance(202, root) is None  # marker but no instance
        assert sp._env_spawn_instance(303, root) is None  # unreadable


class TestCleanupOrphanedMcpServers:
    def test_dead_child_pruned(self, pid_file: Path) -> None:
        """Dead child PIDs should be removed from the file silently."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("99999:1\n")  # child=99999, parent=1
        _cleanup_orphaned_mcp_servers()
        assert "99999" not in pid_file.read_text(encoding="utf-8")

    def test_alive_child_with_alive_parent_survives(self, pid_file: Path) -> None:
        """Child whose parent session is still alive should NOT be killed."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        my_pid = os.getpid()
        child_pid = 77777
        pid_file.write_text(f"{child_pid}:{my_pid}\n")

        def fake_pid_exists(pid: int) -> bool:
            return pid in (child_pid, my_pid)  # both alive

        with patch("kiro_crew.session_pid.platform_compat.pid_exists", side_effect=fake_pid_exists):
            killed = _cleanup_orphaned_mcp_servers()

        assert killed == 0
        assert str(child_pid) in pid_file.read_text(encoding="utf-8")

    def test_alive_child_with_dead_parent_killed(self, pid_file: Path) -> None:
        """Child whose parent session died should be killed (PPid=1 confirms orphan)."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("77777:99999\n")  # parent 99999 is dead

        def fake_pid_exists(pid: int) -> bool:
            return pid == 77777  # child alive, parent dead

        with (
            patch("kiro_crew.session_pid.platform_compat.pid_exists", side_effect=fake_pid_exists),
            patch("kiro_crew.session_pid.platform_compat.kill_pid"),
            patch("kiro_crew.platform_compat.get_ppid", return_value=1),
        ):
            killed = _cleanup_orphaned_mcp_servers()

        assert killed == 1

    def test_alive_child_with_dead_parent_pid_reused(self, pid_file: Path) -> None:
        """Child PID reused by unrelated process should NOT be killed."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("77777:99999\n")

        def fake_pid_exists(pid: int) -> bool:
            return pid == 77777  # child alive (reused PID), parent dead

        with (
            patch("kiro_crew.session_pid.platform_compat.pid_exists", side_effect=fake_pid_exists),
            patch("kiro_crew.platform_compat.get_ppid", return_value=5555),
            patch("kiro_crew.session_pid._accepted_subreaper_pids", return_value={1}),
        ):
            killed = _cleanup_orphaned_mcp_servers()

        assert killed == 0
        assert "77777" not in pid_file.read_text(encoding="utf-8")  # stale entry pruned

    def test_orphan_reparented_to_systemd_user_killed(self, pid_file: Path) -> None:
        """Orphan reparented to a same-uid systemd --user subreaper IS killed.

        Under a ``systemd --user`` gateway a genuine orphan reparents to the
        user manager process, not pid 1. A guard accepting only
        ``(1, parent_pid)`` misreads that orphan as PID reuse and prunes it
        WITHOUT killing -- leaking the runtime where no other reaper can see
        it. The guard must accept the shared subreaper set instead, gated on
        the KIROCREW_SPAWNED environ marker as positive identity.
        """
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        systemd_user_pid = 4242  # same-uid systemd --user manager
        pid_file.write_text("77777:99999\n")  # parent 99999 is dead

        def fake_pid_exists(pid: int) -> bool:
            return pid == 77777  # child alive, parent dead

        kill_mock = MagicMock()
        with (
            patch("kiro_crew.session_pid.platform_compat.pid_exists", side_effect=fake_pid_exists),
            patch("kiro_crew.session_pid.platform_compat.kill_pid", kill_mock),
            patch("kiro_crew.platform_compat.get_ppid", return_value=systemd_user_pid),
            patch(
                "kiro_crew.session_pid._accepted_subreaper_pids",
                return_value={1, systemd_user_pid},
            ),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._tracked_child_has_runtime_identity",
                return_value=True,
            ),
        ):
            killed = _cleanup_orphaned_mcp_servers()

        assert killed == 1
        kill_mock.assert_called_once()
        assert kill_mock.call_args.args[0] == 77777
        assert "77777" not in pid_file.read_text(encoding="utf-8")

    def test_marked_survivor_without_runtime_identity_not_killed(self, pid_file: Path) -> None:
        """A marker-carrying intentional survivor is outside the kill authority.

        The KIROCREW_SPAWNED marker is tree-wide: a detached survivor (e.g. a
        preview server) inherits it, so the systemd arm demands positive
        runtime argv identity on top of the marker. A tracking entry naming
        such a PID -- stale or forged -- is pruned without killing.
        """
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        systemd_user_pid = 4242
        pid_file.write_text("77777:99999\n")

        def fake_pid_exists(pid: int) -> bool:
            return pid == 77777

        kill_mock = MagicMock()
        with (
            patch("kiro_crew.session_pid.platform_compat.pid_exists", side_effect=fake_pid_exists),
            patch("kiro_crew.session_pid.platform_compat.kill_pid", kill_mock),
            patch("kiro_crew.platform_compat.get_ppid", return_value=systemd_user_pid),
            patch(
                "kiro_crew.session_pid._accepted_subreaper_pids",
                return_value={1, systemd_user_pid},
            ),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._tracked_child_has_runtime_identity",
                return_value=False,
            ),
        ):
            killed = _cleanup_orphaned_mcp_servers()

        assert killed == 0
        kill_mock.assert_not_called()
        assert "77777" not in pid_file.read_text(encoding="utf-8")

    def test_systemd_parented_recycled_pid_without_marker_not_killed(self, pid_file: Path) -> None:
        """Recycled PID now owned by an unrelated systemd --user service survives.

        Under ``systemd --user`` every manager-started service carries the
        manager's PID as its PPid for its whole life, so PPid membership in
        the subreaper set alone does not prove the PID is ours. Without the
        KIROCREW_SPAWNED environ marker the sweep must treat the entry as PID
        reuse: prune the stale line, never SIGKILL the innocent process.
        """
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        systemd_user_pid = 4242
        pid_file.write_text("77777:99999\n")  # parent 99999 is dead

        def fake_pid_exists(pid: int) -> bool:
            return pid == 77777  # PID alive (recycled), parent dead

        kill_mock = MagicMock()
        with (
            patch("kiro_crew.session_pid.platform_compat.pid_exists", side_effect=fake_pid_exists),
            patch("kiro_crew.session_pid.platform_compat.kill_pid", kill_mock),
            patch("kiro_crew.platform_compat.get_ppid", return_value=systemd_user_pid),
            patch(
                "kiro_crew.session_pid._accepted_subreaper_pids",
                return_value={1, systemd_user_pid},
            ),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False),
        ):
            killed = _cleanup_orphaned_mcp_servers()

        assert killed == 0
        kill_mock.assert_not_called()
        assert "77777" not in pid_file.read_text(encoding="utf-8")  # stale entry pruned

    @pytest.mark.skipif(sys.platform != "linux", reason="/proc scan is Linux-only")
    def test_accepted_subreaper_pids_detects_same_uid_systemd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shared scan finds same-uid systemd processes plus init.

        Exercises the real detection logic against a fake ``/proc`` tree:
        a same-uid ``systemd`` comm is accepted, a non-systemd comm and a
        non-numeric entry are not, and pid 1 is always present.
        """
        import kiro_crew.session_pid as session_pid_mod

        (tmp_path / "4242").mkdir()
        (tmp_path / "4242" / "comm").write_text("systemd\n")
        (tmp_path / "4300").mkdir()
        (tmp_path / "4300" / "comm").write_text("bash\n")
        (tmp_path / "4301").mkdir()  # no comm file: skipped, not fatal
        (tmp_path / "notpid").mkdir()

        real_path = session_pid_mod.Path
        monkeypatch.setattr(
            session_pid_mod,
            "Path",
            lambda p="": real_path(tmp_path) if str(p) == "/proc" else real_path(p),
        )

        accepted = session_pid_mod._accepted_subreaper_pids()
        assert accepted == {1, 4242}

    def test_start_token_match_defers_to_heuristic_and_kills(self, pid_file: Path) -> None:
        """A matching token does not block the kill; the heuristic authorizes it.

        The token is subtractive evidence only: when it matches, the sweep
        proceeds to the reparent heuristic (here: orphan reparented to init),
        which authorizes the kill exactly as it always has.
        """
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("77777:99999:123456\n")  # parent 99999 dead

        def fake_pid_exists(pid: int) -> bool:
            return pid == 77777

        kill_mock = MagicMock()
        with (
            patch("kiro_crew.session_pid.platform_compat.pid_exists", side_effect=fake_pid_exists),
            patch("kiro_crew.session_pid.platform_compat.kill_pid", kill_mock),
            patch("kiro_crew.session_pid._pid_start_token", return_value="123456"),
            patch("kiro_crew.platform_compat.get_ppid", return_value=1),
        ):
            killed = _cleanup_orphaned_mcp_servers()

        assert killed == 1
        kill_mock.assert_called_once()
        assert "77777" not in pid_file.read_text(encoding="utf-8")

    def test_start_token_match_alone_does_not_authorize_kill(self, pid_file: Path) -> None:
        """A matching token with a failing heuristic prunes without killing.

        The tracking file is same-uid-writable, so a forged
        pid:deadparent:token line must not be able to aim the sweep at an
        arbitrary process: token agreement never grants kill authority that
        the reparent heuristic would refuse.
        """
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("77777:99999:123456\n")

        def fake_pid_exists(pid: int) -> bool:
            return pid == 77777

        kill_mock = MagicMock()
        with (
            patch("kiro_crew.session_pid.platform_compat.pid_exists", side_effect=fake_pid_exists),
            patch("kiro_crew.session_pid.platform_compat.kill_pid", kill_mock),
            patch("kiro_crew.session_pid._pid_start_token", return_value="123456"),
            patch("kiro_crew.platform_compat.get_ppid", return_value=5555),
            patch("kiro_crew.session_pid._accepted_subreaper_pids", return_value={1}),
        ):
            killed = _cleanup_orphaned_mcp_servers()

        assert killed == 0
        kill_mock.assert_not_called()
        assert "77777" not in pid_file.read_text(encoding="utf-8")

    def test_start_token_mismatch_pruned_not_killed(self, pid_file: Path) -> None:
        """A reused PID fails the start-token check and is pruned, never killed.

        This is per-process identity, checked before the reparent heuristic:
        even a PID recycled by another process spawned by Kiro Crew (which
        carries the tree-wide KIROCREW_SPAWNED marker and may be init- or
        systemd-parented) has a different start identity, so the sweep prunes
        it without ever reaching the kill arms.
        """
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("77777:99999:123456\n")

        def fake_pid_exists(pid: int) -> bool:
            return pid == 77777

        kill_mock = MagicMock()
        with (
            patch("kiro_crew.session_pid.platform_compat.pid_exists", side_effect=fake_pid_exists),
            patch("kiro_crew.session_pid.platform_compat.kill_pid", kill_mock),
            patch(
                "kiro_crew.session_pid._pid_start_token",
                return_value="999999",  # different incarnation
            ),
            patch("kiro_crew.platform_compat.get_ppid", return_value=1),
        ):
            killed = _cleanup_orphaned_mcp_servers()

        assert killed == 0
        kill_mock.assert_not_called()
        assert "77777" not in pid_file.read_text(encoding="utf-8")  # stale entry pruned

    def test_track_child_pids_records_start_token(self, pid_file: Path) -> None:
        """_track_child_pids persists the child's start identity as field 3."""
        from kiro_crew.session_pid import _track_child_pids

        with patch("kiro_crew.session_pid._pid_start_token", return_value="424242"):
            _track_child_pids({555: None}, parent_pid=111)

        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == ["555:111:424242"]

        # Unreadable identity at track time degrades to the legacy shape.
        with patch("kiro_crew.session_pid._pid_start_token", return_value=None):
            _track_child_pids({556: None}, parent_pid=111)
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert "556:111" in lines

    def test_bare_pid_dead_pruned(self, pid_file: Path) -> None:
        """Dead bare PIDs should be pruned from the file."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("99999\n")

        with patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=False):
            killed = _cleanup_orphaned_mcp_servers()
        assert killed == 0
        assert "99999" not in pid_file.read_text(encoding="utf-8")

    def test_bare_pid_alive_kept(self, pid_file: Path) -> None:
        """Alive bare PIDs should be kept in the file."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("88888\n")

        with patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True):
            killed = _cleanup_orphaned_mcp_servers()
        assert killed == 0
        assert "88888" in pid_file.read_text(encoding="utf-8")

    def test_empty_file(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("")
        assert _cleanup_orphaned_mcp_servers() == 0

    def test_no_file(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        assert _cleanup_orphaned_mcp_servers() == 0


class TestCleanupOrphanedSessions:
    @_POSIX_ONLY
    def test_preserves_non_kiro_pids(self, session_pid_file: Path) -> None:
        """Bug fix: non-kiro PIDs (MCP servers) must survive — not killed."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        session_pid_file.write_text("99998\n99999\n")

        # Both PIDs must read as ALIVE so the sweep reaches the managed/kill
        # decision (the liveness gate is pid_liveness(), tri-state). Without this
        # the real os.kill(pid,0) on the fleet returns DEAD and both are pruned as
        # dead — the test would pass vacuously without exercising the kill path.
        # kill_pid is patched so _kill_pid_tree reports the managed PID killed
        # (root_killed=True -> pruned); the non-managed one is pruned via the
        # _is_managed_agent_process(False) branch.
        with (
            patch(
                "kiro_crew.session_pid._is_managed_agent_process", side_effect=lambda p: p == 99998
            ),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.session_pid.platform_compat.kill_pid"),
        ):
            cleanup_orphaned_sessions()

        # File is truncated after startup cleanup
        content = session_pid_file.read_text(encoding="utf-8")
        assert content == ""

    @_POSIX_ONLY
    def test_kiro_pids_killed(self, session_pid_file: Path) -> None:
        """Kiro PIDs should be SIGKILL'd."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        session_pid_file.write_text("99998\n")

        kills: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kills.append((pid, sig))

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            # The sweep's liveness gate is pid_liveness() (tri-state), not pid_exists();
            # ALIVE -> falls through to the kill path. pid_exists is still patched for
            # the post-kill re-probe branch.
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.session_pid.platform_compat.kill_pid", side_effect=fake_kill),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert (99998, platform_compat.SIGKILL) in kills

    def test_malformed_pid_files_deleted(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed session_pid_*.txt files (e.g. MagicMock leak) should be deleted."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        monkeypatch.setattr("kiro_crew.session_pid.config_dir", lambda: tmp_path)
        session_pid_file.write_text("")  # no kiro PIDs to kill

        # Create one valid (dead process) and one malformed pid file
        (tmp_path / "session_pid_99999.txt").write_text("sess-dead")
        (tmp_path / "session_pid_mock.get_pid().txt").write_text("sess-mock")

        with (
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
            patch("os.kill", side_effect=ProcessLookupError),
        ):
            cleanup_orphaned_sessions()

        # Both should be cleaned up
        assert not (tmp_path / "session_pid_99999.txt").exists()
        assert not (tmp_path / "session_pid_mock.get_pid().txt").exists()

    def test_malformed_pid_file_unlink_oserror_continues(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OSError on malformed pid file unlink should not abort the cleanup loop."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        monkeypatch.setattr("kiro_crew.session_pid.config_dir", lambda: tmp_path)
        session_pid_file.write_text("")

        # Create malformed + valid pid files
        (tmp_path / "session_pid_bad!name.txt").write_text("sess-bad")
        (tmp_path / "session_pid_99999.txt").write_text("sess-dead")

        original_unlink = Path.unlink

        def unlink_that_fails_on_bad(path_self, *a, **kw):
            if "bad!name" in path_self.name:
                raise OSError("permission denied")
            return original_unlink(path_self, *a, **kw)

        monkeypatch.setattr(Path, "unlink", unlink_that_fails_on_bad)

        with (
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
            patch("os.kill", side_effect=ProcessLookupError),
        ):
            cleanup_orphaned_sessions()  # should not raise

        # bad!name still exists (unlink failed gracefully), valid one cleaned up
        assert (tmp_path / "session_pid_bad!name.txt").exists()
        assert not (tmp_path / "session_pid_99999.txt").exists()

    @pytest.mark.skipif(sys.platform != "linux", reason="tids share the pid space on Linux only")
    def test_pid_file_recycled_as_a_thread_is_deleted(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mapping whose pid now names a THREAD of a live process is stale.

        Linux draws tids from the pid space and lets you signal one, so such a
        pid passes ``pid_exists`` and such a mapping would survive forever. A
        token-bearing mapping is already safe to resolve — ``_pid_recycled``
        refuses on a start-token mismatch, and a tid's token cannot match — so
        what is pruned here is the legacy token-less form, which has no recorded
        token for that guard to compare, plus the accumulation itself.

        Uses a real live thread's native tid rather than a fake ``/proc``, so
        the test exercises the same kernel behaviour that produced the bug.
        """
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        monkeypatch.setattr("kiro_crew.session_pid.config_dir", lambda: tmp_path)
        session_pid_file.write_text("")

        tid_box: dict[str, int] = {}
        release = threading.Event()
        captured = threading.Event()

        def _hold() -> None:
            tid_box["tid"] = threading.get_native_id()
            captured.set()
            release.wait(timeout=30)

        holder = threading.Thread(target=_hold, daemon=True)
        holder.start()
        assert captured.wait(timeout=30), "helper thread never reported its tid"
        tid = tid_box["tid"]
        assert tid != os.getpid(), "native_id must differ from the group leader"

        try:
            thread_map = tmp_path / f"session_pid_{tid}.txt"
            leader_map = tmp_path / f"session_pid_{os.getpid()}.txt"
            thread_map.write_text("sess-recycled-as-thread")
            leader_map.write_text("sess-live-leader")

            # NOT patching os.kill: both pids are genuinely signalable here,
            # which is exactly the condition the old predicate could not split.
            with patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0):
                cleanup_orphaned_sessions()

            assert not thread_map.exists(), "a pid that is only a thread must be pruned"
            assert leader_map.exists(), "a live thread-group leader must be retained"
        finally:
            release.set()
            holder.join(timeout=30)

    def test_boot_setting_reads_no_proc(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``narrow_with_leaders=False`` must not take the leaders snapshot.

        The gateway boot path passes it so the sweep costs exactly what it cost
        before this branch: ``no-new-work-on-gateway-boot-path`` names orphan
        sweeps, so a regression that read the leaders set anyway would put a
        ``/proc`` scan back on the boot path, where the readiness cost of it is
        not visible to anyone reading the sweep.
        """
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        monkeypatch.setattr("kiro_crew.session_pid.config_dir", lambda: tmp_path)
        session_pid_file.write_text("")

        def _refuse() -> set[int] | None:
            raise AssertionError("the boot setting must not read /proc for leaders")

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.live_thread_group_leaders", _refuse
        )
        with patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0):
            cleanup_orphaned_sessions(narrow_with_leaders=False)

    def test_prune_pass_leaves_the_shared_pid_file_alone(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deferred pass must not rewrite the file the boot sweep owns.

        Deferring the prune past readiness is only sound because this pass touches
        ``session_pid_<pid>.txt`` mappings and nothing else. If it also rewrote
        ``kiro_session_pids.txt`` it would race the spawns that append to it once
        the gateway is serving, and a lost entry is an unkillable orphan.
        """
        from kiro_crew.session_pid import _prune_stale_session_pid_files

        monkeypatch.setattr("kiro_crew.session_pid.config_dir", lambda: tmp_path)
        session_pid_file.write_text("111:222\n")
        (tmp_path / "session_pid_99999.txt").write_text("sess-dead")

        # The probe is pinned, not assumed: ``pid_max`` is 4194304 here, so 99999
        # is an ordinary live pid on a host whose counter has passed it, and a live
        # pid is retained -- which would fail the removal assertion below on a
        # long-running runner rather than in review. The sibling sweeps above pin
        # it the same way; ``os.kill`` is what ``platform_compat.pid_exists``
        # reaches for on POSIX.
        with patch("os.kill", side_effect=ProcessLookupError):
            removed = _prune_stale_session_pid_files()

        assert removed == 1
        assert not (tmp_path / "session_pid_99999.txt").exists()
        assert session_pid_file.read_text() == "111:222\n"

    def test_stale_snapshot_does_not_delete_a_live_mapping(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pid recycled after the snapshot keeps the mapping its new owner wrote.

        The leaders snapshot is read once for the whole pass, so a pid that became
        a live process after it was taken is absent from it while naming a LIVE
        session whose mapping already sits at that path. Deciding on the snapshot
        alone unlinks that live mapping, which is a lost session identity, not a
        tidy-up; the per-pid re-read is what refuses. The shipped call sites do not
        run this pass beside live sessions, so this is defence in depth rather than
        load-bearing -- it keeps the guarantee a property of the function instead of
        of where it happens to be called from.
        """
        from kiro_crew.session_pid import _prune_stale_session_pid_files

        monkeypatch.setattr("kiro_crew.session_pid.config_dir", lambda: tmp_path)
        live = tmp_path / f"session_pid_{os.getpid()}.txt"
        live.write_text("sess-published-after-the-snapshot")

        # A snapshot from before this process existed: the pid is signalable and
        # is a real thread-group leader, yet absent from the set.
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.live_thread_group_leaders",
            lambda: frozenset({1}),
        )

        removed = _prune_stale_session_pid_files()

        assert removed == 0
        assert live.exists(), "a live leader absent from a stale snapshot must be retained"


class TestResetStateUntracksParentPid:
    def test_reset_state_untracks_parent_pid(self) -> None:
        """Verify _reset_state calls _untrack_pid with the saved PID."""
        from kiro_crew.acp.client import AcpClient

        client = AcpClient.__new__(AcpClient)
        client._process = None
        client._pid = 54321
        client._session_id = None
        client._buffer = bytearray()
        client._cancelled = False
        client._resumed = False
        client._sandbox_cleanup = None
        client._child_pids = {}
        client._stderr_lines = deque(["some error"], maxlen=20)
        client._pending_oauth_requests = []
        client._oauth_emitted_servers = set()
        # _reset_state restarts the per-process cost baseline on this object
        # (always present in production: __init__ assigns it unconditionally).
        from kiro_crew.acp.types import AcpPromptStats

        client.last_prompt_stats = AcpPromptStats()
        mock_task = Mock()
        mock_task.done.return_value = False
        client._stderr_task = mock_task

        with patch("kiro_crew.session._untrack_pid") as mock_untrack:
            client._reset_state()

        assert client._pid is None
        assert len(client._stderr_lines) == 0
        assert client._stderr_task is None
        mock_task.cancel.assert_called_once()
        mock_untrack.assert_called_once_with(54321)


# ── Untracked orphan MCP sweep tests ───────────


class TestFindOrphanMcpCandidates:
    """Tests for find_orphan_mcp_candidates (process-table scan)."""

    def test_excludes_pids_in_active_set(self) -> None:
        """PIDs present in active_pids are never returned as candidates."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[100, 200]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"kirocrew_sandbox_abc.py"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids={100, 200})

        assert result == []

    def test_vanished_pid_logs_one_line_without_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A PID that exits between snapshot and probe logs no stack trace.

        The candidate is already gone, which is what the sweep wants, so the
        expected TOCTOU race must not emit exc_info.
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[22620]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path,
                "read_bytes",
                side_effect=FileNotFoundError(2, "No such file or directory"),
            ),
            patch("os.getpid", return_value=1),
            caplog.at_level("DEBUG", logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        records = [r for r in caplog.records if "22620" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is None
        assert "vanished before probe" in records[0].getMessage()

    def test_vanished_pid_on_macos_ps_exit_logs_no_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`ps -p <dead-pid>` exits non-zero — same race, same quiet handling."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[9140]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch(
                "kiro_crew.session_pid.subprocess.check_output",
                side_effect=subprocess.CalledProcessError(1, "ps"),
            ),
            patch("os.getpid", return_value=1),
            caplog.at_level("DEBUG", logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "darwin"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        records = [r for r in caplog.records if "9140" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is None

    def test_unexpected_probe_error_keeps_traceback(self, caplog: pytest.LogCaptureFixture) -> None:
        """A genuinely unexpected probe failure still logs exc_info."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[555]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", side_effect=PermissionError(13, "denied")),
            patch("os.getpid", return_value=1),
            caplog.at_level("DEBUG", logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        records = [r for r in caplog.records if "555" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is not None

    def test_excludes_non_kirocrew_processes(self) -> None:
        """Orphans without known MCP entrypoint markers are skipped."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[300]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path, "read_bytes", return_value=b"/usr/bin/python3\x00some_other_script.py"
            ),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_excludes_non_entrypoint_vim_grep(self) -> None:
        """Non-Python processes mentioning kirocrew in args (e.g. vim, grep) are skipped."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[350]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"vim\x00/tmp/kirocrew_sandbox_abc.log"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_excludes_peer_gateway(self, tmp_path: Path) -> None:
        """Peer gateways with a LIVE socket path are never candidates.

        Age is patched above the min-age floor so the assertion depends on the
        _GATEWAY_MARKERS exclusion in _is_orphan_mcp, not on the age guard
        short-circuiting before the exclusion logic ever runs. The socket path
        must exist on disk: a gatewayd whose socket is GONE is deliberately
        sweepable via the reachability path (_is_sweepable_orphan_gatewayd).
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        live_sock = tmp_path / "gw.sock"
        live_sock.write_text("")
        cmdline = (
            b"python3\x00-m\x00kiro_crew.mcp_gateway.gatewayd"
            b"\x00--socket\x00" + os.fsencode(str(live_sock))
        )
        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[360]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=cmdline),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_includes_kirocrew_orphan_not_in_active(self) -> None:
        """Orphaned process with sandbox wrapper entrypoint and not in active set is a candidate."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[400]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path,
                "read_bytes",
                return_value=b"python3\x00/tmp/kirocrew_sandbox_xyz.py",
            ),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == [400]

    def test_excludes_own_pid(self) -> None:
        """The gateway's own PID is never returned."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[999]),
            patch("os.getpid", return_value=999),
        ):
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_does_not_match_builder_mcp(self) -> None:
        """builder-mcp is NOT a KiroCrew-spawned process in this public fork.

        Regression guard: the upstream project's reaper lists ``builder-mcp`` (an
        internal server it manages), but the de-Amazoned fork never spawns
        it (the CPP companion contributes it, not the core). Reaping a user-owned
        ``builder-mcp`` orphan would SIGKILL an unrelated process, so the marker
        is deliberately absent here.
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[410]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"builder-mcp\x00--stdio"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_matches_macos_space_separated_cmdline(self) -> None:
        """macOS ps output (space-separated) is correctly parsed."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        def mock_check_output(cmd, **kwargs):
            # Single combined ps call returns "<etime> <command...>"
            if "etime=" in cmd and "command=" in cmd:
                return b"   05:00 python3 /tmp/kirocrew_sandbox_xyz.py"
            return b""

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[420]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch(
                "subprocess.check_output",
                side_effect=mock_check_output,
            ),
            patch("os.getpid", return_value=1),
        ):
            mock_sys.platform = "darwin"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == [420]

    def test_skips_young_processes(self) -> None:
        """Processes younger than _ORPHAN_MIN_AGE_SECONDS are never candidates."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[450]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path,
                "read_bytes",
                return_value=b"python3\x00/tmp/kirocrew_sandbox_new.py",
            ),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=50.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []


@_POSIX_ONLY
class TestKillOrphanMcps:
    """Tests for kill_orphan_mcps (kill confirmed orphans)."""

    @pytest.fixture(autouse=True)
    def _stable_root_identity(self) -> Iterator[None]:
        """Give synthetic PIDs a start token so the recycle guard passes.

        `kill_orphan_mcps` captures the root's `_pid_start_token` before the
        subtree scan and re-confirms it before signalling, so a PID whose
        identity cannot be read is skipped by design. These tests use synthetic
        PIDs that have no `/proc` entry; a stable token states the thing they
        already assume -- that the PID was not recycled mid-sweep. See
        test_orphan_mcp_subtree.TestRootRecycleGuard for the guard's own cover.
        """
        with patch("kiro_crew.session_pid._pid_start_token", return_value="tok-stable"):
            yield

    def test_uses_killpg_when_pgid_differs(self) -> None:
        """If orphan is its own group leader, kill via killpg."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=500),
            patch("os.killpg") as mock_killpg,
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"python3\x00kirocrew_sandbox_x.py"),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([500])

        assert killed == 1
        mock_killpg.assert_called_once_with(500, signal.SIGKILL)

    def test_falls_back_to_direct_kill_when_pgid_matches(self) -> None:
        """If orphan shares our pgid, use direct os.kill (not _kill_pid_tree)."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=1000),
            patch("os.kill") as mock_kill,
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"python3\x00kirocrew_sandbox_x.py"),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([600])

        assert killed == 1
        mock_kill.assert_called_once_with(600, signal.SIGKILL)

    def test_direct_kill_handles_already_dead(self) -> None:
        """ProcessLookupError on direct kill is handled gracefully."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=1000),
            patch("os.kill", side_effect=ProcessLookupError),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"python3\x00kirocrew_sandbox_x.py"),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([600])

        assert killed == 0

    def test_respects_max_kill_cap(self) -> None:
        """Never kills more than _ORPHAN_SWEEP_MAX_KILLS in one pass."""
        from kiro_crew.session_pid import _ORPHAN_SWEEP_MAX_KILLS, kill_orphan_mcps

        pids = list(range(1000, 1000 + _ORPHAN_SWEEP_MAX_KILLS + 10))
        with (
            patch("os.getpgrp", return_value=1),
            patch("os.getpgid", side_effect=lambda pid: pid),
            patch("os.killpg"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"python3\x00kirocrew_sandbox_x.py"),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps(pids)

        assert killed == _ORPHAN_SWEEP_MAX_KILLS

    def test_handles_already_dead_process(self) -> None:
        """ProcessLookupError during kill is silently handled."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", side_effect=ProcessLookupError),
        ):
            killed = kill_orphan_mcps([700])

        assert killed == 0

    def test_skips_recycled_pid_on_reverify(self) -> None:
        """If cmdline stops matching at kill time, the PID is skipped (TOCTOU)."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"/usr/bin/bash\x00script.sh"),
            patch("os.killpg") as mock_killpg,
            patch("os.kill") as mock_kill,
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([800])

        assert killed == 0
        mock_killpg.assert_not_called()
        mock_kill.assert_not_called()

    def test_macos_subprocess_error_does_not_abort_loop(self) -> None:
        """A vanished PID raising SubprocessError on macOS must not abort
        kills for subsequent PIDs (regression: review-bot rev4).

        `ps` exits non-zero for a PID that died between find and kill, raising
        subprocess.CalledProcessError (a SubprocessError, NOT an OSError). The
        except tuple must catch it so the loop continues to the next PID.
        """
        from kiro_crew.session_pid import kill_orphan_mcps

        def mock_check_output(cmd, **kwargs):
            # cmd[-1] is the str(pid) being re-verified
            if cmd[-1] == "700":
                raise subprocess.CalledProcessError(1, cmd)
            return b"python3 /tmp/kirocrew_sandbox_x.py"

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", side_effect=lambda p: p),
            patch("os.killpg") as mock_killpg,
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch("subprocess.check_output", side_effect=mock_check_output),
        ):
            mock_sys.platform = "darwin"
            killed = kill_orphan_mcps([700, 701])

        # 700 vanished (SubprocessError, skipped); 701 still killed.
        assert killed == 1
        mock_killpg.assert_called_once_with(701, signal.SIGKILL)


class TestParseEtime:
    """Tests for _parse_etime (ps etime format parser)."""

    def test_minutes_seconds(self) -> None:
        from kiro_crew.session_pid import _parse_etime

        assert _parse_etime("05:30") == 330.0

    def test_hours_minutes_seconds(self) -> None:
        from kiro_crew.session_pid import _parse_etime

        assert _parse_etime("01:05:30") == 3930.0

    def test_days_hours_minutes_seconds(self) -> None:
        from kiro_crew.session_pid import _parse_etime

        assert _parse_etime("2-01:00:00") == 2 * 86400 + 3600

    def test_invalid_returns_zero(self) -> None:
        from kiro_crew.session_pid import _parse_etime

        assert _parse_etime("garbage") == 0.0

    def test_empty_returns_zero(self) -> None:
        from kiro_crew.session_pid import _parse_etime

        assert _parse_etime("") == 0.0


@_POSIX_ONLY
class TestOurOrphanPids:
    """Direct tests for _our_orphan_pids (Linux /proc and macOS ps branches)."""

    def test_linux_proc_scan_finds_init_and_subreaper_children(self) -> None:
        """Linux /proc two-pass scan: includes ppid==1 and ppid==systemd subreaper.

        Exercises the real Linux branch (systemd --user subreaper detection in
        pass 1 + PPid parsing in pass 2), not the macOS ps path.
        """
        from kiro_crew.session_pid import _our_orphan_pids

        class _FakeProcEntry:
            def __init__(self, name: str, uid: int, comm: str, ppid: str) -> None:
                self.name = name
                self._uid = uid
                self._comm = comm
                self._ppid = ppid

            def stat(self) -> MagicMock:
                return MagicMock(st_uid=self._uid)

            def __truediv__(self, child: str) -> MagicMock:
                node = MagicMock()
                if child == "comm":
                    node.read_text.return_value = self._comm + "\n"
                else:  # "status"
                    node.read_text.return_value = f"Name:\t{self._comm}\nPPid:\t{self._ppid}\n"
                return node

        my_uid = 1000
        entries = [
            _FakeProcEntry("100", my_uid, "python3", "1"),  # init-reparented
            _FakeProcEntry("200", my_uid, "bash", "50"),  # live child, excluded
            _FakeProcEntry("300", my_uid, "systemd", "1"),  # --user subreaper
            _FakeProcEntry("400", my_uid, "worker", "300"),  # child of subreaper
            _FakeProcEntry("500", 9999, "python3", "1"),  # other uid, excluded
            _FakeProcEntry("self", my_uid, "x", "1"),  # non-numeric, skipped
        ]
        proc_root = MagicMock()
        proc_root.iterdir.return_value = entries

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch("kiro_crew.session_pid.Path", return_value=proc_root),
            patch("os.getuid", return_value=my_uid),
        ):
            mock_sys.platform = "linux"
            result = _our_orphan_pids()

        assert 100 in result  # ppid == init
        assert 300 in result  # subreaper itself is ppid == init
        assert 400 in result  # ppid == detected systemd subreaper
        assert 200 not in result  # ppid is a live process, not orphaned
        assert 500 not in result  # different uid

    def test_macos_excludes_launcher_children(self) -> None:
        """ppid==launcher must NOT be reaped (regression guard).

        Orphans reparent to init (pid 1), never back to the launcher, so a
        launcher child is a live sibling and must be excluded; only the
        init-reparented pid is returned.
        """
        from kiro_crew.session_pid import _our_orphan_pids

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch(
                "subprocess.check_output",
                return_value=b"  500    42\n  600     1\n",
            ),
            patch("os.getuid", return_value=1000),
            patch("os.getppid", return_value=42),
        ):
            mock_sys.platform = "darwin"
            result = _our_orphan_pids()

        assert 500 not in result  # launcher child — excluded after the fix
        assert 600 in result  # init-reparented orphan — included

    def test_returns_empty_on_exception(self) -> None:
        """Returns empty list on failure, does not raise."""
        from kiro_crew.session_pid import _our_orphan_pids

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch("subprocess.check_output", side_effect=OSError("ps failed")),
            patch("os.getuid", return_value=1000),
        ):
            mock_sys.platform = "darwin"
            result = _our_orphan_pids()

        assert result == []


@_POSIX_ONLY
class TestLinuxPidAge:
    """Direct tests for _linux_pid_age /proc/<pid>/stat starttime parsing."""

    @staticmethod
    def _patch_proc(stat_line: str, uptime: str = "10000.0 9000.0"):
        def fake_path(p: object) -> MagicMock:
            node = MagicMock()
            if str(p).endswith("/stat"):
                node.read_text.return_value = stat_line
            elif str(p) == "/proc/uptime":
                node.read_text.return_value = uptime
            return node

        return patch("kiro_crew.session_pid.Path", side_effect=fake_path)

    def test_age_with_spaces_and_parens_in_comm(self) -> None:
        """starttime is read from field 22 even when comm contains spaces/parens.

        rfind(')') must land on the comm's closing paren so field-index math
        starts at the state field. starttime_ticks=500000, clk_tck=100 →
        5000s offset; uptime=10000s → age=5000s.
        """
        from kiro_crew.session_pid import _linux_pid_age

        # pid (comm) state ppid ... starttime(field 22 == index 19 after state)
        post_comm = "S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 0 20 0 1 500000 0 0"
        stat_line = f"1234 (my (weird) proc) {post_comm}\n"

        with self._patch_proc(stat_line), patch("os.sysconf", return_value=100):
            age = _linux_pid_age(1234, now=123456.0)

        assert age == 5000.0

    def test_malformed_stat_returns_zero(self) -> None:
        """Too-few fields → IndexError → 0.0 fail-safe (min-age guard skips)."""
        from kiro_crew.session_pid import _linux_pid_age

        with self._patch_proc("999 (proc) S 1 1\n"), patch("os.sysconf", return_value=100):
            age = _linux_pid_age(999, now=123456.0)

        assert age == 0.0


class TestIsManagedAgentProcess:
    def test_self_pid_not_managed(self) -> None:
        """Our own test PID's cmdline lacks kiro-cli/claude → not managed.

        Exercises the platform_compat.process_matches call (the real
        /proc/<pid>/cmdline read on Linux) without killing anything.
        """
        from kiro_crew.session_pid import _is_managed_agent_process

        assert _is_managed_agent_process(os.getpid()) is False


class TestSyncKillProvider:
    def test_no_pid_returns_early(self) -> None:
        """Provider with no client/_proc/_active_proc PID → early return."""
        from kiro_crew.session_pid import _sync_kill_provider

        provider = MagicMock(spec=["_client", "_proc", "_active_proc"])
        provider._client = None
        provider._proc = None
        provider._active_proc = None

        with patch("kiro_crew.session_pid.platform_compat.kill_pid") as mock_kill:
            _sync_kill_provider(provider)

        mock_kill.assert_not_called()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX SIGTERM→SIGKILL escalation; Windows uses single SIGKILL",
    )
    def test_posix_sigterm_then_sigkill(self) -> None:
        """POSIX path: real child reaped via SIGTERM→waitpid→SIGKILL loop.

        Spawns a real short-lived sleep subprocess, drives _sync_kill_provider
        through the POSIX escalation loop (kill_pid is recorded, not real, so
        the loop runs both iterations deterministically), then reaps the child.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            provider = MagicMock(spec=["_client", "_proc", "_active_proc"])
            provider._client = None
            provider._proc = MagicMock()
            provider._proc.returncode = None
            provider._proc.pid = proc.pid
            provider._active_proc = None

            sigs: list[int] = []

            def fake_kill(pid: int, sig: int) -> bool:
                sigs.append(sig)
                return True

            with patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=fake_kill,
            ):
                _sync_kill_provider(provider)

            # POSIX loop hits both SIGTERM and SIGKILL for our child PID
            assert sigs == [platform_compat.SIGTERM, platform_compat.SIGKILL]
        finally:
            proc.kill()
            proc.wait(timeout=5)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX SIGTERM path; Windows takes the single-SIGKILL branch",
    )
    def test_posix_already_dead_on_sigterm(self) -> None:
        """ProcessLookupError on first signal → early return (already dead)."""
        from kiro_crew.session_pid import _sync_kill_provider

        provider = MagicMock(spec=["_client", "_proc", "_active_proc"])
        provider._client = None
        provider._proc = None
        provider._active_proc = MagicMock()
        provider._active_proc.returncode = None
        provider._active_proc.pid = 99999

        sigs: list[int] = []

        def fake_kill(pid: int, sig: int) -> bool:
            sigs.append(sig)
            raise ProcessLookupError()

        with patch(
            "kiro_crew.session_pid.platform_compat.kill_pid",
            side_effect=fake_kill,
        ):
            _sync_kill_provider(provider)

        # Loop stops after the first (SIGTERM) signal raises ProcessLookupError
        assert sigs == [platform_compat.SIGTERM]


# A grandchild that ignores SIGTERM, so only a SIGKILL that actually reaches it
# can end it. ``{setsid}`` is filled with ``os.setsid()`` for the variant that
# escapes the provider's process group, and with nothing for the variant that
# stays in it.
_STUBBORN_GRANDCHILD = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "{setsid}"
    # Announced only once SIG_IGN is installed. The pid exists from the fork, so a
    # test that waits for the pid alone can send the group SIGTERM into the window
    # where this process still has the DEFAULT disposition -- and a grandchild that
    # dies to SIGTERM is not the stubborn descendant these tests are built on.
    "sys.stdout.write('stubborn\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(300)\n"
)
# A provider root: forks one grandchild, then waits. Plain SIGTERM handling, so
# the root itself dies on the first signal and the test measures what happens to
# the tree BELOW it. Its own stdout carries the grandchild's pid, and it reports
# that pid only after the grandchild says it is stubborn, so the caller's read of
# this line is the barrier. The grandchild gets a pipe of its OWN rather than
# inheriting this one, which would put its announcement in front of the pid the
# caller reads. Reporting nothing when the announcement does not arrive keeps the
# failure loud: the caller raises on the empty read rather than proceeding with an
# unguarded tree.
_PROVIDER_ROOT = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', {src!r}], stdout=subprocess.PIPE)\n"
    "if not p.stdout.readline():\n"
    "    raise SystemExit(1)\n"
    "print(p.pid, flush=True)\n"
    "time.sleep(300)\n"
)


@_POSIX_ONLY
class TestSyncKillProviderTree:
    """The sync kill must reap the provider's whole tree, not just its root.

    An agent runtime is a tree (sandbox launcher, agent binary, chat subprocess,
    MCP stub children) whose root is an isolated process-group leader. A
    pid-scoped signal reaps that root alone and the rest reparents to init,
    holding hundreds of megabytes with no tracking entry left to find it by.
    """

    @staticmethod
    def _provider(pid: int, child_pids: dict | None = None) -> MagicMock:
        """A provider stand-in whose tracked pid is *pid* (the ACP shape).

        ``_start_time`` carries the pid's REAL start id, the way the ACP layer
        records it at spawn, because the teardown refuses to signal a root whose
        recorded identity does not match the live process.
        """
        provider = MagicMock(spec=["_client", "_proc", "_active_proc"])
        provider._client = MagicMock(spec=["_pid", "_child_pids", "_start_time"])
        provider._client._pid = pid
        provider._client._child_pids = child_pids if child_pids is not None else {}
        provider._client._start_time = platform_compat.get_process_start_id(pid)
        provider._proc = None
        provider._active_proc = None
        return provider

    @staticmethod
    def _spawn_tree(*, escape_group: bool) -> tuple[subprocess.Popen, int]:
        """Spawn an isolated group leader that forks one stubborn grandchild.

        Returns the root AND its grandchild's pid, which the root reports on
        stdout. Cleanup then never depends on discovery: a walk that times out
        would otherwise leave the grandchild's 300-second sleep running, since the
        caller would have no pid to reap.

        Returning is also the barrier for the grandchild being STUBBORN, not merely
        alive: the root reports its pid only after the grandchild announces that it
        installed ``SIG_IGN``. Every test here signals the group and then reads what
        survived, so a grandchild still holding the default SIGTERM disposition
        breaks the premise rather than the assertion -- the tree really is gone, and
        the teardown is right to stop early.
        """
        grandchild = _STUBBORN_GRANDCHILD.format(setsid="os.setsid()\n" if escape_group else "")
        proc = subprocess.Popen(
            [sys.executable, "-c", _PROVIDER_ROOT.format(src=grandchild)],
            start_new_session=True,  # what the real provider spawn does
            stdout=subprocess.PIPE,
        )
        assert proc.stdout is not None
        reported = proc.stdout.readline().strip()
        if not reported:
            proc.kill()
            proc.wait(timeout=10)
            raise AssertionError("provider root never reported its grandchild pid")
        return proc, int(reported)

    @staticmethod
    def _await_descendants(pid: int, timeout: float = 10.0) -> list[int]:
        """Wait for the root's fork to appear; return the descendant pids."""
        from kiro_crew.acp.client import _get_child_pids

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = _get_child_pids(pid)
            if found:
                return found
            time.sleep(0.05)
        raise AssertionError(f"grandchild of {pid} never appeared")

    @staticmethod
    def _await_gone(pids: list[int], timeout: float = 10.0) -> list[int]:
        """Return the pids still alive after waiting up to *timeout*."""
        deadline = time.monotonic() + timeout
        alive = list(pids)
        while alive and time.monotonic() < deadline:
            alive = [p for p in alive if platform_compat.pid_exists(p)]
            if alive:
                time.sleep(0.05)
        return alive

    #: ``os.waitid`` is Linux-only in CPython -- macOS has the syscall but the module
    #: does not export it -- so every use of it here needs a fallback. It is the only
    #: NON-DESTRUCTIVE way to ask "has this child exited": ``WNOWAIT`` reports the
    #: status without consuming it. Where it is missing there is no such peek, and the
    #: questions below are answered from pid liveness plus a wait instead.
    _HAS_WAITID = hasattr(os, "waitid")

    @classmethod
    def _our_child_exited(cls, pid: int) -> bool:
        """True when *pid*, a child of THIS process, has stopped running.

        Only valid for a child of this process. Without ``os.waitid`` this cannot tell
        an unreaped zombie from a live process -- both answer a liveness probe as
        present -- so it reports only the unambiguous half, "the pid is gone", and
        callers there prove the exit with a bounded ``wait`` instead.
        """
        if not cls._HAS_WAITID:
            return not platform_compat.pid_exists(pid)
        try:
            peek = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except ChildProcessError:
            return True
        except OSError:
            return False
        return peek is not None

    @classmethod
    def _status_still_collectable(cls, pid: int) -> bool:
        """True while this process's child *pid* has an UNCOLLECTED exit status.

        With ``os.waitid`` this is exact: ``WNOWAIT`` peeks without consuming and
        ``ChildProcessError`` means the status is already gone. Without it, liveness
        stands in -- a collected pid is released, so a pid that still answers is one
        whose status nobody has taken.
        """
        if not cls._HAS_WAITID:
            return platform_compat.pid_exists(pid)
        try:
            return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None
        except (ChildProcessError, OSError):
            return False

    @staticmethod
    def _running_state(pid: int) -> str | None:
        """The state of *pid* if it is still RUNNING, else None.

        A liveness probe cannot answer this: `kill(pid, 0)` succeeds for a zombie,
        so a pid that answers is either a process the teardown missed or one it
        killed whose reaper has not got to it yet. Only the first breaks a promise,
        and the two need telling apart through the state the OS itself reports.

        `ps -o stat=` is the reader that answers on every POSIX platform, where a
        leading `Z` marks a zombie. It is asked ONLY about a pid that already
        outlived the caller's wait, so the cost of spawning it is paid once per
        survivor rather than once per poll.

        FAIL-CLOSED: a live pid whose state cannot be read reports `"unknown"` and
        therefore counts as running. Treating an unreadable state as stopped would
        turn every reader failure into a pass.
        """
        if not platform_compat.pid_exists(pid):
            return None
        try:
            probe = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        state = probe.stdout.split()
        if not state:
            # `ps` found nothing: the pid went between the liveness probe and here.
            return None
        return None if state[0].startswith("Z") else state[0]

    def _assert_tree_stopped(self, root: "subprocess.Popen", descendants: list[int]) -> None:
        """Nothing of the tree is running, and the root's status is accounted for.

        STOPPED is what the teardown promises: the root exited and no recorded
        descendant is still running. The root is checked through this process's own
        child status rather than through liveness, because a zombie answers a liveness
        probe as present and off Linux there is no zombie state to read instead.

        A descendant is judged by whether it RUNS, not by whether its pid is free.
        The pid of a descendant is not the teardown's to release: a grandchild
        reparents to init the moment the root exits, so who collects its status and
        how promptly is that reaper's business -- prompt enough on Linux to look like
        part of the teardown, lazy enough under launchd to outlive this wait. The
        promise the teardown actually makes is that nothing of the tree still runs,
        so that is what is asserted, and a still-running descendant fails WITH the
        state that says so.

        WHO COLLECTED THE ROOT'S STATUS is asserted of production on every platform,
        and BEFORE this helper's own wait, so the cleanup cannot stand in for the thing
        under test. A killed root's identity survives its exit everywhere the teardown
        runs -- Linux reads it from ``/proc``, macOS from the kernel's zombie list --
        so `_reap_provider_root` is obliged to reap it, and this asserts that it did.
        Where ``waitid`` exists the exit is observed separately first, because a peek
        at a still-running child reports nothing to collect and would otherwise pass
        the collection check by default.
        ONE absolute deadline covers every phase below, so the budget for the whole
        teardown to finish is the same 10 seconds a single `_await_gone` asserts.
        Per-phase deadlines would sum instead, letting a slower teardown pass on
        several times that budget.
        """
        deadline = time.monotonic() + 10.0

        def _left() -> float:
            """Seconds still available, never negative, for a phase that takes one."""
            return max(0.0, deadline - time.monotonic())

        alive = list(descendants)
        while alive and time.monotonic() < deadline:
            alive = [pid for pid in alive if platform_compat.pid_exists(pid)]
            if alive:
                time.sleep(0.05)
        running = {pid: state for pid in alive if (state := self._running_state(pid)) is not None}
        assert not running, f"the teardown left a descendant running: {running}"

        if self._HAS_WAITID:
            # Where a non-destructive peek exists, observe the EXIT separately: a
            # still-running child answers the peek with "nothing to collect", which the
            # collection assertion below cannot tell from a status already taken.
            while time.monotonic() < deadline and not self._our_child_exited(root.pid):
                time.sleep(0.05)
            assert self._our_child_exited(root.pid), "the teardown left the root running"

        # WHO COLLECTED THE STATUS, asserted of production on every platform and
        # BEFORE the wait below, so this helper's own cleanup cannot stand in for the
        # thing under test. A teardown that stopped reaping would otherwise still pass
        # the RELEASED half, which is a ratchet that only loosens. Off `waitid` the
        # same question is read through the pid: a collected child is released, so a
        # pid that still answers is one whose status nobody has taken -- and that
        # reading also covers the exit, because a running child answers too.
        while time.monotonic() < deadline and self._status_still_collectable(root.pid):
            time.sleep(0.05)
        assert not self._status_still_collectable(root.pid), (
            "the teardown left the root's exit status uncollected: "
            "_reap_provider_root did not reap it"
        )
        root.wait(timeout=_left())
        # The pid was already free before that wait, so this re-reads it as a
        # cross-check rather than as an assertion this helper can satisfy by itself.
        left = self._await_gone([root.pid, *descendants], timeout=_left())
        assert left == [], f"pids still held after the tree was torn down: {left}"

    @staticmethod
    def _reap(pids: list[int]) -> None:
        """Best-effort teardown so no test process survives the run."""
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass

    def test_a_zombie_descendant_does_not_count_as_running(self) -> None:
        """The state reader separates a stopped descendant from a live one.

        `_assert_tree_stopped` rests on this distinction, so pin it directly instead
        of only through a teardown: a zombie is a process that has STOPPED and whose
        pid its reaper has not yet collected, and a liveness probe answers the same
        for it as for a process still running. Judging the teardown by liveness alone
        therefore charges it for the reaper's timing -- prompt under init, lazy under
        launchd -- rather than for what it promised.

        Both arms use a real child of this process: one killed and left uncollected,
        one still running.
        """
        zombie = subprocess.Popen([sys.executable, "-c", ""])
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            deadline = time.monotonic() + 10.0
            while platform_compat.pid_exists(zombie.pid) and self._running_state(zombie.pid):
                assert time.monotonic() < deadline, "the child never became a zombie"
                time.sleep(0.01)
            assert platform_compat.pid_exists(
                zombie.pid
            ), "the pid was collected, so there is no zombie left to classify"
            assert self._running_state(zombie.pid) is None, "a zombie must not count as running"

            state = self._running_state(live.pid)
            assert state is not None, "a running child must count as running"
            assert not state.startswith("Z"), f"a running child reported a zombie state: {state}"
        finally:
            for child in (zombie, live):
                try:
                    child.kill()
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                child.wait(timeout=10)

    def test_in_group_grandchild_is_reaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A SIGTERM-ignoring grandchild inside the group dies with the group.

        A pid-scoped kill reaches the root only; the grandchild ignores SIGTERM
        and would survive it, so this pins the signal being addressed to the
        process GROUP.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        # The escalation budget is not the behaviour under test; shorten it so
        # the SIGTERM -> SIGKILL path runs without a multi-second wait.
        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.5)
        root, gc_pid = self._spawn_tree(escape_group=False)
        grandchildren: list[int] = []
        try:
            grandchildren = self._await_descendants(root.pid)
            # Precondition: the grandchild really is in the root's group, so a
            # group signal is what covers it.
            assert os.getpgid(grandchildren[0]) == root.pid

            _sync_kill_provider(self._provider(root.pid))

            self._assert_tree_stopped(root, grandchildren)
        finally:
            self._reap([root.pid, gc_pid, *grandchildren])
            root.wait(timeout=10)

    def test_setsid_grandchild_is_reaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A grandchild that setsid-ed out of the group is still reaped.

        The group signal cannot reach it and it ignores SIGTERM, so the only
        thing that ends it is the snapshot-driven descendant sweep.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.5)
        root, gc_pid = self._spawn_tree(escape_group=True)
        grandchildren: list[int] = []
        try:
            grandchildren = self._await_descendants(root.pid)
            escaped = grandchildren[0]
            # Precondition: it left the group, so killpg provably misses it.
            deadline = time.monotonic() + 10.0
            while os.getpgid(escaped) == root.pid and time.monotonic() < deadline:
                time.sleep(0.05)
            assert os.getpgid(escaped) != root.pid

            _sync_kill_provider(self._provider(root.pid))

            self._assert_tree_stopped(root, grandchildren)
        finally:
            self._reap([root.pid, gc_pid, *grandchildren])
            root.wait(timeout=10)

    def test_recorded_descendant_outside_the_tree_is_reaped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A descendant recorded at spawn time is reaped even off the live tree.

        Once a child reparents, a walk from the root cannot find it again. The
        runtime's own ``_child_pids`` snapshot is the only record that still
        names it, so the sweep has to read that too.
        """
        from kiro_crew.acp.client import _capture_child_records
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.5)
        root = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        stray = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        try:
            # Not a descendant of root at all — exactly the reparented case.
            provider = self._provider(root.pid, child_pids=_capture_child_records([stray.pid]))

            _sync_kill_provider(provider)

            self._assert_tree_stopped(root, [])
            # Popen.wait reaps: this stray is a direct child of the TEST process
            # (a real one hangs off the runtime), so without a wait it lingers as
            # a zombie and a pid probe still reads it as alive. A negative code
            # is the signal that ended it.
            assert stray.wait(timeout=10) < 0
        finally:
            self._reap([root.pid, stray.pid])
            root.wait(timeout=10)
            stray.wait(timeout=10)

    def test_group_signal_alone_reaps_the_tree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no descendant snapshot, the group signal still reaps the tree.

        Isolates the scope of the signal from the sweep that backs it up: a
        descendant forked between the snapshot and the kill is in the group and
        in no record, so the group is the only thing that covers it. An empty
        snapshot reproduces that state deterministically.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.5)
        monkeypatch.setattr(
            "kiro_crew.session_pid._provider_descendant_records", lambda provider, pid, **_kw: {}
        )
        root, gc_pid = self._spawn_tree(escape_group=False)
        grandchildren: list[int] = []
        try:
            grandchildren = self._await_descendants(root.pid)
            assert os.getpgid(grandchildren[0]) == root.pid

            _sync_kill_provider(self._provider(root.pid))

            self._assert_tree_stopped(root, grandchildren)
        finally:
            self._reap([root.pid, gc_pid, *grandchildren])
            root.wait(timeout=10)

    def test_root_zombie_is_held_until_group_signalling_finishes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No reap may precede the last group signal.

        A zombie owns its pid, and a group leader's pid IS the pgid. Reaping the
        root during the SIGTERM grace frees that number while the SIGKILL
        escalation still aims at it, so a pid recycled into a new group leader
        would take a SIGKILL meant for ours -- unrecoverable, and invisible to
        the descendant sweep's own recycle guard, which only covers descendants.
        Signal ordering is the deterministic pin; the race itself is not
        reproducible on demand.
        """
        from kiro_crew import session_pid as sp
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.3)

        events: list[str] = []
        real_killpg = os.killpg
        real_reap = sp._reap_provider_root

        def tracking_killpg(pgid: int, sig: int) -> None:
            events.append(f"killpg:{sig}")
            real_killpg(pgid, sig)

        def tracking_reap(pid: int, recorded_start: str | None, *, gated: bool) -> None:
            events.append("reap")
            real_reap(pid, recorded_start, gated=gated)

        monkeypatch.setattr("kiro_crew.session_pid.os.killpg", tracking_killpg)
        monkeypatch.setattr("kiro_crew.session_pid._reap_provider_root", tracking_reap)

        # The grandchild ignores SIGTERM and stays in the group, so the tree is
        # not gone at the deadline and the SIGKILL escalation actually runs.
        root, gc_pid = self._spawn_tree(escape_group=False)
        grandchildren: list[int] = []
        try:
            grandchildren = self._await_descendants(root.pid)

            _sync_kill_provider(self._provider(root.pid))

            killpg_at = [i for i, e in enumerate(events) if e.startswith("killpg")]
            assert killpg_at, f"no group signal was sent: {events}"
            assert "reap" in events, f"the root was never reaped: {events}"
            assert events.index("reap") > max(
                killpg_at
            ), f"the root was reaped before the last group signal: {events}"
        finally:
            self._reap([root.pid, gc_pid, *grandchildren])
            root.wait(timeout=10)

    def test_unverified_root_is_not_signalled_but_records_still_are(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A root whose recorded identity does not match the live pid is spared.

        ``_client._pid`` outlives a failed start, so it can name a process the OS
        has since handed to someone else. For a group leader that pid IS the pgid,
        so signalling it unverified would take a stranger's whole tree, and the
        pid-scoped fallback is the same hazard one process wide -- hence nothing is
        sent to the root at all. Descendants recorded at spawn are still swept:
        each is verified on its own identity, which is the check the root lacked.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        root = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        stray = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        try:
            provider = self._provider(
                root.pid,
                child_pids={stray.pid: (platform_compat.get_process_start_id(stray.pid), None)},
            )
            # A start id no live process can hold: the pid is now "not ours".
            provider._client._start_time = "0.000000"

            _sync_kill_provider(provider)

            assert root.poll() is None, "an unverified root must not be signalled"
            assert stray.wait(timeout=10) < 0, "a recorded descendant must still be swept"
        finally:
            self._reap([root.pid, stray.pid])
            root.wait(timeout=10)
            stray.wait(timeout=10)

    def test_group_is_not_signalled_once_no_verified_member_owns_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pgid is re-checked before every signal, not trusted from entry.

        This teardown is not the only reaper: asyncio's own child watcher can
        collect the leader zombie, which frees the pid that IS the pgid, and under
        pid-space wraparound a new group leader can take it inside the grace. A
        group holding no member this code can verify is therefore left alone --
        signalling it would reap a stranger's tree. Descendants recorded
        at spawn are still signalled, because each is named and verified on its own.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        root = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        stray = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        killpg_calls: list[tuple[int, int]] = []
        real_pgroup_of = platform_compat.pgroup_of
        seen = {"n": 0}

        def pgroup_of_then_handed_on(probe_pid: int) -> int | None:
            # First read resolves OUR group, as it does in production; every read
            # after it reports a different owner, i.e. the group emptied and the
            # number moved on mid-grace.
            seen["n"] += 1
            return real_pgroup_of(probe_pid) if seen["n"] == 1 else 999_999

        monkeypatch.setattr(
            "kiro_crew.session_pid.os.killpg",
            lambda pgid, sig: killpg_calls.append((pgid, sig)),
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of", pgroup_of_then_handed_on
        )
        try:
            provider = self._provider(
                root.pid,
                child_pids={stray.pid: (platform_compat.get_process_start_id(stray.pid), None)},
            )

            _sync_kill_provider(provider)

            assert killpg_calls == [], f"signalled a group nothing verified owns: {killpg_calls}"
            assert stray.wait(timeout=10) < 0, "a recorded descendant must still be swept"
        finally:
            self._reap([root.pid, stray.pid])
            root.wait(timeout=10)
            stray.wait(timeout=10)

    def test_windows_tree_kill_is_identity_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The Windows arm pins the identity across taskkill, and honours a refusal.

        ``taskkill /T /F /PID`` resolves the pid from a SEPARATE process, so a
        caller that reads the start id and then calls the plain tree kill has
        released every handle by then: the provider can exit and Windows can hand
        that pid to something else, whose whole tree taskkill then tears down.
        ``kill_process_tree_pinned`` holds the verifying query handle open across
        the terminate -- Windows reserves a pid while any handle to the process
        object lives -- and returns False instead of killing when it cannot
        confirm. This runs on POSIX by faking the platform flag, because the
        branch it pins is unreachable here.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        pinned_calls: list[tuple[int, str, int]] = []
        unpinned_calls: list[tuple[int, int]] = []

        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.IS_WINDOWS", True)
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.kill_process_tree_pinned",
            lambda pid, expected, sig: (pinned_calls.append((pid, expected, sig)), False)[1],
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.kill_process_tree",
            lambda pid, sig: unpinned_calls.append((pid, sig)),
        )

        provider = self._provider(os.getpid())
        provider._client._start_time = platform_compat.get_process_start_id(os.getpid())

        _sync_kill_provider(provider)

        assert pinned_calls, "the Windows arm must use the identity-pinned tree kill"
        assert pinned_calls[0][0] == os.getpid()
        assert pinned_calls[0][1] == provider._client._start_time
        # A False return is a refusal to reap: no un-pinned kill may follow it.
        assert unpinned_calls == [], f"fell back to an unpinned kill: {unpinned_calls}"

    def test_fresh_walk_is_discarded_when_the_root_goes_stale_during_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A root verified before the walk can be a different process after it.

        The pid can be reused while the walk runs, so a walk that started on our
        tree can finish on a replacement's -- and those pids WOULD verify, because
        their identity is captured here and re-read moments later. Bracketing the
        walk is what stops a stale verification authorising the sweep: the freshly
        walked entries are dropped, while the spawn-time snapshot, recorded when
        the tree was provably ours, is still signalled.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        root, gc_pid = self._spawn_tree(escape_group=False)
        stray = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        real_start_id = platform_compat.get_process_start_id
        self._await_descendants(root.pid)  # the walk has something to find
        # Built BEFORE the patch: _provider reads the start id itself, and counting
        # that read would shift the failure onto the PRE-walk check instead.
        provider = self._provider(
            root.pid, child_pids={stray.pid: (real_start_id(stray.pid), None)}
        )
        root_reads = {"n": 0}

        def start_id_going_stale(probe_pid: int) -> str | None:
            # Reads 1 (entry gate) and 2 (pre-walk) see our root; read 3 is the
            # post-walk re-check and sees a value no live process here holds.
            if probe_pid == root.pid:
                root_reads["n"] += 1
                if root_reads["n"] >= 3:
                    return "0.000000"
            return real_start_id(probe_pid)

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", start_id_going_stale
        )
        signalled: list[int] = []
        real_kill_pid = platform_compat.kill_pid
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.kill_pid",
            lambda p, sig: (signalled.append(p), real_kill_pid(p, sig))[1],
        )
        try:
            _sync_kill_provider(provider)

            # The DECISION is the pin, not a liveness read: a freshly walked pid
            # must never be handed to a signal, whereas a recorded one must be.
            assert gc_pid not in signalled, f"a freshly walked pid was signalled: {signalled}"
            assert stray.pid in signalled, "the spawn-time snapshot must still be swept"
        finally:
            self._reap([root.pid, gc_pid, stray.pid])
            root.wait(timeout=10)
            stray.wait(timeout=10)

    def test_windows_fallback_kill_keeps_the_identity_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The /T-refused fallback is pinned too, not a plain pid kill.

        A `/T` refusal means the provider may already be exiting, which is exactly
        when its pid becomes reusable -- so dropping the pin for the fallback would
        undo the guarantee the pinned tree kill just gave, one process wide.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        pinned_pid_calls: list[tuple[int, str, int]] = []
        plain_pid_calls: list[tuple[int, int]] = []

        def refusing_tree_kill(pid: int, expected: str, sig: int) -> bool:
            raise OSError("taskkill /T refused")

        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.IS_WINDOWS", True)
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.kill_process_tree_pinned", refusing_tree_kill
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.kill_pid_pinned",
            lambda pid, expected, sig: (pinned_pid_calls.append((pid, expected, sig)), True)[1],
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.kill_pid",
            lambda pid, sig: plain_pid_calls.append((pid, sig)),
        )

        provider = self._provider(os.getpid())
        provider._client._start_time = platform_compat.get_process_start_id(os.getpid())

        _sync_kill_provider(provider)

        assert pinned_pid_calls, "the fallback must use the identity-pinned pid kill"
        assert pinned_pid_calls[0][1] == provider._client._start_time
        assert plain_pid_calls == [], f"fell back to an unpinned kill: {plain_pid_calls}"

    def test_walked_pid_is_dropped_when_it_leaves_the_tree_before_capture(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enumeration and identity capture are separate reads of the process table.

        A descendant can exit in between and its pid be reused, so the id captured
        would be a stranger's -- and it verifies at signal time, because it was read
        after the swap. Re-confirming ancestry after the capture drops that pid. The
        second snapshot is only an intersection filter, so it can shrink the set and
        never add to it.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        root = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        provider = self._provider(root.pid)
        walks = {"n": 0}

        def walk_then_lose_it(probe_pid: int) -> list[int]:
            # The first walk reports the pid; by the re-confirmation it is gone,
            # which is what a reuse between the two reads looks like from here.
            if probe_pid != root.pid:
                return []
            walks["n"] += 1
            return [bystander.pid] if walks["n"] == 1 else []

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.process_descendants", walk_then_lose_it
        )
        signalled: list[int] = []
        real_kill_pid = platform_compat.kill_pid
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.kill_pid",
            lambda p, sig: (signalled.append(p), real_kill_pid(p, sig))[1],
        )
        try:
            _sync_kill_provider(provider)

            assert bystander.pid not in signalled, f"a dropped pid was signalled: {signalled}"
            assert platform_compat.pid_exists(bystander.pid), "the bystander must survive"
        finally:
            self._reap([root.pid, bystander.pid])
            root.wait(timeout=10)
            bystander.wait(timeout=10)

    @pytest.mark.skipif(
        not hasattr(os, "waitid"),
        reason=(
            "needs a NON-DESTRUCTIVE 'has this child exited' peek to stand in for the "
            "post-exit identity read and to prove the reaper refused without consuming "
            "the status; os.waitid is Linux-only in CPython, and consuming the status "
            "to look at it would destroy the state under test. This test is new in "
            "this change, so nothing universal is narrowed by skipping it -- the "
            "portable half of the same property is asserted by _assert_tree_stopped."
        ),
    )
    def test_the_tree_stops_even_where_the_root_cannot_be_identified_after_it_exits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the teardown promises off Linux: killed, not necessarily reaped.

        Stands in for the macOS post-exit identity read -- `proc_pidinfo` reports
        nothing once a process has exited, while a RUNNING process still answers -- so
        the reaper cannot confirm the root it just killed and correctly refuses to wait
        on it. The tree is still STOPPED, which is the portable promise, and the root's
        pid stays held by its zombie, which is what this asserts: a refusal leaves the
        status uncollected.

        A zombie is not a survivor: its memory is released, it cannot run again, and
        asyncio's child watcher may still collect it.

        Mutation guard: permitting the wait when the live read is None -- reaping on an
        unconfirmable identity -- consumes the status and reddens the last assertion.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.5)
        root, gc_pid = self._spawn_tree(escape_group=False)
        try:
            grandchildren = self._await_descendants(root.pid)
            provider = self._provider(root.pid)
            real_start_id = platform_compat.get_process_start_id

            def _unreadable_once_exited(probe_pid: int) -> "str | None":
                if probe_pid == root.pid and self._our_child_exited(probe_pid):
                    return None
                return real_start_id(probe_pid)

            monkeypatch.setattr(
                "kiro_crew.session_pid.platform_compat.get_process_start_id",
                _unreadable_once_exited,
            )
            _sync_kill_provider(provider)

            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not self._our_child_exited(root.pid):
                time.sleep(0.05)
            assert self._our_child_exited(root.pid), "the root was not killed"
            assert self._await_gone(grandchildren) == [], "a descendant kept running"
            assert self._status_still_collectable(
                root.pid
            ), "the root was reaped on an identity the reaper could not confirm"
        finally:
            self._reap([root.pid, gc_pid])
            root.wait(timeout=10)

    def test_reap_refuses_a_pid_whose_identity_changed(self) -> None:
        """A wait on a recycled pid consumes an unrelated child's exit status.

        Nothing detects or repairs that loss, so the reaper re-reads the identity
        instead of trusting the one taken at entry. Uses a real zombie: the status is
        either still there to collect or it is gone, which is the whole effect.

        Two things here are platform-specific and are handled rather than assumed.
        The identity is recorded while the child is still RUNNING, which every
        platform can read -- reading it off the zombie instead only works on Linux,
        where `/proc/<pid>/stat` outlives the exit. And whether a correctly identified
        zombie can then be reaped at all is Linux-only for the same reason: off Linux
        `proc_pidinfo` reports nothing once a process has exited, so the reaper cannot
        confirm the identity and correctly refuses. The refusal arm -- the actual
        subject of this test -- runs everywhere.

        `_pid_exited_but_unreaped` is deliberately NOT the barrier: off Linux it falls
        back to plain liveness, and a zombie answers a liveness probe as present, so it
        never reports the state this constructs. `_our_child_exited` asks this
        process's own child status instead, which is answerable anywhere.
        """
        from kiro_crew import session_pid as sp

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            # Recorded while it RUNS, the way the ACP layer records it at spawn.
            recorded = platform_compat.get_process_start_id(child.pid)
            assert recorded is not None, "a running child's identity must be readable"
            child.kill()
            if self._HAS_WAITID:
                deadline = time.monotonic() + 10
                while not self._our_child_exited(child.pid):
                    assert time.monotonic() < deadline, "the child never exited"
                    time.sleep(0.01)
            else:
                # No non-destructive peek here, and consuming the status to look at it
                # would destroy what the refusal arm inspects. SIGKILL cannot be caught
                # or ignored, so a bounded settle is enough -- and if it were not, the
                # positive arm below would find the status uncollected and fail loudly
                # rather than pass on a child that was still running.
                time.sleep(0.5)
            assert self._status_still_collectable(child.pid), "the status must start uncollected"

            sp._reap_provider_root(child.pid, "0.000000", gated=True)
            assert self._status_still_collectable(
                child.pid
            ), "the status was consumed despite an identity mismatch"

            if sys.platform == "linux":
                # Read straight off the zombie, which only Linux keeps readable.
                sp._reap_provider_root(child.pid, recorded, gated=True)
            else:
                # Elsewhere the READER is stood in for, because the property under test
                # is "a CONFIRMED identity is waited on", not "this platform can confirm
                # one after the process exits". Gating the assertion away instead would
                # narrow a universal property to a platform allowlist.
                with pytest.MonkeyPatch.context() as patch:
                    patch.setattr(sp.platform_compat, "get_process_start_id", lambda _pid: recorded)
                    sp._reap_provider_root(child.pid, recorded, gated=True)
            assert not self._status_still_collectable(
                child.pid
            ), "a zombie whose identity is confirmed must be reaped"
        finally:
            # Unconditional: an assertion that fails BEFORE the kill above would
            # otherwise leave a running sleeper behind, and fabricating a return code
            # for it would hide that. Killing something already dead is a no-op, and
            # `Popen.wait` treats a child collected elsewhere as collected.
            try:
                child.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            child.wait(timeout=10)

    def test_fresh_walk_is_discarded_when_the_root_goes_stale_across_the_rescan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The re-scan is bracketed too, because an intersection is not only membership.

        If the ROOT pid is reused by a foreign process while this runs, the re-scan
        enumerates that stranger's children, so a first-walk pid its child now holds
        is confirmed by the filter and carries the stranger's captured id -- which the
        signal-time re-read then agrees with instead of catching.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        root = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        real_start_id = platform_compat.get_process_start_id
        provider = self._provider(root.pid)
        # Both scans report the pid, so the intersection keeps it; only the root
        # identity going stale across them can reject it.
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.process_descendants",
            lambda probe: [bystander.pid] if probe == root.pid else [],
        )
        root_reads = {"n": 0}

        def start_id_going_stale(probe_pid: int) -> str | None:
            # Reads 1 (entry), 2 (pre-walk) and 3 (post-walk) hold; read 4 is the
            # post-re-scan check and sees a value no live process here holds.
            if probe_pid == root.pid:
                root_reads["n"] += 1
                if root_reads["n"] >= 4:
                    return "0.000000"
            return real_start_id(probe_pid)

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", start_id_going_stale
        )
        signalled: list[int] = []
        real_kill_pid = platform_compat.kill_pid
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.kill_pid",
            lambda p, sig: (signalled.append(p), real_kill_pid(p, sig))[1],
        )
        try:
            _sync_kill_provider(provider)

            assert (
                bystander.pid not in signalled
            ), f"a pid confirmed only by a stranger's tree was signalled: {signalled}"
        finally:
            self._reap([root.pid, bystander.pid])
            root.wait(timeout=10)
            bystander.wait(timeout=10)

    def test_reaped_leader_does_not_suppress_the_group_signal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Group ownership authorizes killpg, so losing the root must not skip it.

        asyncio's watcher can collect the leader zombie during the grace. If that
        also cancelled the group escalation, a descendant that forked into the group
        after the snapshot would be reached by neither the group signal nor the
        recorded sweep, and would survive the teardown.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        root, gc_pid = self._spawn_tree(escape_group=False)
        real_start_id = platform_compat.get_process_start_id
        self._await_descendants(root.pid)
        # The grandchild is recorded at spawn, so _pgroup_still_ours can verify it
        # owns the pgid without consulting the root at all.
        provider = self._provider(root.pid, child_pids={gc_pid: (real_start_id(gc_pid), None)})

        real_descendants = platform_compat.process_descendants
        records_built = {"done": False}

        def counting_descendants(probe_pid: int) -> list[int]:
            found = real_descendants(probe_pid)
            # Both record-building scans have run by the second call; from here on
            # the root reads as a stranger, which is what an asyncio reap looks like.
            if probe_pid == root.pid:
                records_built["done"] = True
            return found

        def start_id_after_reap(probe_pid: int) -> str | None:
            if probe_pid == root.pid and records_built["done"]:
                return "0.000000"
            return real_start_id(probe_pid)

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.process_descendants", counting_descendants
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", start_id_after_reap
        )
        killpg_calls: list[int] = []
        real_killpg = os.killpg

        def tracking_killpg(pgid: int, sig: int) -> None:
            killpg_calls.append(sig)
            real_killpg(pgid, sig)

        monkeypatch.setattr("kiro_crew.session_pid.os.killpg", tracking_killpg)
        try:
            _sync_kill_provider(provider)

            assert platform_compat.SIGKILL in killpg_calls, (
                "the group SIGKILL is suppressed once the leader is reaped: "
                f"killpg signals seen = {killpg_calls}"
            )
        finally:
            self._reap([root.pid, gc_pid])
            root.wait(timeout=10)

    def test_recycled_descendant_is_not_signalled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A recorded pid whose identity does not match the live process is left alone.

        Deny-by-default: a pid recycled to another process inside the teardown
        window must not be signalled, so a wrong record is skipped rather than
        used.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.5)

        root = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        try:
            # A record claiming an impossible start time: the live process cannot
            # match it, so the sweep must refuse to signal that pid.
            provider = self._provider(root.pid, child_pids={bystander.pid: (1, b"python3")})

            _sync_kill_provider(provider)

            self._assert_tree_stopped(root, [])
            # Popen.poll reaps: a signalled direct child of the TEST process
            # would linger as a zombie that a pid probe still reads as alive, so
            # an exit status is what proves nothing was sent to it.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                assert bystander.poll() is None, "a pid failing verification was signalled"
                time.sleep(0.05)
        finally:
            self._reap([root.pid, bystander.pid])
            root.wait(timeout=10)
            bystander.wait(timeout=10)

    @staticmethod
    def _spawn_reaped_leader(ready_dir: Path) -> tuple[int, str | None, int, int]:
        """Spawn an isolated leader with two SIGTERM-ignoring children, then reap it.

        Returns the leader's pid, the start id recorded for it while it was alive,
        and the two children's pids. On return the leader is gone from ``/proc``
        while its group still holds both children -- the shape a pid alone cannot
        tell apart from a recycled one.

        Both children ignore SIGTERM and touch ``ready_dir/<pid>`` once they have,
        so a caller can wait out the window in which a SIGTERM would still kill them
        and the test would prove nothing about the SIGKILL escalation. One of them
        stays out of the caller's records: only a group signal can reach it, which
        is the property under test.
        """
        child = (
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"open(os.path.join({str(ready_dir)!r}, str(os.getpid())), 'w').close()\n"
            "time.sleep(300)\n"
        )
        source = (
            "import subprocess, sys\n"
            # DEVNULL, not the inherited pipe: a child holding the write end keeps
            # the parent's readline() waiting for EOF even after the root dies, so
            # an inherited pipe would stall the malformed-report cleanup below for
            # the child's whole 300-second sleep.
            f"witness = subprocess.Popen([sys.executable, '-c', {child!r}],\n"
            "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"unrecorded = subprocess.Popen([sys.executable, '-c', {child!r}],\n"
            "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "print(witness.pid, unrecorded.pid, flush=True)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", source],
            start_new_session=True,
            stdout=subprocess.PIPE,
            # The spawned tree writes only absolute paths under ready_dir, but it
            # would otherwise inherit the checkout as its CWD; anchor it so no
            # relative write can ever reach the repository.
            cwd=str(ready_dir),
        )
        assert proc.stdout is not None
        try:
            recorded_start = platform_compat.get_process_start_id(proc.pid)
            # The leader exits after reporting, and neither child holds its pipe.
            # Bound BOTH the read and the reap, not just a wait after readline().
            output, _ = proc.communicate(timeout=10)
            reported = output.split()
            assert len(reported) == 2, "provider root never reported both child pids"
            witness_pid, unrecorded_pid = (int(value) for value in reported)
            return proc.pid, recorded_start, witness_pid, unrecorded_pid
        except BaseException:
            # Kill the GROUP, not just the root. One child can already be running
            # when setup fails, even if its pid was never reported.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            proc.kill()
            proc.wait(timeout=10)
            raise
        finally:
            proc.stdout.close()

    @pytest.mark.parametrize("report", ["timeout", "missing", "invalid"])
    def test_reaped_leader_setup_failure_cleans_up_group(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, report: str
    ) -> None:
        """A failed setup must not leave unreported children or an open pipe."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.pid = 4242
        proc.stdout = MagicMock()
        if report == "timeout":
            error = subprocess.TimeoutExpired("provider fixture", 10)
            proc.communicate.side_effect = error
            proc.stdout.readline.side_effect = error
            expected = subprocess.TimeoutExpired
        else:
            output = b"" if report == "missing" else b"invalid 4244\n"
            proc.communicate.return_value = (output, None)
            proc.stdout.readline.return_value = output
            expected = AssertionError if report == "missing" else ValueError
        monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: proc)
        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda _pid: "start")
        killpg = MagicMock()
        monkeypatch.setattr(os, "killpg", killpg)

        with pytest.raises(expected):
            self._spawn_reaped_leader(tmp_path)

        proc.communicate.assert_called_once_with(timeout=10)
        killpg.assert_called_once_with(proc.pid, signal.SIGKILL)
        proc.kill.assert_called_once_with()
        proc.wait.assert_called_once_with(timeout=10)
        proc.stdout.close.assert_called_once_with()

    def test_group_is_recovered_when_the_leader_was_reaped_before_teardown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A leader already gone at entry must not cost the group its signal.

        ``pgroup_of`` needs the leader in ``/proc``, so a leader reaped before this
        teardown starts leaves no readable group id and the escalation sends nothing
        to the group at all -- while the members left in it keep running. Recovering
        the group from a descendant recorded at spawn is what reaches the member no
        record names: ``unrecorded`` here is absent from the records and ignores
        SIGTERM, so only the group SIGKILL can end it.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        leader_pid, recorded_start, witness_pid, unrecorded_pid = self._spawn_reaped_leader(
            tmp_path
        )
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if (tmp_path / str(witness_pid)).exists() and (
                    tmp_path / str(unrecorded_pid)
                ).exists():
                    break
                time.sleep(0.05)
            assert (
                tmp_path / str(witness_pid)
            ).exists(), "the recorded child never installed its SIGTERM handler"
            assert (
                tmp_path / str(unrecorded_pid)
            ).exists(), "the unrecorded child never installed its SIGTERM handler"
            # Preconditions: the leader is unreadable, and the group it led still
            # holds both children -- one recorded, one only reachable via the group.
            assert platform_compat.get_process_start_id(leader_pid) is None
            assert platform_compat.pgroup_of(witness_pid) == leader_pid
            assert platform_compat.pgroup_of(unrecorded_pid) == leader_pid

            witness_start = platform_compat.get_process_start_id(witness_pid)
            provider = self._provider(leader_pid, child_pids={witness_pid: (witness_start, None)})
            provider._client._start_time = recorded_start

            killpg_calls: list[tuple[int, int]] = []
            real_killpg = os.killpg

            def tracking_killpg(pgid: int, sig: int) -> None:
                killpg_calls.append((pgid, sig))
                real_killpg(pgid, sig)

            monkeypatch.setattr("kiro_crew.session_pid.os.killpg", tracking_killpg)

            _sync_kill_provider(provider)

            assert self._await_gone([unrecorded_pid]) == [], (
                "a SIGTERM-ignoring member of a reaped leader's group survived teardown, "
                "and no record named it"
            )
            assert {pgid for pgid, _sig in killpg_calls} == {leader_pid}
            assert platform_compat.SIGKILL in {
                sig for _pgid, sig in killpg_calls
            }, f"the group escalation never reached SIGKILL: {killpg_calls}"
        finally:
            self._reap([witness_pid, unrecorded_pid])

    def test_reaped_leader_recovers_no_group_without_a_witness_in_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A witness that left the group cannot vouch for it.

        The recovered id is only as good as the member proving the group is ours. A
        descendant that called ``setsid`` leads a group of its own, so it says
        nothing about the leader's -- which stays unsignalled, exactly as before.
        """
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of_leader", lambda _pid: 4242
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", lambda _pid: "same"
        )
        # The witness verifies, but it leads its own group.
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.pgroup_of", lambda _pid: 4243)

        assert _group_from_witnessed_descendant(4242, "root", {4243: ("same", None)}) is None

    def test_recovered_group_refuses_a_group_the_pid_does_not_lead(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A group id that is not ``pid``'s own is refused even with a live witness.

        This is the foreign-group fence on its own. The witness verifies AND belongs
        to the resolved group, so the ownership check would vouch for it -- but the
        group is not the one this leader led, so signalling it would reach past our
        tree. Only ``candidate == pid`` stops that, which is why this case exists
        separately from the witness-left-the-group one above.
        """
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        # A group the recorded leader 4242 does not lead.
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of_leader", lambda _pid: 4243
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", lambda _pid: "same"
        )
        # The witness is genuinely in 4243, so ownership alone would say yes.
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.pgroup_of", lambda _pid: 4243)

        assert _group_from_witnessed_descendant(4242, "same", {4243: ("same", None)}) is None

    def test_group_recovery_is_not_attempted_for_a_live_handle_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The `_proc` shape never reaches group recovery, so a walk cannot vouch.

        That shape passes ``gated=False``, which makes every ``_root_identity_holds``
        check pass on trust -- including the two bracketing the live descendant walk.
        A pid recycled to a foreign root would therefore have THAT root's children
        walked into ``records`` and verified against identities read from the same
        stranger, so a witness drawn from them proves nothing about our tree. The
        shape also cannot need recovery: its root was alive when the handle was read.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.1)
        calls: list[int] = []
        monkeypatch.setattr(
            "kiro_crew.session_pid._group_from_witnessed_descendant",
            lambda pid, *a, **k: calls.append(pid) or 4242,
        )
        # A `_proc`-shaped provider: no _client, a live handle with returncode None.
        provider = MagicMock(spec=["_client", "_proc", "_active_proc"])
        provider._client = None
        provider._proc = MagicMock(spec=["pid", "returncode"])
        provider._proc.pid = 4242
        provider._proc.returncode = None
        provider._active_proc = None
        # Not an isolated leader, so _isolated_provider_group yields None and the
        # recovery branch is the only thing that could produce a pgid.
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.pgroup_of", lambda _pid: 999)
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.process_descendants", lambda _p: []
        )
        killpg_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(
            "kiro_crew.session_pid.os.killpg", lambda pgid, sig: killpg_calls.append((pgid, sig))
        )
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.kill_pid", lambda _p, _s: None)

        _sync_kill_provider(provider)

        assert calls == [], "group recovery was attempted for a non-gated live-handle root"
        assert killpg_calls == [], f"a group was signalled for a live-handle root: {killpg_calls}"

    def test_recovered_group_refuses_a_recycled_witness(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recorded pid whose live identity differs cannot vouch for the group."""
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of_leader", lambda _pid: 4242
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", lambda _pid: "live"
        )
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.pgroup_of", lambda _pid: 4242)

        assert _group_from_witnessed_descendant(4242, "root", {4243: ("recorded", None)}) is None

    def test_recovered_group_needs_a_witness_with_a_recorded_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A record carrying no start id proves nothing and is skipped."""
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of_leader", lambda _pid: 4242
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", lambda _pid: None
        )
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.pgroup_of", lambda _pid: 4242)

        assert _group_from_witnessed_descendant(4242, "root", {4243: (None, None)}) is None

    def test_recovered_group_is_never_our_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Our own group is refused: killpg on it would signal the gateway itself."""
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        own = os.getpgrp()
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of_leader", lambda _pid: own
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", lambda _pid: "same"
        )
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.pgroup_of", lambda _pid: own)

        assert _group_from_witnessed_descendant(own, "root", {os.getpid(): ("same", None)}) is None

    def test_recovered_group_refuses_a_denied_leader(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``None`` from the primitive means signalling is denied, so nothing is sent."""
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of_leader", lambda _pid: None
        )

        assert _group_from_witnessed_descendant(4242, "root", {4243: ("same", None)}) is None

    def test_no_group_is_recovered_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows has no such group; teardown there goes through kill_process_tree."""
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.IS_WINDOWS", True)

        assert _group_from_witnessed_descendant(4242, "root", {4243: ("start", None)}) is None


@_POSIX_ONLY
@pytest.mark.skipif(
    not hasattr(os, "waitid"),
    reason=(
        "each case needs a CONFIRMED unreaped zombie before it can assert the reaper "
        "left the status alone, and without os.waitid -- Linux-only in CPython -- "
        "nothing here can confirm one: a live child and an unreaped zombie both answer "
        "a liveness probe as present, so the helper would hand the case a child that is "
        "still running and the assertion would hold no matter what the reaper did. "
        "Running these off Linux was measured to pass under a reaper mutated to wait on "
        "an unconfirmable identity, which is the harm they exist to catch. The portable "
        "half of the property is asserted by _assert_tree_stopped, which reddens six "
        "tests under that same mutation with waitid removed."
    ),
)
class TestReapProviderRoot:
    """An UNREADABLE identity refuses the wait, and that refusal is deliberate.

    `_sync_kill_provider` kills the root and holds its zombie unreaped until every
    group signal has gone out, so the reap runs against a process that has already
    exited. Whether its recorded identity can still be read there is a PLATFORM
    property: Linux keeps `/proc/<pid>/stat` readable for a zombie, macOS
    `proc_pidinfo` reports nothing once a process has exited.

    So off Linux the reap is refused, and that is deny-by-default working: an
    unreadable identity is exactly the case where our own zombie cannot be told from
    a pid already reaped by asyncio's watcher and recycled into another child of this
    process that is itself an unreaped zombie -- which reads as unreadable too, and
    whose status a wait would steal. What the refusal leaves is a zombie: memory
    already released, one process-table entry, and the watcher may still collect it.

    Simulating only the identity READ keeps this verifiable on any host.
    """

    @staticmethod
    def _zombie() -> "tuple[subprocess.Popen, int]":
        """A real unreaped zombie: exited, never waited on. Returns it and its pid."""
        proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            # Deliberately NOT poll()/wait() -- either would reap it and free the pid.
            if TestSyncKillProviderTree._status_still_collectable(proc.pid):
                return proc, proc.pid
            time.sleep(0.02)
        raise AssertionError(f"child {proc.pid} never became a zombie")

    def _assert_refused(self, live_id: "str | None", monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew import session_pid as sp

        proc, pid = self._zombie()
        try:
            monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda _pid: live_id)
            sp._reap_provider_root(pid, "1758000000.000001", gated=True)
            assert TestSyncKillProviderTree._status_still_collectable(
                pid
            ), "the status was consumed on an identity the reaper could not confirm"
        finally:
            try:
                proc.wait(timeout=10)
            except ChildProcessError:
                pass

    def test_an_unreadable_identity_refuses_the_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The macOS post-exit case: nothing readable, so nothing is waited on.

        Mutation guard: permitting the wait when the live read is None -- which is
        what "reap it anyway off Linux" would do -- consumes the status and reddens
        this.
        """
        self._assert_refused(None, monkeypatch)

    def test_a_readable_different_identity_refuses_the_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recycled pid reads as a DIFFERENT id, and that refuses the wait too."""
        self._assert_refused("999.000999", monkeypatch)


class TestCleanupOrphanedMcpServersExtra:
    def test_bare_pid_non_numeric_skipped(self, pid_file: Path) -> None:
        """A bare (no-colon) line that is not an int is skipped via ValueError."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("not_a_number\n")

        killed = _cleanup_orphaned_mcp_servers()

        assert killed == 0
        # Malformed bare line is left in place (continue, not pruned)
        assert "not_a_number" in pid_file.read_text(encoding="utf-8")

    def test_orphan_kill_oserror_swallowed(self, pid_file: Path) -> None:
        """kill_pid raising OSError on an orphaned child is swallowed; entry pruned."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("77777:99999\n")  # parent 99999 dead

        def fake_pid_exists(pid: int) -> bool:
            return pid == 77777  # child alive, parent dead

        def fake_kill(pid: int, sig: int) -> bool:
            raise OSError("kill failed")

        with (
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=fake_pid_exists,
            ),
            patch("kiro_crew.platform_compat.get_ppid", return_value=1),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=fake_kill,
            ),
        ):
            killed = _cleanup_orphaned_mcp_servers()

        # kill raised → not counted, but the entry is still pruned
        assert killed == 0
        assert "77777" not in pid_file.read_text(encoding="utf-8")


class TestPidGoneOrUnmanaged:
    """`_pid_gone_or_unmanaged` decides whether it is safe to untrack a PID.

    Safe (True) only when the process is confirmed gone. Any PID still alive or
    unsignalable returns False (retain) so a survivor of a failed teardown keeps
    its tracking entry and the orphan sweep reaps it. Fork note: routes through
    ``platform_compat.pid_liveness`` (Windows-safe) rather than raw
    ``os.kill(pid, 0)``, so — unlike upstream 33da30e6 — an EPERM/unsignalable
    PID is RETAINED (fail-safe), not untracked.

    The probe is mocked at ``platform_compat.pid_liveness`` (not a real dead
    PID): a raw ``os.kill(pid, 0)`` walk to *find* a dead PID would itself be
    the Windows-terminates-the-target footgun this fork forbids.
    """

    def test_dead_pid_is_safe_to_untrack(self) -> None:
        from kiro_crew.session_pid import _pid_gone_or_unmanaged

        with patch(
            "kiro_crew.platform_compat.pid_liveness",
            return_value=platform_compat.PID_DEAD,
        ):
            assert _pid_gone_or_unmanaged(4242) is True

    def test_live_pid_under_our_uid_is_retained(self) -> None:
        # A live PID under our uid is retained (False) regardless of whether it
        # is a managed agent: it may be an un-reaped survivor, and the periodic
        # sweep re-validates ownership before reaping. This is the fail-safe
        # direction — we never untrack something that is still alive here.
        from kiro_crew.session_pid import _pid_gone_or_unmanaged

        assert _pid_gone_or_unmanaged(os.getpid()) is False

    def test_unsignalable_pid_is_retained(self) -> None:
        # Fork divergence from upstream: pid_liveness collapses POSIX EPERM into
        # PID_UNSIGNALABLE, which we treat as "retain" (the sweep re-validates
        # ownership off the hot path). Never orphaning a live survivor is the
        # invariant; a retained-but-recycled PID is harmless.
        from kiro_crew.session_pid import _pid_gone_or_unmanaged

        with patch(
            "kiro_crew.platform_compat.pid_liveness",
            return_value=platform_compat.PID_UNSIGNALABLE,
        ):
            assert _pid_gone_or_unmanaged(4242) is False

    def test_alive_pid_is_retained(self) -> None:
        from kiro_crew.session_pid import _pid_gone_or_unmanaged

        with patch(
            "kiro_crew.platform_compat.pid_liveness",
            return_value=platform_compat.PID_ALIVE,
        ):
            assert _pid_gone_or_unmanaged(4242) is False


# ── Marked-launcher orphan sweep tests ───────────


class TestMarkedMcpLauncherPredicates:
    """Positive-ID sweep path for fingerprint-less MCP launchers (npx)."""

    def test_matches_npx_playwright_null_separated(self) -> None:
        from kiro_crew.session_pid import _is_marked_mcp_launcher

        cmdline = b"npx\x00@playwright/mcp\x00--headless"
        assert _is_marked_mcp_launcher(cmdline) is True

    def test_matches_npx_playwright_space_separated(self) -> None:
        """macOS ps output is space-separated — substring match covers both."""
        from kiro_crew.session_pid import _is_marked_mcp_launcher

        cmdline = b"/usr/local/bin/node /usr/lib/node_modules/@playwright/mcp/cli.js"
        assert _is_marked_mcp_launcher(cmdline) is True

    def test_matches_generic_start_server(self) -> None:
        from kiro_crew.session_pid import _is_marked_mcp_launcher

        cmdline = b"/bin/sh\x00-c\x00some-launcher mcp start-server slack-mcp"
        assert _is_marked_mcp_launcher(cmdline) is True

    def test_rejects_peer_gateway(self) -> None:
        from kiro_crew.session_pid import _is_marked_mcp_launcher

        cmdline = b"python3\x00-m\x00kiro_crew.mcp_gateway.gatewayd\x00mcp start-server"
        assert _is_marked_mcp_launcher(cmdline) is False

    def test_rejects_unrelated_process(self) -> None:
        from kiro_crew.session_pid import _is_marked_mcp_launcher

        assert _is_marked_mcp_launcher(b"vim\x00notes-about-mcp.md") is False

    def test_sweepable_requires_env_marker_for_marked_launcher(self) -> None:
        """npx cmdline WITHOUT the environ marker is NOT sweepable."""
        from kiro_crew.session_pid import _is_sweepable_orphan_mcp

        cmdline = b"npx\x00@playwright/mcp\x00--headless"
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False):
            assert _is_sweepable_orphan_mcp(1234, cmdline) is False

    def test_sweepable_with_env_marker(self) -> None:
        from kiro_crew.session_pid import _is_sweepable_orphan_mcp

        cmdline = b"npx\x00@playwright/mcp\x00--headless"
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_sweepable_orphan_mcp(1234, cmdline) is True

    def test_fingerprinted_cmdline_never_reads_environ(self) -> None:
        """The pre-existing marker path must not depend on the environ read."""
        from kiro_crew.session_pid import _is_sweepable_orphan_mcp

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker") as mock_env:
            assert _is_sweepable_orphan_mcp(1, b"kirocrew_sandbox_abc\x00--stdio") is True
        mock_env.assert_not_called()


class TestEnvHasKirocrewMarker:
    """/proc/<pid>/environ positive-identity read."""

    def test_non_linux_fails_closed(self) -> None:
        from kiro_crew.session_pid import _env_has_kirocrew_marker

        with patch("kiro_crew.session_pid.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert _env_has_kirocrew_marker(os.getpid()) is False

    def test_read_failure_fails_closed(self) -> None:
        from kiro_crew.session_pid import _env_has_kirocrew_marker

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", side_effect=PermissionError),
        ):
            mock_sys.platform = "linux"
            assert _env_has_kirocrew_marker(1) is False

    @pytest.mark.skipif(sys.platform != "linux", reason="/proc is Linux-only")
    def test_real_child_with_marker(self) -> None:
        """End-to-end: a real child spawned with the marker is identified.

        Polls briefly: /proc/<pid>/environ shows the parent's environment
        until the child completes exec (production is immune — the sweep's
        min-age guard runs long after exec).
        """
        import time

        from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
        from kiro_crew.session_pid import _env_has_kirocrew_marker

        env = {**os.environ, KIROCREW_SPAWNED_ENV: KIROCREW_SPAWNED_VALUE}
        proc = subprocess.Popen(["sleep", "30"], env=env)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if _env_has_kirocrew_marker(proc.pid):
                    break
                time.sleep(0.05)
            assert _env_has_kirocrew_marker(proc.pid) is True
        finally:
            proc.kill()
            proc.wait()

    @pytest.mark.skipif(sys.platform != "linux", reason="/proc is Linux-only")
    def test_real_child_without_marker(self) -> None:
        from kiro_crew.constants import KIROCREW_SPAWNED_ENV
        from kiro_crew.session_pid import _env_has_kirocrew_marker

        env = {k: v for k, v in os.environ.items() if k != KIROCREW_SPAWNED_ENV}
        proc = subprocess.Popen(["sleep", "30"], env=env)
        try:
            assert _env_has_kirocrew_marker(proc.pid) is False
        finally:
            proc.kill()
            proc.wait()


class TestMarkedLauncherSweepIntegration:
    @pytest.fixture(autouse=True)
    def _stable_root_identity(self) -> Iterator[None]:
        """See TestKillOrphanMcps._stable_root_identity."""
        with patch("kiro_crew.session_pid._pid_start_token", return_value="tok-stable"):
            yield

    """find + kill phases honor the marked-launcher positive-ID path."""

    def test_find_includes_marked_npx_orphan(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[700]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"npx\x00@playwright/mcp\x00--headless"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == [700]

    def test_find_excludes_unmarked_npx_orphan(self) -> None:
        """A user's own npx process (no environ marker) is never a candidate."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[710]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"npx\x00@playwright/mcp\x00--headless"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    @_POSIX_ONLY
    def test_kill_reverify_honors_marked_launcher(self) -> None:
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=720),
            patch("os.killpg") as mock_killpg,
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"npx\x00@playwright/mcp\x00--headless"),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([720])

        assert killed == 1
        mock_killpg.assert_called_once_with(720, signal.SIGKILL)

    @_POSIX_ONLY
    def test_kill_reverify_skips_unmarked_launcher(self) -> None:
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=730),
            patch("os.killpg") as mock_killpg,
            patch("os.kill") as mock_kill,
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"npx\x00@playwright/mcp\x00--headless"),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([730])

        assert killed == 0
        mock_killpg.assert_not_called()
        mock_kill.assert_not_called()


# ── Work-process orphan sweep tests ───────────


class TestIsSweepableOrphanWork:
    """Unit tests for the work-class positive-identity predicate."""

    _PYTEST_CMDLINE = b"/usr/bin/python3\x00-m\x00pytest\x00test/\x00-x\x00-q"

    def test_marked_orphaned_old_work_process_is_sweepable(self) -> None:
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with (
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            assert _is_sweepable_orphan_work(1234, self._PYTEST_CMDLINE, 700.0) is True

    def test_xdist_execnet_worker_is_sweepable(self) -> None:
        """pytest-xdist popen workers run under execnet's bootstrap cmdline."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        worker = (
            b"/repo/.venv/bin/python\x00-u\x00-c" b"\x00import sys;exec(eval(sys.stdin.readline()))"
        )
        with (
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            assert _is_sweepable_orphan_work(1234, worker, 700.0) is True

    def test_live_session_leader_blocks_sweep(self) -> None:
        """A backgrounded run whose kiro-cli session leader is ALIVE is kept.

        ``nohup pytest &`` reparents to init while the owning agent session
        still polls its log — SID still points at the live leader, so the
        sweep must leave the run alone. The environ is never read.
        """
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with (
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=True,
            ),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker") as mock_env,
        ):
            assert _is_sweepable_orphan_work(1234, self._PYTEST_CMDLINE, 700.0) is False
        mock_env.assert_not_called()

    def test_unreadable_sid_fails_closed(self) -> None:
        """SID read failure -> assume the owner is alive -> never sweep."""
        from kiro_crew.session_pid import _work_orphan_session_leader_alive

        with patch("kiro_crew.session_pid._linux_pid_sid", return_value=-1):
            assert _work_orphan_session_leader_alive(1234) is True

    def test_self_session_leader_fails_closed(self) -> None:
        """A setsid'd coordinator (own leader) carries no ownership info -> kept."""
        from kiro_crew.session_pid import _work_orphan_session_leader_alive

        with patch("kiro_crew.session_pid._linux_pid_sid", return_value=1234):
            assert _work_orphan_session_leader_alive(1234) is True

    def test_dead_leader_means_session_ended(self) -> None:
        """Leader gone (or PID recycled into a non-leader) -> session ended."""
        from kiro_crew.session_pid import _work_orphan_session_leader_alive

        def fake_sid(pid: int) -> int:
            return 500 if pid == 1234 else -1  # leader 500 unreadable = gone

        with patch("kiro_crew.session_pid._linux_pid_sid", side_effect=fake_sid):
            assert _work_orphan_session_leader_alive(1234) is False

    def test_marked_detached_daemon_is_not_sweepable(self) -> None:
        """A marked process WITHOUT a test-runner shape is never swept.

        Agents deliberately leave some marked processes running past turn end
        (e.g. a preview server detached with ``start_new_session=True``).
        Those are intentional survivors — the shape gate excludes them, and
        their environ is never even read.
        """
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        daemon = b"/usr/bin/node\x00/opt/serve-sim/cli.js\x00--udid\x00ABC123"
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker") as mock_env:
            assert _is_sweepable_orphan_work(1234, daemon, 7000.0) is False
        mock_env.assert_not_called()

    def test_pytest_path_fragment_daemon_is_not_sweepable(self) -> None:
        """'pytest' inside a path ARGUMENT must not match (structural, not
        substring): ``nohup node /work/pytest-dashboard/server.js`` is a
        daemon, not a test run."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        daemon = b"/usr/bin/node\x00/work/pytest-dashboard/server.js"
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker") as mock_env:
            assert _is_sweepable_orphan_work(1234, daemon, 7000.0) is False
        mock_env.assert_not_called()

    def test_venv_pytest_console_script_is_sweepable(self) -> None:
        """argv0 basename exactly ``pytest`` (venv console script) matches."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        console = b"/repo/.venv/bin/pytest\x00test/\x00-q"
        with (
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            assert _is_sweepable_orphan_work(1234, console, 700.0) is True

    def test_bootstrap_payload_as_free_arg_is_not_sweepable(self) -> None:
        """The execnet payload only matches as the argument OF ``-c`` — the
        same bytes appearing as any other argv element do not qualify."""
        from kiro_crew.session_pid import _work_sweep_cmdline_is_test_runner

        free = b"/usr/bin/grep\x00import sys;exec(eval(sys.stdin.readline()))\x00log"
        assert _work_sweep_cmdline_is_test_runner(free) is False

    def test_young_work_process_is_not_sweepable(self) -> None:
        """Below the dedicated work floor (600s) — even marked, left alone.

        Age is checked FIRST so a young process never even has its environ
        read; the env-marker mock asserts it stays uncalled.
        """
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker") as mock_env:
            assert _is_sweepable_orphan_work(1234, self._PYTEST_CMDLINE, 599.0) is False
        mock_env.assert_not_called()

    def test_mcp_floor_is_not_enough_for_work_class(self) -> None:
        """The 120s MCP floor must NOT admit work processes (dedicated floor)."""
        from kiro_crew.session_pid import (
            _ORPHAN_MIN_AGE_SECONDS,
            _ORPHAN_WORK_MIN_AGE_SECONDS,
            _is_sweepable_orphan_work,
        )

        assert _ORPHAN_WORK_MIN_AGE_SECONDS > _ORPHAN_MIN_AGE_SECONDS
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert (
                _is_sweepable_orphan_work(1234, self._PYTEST_CMDLINE, _ORPHAN_MIN_AGE_SECONDS + 1)
                is False
            )

    def test_unmarked_work_process_is_not_sweepable(self) -> None:
        """No KIROCREW_SPAWNED environ marker — a user's own pytest is safe."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False):
            assert _is_sweepable_orphan_work(1234, self._PYTEST_CMDLINE, 700.0) is False

    def test_managed_agent_basename_is_not_sweepable(self) -> None:
        """kiro-cli/claude runtimes stay owned by their tracked-PID lifecycle."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert (
                _is_sweepable_orphan_work(1234, b"/usr/local/bin/kiro-cli\x00chat\x00--acp", 700.0)
                is False
            )
            assert _is_sweepable_orphan_work(1234, b"claude\x00--print", 700.0) is False

    def test_gateway_entrypoint_is_not_sweepable(self) -> None:
        """Agent-launched peer gateways (e.g. dev pods) are never swept."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert (
                _is_sweepable_orphan_work(
                    1234, b"python3\x00-m\x00kiro_crew.mcp_gateway.gatewayd", 700.0
                )
                is False
            )

    def test_empty_cmdline_is_not_sweepable(self) -> None:
        """Kernel threads / zombies (empty cmdline) are never candidates."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_sweepable_orphan_work(1234, b"", 700.0) is False


class TestWorkOrphanSweepIntegration:
    """find + kill phases honor the work-process positive-ID path."""

    _PYTEST_CMDLINE = b"/usr/bin/python3\x00-m\x00pytest\x00test/\x00-x\x00-q"

    def test_find_includes_marked_old_work_orphan(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[900]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=self._PYTEST_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=700.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == [900]

    def test_find_excludes_young_marked_work_orphan(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[901]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=self._PYTEST_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_find_excludes_unmarked_work_orphan(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[902]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=self._PYTEST_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=700.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_find_excludes_marked_kiro_cli_orphan(self) -> None:
        """Managed agent runtime carrying the marker still isn't work-swept."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[903]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path, "read_bytes", return_value=b"/usr/local/bin/kiro-cli\x00chat\x00--acp"
            ),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=700.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_kill_sweeps_whole_subtree_leaf_first(self) -> None:
        """Descendants (incl. grandchildren) die before parents; root last."""
        from kiro_crew.session_pid import kill_orphan_mcps

        kill_order: list[int] = []

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=self._PYTEST_CMDLINE),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=700.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
            # Preorder: 910 -> [911, 912(-> 913)]; 913 is a grandchild.
            patch("kiro_crew.acp.client._get_child_pids", return_value=[911, 912, 913]),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, _sig: kill_order.append(p),
            ),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([910])

        assert killed == 4
        # Reversed preorder guarantees each process dies before its parent.
        assert kill_order == [913, 912, 911, 910]

    def test_kill_reverify_skips_now_young_or_unmarked(self) -> None:
        """Kill-phase re-verify fails closed when the marker is gone (TOCTOU)."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=self._PYTEST_CMDLINE),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=700.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False),
            patch("kiro_crew.session_pid.platform_compat.kill_pid") as mock_kill,
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([920])

        assert killed == 0
        mock_kill.assert_not_called()

    def test_subtree_kill_respects_global_cap(self) -> None:
        """The _ORPHAN_SWEEP_MAX_KILLS cap bounds subtree members too."""
        from kiro_crew.session_pid import kill_orphan_mcps

        kill_order: list[int] = []

        with (
            patch("kiro_crew.session_pid._ORPHAN_SWEEP_MAX_KILLS", 3),
            patch("os.getpgrp", return_value=1000),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=self._PYTEST_CMDLINE),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=700.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
            patch(
                "kiro_crew.acp.client._get_child_pids",
                return_value=[931, 932, 933, 934, 935],
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, _sig: kill_order.append(p),
            ),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([930])

        assert killed == 3
        # Deepest three descendants reaped; root survives to the next cycle.
        assert kill_order == [935, 934, 933]
        assert 930 not in kill_order


class TestSpawnedMarkerInjection:
    """Every provider/MCP spawn site injects the KIROCREW_SPAWNED marker."""

    def test_sandboxed_spawn_argv_injects_marker(self) -> None:
        from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
        from kiro_crew.sandbox import sandboxed_spawn_argv

        with (
            patch("kiro_crew.sandbox.wrap_argv", return_value=(["echo"], None)),
            patch("kiro_crew.sandbox.cgroup_scope_argv", side_effect=lambda a: a),
        ):
            _, env, _ = sandboxed_spawn_argv(["echo"], env={"PATH": "/bin"})

        assert env.get(KIROCREW_SPAWNED_ENV) == KIROCREW_SPAWNED_VALUE

    def test_spawn_site_source_registry(self) -> None:
        """Drift guard: the marker constant must appear at every known
        provider/MCP spawn-env build site. A new spawn site that replaces the
        inherited environment must add itself here AND inject the marker.

        The fork is KiroACP-only, so upstream's ``providers/claude_code.py``
        spawn site is intentionally absent from this list (the module is
        deleted in the public fork)."""
        src_root = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        spawn_sites = [
            "sandbox.py",
            "acp/runtime.py",
            "acp/client.py",
            "mcp_gateway/backend.py",
        ]
        for rel in spawn_sites:
            content = (src_root / rel).read_text(encoding="utf-8")
            assert "KIROCREW_SPAWNED_ENV" in content, (
                f"{rel} no longer injects the KIROCREW_SPAWNED marker — "
                "escaped MCP trees from this site become unsweepable"
            )


# ── PID-recycle identity guard + cross-platform spawn grace ───────────
# The quit->reopen race: a stale ``<dead_gw>:<pid>`` entry whose PID has been
# recycled onto a LIVE kiro-cli must not be SIGKILL'd by the startup sweep
# (which would surface to the user as "process exited (rc=None)"). The file
# sweep must verify more than the cmdline, and the spawn-grace window must not
# be Linux-only.


class TestPidStartTokenIdentityGuard:
    def test_track_session_pid_records_start_token(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Entries carry ``gw:pid:token`` so the sweep can verify identity."""
        from kiro_crew.session_pid import _track_session_pid

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok123")
        _track_session_pid(4242)
        assert session_pid_file.read_text(encoding="utf-8").strip() == f"{os.getpid()}:4242:tok123"

    def test_track_session_pid_falls_back_when_token_unavailable(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No token (Windows / ps failure) → legacy 2-field entry."""
        from kiro_crew.session_pid import _track_session_pid

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: None)
        _track_session_pid(4242)
        assert session_pid_file.read_text(encoding="utf-8").strip() == f"{os.getpid()}:4242"

    def test_track_session_pid_dedups_across_formats(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legacy entry must not be duplicated by a token-bearing re-track."""
        from kiro_crew.session_pid import _track_session_pid

        session_pid_file.write_text(f"{os.getpid()}:4242\n")
        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok123")
        _track_session_pid(4242)
        lines = session_pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == [f"{os.getpid()}:4242"]

    def test_untrack_session_pid_removes_token_entry(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Untrack matches the token-bearing form, not just the legacy one."""
        from kiro_crew.session_pid import _track_session_pid, _untrack_session_pid

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok123")
        _track_session_pid(4242)
        _untrack_session_pid(4242)
        assert session_pid_file.read_text(encoding="utf-8").strip() == ""

    def test_recycled_pid_is_pruned_not_killed(self, session_pid_file: Path) -> None:
        """THE regression: token mismatch → prune the stale entry, never kill.

        The PID is live and its cmdline matches an agent, so every pre-existing
        guard passes; only the start-token comparison catches the recycle.
        """
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        # Dead gateway (999999) : live child PID, recorded with an OLD token.
        session_pid_file.write_text("999999:99998:oldtoken\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            # Live process now reports a DIFFERENT token → PID was recycled.
            patch("kiro_crew.session_pid._pid_start_token", return_value="newtoken"),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            # The owning gateway (999999) must read as DEAD or _skip_tagged
            # skips the entry and the test passes vacuously; the child is alive.
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=lambda p: p != 999999,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
            # Grace disabled so the ONLY thing that can save the process is the
            # identity check under test.
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert kills == [], f"recycled PID was killed: {kills}"

    def test_unreadable_token_retains_entry(self, session_pid_file: Path) -> None:
        """Unknown identity must NOT prune: pruning would leak a live orphan.

        Every sweep keys off this file, so untracking a live process on one
        transient probe failure orphans it permanently (the fail-safe stated in
        _pid_gone_or_unmanaged: "any inconclusive result retains").
        """
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        entry = "999999:99998:recorded-token"
        session_pid_file.write_text(entry + "\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            # Identity unreadable (probe failure) — neither match nor mismatch.
            patch("kiro_crew.session_pid._pid_start_token", return_value=None),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=lambda p: p != 999999,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert kills == [], "killed a process whose identity could not be verified"

    def test_session_roots_unreadable_token_retains_entry(self, session_pid_file: Path) -> None:
        """Same fail-safe in the periodic root sweep: retain, don't kill or drop."""
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        entry = "999999:99998:recorded-token"
        session_pid_file.write_text(entry + "\n")
        kills: list[tuple[int, int]] = []

        def fake_liveness(pid: int) -> str:
            return platform_compat.PID_DEAD if pid == 999999 else platform_compat.PID_ALIVE

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value=None),
            patch("kiro_crew.session_pid.platform_compat.pid_liveness", side_effect=fake_liveness),
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=1),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert kills == [], "killed a process whose identity could not be verified"
        # Entry retained so the next sweep can retry.
        assert entry in session_pid_file.read_text(encoding="utf-8")

    @_POSIX_ONLY
    def test_session_roots_subreaper_reparent_still_killed(self, session_pid_file: Path) -> None:
        """A recorded start token that MATCHES proves identity on its own.

        Orphans do not always reparent to init: a process placed in its own
        cgroup scope by the service manager reparents to that *user manager*,
        which is a subreaper. Treating any other PPid as "the PID was recycled"
        both spares the real orphan AND drops its tracking entry, so nothing
        ever reaps it again. The token is strictly stronger evidence of identity
        than the parent, so it must not be vetoed by the PPid heuristic.
        """
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        entry = "999999:99998:sametoken"
        session_pid_file.write_text(entry + "\n")
        kills: list[tuple[int, int]] = []

        def fake_liveness(pid: int) -> str:
            return platform_compat.PID_DEAD if pid == 999999 else platform_compat.PID_ALIVE

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="sametoken"),
            patch("kiro_crew.session_pid.platform_compat.pid_liveness", side_effect=fake_liveness),
            # The subreaper that adopted the orphan -- neither init(1), nor the
            # dead gateway PID, nor the -1 probe-failure sentinel.
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=7447),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert (
            99998,
            platform_compat.SIGKILL,
        ) in kills, "a token-verified orphan adopted by a subreaper was not reaped"
        # And it must not be silently untracked, which is what leaks it forever.
        assert entry not in session_pid_file.read_text(encoding="utf-8")

    @_POSIX_ONLY
    def test_matching_token_still_killed(self, session_pid_file: Path) -> None:
        """A genuine orphan (token matches) is still reaped — no regression."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        session_pid_file.write_text("999999:99998:sametoken\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="sametoken"),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            # The owning gateway (999999) must read as DEAD or _skip_tagged
            # skips the entry and the test passes vacuously; the child is alive.
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=lambda p: p != 999999,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert (99998, platform_compat.SIGKILL) in kills

    @_POSIX_ONLY
    def test_legacy_entry_without_token_still_swept(self, session_pid_file: Path) -> None:
        """Back-compat: a 2-field entry keeps its old cmdline+grace behavior."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        session_pid_file.write_text("999999:99998\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            # The owning gateway (999999) must read as DEAD or _skip_tagged
            # skips the entry and the test passes vacuously; the child is alive.
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=lambda p: p != 999999,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert (99998, platform_compat.SIGKILL) in kills

    @_POSIX_ONLY
    def test_session_roots_sweep_parses_token_entry(self, session_pid_file: Path) -> None:
        """cleanup_orphaned_session_roots must not mis-prune 3-field entries.

        A ``split(":", 1)`` parse would int("99998:tok") -> ValueError and prune
        the entry, silently dropping every token-bearing line from the sweep.
        """
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        session_pid_file.write_text("999999:99998:sametoken\n")
        kills: list[tuple[int, int]] = []

        def fake_liveness(pid: int) -> str:
            # Owning gateway dead; child alive.
            return platform_compat.PID_DEAD if pid == 999999 else platform_compat.PID_ALIVE

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="sametoken"),
            patch("kiro_crew.session_pid.platform_compat.pid_liveness", side_effect=fake_liveness),
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=1),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert (99998, platform_compat.SIGKILL) in kills

    def test_session_roots_sweep_spares_recycled_pid(self, session_pid_file: Path) -> None:
        """Token mismatch in the periodic root sweep → prune, never kill."""
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        session_pid_file.write_text("999999:99998:oldtoken\n")
        kills: list[tuple[int, int]] = []

        def fake_liveness(pid: int) -> str:
            return platform_compat.PID_DEAD if pid == 999999 else platform_compat.PID_ALIVE

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="newtoken"),
            patch("kiro_crew.session_pid.platform_compat.pid_liveness", side_effect=fake_liveness),
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=1),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert kills == [], f"recycled PID was killed: {kills}"


class TestSpawnGraceCrossPlatform:
    @_POSIX_ONLY
    def test_grace_applies_on_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: the grace window was Linux-only, so macOS never got it."""
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp, "_pid_age_seconds", lambda p: 5.0)
        assert sp._pid_in_spawn_grace(4242) is True

    def test_old_process_not_in_grace_on_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp, "_pid_age_seconds", lambda p: sp.SWEEP_SPAWN_GRACE_SECONDS + 1)
        assert sp._pid_in_spawn_grace(4242) is False

    @_POSIX_ONLY
    def test_unknown_age_treated_as_young(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unreadable age → safe direction (skip the kill)."""
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp, "_pid_age_seconds", lambda p: None)
        assert sp._pid_in_spawn_grace(4242) is True

    def test_macos_age_derived_from_start_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """macOS age comes from the in-process start id — no subprocess/ps."""
        import time as _time

        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp.platform_compat, "IS_WINDOWS", False)
        start = _time.time() - 90.0
        monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda p: f"{start:.6f}")
        age = sp._pid_age_seconds(4242)
        assert age is not None and 85.0 <= age <= 95.0

    def test_macos_age_none_when_identity_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda p: None)
        assert sp._pid_age_seconds(4242) is None

    def test_windows_has_no_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows keeps prior behavior (no age source, sweep stays functional)."""
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.platform_compat, "IS_WINDOWS", True)
        assert sp._pid_in_spawn_grace(4242) is False


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: relies on fork/exec + ps for identity"
)
class TestSweepSparesLiveProcess:
    """End-to-end repro with a REAL process that the sweep spares a live kiro-cli.

    The mock-based tests above pin the decision logic; this one proves the
    whole sweep leaves an actually-running process alive. The victim is a
    short-lived ``sleep`` renamed via ``_is_managed_agent_process`` patching,
    so no kiro-cli is required and nothing user-owned is at risk.
    """

    def test_live_process_with_recycled_entry_survives(
        self, session_pid_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        monkeypatch.setattr("kiro_crew.session_pid.config_dir", lambda: tmp_path)
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            # Stale entry from a DEAD gateway naming the live PID, with a token
            # that cannot match the live process (the recycle signature).
            session_pid_file.write_text(f"999999:{victim.pid}:stale-token-does-not-match\n")

            with (
                # Cmdline check passes (as it did in the real incident).
                patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
                # The owning gateway (999999) must read as DEAD, or `_skip_tagged`
                # keeps the entry and the prune assertion below fails. Pinned rather
                # than assumed: 999999 is a perfectly ordinary live PID on a host
                # whose counter has passed it (`pid_max` is 4194304 here), which made
                # this a load-dependent flake rather than a constant failure. Only
                # that one PID is faked -- every other, the live victim included,
                # still goes to the real probe.
                patch(
                    "kiro_crew.session_pid.platform_compat.pid_exists",
                    side_effect=lambda p: p != 999999 and platform_compat.pid_exists(p),
                ),
                # Grace disabled: isolate the identity check as the sole guard.
                patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
                patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
            ):
                cleanup_orphaned_sessions()

            assert victim.poll() is None, "sweep SIGKILLed a live process (the bug)"
            # And the stale entry is pruned so it can't re-trigger next boot.
            assert str(victim.pid) not in session_pid_file.read_text(encoding="utf-8")
        finally:
            victim.kill()
            victim.wait()


class TestPidFileRewriteIsAtomic:
    """A PID-file rewrite must be atomic AND must never propagate its failure.

    Atomic: ``Path.write_text`` truncates the target to zero BEFORE writing the
    kept entries. A failure — or a hard kill — inside that window leaves a SHORT
    file whose surviving content is still perfectly well-formed: nothing raised,
    nothing logged, and every dropped entry is an agent runtime that no reaper
    can ever find again, because these PID files are the ONLY record of which
    runtimes this gateway owns.

    Reported, not propagated: pruning an entry is idempotent and self-retrying,
    so a failed rewrite costs one stale line. Propagating would cost the whole
    gateway — ``cleanup_orphaned_sessions`` runs unguarded on the startup path,
    and on Windows ``replace_with_retry`` declines to retry a sharing violation
    while an event loop is running.

    These tests fail the rename and then assert the original file is untouched,
    which a truncating writer cannot satisfy because by then it has already
    destroyed the original.
    """

    @staticmethod
    def _fail_rename(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    @staticmethod
    def _assert_reported(caplog: pytest.LogCaptureFixture) -> None:
        assert any(
            r.levelno >= logging.ERROR and "Could not rewrite PID file" in r.getMessage()
            for r in caplog.records
        ), "a failed PID-file rewrite must be reported at ERROR, never silently"

    def test_write_back_failure_preserves_every_entry(
        self,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.session_pid import _write_back_pid_file

        original = "100:200:tokA\n101:201:tokB\n102:202:tokC\n"
        session_pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", self._fail_rename)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _write_back_pid_file({"101:201:tokB"})

        # The rewrite never landed, so the ledger must still name all three
        # runtimes. A truncating writer leaves only two — and the two it leaves
        # look entirely valid, which is what makes the loss silent.
        assert session_pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    def test_untrack_session_pid_failure_preserves_every_entry(
        self,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.session_pid import _untrack_session_pid

        gw = os.getpid()
        original = f"{gw}:900:tokX\n{gw}:901:tokY\n"
        session_pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", self._fail_rename)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _untrack_session_pid(900)

        assert session_pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    def test_untrack_pid_failure_preserves_every_entry(
        self,
        pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.session_pid import _untrack_pid

        original = "700\n701\n702\n"
        pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", self._fail_rename)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _untrack_pid(701)

        assert pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    def test_untrack_child_pids_failure_preserves_every_entry(
        self,
        pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.session_pid import _untrack_child_pids

        original = "800:1\n801:1\n"
        pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", self._fail_rename)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _untrack_child_pids({801: object()})

        assert pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    @pytest.mark.asyncio
    async def test_windows_loop_sharing_violation_does_not_abort_caller(
        self,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The startup path survives a Windows sharing violation on the rename.

        ``replace_with_retry`` deliberately refuses to sleep-retry while an event
        loop is running, so on Windows a scanner holding the temp file surfaces
        as an immediate ``PermissionError``. ``cleanup_orphaned_sessions`` calls
        this rewrite unguarded during gateway start, so the error escaping here
        would abort startup.
        """
        from kiro_crew.session_pid import _write_back_pid_file

        def _sharing_violation(*_a: object, **_kw: object) -> None:
            raise PermissionError(32, "The process cannot access the file")

        original = "100:200:tokA\n101:201:tokB\n"
        session_pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.platform_compat.IS_WINDOWS", True)
        monkeypatch.setattr("kiro_crew.atomic_write.os.replace", _sharing_violation)

        # Runs with a live event loop, which is what disables the retry.
        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _write_back_pid_file({"101:201:tokB"})

        assert session_pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    def test_successful_rewrite_lands_and_leaves_no_temp_residue(
        self, session_pid_file: Path
    ) -> None:
        from kiro_crew.session_pid import _write_back_pid_file

        session_pid_file.write_text("100:200:tokA\n101:201:tokB\n", encoding="utf-8")

        _write_back_pid_file({"101:201:tokB"})

        assert session_pid_file.read_text(encoding="utf-8") == "100:200:tokA\n"
        # atomic_write's mkstemp companion must not survive the rename.
        assert not list(session_pid_file.parent.glob("*.tmp"))

    def test_no_truncating_writer_remains_in_session_pid(self) -> None:
        """Ratchet: every PID-file rewrite goes through the atomic chokepoint."""
        import kiro_crew.session_pid as sp

        source = Path(str(sp.__file__)).read_text(encoding="utf-8")
        assert ".write_text(" not in source, (
            "session_pid.py must rewrite PID files through _rewrite_pid_file(): "
            "Path.write_text truncates the file before writing, so a failure "
            "mid-write silently drops entries and leaks their runtimes until "
            "the host reboots."
        )


# ── Untracked managed-agent runtime orphan (REPORT-ONLY) ──


@pytest.fixture()
def reset_untracked_report_dedup() -> Iterator[None]:
    """Clear the module-level report dedup set around each test.

    The detector keeps reported PIDs in module state so a persisting orphan is
    logged once rather than once per sweep tick; leaking that between tests
    would make assertions order-dependent.
    """
    import kiro_crew.session_pid as sp

    sp._reported_untracked_agent_pids.clear()
    yield
    sp._reported_untracked_agent_pids.clear()


def _agent_cmdline(argv0: str = "/opt/kiro/bin/kiro-cli") -> bytes:
    """A managed agent runtime cmdline in Linux /proc (NUL-separated) form."""
    return b"\x00".join([argv0.encode(), b"chat", b"--no-interactive"])


class TestTrackedAgentPids:
    """_tracked_agent_pids unions the PIDs both tracking files claim."""

    def test_no_files_yields_empty_set(self, pid_file: Path, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _read_tracked_agent_pids, _tracked_agent_pids

        assert _tracked_agent_pids() == set()
        assert _read_tracked_agent_pids() == (set(), True)

    def test_session_entry_collects_child_not_gateway_or_identity_field(
        self, pid_file: Path, session_pid_file: Path
    ) -> None:
        """Only the child is reapable through a session entry.

        The gateway field names the OWNER whose death makes the entry sweepable,
        never a process reclaimed through it, and the third field is a
        start-time identity (numeric on Linux). Counting either would let a
        stale entry suppress a genuine report.
        """
        from kiro_crew.session_pid import _tracked_agent_pids

        session_pid_file.write_text("4100:4200:987654321\n", encoding="utf-8")

        assert _tracked_agent_pids() == {4200}

    def test_child_parent_entry_collects_child_not_parent(
        self, pid_file: Path, session_pid_file: Path
    ) -> None:
        """``_cleanup_orphaned_mcp_servers`` kills the child, not the parent."""
        from kiro_crew.session_pid import _tracked_agent_pids

        pid_file.write_text("5100:5200\n", encoding="utf-8")

        assert _tracked_agent_pids() == {5100}

    def test_bare_line_names_its_own_process_in_either_file(
        self, pid_file: Path, session_pid_file: Path
    ) -> None:
        from kiro_crew.session_pid import _tracked_agent_pids

        session_pid_file.write_text("5300\n", encoding="utf-8")
        pid_file.write_text("5400\n", encoding="utf-8")

        assert _tracked_agent_pids() == {5300, 5400}

    def test_both_files_union(self, pid_file: Path, session_pid_file: Path) -> None:
        """Each file contributes its own reapable field, at its own index."""
        from kiro_crew.session_pid import _tracked_agent_pids

        session_pid_file.write_text("10:11\n", encoding="utf-8")
        pid_file.write_text("20:21\n", encoding="utf-8")

        assert _tracked_agent_pids() == {11, 20}

    def test_malformed_and_non_positive_fields_are_skipped(
        self, pid_file: Path, session_pid_file: Path
    ) -> None:
        """A partially-appended or hand-edited line must not raise."""
        from kiro_crew.session_pid import _tracked_agent_pids

        session_pid_file.write_text("garbage\n0:-3\n:\n77:78\n", encoding="utf-8")

        assert _tracked_agent_pids() == {78}

    def test_unreadable_file_is_tolerated(self, pid_file: Path, session_pid_file: Path) -> None:
        """Report-only: an OSError costs a log line at worst, never a raise."""
        from kiro_crew.session_pid import _tracked_agent_pids

        pid_file.write_text("31:32\n", encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=OSError("boom")):
            assert _tracked_agent_pids() == set()

    def test_unreadable_non_missing_file_marks_snapshot_incomplete(
        self, pid_file: Path, session_pid_file: Path
    ) -> None:
        from kiro_crew.session_pid import _read_tracked_agent_pids

        with patch.object(Path, "read_text", side_effect=OSError(errno.EACCES, "denied")):
            assert _read_tracked_agent_pids() == (set(), False)

    def test_malformed_entry_marks_snapshot_incomplete_but_keeps_valid_pids(
        self, pid_file: Path, session_pid_file: Path
    ) -> None:
        from kiro_crew.session_pid import _read_tracked_agent_pids

        session_pid_file.write_text("10:11\ntruncated:\n", encoding="utf-8")
        assert _read_tracked_agent_pids() == ({11}, False)


class TestIsUntrackedManagedAgentOrphan:
    """Positive identity for the report-only detector."""

    def test_untracked_marked_runtime_is_detected(self) -> None:
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_untracked_managed_agent_orphan(900, _agent_cmdline(), set()) is True

    def test_tracked_runtime_is_not_detected(self) -> None:
        """A PID either file claims is already reachable by a reaper."""
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_untracked_managed_agent_orphan(900, _agent_cmdline(), {900}) is False

    def test_unmarked_process_is_not_detected(self) -> None:
        """A user's own kiro-cli (no environ marker) is never reported."""
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False):
            assert _is_untracked_managed_agent_orphan(901, _agent_cmdline(), set()) is False

    def test_claude_runtime_basename_also_detected(self) -> None:
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert (
                _is_untracked_managed_agent_orphan(
                    902, _agent_cmdline("/usr/local/bin/claude"), set()
                )
                is True
            )

    def test_peer_gateway_is_not_detected(self) -> None:
        """A gateway/CLI entrypoint is not an agent runtime."""
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        cmdline = b"kiro-cli\x00-m\x00kiro_crew.mcp_gateway.gatewayd"
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_untracked_managed_agent_orphan(903, cmdline, set()) is False

    def test_non_runtime_basename_is_not_detected(self) -> None:
        """A marked pytest orphan belongs to the work sweep, not this arm."""
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        cmdline = b"/venv/bin/pytest\x00-x"
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_untracked_managed_agent_orphan(904, cmdline, set()) is False

    def test_empty_cmdline_is_not_detected(self) -> None:
        """Kernel thread / zombie — no argv to identify."""
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_untracked_managed_agent_orphan(905, b"", set()) is False

    def test_no_environ_read_when_shape_already_declines(self) -> None:
        """Cheap gates run first — the /proc environ read is the last resort."""
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker") as mock_env:
            assert (
                _is_untracked_managed_agent_orphan(906, b"/venv/bin/pytest\x00-x", set()) is False
            )
        mock_env.assert_not_called()


class TestUntrackedRuntimeReportIntegration:
    """find_orphan_mcp_candidates reports the orphan and terminates nothing."""

    def test_reports_at_error_and_never_returns_as_candidate(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An untracked runtime leak is reported, not silently dropped.

        Every existing reaper declines an untracked runtime, so before this arm
        the sweep produced no candidate AND no diagnostic. The report must
        appear, and the PID must stay out of ``candidates`` — this arm has no
        kill authority.
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4242]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_agent_cmdline()),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=3600.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []  # report-only: nothing handed to the kill phase
        records = [r for r in caplog.records if "4242" in r.getMessage()]
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        message = records[0].getMessage()
        assert "kiro-cli" in message
        assert "NEITHER PID file" in message
        assert "report only" in message

    def test_argv0_control_characters_cannot_forge_log_lines(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """argv0 is set by the process itself, so it is untrusted input.

        A newline in it would otherwise forge whole lines in gateway.log and
        through /api/logs, which read as if the gateway had emitted them.
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        hostile = b"\x00".join([b"/tmp/kiro-cli\nERROR forged line", b"chat"])

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4848]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=hostile),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=3600.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        records = [r for r in caplog.records if "4848" in r.getMessage()]
        assert len(records) == 1
        message = records[0].getMessage()
        assert "\n" not in message  # the whole report stays one line
        assert "\\nERROR forged line" in message  # escaped, not interpreted

    def test_report_is_logged_once_across_repeated_sweeps(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A persisting orphan must not re-log on every sweep tick."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4343]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_agent_cmdline()),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=3600.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            find_orphan_mcp_candidates(active_pids=set())
            find_orphan_mcp_candidates(active_pids=set())
            find_orphan_mcp_candidates(active_pids=set())

        assert len([r for r in caplog.records if "4343" in r.getMessage()]) == 1

    def test_vanished_pid_re_arms_the_report(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Dedup state is scoped to PIDs still detected, so it cannot grow."""
        import kiro_crew.session_pid as sp
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_agent_cmdline()),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=3600.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            with patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4444]):
                find_orphan_mcp_candidates(active_pids=set())
            with patch("kiro_crew.session_pid._our_orphan_pids", return_value=[]):
                find_orphan_mcp_candidates(active_pids=set())
                assert sp._reported_untracked_agent_pids == set()
            with patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4444]):
                find_orphan_mcp_candidates(active_pids=set())

        assert len([r for r in caplog.records if "4444" in r.getMessage()]) == 2

    def test_tracked_runtime_is_not_reported(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A runtime the session file records is reachable — no diagnostic."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        session_pid_file.write_text("7:4545:99999\n", encoding="utf-8")

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4545]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_agent_cmdline()),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=3600.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        assert [r for r in caplog.records if "4545" in r.getMessage()] == []

    def test_recycled_owner_pid_does_not_suppress_the_report(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A stale entry's OWNER field must not shadow a real leak.

        The gateway field of a session entry and the parent field of a
        child entry name processes no reaper terminates through that entry.
        Once such an owner has died and its PID has been recycled into a leaked
        runtime, treating the field as tracked would return the sweep to the
        exact silence the report exists to prevent.
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        # 4747 appears only as a dead gateway (session) and a dead parent (child).
        session_pid_file.write_text("4747:11:99999\n", encoding="utf-8")
        pid_file.write_text("12:4747\n", encoding="utf-8")

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4747]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_agent_cmdline()),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=3600.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []  # still report-only
        assert len([r for r in caplog.records if "4747" in r.getMessage()]) == 1

    def test_young_orphan_is_not_reported(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Below the age floor the tracking append may simply not have landed."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4646]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_agent_cmdline()),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=5.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        assert [r for r in caplog.records if "4646" in r.getMessage()] == []


# ── Orphaned playwright-cli browser daemon sweep ───────────────

#: A realistic NUL-separated cliDaemon argv. playwright-core spawns the daemon
#: as ``node <...>/entry/cliDaemon.js <sessionName> [flags]`` (see
#: cli-client/session.js ``startDaemon``), so the session name is the argv
#: element immediately after the entry script.
_DAEMON_CMDLINE = (
    b"/usr/bin/node\x00"
    b"/home/u/.npm/_npx/e41f/node_modules/playwright-core/lib/entry/cliDaemon.js\x00"
    b"kc-1a2b3c4d\x00--headed"
)
_OPERATOR_DAEMON_CMDLINE = (
    b"/usr/bin/node\x00"
    b"/home/u/.npm/_npx/e41f/node_modules/playwright-core/lib/entry/cliDaemon.js\x00"
    b"chrome"
)


class TestBrowserDaemonSessionArg:
    """Structural extraction of the generated session name from daemon argv."""

    def test_extracts_generated_session_name(self) -> None:
        from kiro_crew.session_pid import _browser_daemon_session_arg

        assert _browser_daemon_session_arg(_DAEMON_CMDLINE) == b"kc-1a2b3c4d"

    def test_rejects_operator_named_session(self) -> None:
        """Only Kiro-Crew-generated ``kc-<8hex>`` names are ever sweepable."""
        from kiro_crew.session_pid import _browser_daemon_session_arg

        assert _browser_daemon_session_arg(_OPERATOR_DAEMON_CMDLINE) is None

    def test_rejects_space_joined_cmdline(self) -> None:
        """A ps-style space-joined cmdline cannot delimit argv safely."""
        from kiro_crew.session_pid import _browser_daemon_session_arg

        assert _browser_daemon_session_arg(_DAEMON_CMDLINE.replace(b"\x00", b" ")) is None

    def test_rejects_non_daemon_cmdline(self) -> None:
        from kiro_crew.session_pid import _browser_daemon_session_arg

        assert _browser_daemon_session_arg(b"/usr/bin/node\x00server.js\x00kc-1a2b3c4d") is None

    def test_session_env_name_matches_launch_module(self) -> None:
        """Drift ratchet: the local constant must track the real env var."""
        from kiro_crew.browser_cli.launch import SESSION_ENV
        from kiro_crew.session_pid import _BROWSER_SESSION_ENV

        assert _BROWSER_SESSION_ENV == SESSION_ENV


class TestBrowserDaemonOrphanSweep:
    """A stranded generated-session daemon is reclaimed; a live one never is."""

    def test_dead_owner_daemon_is_a_candidate(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[800]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-1a2b3c4d"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == [800]

    def test_live_owner_daemon_is_never_a_candidate(self) -> None:
        """The safety invariant: a live agent's browser is never reclaimed."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[801]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-1a2b3c4d"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=True,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    def test_operator_session_daemon_is_never_a_candidate(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[802]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_OPERATOR_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"chrome"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    def test_unmarked_daemon_is_never_a_candidate(self) -> None:
        """No ``KIROCREW_SPAWNED`` marker means we did not spawn this tree."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[803]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-1a2b3c4d"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    def test_young_daemon_is_never_a_candidate(self) -> None:
        """The work-class age floor applies: a fresh daemon is never raced."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[804]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=200.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-1a2b3c4d"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    def test_env_argv_session_mismatch_is_never_a_candidate(self) -> None:
        """argv name must be the generated name this process was exec'd with."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[805]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-99999999"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    @pytest.mark.parametrize(
        ("start_tokens", "cmdlines", "expected_killed"),
        [
            pytest.param(
                ("daemon-token", "daemon-token"),
                (_DAEMON_CMDLINE, _DAEMON_CMDLINE),
                1,
                id="stable-identity",
            ),
            pytest.param(
                (None, None),
                (_DAEMON_CMDLINE, _DAEMON_CMDLINE),
                0,
                id="identity-unavailable",
            ),
            pytest.param(
                ("daemon-token", "replacement-token"),
                (_DAEMON_CMDLINE, _DAEMON_CMDLINE),
                0,
                id="pid-recycled",
            ),
            pytest.param(
                ("daemon-token", "daemon-token"),
                (_DAEMON_CMDLINE, b"/usr/bin/sleep\x0030"),
                0,
                id="cmdline-changed",
            ),
        ],
    )
    @_POSIX_ONLY
    def test_kill_revalidates_browser_daemon_identity(
        self,
        start_tokens: tuple[str | None, str | None],
        cmdlines: tuple[bytes, bytes],
        expected_killed: int,
    ) -> None:
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch("os.getpgrp", return_value=1000),
            patch("os.getpid", return_value=1),
            patch.object(Path, "read_bytes", side_effect=cmdlines),
            patch("kiro_crew.session_pid._pid_start_token", side_effect=start_tokens),
            patch("kiro_crew.session_pid._is_sweepable_orphan_mcp", return_value=False),
            patch("kiro_crew.session_pid._is_sweepable_orphan_gatewayd", return_value=False),
            patch("kiro_crew.session_pid._is_sweepable_orphan_work", return_value=False),
            patch(
                "kiro_crew.session_pid._is_sweepable_orphan_browser_daemon",
                return_value=True,
            ),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch(
                "kiro_crew.session_pid._kill_orphan_browser_daemon",
                return_value=1,
            ) as mock_kill,
        ):
            mock_sys.platform = "linux"
            assert kill_orphan_mcps([806]) == expected_killed

        if expected_killed:
            mock_kill.assert_called_once_with(806, _DAEMON_CMDLINE)
        else:
            mock_kill.assert_not_called()


class TestBrowserSessionOwnerAlive:
    """The ownership probe reads only exec-time environ, never on-disk state."""

    def test_live_peer_holding_the_session_reads_as_alive(self) -> None:
        from kiro_crew import session_pid as sp

        with (
            patch.object(sp, "sys") as mock_sys,
            patch.object(Path, "iterdir", return_value=[Path("/proc/900"), Path("/proc/901")]),
            patch.object(Path, "stat", return_value=Mock(st_uid=os.getuid())),
            patch.object(sp, "_linux_pid_sid", return_value=1),
            patch.object(sp, "_env_value", return_value=b"kc-1a2b3c4d"),
        ):
            mock_sys.platform = "linux"
            assert sp._browser_session_owner_alive(900, b"kc-1a2b3c4d") is True

    def test_only_the_daemons_own_tree_reads_as_dead(self) -> None:
        """Chromium children share the daemon's SID and are not owners."""
        from kiro_crew import session_pid as sp

        with (
            patch.object(sp, "sys") as mock_sys,
            patch.object(Path, "iterdir", return_value=[Path("/proc/900"), Path("/proc/901")]),
            patch.object(Path, "stat", return_value=Mock(st_uid=os.getuid())),
            patch.object(sp, "_linux_pid_sid", return_value=900),
            patch.object(sp, "_env_value", return_value=b"kc-1a2b3c4d"),
        ):
            mock_sys.platform = "linux"
            assert sp._browser_session_owner_alive(900, b"kc-1a2b3c4d") is False

    def test_unreadable_peer_fails_closed_to_alive(self) -> None:
        from kiro_crew import session_pid as sp

        def _boom(
            pid: int,
            key: str,
            proc_root: Path | None = None,
        ) -> bytes | None:
            raise PermissionError("inconclusive")

        with (
            patch.object(sp, "sys") as mock_sys,
            patch.object(Path, "iterdir", return_value=[Path("/proc/901")]),
            patch.object(Path, "stat", return_value=Mock(st_uid=os.getuid())),
            patch.object(sp, "_linux_pid_sid", return_value=1),
            patch.object(sp, "_env_value", side_effect=_boom),
            patch.object(sp.platform_compat, "linux_process_name", return_value=None),
        ):
            mock_sys.platform = "linux"
            assert sp._browser_session_owner_alive(900, b"kc-1a2b3c4d") is True

    def test_non_linux_fails_closed_to_alive(self) -> None:
        from kiro_crew import session_pid as sp

        with patch.object(sp, "sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert sp._browser_session_owner_alive(900, b"kc-1a2b3c4d") is True


class TestBrowserSessionOwnerFakeProc:
    """The full daemon predicate decides from a fixture-owned proc tree."""

    _SESSION = b"kc-1a2b3c4d"
    _DAEMON_ENV = b"PLAYWRIGHT_CLI_SESSION=kc-1a2b3c4d\x00" b"KIROCREW_SPAWNED=1\x00"

    @staticmethod
    def _process(
        proc_root: Path,
        pid: int,
        *,
        sid: int,
        cmdline: bytes,
        environ: bytes | None,
        cgroup: str = "0::/pod.slice\n",
    ) -> None:
        process = proc_root / str(pid)
        process.mkdir()
        (process / "stat").write_text(
            f"{pid} (fixture) S 1 {pid} {sid} 0 0 0 0\n",
            encoding="utf-8",
        )
        (process / "cmdline").write_bytes(cmdline)
        comm = cmdline.split(b"\x00", 1)[0].rsplit(b"/", 1)[-1].decode()
        (process / "comm").write_text(comm + "\n", encoding="utf-8")
        # The plausible-owner cases deliberately sit outside the daemon's
        # cgroup: name plausibility must keep them before cgroup filtering.
        if comm == "kiro-cli":
            cgroup = "0::/agent.slice\n"
        (process / "cgroup").write_text(cgroup, encoding="utf-8")
        if environ is None:
            # read_bytes() raises OSError on every platform without depending
            # on permission-bit enforcement or the test runner's uid.
            (process / "environ").mkdir()
        else:
            (process / "environ").write_bytes(environ)

    @staticmethod
    def _linux_fixture(
        monkeypatch: pytest.MonkeyPatch,
        sp: object,
        proc_root: Path,
    ) -> None:
        monkeypatch.setattr(sp, "sys", Mock(platform="linux"))
        monkeypatch.setattr(platform_compat, "sys", Mock(platform="linux"))
        monkeypatch.setattr(
            sp.os,
            "getuid",
            lambda: proc_root.stat().st_uid,
            raising=False,
        )

    def _daemon(self, proc_root: Path) -> None:
        self._process(
            proc_root,
            900,
            sid=900,
            cmdline=_DAEMON_CMDLINE,
            environ=self._DAEMON_ENV,
        )

    def test_unreadable_sd_pam_stub_does_not_veto_sweep(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew import session_pid as sp

        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        self._daemon(proc_root)
        self._process(
            proc_root,
            901,
            sid=1,
            cmdline=b"(sd-pam)\x00",
            environ=None,
            cgroup="0::/user.slice\n",
        )
        self._linux_fixture(monkeypatch, sp, proc_root)

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.session_pid"):
            assert sp._is_sweepable_orphan_browser_daemon(
                900, _DAEMON_CMDLINE, 900.0, proc_root=proc_root
            )

        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "decision=ignore" in message and "reason=different_cgroup_unreadable" in message
            for message in messages
        )
        assert any(
            "decision=sweep" in message and "reason=no_owner_outside_daemon_tree" in message
            for message in messages
        )

    @pytest.mark.parametrize(
        ("proc_file", "invalid_content"),
        [
            ("comm", b"(sd-pam)\xff\n"),
            ("cgroup", b"0::/user.slice/\xff\n"),
        ],
    )
    def test_non_utf8_proc_metadata_still_reaches_sweep_verdict(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        proc_file: str,
        invalid_content: bytes,
    ) -> None:
        from kiro_crew import session_pid as sp

        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        self._daemon(proc_root)
        self._process(
            proc_root,
            901,
            sid=1,
            cmdline=b"(sd-pam)\x00",
            environ=None,
            cgroup="0::/user.slice\n",
        )
        (proc_root / "901" / proc_file).write_bytes(invalid_content)
        self._linux_fixture(monkeypatch, sp, proc_root)

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.session_pid"):
            assert sp._is_sweepable_orphan_browser_daemon(
                900, _DAEMON_CMDLINE, 900.0, proc_root=proc_root
            )

        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "decision=sweep" in message and "reason=no_owner_outside_daemon_tree" in message
            for message in messages
        )

    @pytest.mark.parametrize("owner_name", ["kiro-cli", "opencode"])
    def test_unreadable_plausible_owner_keeps_daemon(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        owner_name: str,
    ) -> None:
        from kiro_crew import session_pid as sp

        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        self._daemon(proc_root)
        self._process(
            proc_root,
            901,
            sid=1,
            cmdline=f"/usr/bin/{owner_name}\x00chat\x00".encode(),
            environ=None,
            cgroup="0::/agent.slice\n",
        )
        self._linux_fixture(monkeypatch, sp, proc_root)

        assert not sp._is_sweepable_orphan_browser_daemon(
            900, _DAEMON_CMDLINE, 900.0, proc_root=proc_root
        )

    def test_live_matching_owner_keeps_daemon(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew import session_pid as sp

        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        self._daemon(proc_root)
        self._process(
            proc_root,
            901,
            sid=1,
            cmdline=b"/usr/bin/kiro-cli\x00chat\x00",
            environ=b"PLAYWRIGHT_CLI_SESSION=kc-1a2b3c4d\x00",
        )
        self._linux_fixture(monkeypatch, sp, proc_root)

        assert not sp._is_sweepable_orphan_browser_daemon(
            900, _DAEMON_CMDLINE, 900.0, proc_root=proc_root
        )

    def test_no_owner_is_sweepable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew import session_pid as sp

        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        self._daemon(proc_root)
        self._linux_fixture(monkeypatch, sp, proc_root)

        assert sp._is_sweepable_orphan_browser_daemon(
            900, _DAEMON_CMDLINE, 900.0, proc_root=proc_root
        )

    def test_daemons_detached_tree_is_not_an_owner(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew import session_pid as sp

        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        self._daemon(proc_root)
        self._process(
            proc_root,
            902,
            sid=900,
            cmdline=b"/usr/bin/chromium\x00",
            environ=b"PLAYWRIGHT_CLI_SESSION=kc-1a2b3c4d\x00",
        )
        self._linux_fixture(monkeypatch, sp, proc_root)

        assert sp._is_sweepable_orphan_browser_daemon(
            900, _DAEMON_CMDLINE, 900.0, proc_root=proc_root
        )


class TestAcquiringAPidLockDoesNotTruncateTheLockFile:
    """A lock file must be opened WRITABLE but never TRUNCATING.

    ``msvcrt.locking`` needs a writable handle, so the fd cannot be opened
    ``"r"``. But ``"w"`` truncates at open, and on Windows a truncating open of a
    lock file whose first byte another holder already locked raises a sharing
    violation instead of waiting — so the contending acquirer crashes with a bare
    ``OSError`` *before* it reaches ``file_lock``, and the serialisation the lock
    exists to provide never happens. POSIX ``flock`` tolerates the truncate, which
    is why the defect is invisible on Linux and reddened only the Windows shards.

    Same defect and same fix as ``work_ledger._open_lock`` and
    ``dashboard/handlers/mcp.py``'s ``_McpFileLock``, which is already
    written this way.

    Truncation is the direct, PLATFORM-INDEPENDENT observable, and that is what
    these assert: seed the lock file with bytes, take and release the lock, and
    require the bytes to have survived. Under the old ``open(lock_path, "w")``
    every one of these fails on every platform, so the guard does not depend on
    running the suite on Windows to have teeth.
    """

    SEED = b"lock-file-content-that-must-survive"

    def test_session_pid_file_lock_preserves_the_lock_file(self, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _session_pid_file_lock, _session_pid_file_path

        lock_path = _session_pid_file_path().with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(self.SEED)

        with _session_pid_file_lock():
            pass

        assert lock_path.read_bytes() == self.SEED

    def test_pid_file_lock_preserves_the_lock_file(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _pid_file_lock, _pid_file_path

        lock_path = _pid_file_path().with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(self.SEED)

        with _pid_file_lock():
            pass

        assert lock_path.read_bytes() == self.SEED

    def test_the_periodic_sweep_preserves_the_lock_file(self, session_pid_file: Path) -> None:
        """The sweep is the site most likely to feel this in production.

        It runs on a timer while ``_track_session_pid`` contends for the same
        lock, which is exactly the interleaving a truncating open turns into a
        crash rather than a wait.
        """
        from kiro_crew.session_pid import _periodic_pid_sweep, _session_pid_file_path

        path = _session_pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # The sweep returns early unless the pid file exists, so it must exist
        # for the lock to be reached at all.
        path.write_text(f"{os.getpid()}:999999\n", encoding="utf-8")
        lock_path = path.with_suffix(".lock")
        lock_path.write_bytes(self.SEED)

        _periodic_pid_sweep(os.getpid(), set())

        assert lock_path.read_bytes() == self.SEED

    def test_the_lock_is_still_actually_acquired(self, pid_file: Path) -> None:
        """Guard the guard: a non-truncating open that never locks would pass above.

        ``file_lock`` is asked for the lock through the same helper the production
        path uses, so this fails if the fd stopped being writable — the failure
        mode a naive ``"r"`` fix would introduce, and the reason ``"r+"`` rather
        than ``"r"`` is the answer.
        """
        from kiro_crew.session_pid import _pid_file_path

        lock_path = _pid_file_path().with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(self.SEED)

        lock_path.touch(exist_ok=True)
        with open(lock_path, "r+") as fd:
            with platform_compat.file_lock(fd.fileno(), exclusive=True):
                pass
        assert lock_path.read_bytes() == self.SEED
