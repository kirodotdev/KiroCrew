"""Tests for PID sweep helpers in session.py."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.session_pid import (
    _kill_confirmed_and_writeback,
    _periodic_pid_sweep,
)


@pytest.fixture()
def session_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "kiro_session_pids.txt"
    monkeypatch.setattr("kiro_crew.session_pid._session_pid_file_path", lambda: p)
    return p


# ── _kill_pid_tree ──


class TestKillPidTree:
    def test_rejects_non_positive_pid(self) -> None:
        """pid <= 0 is catastrophic — must return immediately."""
        from kiro_crew.session_pid import _kill_pid_tree

        with patch("os.kill") as mock_kill:
            assert _kill_pid_tree(0) == (0, False)
            assert _kill_pid_tree(-1) == (0, False)
            mock_kill.assert_not_called()

    def test_returns_root_killed_true_on_success(self) -> None:
        from kiro_crew.session_pid import _kill_pid_tree

        kills: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kills.append((pid, sig))

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("os.kill", side_effect=fake_kill),
        ):
            total, root_killed = _kill_pid_tree(99999)

        assert total == 1
        assert root_killed is True
        assert (99999, signal.SIGKILL) in kills

    def test_returns_root_killed_false_when_not_kiro(self) -> None:
        from kiro_crew.session_pid import _kill_pid_tree

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=False),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
        ):
            total, root_killed = _kill_pid_tree(99999)

        assert total == 0
        assert root_killed is False

    def test_kills_children_bottom_up(self) -> None:
        from kiro_crew.session_pid import _kill_pid_tree

        kills: list[int] = []

        def fake_kill(pid: int, sig: int) -> None:
            kills.append(pid)

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[100, 200]),
            patch("os.kill", side_effect=fake_kill),
        ):
            total, root_killed = _kill_pid_tree(50)

        # Children reversed (200, 100), then root (50)
        assert kills == [200, 100, 50]
        assert total == 3
        assert root_killed is True

    def test_handles_already_dead_root(self) -> None:
        from kiro_crew.session_pid import _kill_pid_tree

        def fake_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError()

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("os.kill", side_effect=fake_kill),
        ):
            total, root_killed = _kill_pid_tree(99999)

        assert total == 0
        assert root_killed is False


# ── _sweep_pid_entries ──


class TestSweepPidEntries:
    def test_prunes_dead_pids(self) -> None:
        from kiro_crew.session_pid import _sweep_pid_entries

        def fake_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError()

        with patch("os.kill", side_effect=fake_kill):
            killed, dead, _ = _sweep_pid_entries(
                ["1:99999"],
                should_skip_tagged=lambda gw, p: False,
                should_skip_bare=lambda p: False,
            )

        assert "1:99999" in dead

    def test_skips_tagged_entries_per_predicate(self) -> None:
        from kiro_crew.session_pid import _sweep_pid_entries

        killed, dead, _ = _sweep_pid_entries(
            ["1:99999"],
            should_skip_tagged=lambda gw, p: True,  # skip all
            should_skip_bare=lambda p: False,
        )

        assert killed == 0
        assert len(dead) == 0

    def test_skips_bare_entries_per_predicate(self) -> None:
        from kiro_crew.session_pid import _sweep_pid_entries

        killed, dead, _ = _sweep_pid_entries(
            ["99999"],
            should_skip_tagged=lambda gw, p: False,
            should_skip_bare=lambda p: True,  # skip all bare
        )

        assert killed == 0
        assert len(dead) == 0

    def test_prunes_invalid_entries(self) -> None:
        from kiro_crew.session_pid import _sweep_pid_entries

        killed, dead, _ = _sweep_pid_entries(
            ["not_a_pid", "abc:def"],
            should_skip_tagged=lambda gw, p: False,
            should_skip_bare=lambda p: False,
        )

        assert "not_a_pid" in dead
        assert "abc:def" in dead

    def test_rejects_non_positive_pids(self) -> None:
        """pid <= 0 is catastrophic for os.kill — must be pruned immediately."""
        from kiro_crew.session_pid import _sweep_pid_entries

        with patch("os.kill") as mock_kill:
            killed, dead, _ = _sweep_pid_entries(
                ["0", "-1", "1:0", "0:100", "1:-1"],
                should_skip_tagged=lambda gw, p: False,
                should_skip_bare=lambda p: False,
            )

            assert "0" in dead
            assert "-1" in dead
            assert "1:0" in dead
            assert "0:100" in dead
            assert "1:-1" in dead
            mock_kill.assert_not_called()

    def test_skips_managed_pids(self) -> None:
        from kiro_crew.session_pid import _sweep_pid_entries

        def fake_kill(pid: int, sig: int) -> None:
            pass  # alive

        with patch("os.kill", side_effect=fake_kill):
            killed, dead, _ = _sweep_pid_entries(
                ["1:99999"],
                should_skip_tagged=lambda gw, p: False,
                should_skip_bare=lambda p: False,
                is_managed=lambda p: True,  # all managed
            )

        assert killed == 0
        assert len(dead) == 0

    def test_prunes_non_kiro_alive_pids(self) -> None:
        from kiro_crew.session_pid import _sweep_pid_entries

        def fake_kill(pid: int, sig: int) -> None:
            pass  # alive

        with (
            patch("os.kill", side_effect=fake_kill),
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=False),
        ):
            killed, dead, _ = _sweep_pid_entries(
                ["1:99999"],
                should_skip_tagged=lambda gw, p: False,
                should_skip_bare=lambda p: False,
            )

        assert "1:99999" in dead

    def test_permission_error_skips_entry(self) -> None:
        """PermissionError on liveness probe means alive but owned by another user — skip."""
        from kiro_crew.session_pid import _sweep_pid_entries

        def fake_kill(pid: int, sig: int) -> None:
            raise PermissionError()

        with patch("os.kill", side_effect=fake_kill):
            killed, dead, _ = _sweep_pid_entries(
                ["1:99999"],
                should_skip_tagged=lambda gw, p: False,
                should_skip_bare=lambda p: False,
            )

        assert killed == 0
        assert "1:99999" not in dead  # NOT removed

    def test_kills_alive_orphaned_kiro_pid(self) -> None:
        """Exercises the successful-kill branch: alive, not managed, is kiro."""
        from kiro_crew.session_pid import _sweep_pid_entries

        with (
            patch("os.kill"),  # signal-0 (alive) and SIGKILL both succeed
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
        ):
            killed, dead, _ = _sweep_pid_entries(
                ["1:99999"],
                should_skip_tagged=lambda gw, p: False,
                should_skip_bare=lambda p: False,
            )

        assert killed == 1
        assert "1:99999" in dead

    def test_reprobe_prunes_when_root_not_killed_but_dead(self) -> None:
        """root_killed=False + re-probe ProcessLookupError → entry pruned."""
        from kiro_crew.session_pid import _sweep_pid_entries

        call_count = 0

        def fake_kill(pid: int, sig: int) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return  # liveness probe: alive
            raise ProcessLookupError()  # re-probe: dead

        with (
            patch("os.kill", side_effect=fake_kill),
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._kill_pid_tree", return_value=(1, False)),
        ):
            killed, dead, _ = _sweep_pid_entries(
                ["1:99999"],
                should_skip_tagged=lambda gw, p: False,
                should_skip_bare=lambda p: False,
            )

        assert killed == 1
        assert "1:99999" in dead

    def test_reprobe_keeps_entry_when_root_not_killed_and_alive(self) -> None:
        """root_killed=False + re-probe alive → entry kept for retry."""
        from kiro_crew.session_pid import _sweep_pid_entries

        with (
            patch("os.kill"),  # all probes succeed (alive)
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._kill_pid_tree", return_value=(1, False)),
        ):
            killed, dead, _ = _sweep_pid_entries(
                ["1:99999"],
                should_skip_tagged=lambda gw, p: False,
                should_skip_bare=lambda p: False,
            )

        assert killed == 1
        assert "1:99999" not in dead  # kept for next sweep


# ── _write_back_pid_file ──


class TestWriteBackPidFile:
    def test_removes_killed_entries(self, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _write_back_pid_file

        session_pid_file.write_text("1:100\n1:200\n1:300\n")
        _write_back_pid_file({"1:200"})

        content = session_pid_file.read_text()
        assert "1:100" in content
        assert "1:200" not in content
        assert "1:300" in content

    def test_empties_file_when_all_removed(self, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _write_back_pid_file

        session_pid_file.write_text("1:100\n")
        _write_back_pid_file({"1:100"})

        assert session_pid_file.read_text() == ""


# ── _periodic_pid_sweep ──


class TestPeriodicPidSweep:
    def test_only_sweeps_own_gateway_entries(self, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _periodic_pid_sweep

        my_gw = os.getpid()
        other_gw = my_gw + 1  # guaranteed different from my_gw
        session_pid_file.write_text(f"{my_gw}:99999\n{other_gw}:88888\n")

        def fake_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError()  # all dead

        with patch("os.kill", side_effect=fake_kill):
            killed_or_dead, candidates = _periodic_pid_sweep(my_gw, set())

        # Own gateway's dead entry identified, other gateway's preserved
        assert f"{my_gw}:99999" in killed_or_dead
        assert f"{other_gw}:88888" not in killed_or_dead
        assert candidates == []  # dead PIDs are not candidates

    def test_skips_active_pids(self, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _periodic_pid_sweep

        my_gw = os.getpid()
        session_pid_file.write_text(f"{my_gw}:99999\n")

        def fake_kill(pid: int, sig: int) -> None:
            pass  # alive

        with patch("os.kill", side_effect=fake_kill):
            killed_or_dead, candidates = _periodic_pid_sweep(my_gw, {99999})  # active

        assert len(killed_or_dead) == 0
        assert 99999 not in candidates  # managed — not a candidate

    def test_skips_bare_entries(self, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _periodic_pid_sweep

        session_pid_file.write_text("99999\n")

        killed_or_dead, candidates = _periodic_pid_sweep(os.getpid(), set())

        # Bare entries skipped (startup handles them)
        assert len(killed_or_dead) == 0
        assert candidates == []

    def test_returns_candidates_for_orphaned_pids(self, session_pid_file: Path) -> None:
        """Alive, unmanaged, kiro-cli PIDs become candidates (not killed in phase 1)."""
        from kiro_crew.session_pid import _periodic_pid_sweep

        my_gw = os.getpid()
        session_pid_file.write_text(f"{my_gw}:99999\n")

        def fake_kill(pid: int, sig: int) -> None:
            pass  # alive

        with (
            patch("os.kill", side_effect=fake_kill),
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
        ):
            killed_or_dead, candidates = _periodic_pid_sweep(my_gw, set())

        assert 99999 in candidates
        assert f"{my_gw}:99999" not in killed_or_dead  # not pruned yet — deferred to phase 2


# ── _kill_confirmed_and_writeback ──


class TestKillConfirmedAndWriteback:
    def test_kills_confirmed_and_prunes_entries(self, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _kill_confirmed_and_writeback

        session_pid_file.write_text("1:99999\n1:88888\n")

        with (
            patch("kiro_crew.session_pid._kill_pid_tree", return_value=(1, True)),
            patch("kiro_crew.session_pid._write_back_pid_file") as mock_wb,
        ):
            killed = _kill_confirmed_and_writeback(1, [99999], set())

        assert killed == 1
        mock_wb.assert_called_once()
        assert "1:99999" in mock_wb.call_args[0][0]

    def test_keeps_entry_on_kill_failure(self, session_pid_file: Path) -> None:
        """root_killed=False and process still alive → entry not pruned."""
        from kiro_crew.session_pid import _kill_confirmed_and_writeback

        with (
            patch("kiro_crew.session_pid._kill_pid_tree", return_value=(0, False)),
            patch("os.kill"),  # re-probe: still alive
            patch("kiro_crew.session_pid._write_back_pid_file") as mock_wb,
        ):
            killed = _kill_confirmed_and_writeback(1, [99999], set())

        assert killed == 0
        # Entry not added to killed_or_dead, but writeback not called (empty set)
        mock_wb.assert_not_called()

    def test_no_writeback_when_nothing_to_prune(self) -> None:
        from kiro_crew.session_pid import _kill_confirmed_and_writeback

        with patch("kiro_crew.session_pid._write_back_pid_file") as mock_wb:
            killed = _kill_confirmed_and_writeback(1, [], set())

        assert killed == 0
        mock_wb.assert_not_called()


# ── Integration tests for _run_periodic_tasks sweep pipeline (L1574-L1628) ──


class TestPeriodicSweepIntegration:
    """Integration tests covering the Phase 1 → 2a → 2b orchestration
    inside SessionManager._cleanup_loop's orphan sweep block."""

    def _make_session(self, pid: int | str = 12345):
        """Create a mock session with provider.client._pid."""
        sess = MagicMock()
        sess.provider.client._pid = pid
        return sess

    @pytest.mark.asyncio
    async def test_happy_path_kills_orphan(self, session_pid_file: Path) -> None:
        """Full pipeline: Phase 1 finds candidate → 2a confirms → 2b kills."""
        my_gw = os.getpid()
        orphan_pid = 99999
        session_pid_file.write_text(f"{my_gw}:{orphan_pid}\n")

        with (
            patch("os.kill"),
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
        ):
            # Phase 1: identify candidates
            killed_or_dead, candidates = _periodic_pid_sweep(my_gw, set())
            assert orphan_pid in candidates

            # Phase 2a: re-check against live sessions (none → confirmed)
            confirmed = [p for p in candidates if p not in set()]
            assert orphan_pid in confirmed

            # Phase 2b: kill and writeback
            orphan_killed = _kill_confirmed_and_writeback(my_gw, confirmed, killed_or_dead)
            assert orphan_killed == 1

        # PID file should be cleaned
        content = session_pid_file.read_text()
        assert f"{my_gw}:{orphan_pid}" not in content

    @pytest.mark.asyncio
    async def test_skip_sweep_when_pid_not_int(self) -> None:
        """_collect_active_pids returns ok=False when PID is not an int."""
        from kiro_crew.session_pid import _collect_active_pids

        sess = self._make_session(pid="not_an_int")
        pids, ok = _collect_active_pids({"s1": sess})
        assert ok is False
        assert len(pids) == 0

    @pytest.mark.asyncio
    async def test_phase2_safe_false_on_pid_extraction_failure(self) -> None:
        """_collect_active_pids returns ok=False when _pid attr missing."""
        from kiro_crew.session_pid import _collect_active_pids

        sess = MagicMock()
        sess.provider.client = MagicMock(spec=[])  # no _pid attr
        pids, ok = _collect_active_pids({"s1": sess})
        assert ok is False

    @pytest.mark.asyncio
    async def test_managed_pid_not_killed(self, session_pid_file: Path) -> None:
        """Active session PID is not killed even if in PID file."""
        my_gw = os.getpid()
        managed_pid = 88888
        session_pid_file.write_text(f"{my_gw}:{managed_pid}\n")

        with patch("os.kill"):  # alive
            killed_or_dead, candidates = _periodic_pid_sweep(my_gw, {managed_pid})

        assert managed_pid not in candidates
        assert len(killed_or_dead) == 0

    @pytest.mark.asyncio
    async def test_catch_all_exception_does_not_crash(self, session_pid_file: Path) -> None:
        """The except Exception catch-all at L1628 prevents crashes."""
        my_gw = os.getpid()
        session_pid_file.write_text(f"{my_gw}:99999\n")

        with patch(
            "kiro_crew.session_pid._periodic_pid_sweep",
            side_effect=RuntimeError("boom"),
        ) as mock_sweep:
            try:
                await asyncio.to_thread(mock_sweep, my_gw, set())
            except Exception:
                pass  # L1628: catch-all — should not propagate
            mock_sweep.assert_called_once()


