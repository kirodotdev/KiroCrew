"""The kill and reclaim paths see a process whose name is not UTF-8.

A process named :data:`non_utf8_comm.BAD_COMM` -- ``run_データ処理.py`` cut at the
kernel's 15 bytes -- has a ``stat``, ``status`` and ``comm`` that are not valid
UTF-8. Every reader below reads them as bytes, so on such a process:

* the root's exit check answers instead of raising out of the SIGTERM grace;
* the group scans count the member, so it is signalled and holds the group open;
* the session-leader check reads a live leader as alive, and only a leader that
  is gone as gone;
* the parent walk, the child map and the reconciler's table keep the process
  and every descendant behind it;
* a tracked orphan is killed rather than pruned as a reused pid, and an
  untracked one is listed for the orphan sweep;
* the identity, start and age readers answer;
* the one-shot supervisor lists and signals the member.

The live tests run a real child under that name, so the kernel and not a
fixture writes the line. Each signal a sweep would send goes to a recorder;
the only real signals are to the test's own children.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from non_utf8_comm import BAD_COMM, comm_is_settable, renamed_child, script_named_child, write_stat

from kiro_crew import _process_group_supervisor as supervisor
from kiro_crew import platform_compat, runtime_reconcile
from kiro_crew import session_pid as sp
from kiro_crew.acp import runtime_process_tree as tree

live = pytest.mark.skipif(not comm_is_settable(), reason="needs a real Linux /proc")


def test_the_name_really_is_not_utf8() -> None:
    """The premise every test here depends on."""
    with pytest.raises(UnicodeDecodeError):
        BAD_COMM.decode("utf-8")


@live
class TestALiveProcessUnderTheName:
    def test_the_child_does_not_run_in_the_checkout(self) -> None:
        """A relative write by the child or its grandchild must not land in the tree."""
        with renamed_child(with_grandchild=True) as (child, grandchild):
            for pid in (child, grandchild):
                assert os.readlink(f"/proc/{pid}/cwd") != os.getcwd()

    def test_the_root_is_running_until_it_exits(self) -> None:
        """The SIGTERM grace loop asks this while it waits for the root."""
        with renamed_child() as (child, _):
            assert sp._pid_exited_but_unreaped(child) is False
            os.kill(child, 9)  # this test's own child
            deadline = time.monotonic() + 10
            while not sp._pid_exited_but_unreaped(child):  # a zombie until reaped
                assert time.monotonic() < deadline, "the exit was never seen"
                time.sleep(0.02)

    def test_its_group_holds_a_live_member(self) -> None:
        with renamed_child() as (child, _):
            assert sp._pgroup_has_member_besides(child, os.getpid()) is True
            assert child in (platform_compat.linux_pgroup_members(child) or {})

    def test_a_script_run_under_the_name_holds_its_group(self, tmp_path: Path) -> None:
        """No prctl: running ``./run_データ処理.py`` is enough to get the name."""
        with script_named_child(tmp_path) as child:
            assert sp._pgroup_has_member_besides(child, os.getpid()) is True

    def test_its_session_leader_is_alive(self) -> None:
        """A live leader under the name keeps its session's work out of the sweep."""
        with renamed_child(with_grandchild=True) as (leader, work):
            assert sp._linux_pid_sid(leader) == leader
            assert sp._linux_pid_sid(work) == leader
            assert sp._work_orphan_session_leader_alive(work) is True
            assert runtime_reconcile._session_leader_alive(work) is True

    def test_its_parent_start_and_identity_are_read(self) -> None:
        with renamed_child() as (child, _):
            start = platform_compat.process_start_time(child)
            assert start is not None and start.isdigit()
            assert platform_compat.get_process_start_id(child) == start
            assert platform_compat.get_process_start_identity(
                child
            ) == platform_compat.ProcessStartIdentity(start, os.getpid())
            assert sp._pid_parent_and_token(child) == (os.getpid(), start)
            assert platform_compat.parent_pid(child) == os.getpid()
            assert platform_compat.get_ppid(child) == os.getpid()

    def test_the_parent_walk_passes_through_it(self) -> None:
        """A pinned tree kill reaches what sits below the renamed process."""
        with renamed_child(with_grandchild=True) as (child, grandchild):
            assert sp._is_our_descendant(child, os.getpid()) is True
            assert sp._is_our_descendant(grandchild, os.getpid()) is True

    def test_its_status_counters_are_read(self) -> None:
        with renamed_child() as (child, _):
            assert platform_compat.process_thread_count(child) == 1
            assert platform_compat._proc_status_rss_kb(child) > 0
            assert tree._get_rss_mb(child) is not None

    def test_its_age_is_read(self) -> None:
        with renamed_child() as (child, _):
            age = sp._pid_age_seconds(child)
            assert age is not None and 0 <= age < 60
            assert sp._linux_pid_age(child) == pytest.approx(age, abs=1.0)

    def test_an_orphan_under_the_name_is_a_sweep_candidate(self, monkeypatch) -> None:
        """Both reads the orphan sweep makes see it: the parent, then the age.

        The parent edge comes from ``stat`` bytes, so the orphan is listed, and its
        age is read too, so it clears an age floor of a microsecond; an age it
        could not read is ``0.0``, which never does.
        """
        with renamed_child() as (child, _):
            monkeypatch.setattr(sp, "_accepted_subreaper_pids", lambda: {os.getpid()})
            monkeypatch.setattr(sp, "_tracked_agent_pids", set)
            monkeypatch.setattr(sp, "_ORPHAN_MIN_AGE_SECONDS", 1e-6)
            monkeypatch.setattr(sp, "_is_sweepable_orphan_mcp", lambda pid, cmdline: pid == child)
            assert child in sp._our_orphan_pids()
            assert sp.find_orphan_mcp_candidates(set()) == [child]

    def test_a_tracked_orphan_is_killed_not_pruned(self, tmp_path, monkeypatch) -> None:
        """Its parent is read from bytes, so the sweep kills it instead of pruning a reused pid."""
        with renamed_child() as (child, _):
            me = os.getpid()
            pid_file = tmp_path / "kiro_pids.txt"
            token = platform_compat.get_process_start_id(child)
            pid_file.write_text(f"{child}:{me}:{token}\n", encoding="utf-8")
            monkeypatch.setattr(sp, "_pid_file_path", lambda: pid_file)
            monkeypatch.setattr(sp.platform_compat, "pid_exists", lambda pid: pid != me)
            kill = MagicMock()
            monkeypatch.setattr(sp.platform_compat, "kill_pid", kill)
            assert sp._cleanup_orphaned_mcp_servers() == 1
            kill.assert_called_once_with(child, platform_compat.SIGKILL)

    def test_the_supervisor_lists_and_signals_it(self) -> None:
        if not supervisor.can_reap():
            pytest.skip("signalling needs Linux pidfd support")
        with renamed_child() as (child, _):
            assert child in supervisor._linux_group_members(child)
            assert supervisor._signal_member(child, child, 0) is True  # signal 0 probes


