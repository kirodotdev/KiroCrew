"""Tests for tearing down one ACP runtime.

session_pid's signal to a reaped root's group members by identity and its kill
of the provider's process, and ``AcpClient._reset_state`` retiring the runtime's
tracking entries once it is confirmed gone.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from non_utf8_comm import BAD_COMM, write_stat

from kiro_crew import platform_compat
from kiro_crew.testing.ids import UNALLOCATABLE_PID


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

        monkeypatch.setattr(
            sp, "_marked_group_members", lambda pgid, inst, **_k: {101: "a", 102: "b"}
        )
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

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst, **_k: {101: "a"})
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

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst, **_k: {101: "a"})
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

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst, **_k: {101: "a"})
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

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst, **_k: {101: "a"})
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

        monkeypatch.setattr(
            sp, "_marked_group_members", lambda pgid, inst, **_k: {101: "a", 102: "b"}
        )
        self._identity(monkeypatch, {101: "a", 102: "b2"})  # 102 changed hands
        sent = self._delivery(monkeypatch, seam)

        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {101: "a"}
        assert sent == [(101, 15)]

    @pytest.mark.parametrize("seam", ["kill", "pidfd"])
    def test_escalation_re_signals_only_the_members_it_vouched(self, monkeypatch, seam) -> None:
        """A vouched member still alive under the same start id is the proof, and
        the only target."""
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(
            sp, "_marked_group_members", lambda pgid, inst, **_k: {101: "a", 103: "c"}
        )
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
        monkeypatch.setattr(
            sp, "_marked_group_members", lambda pgid, inst, **_k: {101: "a2", 200: "z"}
        )
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

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst, **_k: {101: None})
        kill = MagicMock()
        monkeypatch.setattr(sp.os, "kill", kill)

        assert sp._signal_orphaned_runtime_group(100, 9, "inst", expected={101: None}) == {}
        kill.assert_not_called()

    def test_a_member_with_no_readable_identity_is_never_signalled(self, monkeypatch) -> None:
        """Nothing to compare at the instant of the signal means no signal."""
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst, **_k: {101: None})
        kill = MagicMock()
        monkeypatch.setattr(sp.os, "kill", kill)

        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {}
        kill.assert_not_called()

    def test_no_vouching_member_sends_nothing(self, monkeypatch) -> None:
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst, **_k: {})
        kill = MagicMock()
        monkeypatch.setattr(sp.os, "kill", kill)

        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {}
        kill.assert_not_called()

    def test_a_refused_signal_does_not_abort_the_teardown(self, monkeypatch) -> None:
        """The caller is mid-teardown; an error here would skip its state clearing
        and PID pruning. The sweep retries a refused member on its own cadence."""
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst, **_k: {101: "a"})
        self._identity(monkeypatch, {101: "a"})

        def _refused(pid, sig):
            raise PermissionError

        monkeypatch.setattr(sp.os, "kill", _refused)
        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {}

    def test_a_member_that_exited_under_us_counts_as_nothing(self, monkeypatch) -> None:
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst, **_k: {101: "a"})
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
        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst, **_k: {101: "a"})

        assert sp._signal_orphaned_runtime_group(sp.os.getpgrp(), 15, "inst") == {}
        kill.assert_not_called()


class TestMarkedGroupMembers:
    """The vouching read: which live members of a group are ours."""

    @staticmethod
    def _fake_proc(
        tmp_path: Path, rows: dict[int, tuple[str, int, str]], *, comm: bytes = b"x"
    ) -> Path:
        """rows: pid -> (state, pgrp, spawn instance). Minimal /proc/<pid>/{stat,environ}.

        Each member's start ticks are ``pid * 10``, so its start id is ``f"{pid}0"``.
        """
        for pid, (state, pgrp, inst) in rows.items():
            write_stat(tmp_path, pid, comm=comm, state=state, pgrp=pgrp, start_ticks=pid * 10)
            (tmp_path / str(pid) / "environ").write_bytes(
                b"KIROCREW_SPAWNED=1\x00KIROCREW_SPAWN_INSTANCE="
                + inst.encode()
                + b"\x00PATH=/bin\x00"
            )
        (tmp_path / "self").mkdir()  # a non-digit entry, skipped
        return tmp_path

    @pytest.mark.parametrize("comm", [b"x", BAD_COMM], ids=["ascii", "not-utf8"])
    def test_only_live_marked_members_of_the_group_count(
        self, tmp_path, monkeypatch, comm: bytes
    ) -> None:
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
            comm=comm,
        )
        monkeypatch.setattr(
            sp, "_env_has_kirocrew_marker", lambda pid, proc_root=None: pid in (101, 102, 105)
        )
        monkeypatch.setattr(sp, "_tracked_child_has_runtime_identity", lambda pid: True)

        assert sp._marked_group_members(100, "ours", proc_root=root) == {101: "1010"}
        # The instance is the pin: the same group read as a different spawn is empty.
        assert sp._marked_group_members(100, "theirs", proc_root=root) == {105: "1050"}
        assert sp._marked_group_members(100, "", proc_root=root) == {}

    def test_the_start_id_comes_from_the_read_that_admitted_the_member(
        self, tmp_path, monkeypatch
    ) -> None:
        """A second read may already name another process, so it is never taken."""
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "linux")
        root = self._fake_proc(tmp_path, {101: ("S", 100, "ours"), 102: ("S", 100, "ours")})
        stat = (root / "102" / "stat").read_bytes()
        (root / "102" / "stat").write_bytes(stat.replace(b" 1020 ", b" notticks "))
        monkeypatch.setattr(sp, "_env_has_kirocrew_marker", lambda pid, proc_root=None: True)
        monkeypatch.setattr(sp, "_tracked_child_has_runtime_identity", lambda pid: True)
        monkeypatch.setattr(sp, "_pid_start_token", lambda pid: "a stranger's start")

        # 102's start cannot be read from that read, so it is not vouched for.
        assert sp._marked_group_members(100, "ours", proc_root=root) == {101: "1010"}

    def test_a_marked_member_without_runtime_identity_does_not_vouch(
        self, tmp_path, monkeypatch
    ) -> None:
        """A detached survivor that merely inherited the marker."""
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "linux")
        root = self._fake_proc(tmp_path, {101: ("S", 100, "ours")})
        monkeypatch.setattr(sp, "_env_has_kirocrew_marker", lambda pid, proc_root=None: True)
        monkeypatch.setattr(sp, "_tracked_child_has_runtime_identity", lambda pid: False)

        assert sp._marked_group_members(100, "ours", proc_root=root) == {}

    def test_env_spawn_instance_reads_the_value_and_fails_closed(self, tmp_path) -> None:
        from kiro_crew import session_pid as sp

        root = self._fake_proc(tmp_path, {101: ("S", 100, "abc123")})
        (tmp_path / "202").mkdir()
        (tmp_path / "202" / "environ").write_bytes(b"KIROCREW_SPAWNED=1\x00")
        assert sp._env_spawn_instance(101, root) == "abc123"
        assert sp._env_spawn_instance(202, root) is None  # marker but no instance
        assert sp._env_spawn_instance(303, root) is None  # unreadable

    def test_a_caller_may_turn_off_the_runtime_identity_gate(self, tmp_path, monkeypatch) -> None:
        """The argv gate is the ACP tree's shape, not every spawn's.

        An app backend's members are whatever its manifest runs, so the default
        gate rejects all of them and the reap would reach nothing. A caller that
        turns the gate off keeps the group and instance checks unchanged.
        """
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "linux")
        root = self._fake_proc(tmp_path, {101: ("S", 100, "ours"), 104: ("S", 200, "ours")})
        monkeypatch.setattr(sp, "_env_has_kirocrew_marker", lambda pid, proc_root=None: True)
        # The default gate is what an app backend's tree fails.
        monkeypatch.setattr(sp, "_tracked_child_has_runtime_identity", lambda pid: False)

        assert sp._marked_group_members(100, "ours", proc_root=root) == {}
        assert sp._marked_group_members(
            100, "ours", require_runtime_identity=False, proc_root=root
        ) == {101: "1010"}
        # Turning the gate off relaxes ONLY that gate: a member of another group,
        # or one carrying a different instance, is still refused.
        assert (
            sp._marked_group_members(200, "theirs", require_runtime_identity=False, proc_root=root)
            == {}
        )

    def test_the_public_non_runtime_entry_point_turns_the_gate_off(self, monkeypatch) -> None:
        """signal_orphaned_spawn_group differs from the ACP path in exactly one way."""
        from kiro_crew import session_pid as sp

        seen: dict[str, object] = {}

        def _members(pgid, inst, **kwargs):
            seen.update({"pgid": pgid, "inst": inst, **kwargs})
            return {}

        monkeypatch.setattr(sp, "_marked_group_members", _members)

        assert sp.signal_orphaned_spawn_group(100, 15, "inst") == ({}, {})
        assert seen["pgid"] == 100 and seen["inst"] == "inst"
        assert seen["require_runtime_identity"] is False
        # The ACP path keeps the gate ON, and says so rather than relying on a
        # default the public entry point could change under it. It also keeps the
        # single-map return: a refused signal changes nothing an ACP teardown does.
        seen.clear()
        assert sp._signal_orphaned_runtime_group(100, 15, "inst") == {}
        assert seen["require_runtime_identity"] is True

    def test_the_public_entry_point_reports_the_vouch_apart_from_the_signals(
        self, monkeypatch
    ) -> None:
        """An empty signal set alone cannot say whether the group is GONE.

        A caller keeping an orphan's only record has to tell "nothing is there" from
        "everything there refused the signal" -- collapsing them is how a refusal
        comes to read as a completed reap.
        """
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst, **_k: {101: "a"})
        monkeypatch.setattr(sp, "_pid_start_token", lambda pid: "a")

        # Every signal refused: the member is still vouched, and the census says so.
        def _refuse(pid, sig, start):
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(sp, "_signal_pid_by_identity", _refuse)
        assert sp.signal_orphaned_spawn_group(100, 15, "inst") == ({101: "a"}, {})

        # Nothing live in the group: both maps empty, which is the only shape that
        # means "gone".
        monkeypatch.setattr(sp, "_marked_group_members", lambda pgid, inst, **_k: {})
        assert sp.signal_orphaned_spawn_group(100, 15, "inst") == ({}, {})

    def test_the_public_entry_point_keeps_every_other_refusal(self, monkeypatch) -> None:
        """A relaxed argv gate must not relax the group or instance guards."""
        from kiro_crew import session_pid as sp

        members = MagicMock(return_value={101: "a"})
        monkeypatch.setattr(sp, "_marked_group_members", members)
        kill = MagicMock()
        monkeypatch.setattr(sp.os, "kill", kill)

        assert sp.signal_orphaned_spawn_group(0, 15, "inst") == ({}, {})
        assert sp.signal_orphaned_spawn_group(1, 15, "inst") == ({}, {})
        assert sp.signal_orphaned_spawn_group(100, 15, "") == ({}, {})
        assert sp.signal_orphaned_spawn_group(os.getpgrp(), 15, "inst") == ({}, {})
        members.assert_not_called()
        kill.assert_not_called()


class TestResetStateUntracksParentPid:
    def test_reset_state_untracks_parent_pid(self) -> None:
        """Verify _reset_state calls _untrack_pid with the saved PID."""
        from kiro_crew.acp.client import AcpClient

        client = AcpClient.__new__(AcpClient)
        client._process = None
        client._pid = 54321
        # No identity was read at spawn: this pins the prefix-matched fallback,
        # which is what a root without a token keeps.
        client._spawn_start_token = None
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

    def test_reset_state_retires_by_identity_when_a_spawn_token_is_held(self, monkeypatch) -> None:
        """ "Confirmed dead" is a fact about the process, not its number: a root
        spawned since can hold it. With the spawn token in hand, _reset_state
        retires the line that names this process and never the lines that merely
        carry the number."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import AcpPromptStats

        client = AcpClient.__new__(AcpClient)
        client._process = None
        client._pid = 54321
        client._spawn_start_token = "tok-a"
        client._session_id = None
        client._buffer = bytearray()
        client._cancelled = False
        client._resumed = False
        client._sandbox_cleanup = None
        client._child_pids = {}
        client._stderr_lines = deque(maxlen=20)
        client._pending_oauth_requests = []
        client._oauth_emitted_servers = set()
        client.last_prompt_stats = AcpPromptStats()
        client._stderr_task = None

        identity_calls: list[tuple[int, str]] = []

        def _by_identity(pid, token):
            identity_calls.append((pid, token))
            return True

        def _never(*_a):
            raise AssertionError("prefix-matched untrack ran although a token was held")

        monkeypatch.setattr("kiro_crew.session_pid._pid_gone_or_unmanaged", lambda pid: True)
        monkeypatch.setattr("kiro_crew.session_pid._untrack_root_by_identity", _by_identity)
        monkeypatch.setattr("kiro_crew.session._untrack_pid", _never)
        monkeypatch.setattr("kiro_crew.session._untrack_session_pid", _never)

        client._reset_state()

        assert identity_calls == [(54321, "tok-a")]
        assert client._spawn_start_token is None, "the token names a process that is gone"


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
        provider._active_proc.pid = UNALLOCATABLE_PID

        sigs: list[int] = []

        def fake_kill(pid: int, sig: int) -> bool:
            sigs.append(sig)
            raise ProcessLookupError()

        # The fake pid names no process, so the OS is never asked about it: no
        # group, which leaves the pid-scoped signal as the only arm, a pid that
        # reads as gone, as the signal's ProcessLookupError says, and the root's
        # reap observed rather than run.
        with (
            patch("kiro_crew.session_pid.platform_compat.pgroup_of", return_value=None),
            patch("kiro_crew.session_pid._reap_provider_root"),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=False),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=fake_kill,
            ),
        ):
            _sync_kill_provider(provider)

        # Loop stops after the first (SIGTERM) signal raises ProcessLookupError
        assert sigs == [platform_compat.SIGTERM]