class TestProtectedPidSweepShield:
    """Regression (PR #21 HIGH): shared _bg / subagent AcpRuntime PIDs are
    registered as sweep-protected so _collect_active_pids treats them as active
    and the periodic orphan sweep does not SIGKILL them mid-use."""

    def test_collect_active_pids_includes_protected(self) -> None:
        from kiro_crew.session_pid import (
            _collect_active_pids,
            register_protected_pid,
            unregister_protected_pid,
        )

        register_protected_pid(424242)
        try:
            pids, ok = _collect_active_pids({})
            assert ok is True
            assert 424242 in pids
        finally:
            unregister_protected_pid(424242)

    def test_unregister_removes_protection(self) -> None:
        from kiro_crew.session_pid import (
            _collect_active_pids,
            register_protected_pid,
            unregister_protected_pid,
        )

        register_protected_pid(424243)
        unregister_protected_pid(424243)
        pids, _ok = _collect_active_pids({})
        assert 424243 not in pids


# ── Spawn grace period tests (Fix A) ──────────────────────────────────────────


class TestPidAgeSeconds:
    """Tests for _pid_age_seconds using a fake proc_root."""

    def test_young_pid_age_below_grace(self, tmp_path: Path) -> None:
        """A process started recently returns a small age value."""
        from kiro_crew.session_pid import _pid_age_seconds

        # Simulate a process that started 30 seconds ago.
        # We need: uptime, and starttime_ticks such that age = 30s.
        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime_seconds = 100000.0  # system uptime
        age_desired = 30.0
        starttime_ticks = int((uptime_seconds - age_desired) * clk_tck)

        # Create fake /proc/<pid>/stat
        proc_dir = tmp_path / "42"
        proc_dir.mkdir()
        # comm with spaces and parens: "(kiro cli (v2))"
        stat_line = (
            f"42 (kiro cli (v2)) S 1 42 42 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 "
            f"{starttime_ticks} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        )
        (proc_dir / "stat").write_text(stat_line)
        # Fake /proc/uptime
        (tmp_path / "uptime").write_text(f"{uptime_seconds} 50000.0\n")

        # Capture a stable "now" so the age calculation is consistent with the
        # fake uptime we wrote above.
        now = time.time()

        # Call with patched time so calculation is consistent
        with patch("time.time", return_value=now):
            age = _pid_age_seconds(42, proc_root=str(tmp_path))

        assert age is not None
        assert abs(age - age_desired) < 1.0  # within 1s tolerance

    def test_old_pid_age_above_grace(self, tmp_path: Path) -> None:
        """A process started long ago returns a large age value."""
        from kiro_crew.session_pid import _pid_age_seconds

        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime_seconds = 100000.0
        age_desired = 300.0  # 5 minutes — well above 120s grace
        starttime_ticks = int((uptime_seconds - age_desired) * clk_tck)

        proc_dir = tmp_path / "100"
        proc_dir.mkdir()
        stat_line = (
            f"100 (kiro-cli) S 1 100 100 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 "
            f"{starttime_ticks} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        )
        (proc_dir / "stat").write_text(stat_line)
        (tmp_path / "uptime").write_text(f"{uptime_seconds} 50000.0\n")

        now = time.time()
        with patch("time.time", return_value=now):
            age = _pid_age_seconds(100, proc_root=str(tmp_path))

        assert age is not None
        assert age > 200.0  # well above grace threshold

    def test_proc_parse_failure_returns_none(self, tmp_path: Path) -> None:
        """/proc read failure → None (treat as young in caller)."""
        from kiro_crew.session_pid import _pid_age_seconds

        # No /proc/<pid>/stat file exists
        age = _pid_age_seconds(99999, proc_root=str(tmp_path))
        assert age is None

    def test_comm_with_spaces_and_parens(self, tmp_path: Path) -> None:
        """comm field '(Web Content (Manager))' with spaces+parens parses correctly."""
        from kiro_crew.session_pid import _pid_age_seconds

        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime_seconds = 50000.0
        age_desired = 60.0
        starttime_ticks = int((uptime_seconds - age_desired) * clk_tck)

        proc_dir = tmp_path / "777"
        proc_dir.mkdir()
        # Adversarial comm: nested parens and spaces
        stat_line = (
            f"777 (Web Content (Manager)) S 1 777 777 0 -1 0 0 0 0 0 0 0 0 0 "
            f"20 0 1 0 {starttime_ticks} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        )
        (proc_dir / "stat").write_text(stat_line)
        (tmp_path / "uptime").write_text(f"{uptime_seconds} 25000.0\n")

        now = time.time()
        with patch("time.time", return_value=now):
            age = _pid_age_seconds(777, proc_root=str(tmp_path))

        assert age is not None
        assert abs(age - age_desired) < 1.0

    def test_non_linux_returns_none(self) -> None:
        """On non-Linux, _pid_age_seconds returns None immediately."""
        from kiro_crew.session_pid import _pid_age_seconds

        with patch("kiro_crew.session_pid.sys.platform", "darwin"):
            assert _pid_age_seconds(1234) is None


class TestPidInSpawnGrace:
    """Tests for _pid_in_spawn_grace helper."""

    def test_non_linux_returns_false(self) -> None:
        """Non-Linux: grace not applicable, returns False (sweep proceeds)."""
        from kiro_crew.session_pid import _pid_in_spawn_grace

        with patch("kiro_crew.session_pid.sys.platform", "darwin"):
            assert _pid_in_spawn_grace(12345) is False

    def test_linux_young_pid_returns_true(self) -> None:
        """Linux + age < 120s → True (skip the kill)."""
        from kiro_crew.session_pid import _pid_in_spawn_grace

        with patch("kiro_crew.session_pid._pid_age_seconds", return_value=30.0):
            assert _pid_in_spawn_grace(12345) is True

    def test_linux_old_pid_returns_false(self) -> None:
        """Linux + age > 120s → False (proceed with kill)."""
        from kiro_crew.session_pid import _pid_in_spawn_grace

        with patch("kiro_crew.session_pid._pid_age_seconds", return_value=200.0):
            assert _pid_in_spawn_grace(12345) is False

    def test_linux_parse_failure_returns_true(self) -> None:
        """Linux + age=None (parse failure) → True (safe direction: skip kill)."""
        from kiro_crew.session_pid import _pid_in_spawn_grace

        with patch("kiro_crew.session_pid._pid_age_seconds", return_value=None):
            assert _pid_in_spawn_grace(12345) is True


class TestSweepGraceIntegration:
    """Integration tests: grace period in the sweep pipeline."""

    def test_young_pid_not_selected_as_candidate(self, session_pid_file: Path) -> None:
        """A tracked PID younger than 120s is NOT selected as orphan candidate."""
        from kiro_crew.session_pid import _periodic_pid_sweep

        my_gw = os.getpid()
        session_pid_file.write_text(f"{my_gw}:99999\n")

        with (
            patch("os.kill"),  # alive
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=True),
        ):
            killed_or_dead, candidates = _periodic_pid_sweep(my_gw, set())

        assert 99999 not in candidates
        assert f"{my_gw}:99999" not in killed_or_dead

    def test_old_pid_selected_as_candidate(self, session_pid_file: Path) -> None:
        """A tracked PID older than 120s IS selected (regression guard)."""
        from kiro_crew.session_pid import _periodic_pid_sweep

        my_gw = os.getpid()
        session_pid_file.write_text(f"{my_gw}:99999\n")

        with (
            patch("os.kill"),  # alive
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
        ):
            killed_or_dead, candidates = _periodic_pid_sweep(my_gw, set())

        assert 99999 in candidates

    def test_dead_pid_pruned_regardless_of_grace(self, session_pid_file: Path) -> None:
        """Dead PIDs are pruned (no /proc entry) — grace gate is never reached."""
        from kiro_crew.session_pid import _periodic_pid_sweep

        my_gw = os.getpid()
        session_pid_file.write_text(f"{my_gw}:99999\n")

        def fake_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError()  # dead

        with patch("os.kill", side_effect=fake_kill):
            killed_or_dead, candidates = _periodic_pid_sweep(my_gw, set())

        # Dead entry is pruned — grace is irrelevant for dead processes
        assert f"{my_gw}:99999" in killed_or_dead
        assert 99999 not in candidates

    def test_non_linux_old_orphan_still_killed(self, session_pid_file: Path) -> None:
        """On non-Linux, grace is not applied — old orphans are still killed."""
        from kiro_crew.session_pid import _sweep_pid_entries

        with (
            patch("os.kill"),  # alive
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            # _pid_in_spawn_grace returns False on non-linux
            patch("kiro_crew.session_pid.sys.platform", "darwin"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
        ):
            killed, dead, _ = _sweep_pid_entries(
                ["1:99999"],
                should_skip_tagged=lambda gw, p: False,
                should_skip_bare=lambda p: False,
            )

        assert killed == 1
        assert "1:99999" in dead