class TestTheExitCheckOnAnUnreadableRoot:
    @pytest.fixture(autouse=True)
    def _linux(self, monkeypatch) -> None:
        monkeypatch.setattr(sp.sys, "platform", "linux")
        monkeypatch.setattr(sp.platform_compat, "pid_is_zombie", lambda pid: None)

    def test_present_but_unreadable_is_still_running(self, monkeypatch) -> None:
        monkeypatch.setattr(sp.platform_compat, "pid_exists", lambda pid: True)
        assert sp._pid_exited_but_unreaped(42) is False

    def test_gone_has_exited(self, monkeypatch) -> None:
        monkeypatch.setattr(sp.platform_compat, "pid_exists", lambda pid: False)
        assert sp._pid_exited_but_unreaped(42) is True


class TestFixtureTables:
    """The same readers over a fixture table, on any host."""

    def test_a_group_member_is_seen_and_an_exited_one_is_not(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sp.sys, "platform", "linux")
        write_stat(tmp_path, 101, pgrp=100, start_ticks=7)
        write_stat(tmp_path, 102, pgrp=100, state="Z")
        write_stat(tmp_path, 103, pgrp=100, state="X")
        write_stat(tmp_path, 104, pgrp=200)
        assert platform_compat.linux_pgroup_members(100, proc_root=tmp_path) == {101: 7}
        assert sp._pgroup_has_member_besides(100, 100, proc_root=tmp_path) is True
        assert sp._pgroup_has_member_besides(100, 101, proc_root=tmp_path) is False

    def test_a_leader_pid_reused_by_another_session_has_ended(self, tmp_path) -> None:
        """Only a present leader whose stat cannot be read is assumed alive.

        Session 0 is a real reading -- a kernel thread that reused the pid -- so
        it ends the session like any other session id would.
        """
        write_stat(tmp_path, 400, session=400)
        assert sp._linux_session_leader_alive(400, tmp_path) is True
        for reused_by in (0, 401):
            write_stat(tmp_path, 400, comm=b"kworker/0:1", session=reused_by)
            assert sp._linux_session_leader_alive(400, tmp_path) is False
        (tmp_path / "400" / "stat").write_bytes(b"400 (unparseable")
        assert sp._linux_session_leader_alive(400, tmp_path) is True
        assert sp._linux_session_leader_alive(402, tmp_path) is False

    def test_an_unlistable_table_holds_the_group(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sp.sys, "platform", "linux")
        missing = tmp_path / "absent"
        assert platform_compat.linux_pgroup_members(100, proc_root=missing) is None
        assert sp._pgroup_has_member_besides(100, 100, proc_root=missing) is True

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX page size")
    def test_the_rss_tree_reaches_past_the_name(self, tmp_path) -> None:
        """The watchdog sums root, renamed child and its leaf, not the root alone."""
        mib_pages = (1 << 20) // os.sysconf("SC_PAGE_SIZE")
        for pid, ppid, comm, mib in ((10, 1, b"root", 1), (11, 10, BAD_COMM, 2), (12, 11, b"l", 4)):
            write_stat(tmp_path, pid, ppid=ppid, comm=comm)
            (tmp_path / str(pid) / "statm").write_text(f"0 {mib * mib_pages} 0 0 0 0 0\n")
        child_map = sp._build_child_map(tmp_path)
        assert sp._rss_mb_from_tree(10, child_map, proc_root=tmp_path) == 7

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX uid and page size")
    def test_the_reconciler_tree_reaches_past_the_name(self, tmp_path, monkeypatch) -> None:
        page = os.sysconf("SC_PAGE_SIZE")
        write_stat(tmp_path, 10, ppid=1, comm=b"root", rss_pages=1)
        write_stat(tmp_path, 11, ppid=10, rss_pages=2)
        write_stat(tmp_path, 12, ppid=11, comm=b"leaf", rss_pages=4)
        monkeypatch.setattr(
            runtime_reconcile, "Path", lambda p: tmp_path if p == "/proc" else Path(p)
        )
        table = runtime_reconcile.same_uid_process_table()
        assert runtime_reconcile._tree_rss(10, table) == 7 * page

    def test_the_supervisor_parse_reads_bytes(self) -> None:
        line = b"123 (" + BAD_COMM + b") S 1 42 0"
        assert supervisor._proc_stat_group_member(line, 42)
        assert not supervisor._proc_stat_group_member(line.replace(b" S ", b" Z "), 42)
        assert not supervisor._proc_stat_group_member(b"123 (x) S 1 notanumber 0", 42)
        assert not supervisor._proc_stat_group_member(b"no closing paren 1 42 0", 42)

    def test_status_counters_are_read_past_a_name_that_is_not_ascii(self, tmp_path) -> None:
        """``データ`` is valid UTF-8 and still failed a strict ASCII read."""
        for name in ("データ".encode(), BAD_COMM):
            (tmp_path / "42").mkdir(exist_ok=True)
            (tmp_path / "42" / "status").write_bytes(
                b"Name:\t" + name + b"\nPPid:\t7\nThreads:\t3\nVmRSS:\t  2048 kB\n"
            )
            for label, value in (("PPid", 7), ("Threads", 3), ("VmRSS", 2048)):
                assert platform_compat.read_proc_status_int(42, label, proc_root=tmp_path) == value
        assert platform_compat.read_proc_status_int(42, "VmSwap", proc_root=tmp_path) is None
        assert platform_compat.read_proc_status_int(43, "PPid", proc_root=tmp_path) is None

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX clock ticks")
    def test_an_old_process_is_old(self, tmp_path, monkeypatch) -> None:
        """An unreadable age reads as young, which keeps a process inside the grace."""
        hz = os.sysconf("SC_CLK_TCK")
        write_stat(tmp_path, 77, start_ticks=10 * hz)
        (tmp_path / "uptime").write_text("1000.00 50.00\n")
        monkeypatch.setattr(sp.sys, "platform", "linux")
        assert sp._pid_age_seconds(77, str(tmp_path)) == pytest.approx(990.0)
        assert sp._linux_pid_age(77, tmp_path) == pytest.approx(990.0)