@pytest.mark.parametrize("drain_error", [False, True], ids=["identity-refused", "drain-failed"])
def test_sync_windows_tree_refusal_never_falls_back_to_root_only(
    monkeypatch: pytest.MonkeyPatch, drain_error: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed tree drain keeps its scope; killing only the root is not cleanup."""
    from kiro_crew.session_pid import _sync_kill_provider

    pid, start = 4321, "recorded-creation"
    provider = MagicMock(spec=["_client", "_proc", "_active_proc"])
    provider._client = MagicMock(spec=["_pid", "_child_pids", "_start_time"])
    provider._client._pid = pid
    provider._client._start_time = start
    provider._client._child_pids = {}
    provider._proc = provider._active_proc = None
    tree_calls: list[tuple[int, str, int]] = []

    def refused_tree(pid: int, expected: str, sig: int) -> bool:
        tree_calls.append((pid, expected, sig))
        if drain_error:
            raise OSError("exact tree cleanup refused")
        return False

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda candidate: start)
    monkeypatch.setattr(platform_compat, "kill_process_tree_pinned", refused_tree)
    pinned_root_kill = Mock(return_value=True)
    plain_root_kill = Mock(return_value=True)
    unpinned_tree_kill = Mock(return_value=True)
    monkeypatch.setattr(platform_compat, "kill_pid_pinned", pinned_root_kill)
    monkeypatch.setattr(platform_compat, "kill_pid", plain_root_kill)
    monkeypatch.setattr(platform_compat, "kill_process_tree", unpinned_tree_kill)

    with caplog.at_level(logging.WARNING):
        _sync_kill_provider(provider)

    assert tree_calls == [(pid, start, platform_compat.SIGKILL)]
    pinned_root_kill.assert_not_called()
    plain_root_kill.assert_not_called()
    unpinned_tree_kill.assert_not_called()
    assert provider._client._pid == pid
    assert provider._client._start_time == start
    assert caplog.records, "an incomplete tree cleanup must be reported"
    assert not any("killed PID" in record.getMessage() for record in caplog.records)


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
