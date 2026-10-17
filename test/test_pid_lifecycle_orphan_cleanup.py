"""Tests for the startup cleanup of a dead gateway's tracking-file entries.

``_cleanup_orphaned_mcp_servers`` and ``_cleanup_orphaned_sessions`` read the
two tracking files, reap what a dead gateway left running, and prune its lines.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pid_lifecycle_helpers import (  # noqa: F401 -- fixtures are requested by name
    _POSIX_ONLY,
    pid_file_fixture,
    session_pid_file_fixture,
)

from kiro_crew import platform_compat


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

    # A live pid whose mapping records a start token other than its live one names
    # the pid's PREVIOUS owner. ``os.getpid()`` is a live thread-group leader, so
    # these reach the live-pid arm; the start token is pinned so the verdict comes
    # from the recorded/live pair alone.

    @staticmethod
    def _live_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: str | None):
        from kiro_crew import session_pid_sig

        monkeypatch.setattr("kiro_crew.session_pid.config_dir", lambda: tmp_path)
        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda _pid: live)
        txt = tmp_path / f"session_pid_{os.getpid()}.txt"
        txt.write_text("previous-owner-session\nrecorded-token-aaa", encoding="utf-8")
        sig = txt.with_suffix(".sig")
        sig.write_text("00", encoding="utf-8")
        return session_pid_sig, txt, sig

    def test_a_live_recycled_mapping_is_retracted(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HEADLINE: a mapping the live pid's start token disproves is removed.

        The lenient client resolver cannot tell such a record from a planted one,
        so while it sits in a caller's ancestry that caller's walk is refused.
        """
        from kiro_crew.session_pid import _prune_stale_session_pid_files

        _, txt, sig = self._live_mapping(tmp_path, monkeypatch, "live-token-bbb")

        removed = _prune_stale_session_pid_files()

        assert removed == 1
        assert not txt.exists()
        assert not sig.exists()
        assert not list(tmp_path.glob(".session_pid_*")), "no private name may be left"

    @pytest.mark.parametrize(
        "live",
        ["recorded-token-aaa", None],
        ids=["token-matches", "live-token-unreadable"],
    )
    def test_a_live_mapping_without_proof_of_recycle_is_kept(
        self,
        tmp_path: Path,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        live: str | None,
    ) -> None:
        """Only a readable live token that DIFFERS retracts; unknown is not proof."""
        from kiro_crew.session_pid import _prune_stale_session_pid_files

        _, txt, sig = self._live_mapping(tmp_path, monkeypatch, live)

        assert _prune_stale_session_pid_files() == 0
        assert txt.exists()
        assert sig.exists()

    def test_the_boot_setting_does_not_retract_a_live_recycled_mapping(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The boot path carries no new work, so the live-pid arm is off there."""
        from kiro_crew.session_pid import _prune_stale_session_pid_files

        _, txt, _sig = self._live_mapping(tmp_path, monkeypatch, "live-token-bbb")

        assert _prune_stale_session_pid_files(narrow_with_leaders=False) == 0
        assert txt.exists()

    def test_a_mapping_republished_mid_retraction_is_kept(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pid's new owner republishing between the read and the move wins.

        The read saw the stale record, then the publisher replaced it. What the
        move picked up is the NEW mapping, so it must end up back at its path with
        its sidecar, not deleted as the stale one.
        """
        from kiro_crew import session_pid
        from kiro_crew.session_pid import _prune_stale_session_pid_files

        _, txt, sig = self._live_mapping(tmp_path, monkeypatch, "live-token-bbb")
        real_check = session_pid._is_recycled_record
        calls: list[Path] = []

        def _read_then_republish(pid, path, expected):
            verdict = real_check(pid, path, expected)
            if not calls:
                fresh = txt.with_name("republish.tmp")
                fresh.write_text("new-owner-session\nlive-token-bbb", encoding="utf-8")
                os.replace(fresh, txt)
                sig.write_text("11", encoding="utf-8")
            calls.append(path)
            return verdict

        monkeypatch.setattr(session_pid, "_is_recycled_record", _read_then_republish)

        assert _prune_stale_session_pid_files() == 0
        assert txt.read_text(encoding="utf-8") == "new-owner-session\nlive-token-bbb"
        assert sig.read_text(encoding="utf-8") == "11"
        assert not list(tmp_path.glob(".session_pid_*"))

    def test_an_interrupted_retraction_leftover_is_removed(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unowned leftover is settled: proven-stale deleted, anything else restored.

        A live process's leftover may be in flight and stays untouched.
        """
        from kiro_crew.session_pid import _prune_stale_session_pid_files

        _, txt, sig = self._live_mapping(tmp_path, monkeypatch, "live-token-bbb")
        txt.unlink()
        sig.unlink()
        me = os.getpid()
        dead_owner, live_owner = 4194303, 1
        stale = tmp_path / f".session_pid_{me}.txt.retract-{dead_owner}-aaaa"
        stale.write_text("previous-owner-session\nrecorded-token-aaa", encoding="utf-8")
        current = tmp_path / f".session_pid_{me}.txt.retract-{me}-bbbb"
        current.write_text("new-owner-session\nlive-token-bbb", encoding="utf-8")
        lone_sig = tmp_path / f".session_pid_424242.sig.retract-{dead_owner}-cccc"
        lone_sig.write_text("00", encoding="utf-8")
        foreign = tmp_path / f".session_pid_{me}.sig.retract-{live_owner}-dddd"
        foreign.write_text("11", encoding="utf-8")
        real_exists = platform_compat.pid_exists
        monkeypatch.setattr(
            platform_compat,
            "pid_exists",
            lambda pid: False if pid == dead_owner else real_exists(pid),
        )

        _prune_stale_session_pid_files()

        assert not stale.exists(), "a leftover that still proves the recycle is deleted"
        assert not current.exists()
        assert txt.read_text(encoding="utf-8") == "new-owner-session\nlive-token-bbb"
        assert not lone_sig.exists(), "a sidecar with no mapping beside it is never read"
        assert foreign.exists(), "a live process's leftover may be in flight"

    def test_a_sidecar_stays_while_another_pass_holds_its_mapping(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two passes interleaved: the current ``.sig`` survives while B holds the ``.txt``.

        Pass A retracted the stale ``.txt``; the publisher then wrote a current
        pair, and pass B (another process) moved the current ``.txt`` aside. When
        A reaches the sidecar the public ``.txt`` is empty, yet B will put it back,
        so the current ``.sig`` must stay.
        """
        from kiro_crew import session_pid
        from kiro_crew.session_pid import _prune_stale_session_pid_files

        _, txt, sig = self._live_mapping(tmp_path, monkeypatch, "live-token-bbb")
        held_by_b = tmp_path / f".session_pid_{os.getpid()}.txt.retract-1-bbbb"
        real_sidecar = session_pid._retract_sidecar

        def _publisher_then_pass_b(pid_file: Path) -> None:
            held_by_b.write_text("new-owner-session\nlive-token-bbb", encoding="utf-8")
            sig.write_text("44", encoding="utf-8")
            real_sidecar(pid_file)

        monkeypatch.setattr(session_pid, "_retract_sidecar", _publisher_then_pass_b)

        assert _prune_stale_session_pid_files() == 1
        assert sig.read_text(encoding="utf-8") == "44"
        assert not list(tmp_path.glob(f".session_pid_{os.getpid()}.sig.retract-*"))

    @pytest.mark.skipif(
        not hasattr(os, "symlink") or sys.platform == "win32", reason="POSIX symlinks"
    )
    @pytest.mark.parametrize("kind", ["txt", "sig"])
    def test_a_planted_symlink_leftover_is_removed_never_linked(
        self,
        tmp_path: Path,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        kind: str,
    ) -> None:
        """A symlink at a private name is unlinked; its target never gains a public name."""
        from kiro_crew.session_pid import _prune_stale_session_pid_files

        monkeypatch.setattr("kiro_crew.session_pid.config_dir", lambda: tmp_path)
        secret = tmp_path / "outside" / "secret.env"
        secret.parent.mkdir()
        secret.write_text("TOKEN=x", encoding="utf-8")
        me = os.getpid()
        public = tmp_path / f"session_pid_{me}.{kind}"
        if kind == "sig":
            (tmp_path / f"session_pid_{me}.txt").write_text("sess", encoding="utf-8")
        planted = tmp_path / f".session_pid_{me}.{kind}.retract-{me}-aaaa"
        planted.symlink_to(secret)

        _prune_stale_session_pid_files()

        assert not os.path.lexists(planted)
        assert not public.exists()
        assert os.stat(secret).st_nlink == 1
        assert secret.read_text(encoding="utf-8") == "TOKEN=x"

    def test_a_mapping_restored_mid_check_still_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restore landing between the public and private checks is still seen."""
        from kiro_crew.session_pid import _mapping_may_return

        txt = tmp_path / "session_pid_77.txt"
        private = tmp_path / ".session_pid_77.txt.retract-1-aaaa"
        private.write_text("x", encoding="utf-8")
        real_glob = Path.glob

        def _restore_then_glob(self, pattern):
            if self == tmp_path and private.exists():
                os.link(private, txt)
                private.unlink()
            return real_glob(self, pattern)

        monkeypatch.setattr(Path, "glob", _restore_then_glob)

        assert _mapping_may_return(txt) is True

    def test_a_sidecar_leftover_waits_for_its_mapping_leftover(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A paired current ``.txt`` and ``.sig`` left private both go back, whatever the order.

        Settling the sidecar first, while its mapping is still private, must keep
        it; the pass then restores the mapping and the sidecar beside it.
        """
        from kiro_crew.session_pid import (
            _prune_stale_session_pid_files,
            _settle_retraction_leftover,
        )

        _, txt, sig = self._live_mapping(tmp_path, monkeypatch, "live-token-bbb")
        txt.unlink()
        sig.unlink()
        me = os.getpid()
        txt_left = tmp_path / f".session_pid_{me}.txt.retract-{me}-aaaa"
        txt_left.write_text("new-owner-session\nlive-token-bbb", encoding="utf-8")
        sig_left = tmp_path / f".session_pid_{me}.sig.retract-{me}-bbbb"
        sig_left.write_text("33", encoding="utf-8")

        _settle_retraction_leftover(sig_left)
        assert sig_left.exists(), "the sidecar must wait while its mapping is private"

        _prune_stale_session_pid_files()

        assert txt.read_text(encoding="utf-8") == "new-owner-session\nlive-token-bbb"
        assert sig.read_text(encoding="utf-8") == "33"
        assert not list(tmp_path.glob(".session_pid_*"))

    def test_a_mapping_that_cannot_be_put_back_is_kept_not_deleted(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without hard links the moved mapping stays private, then the next pass restores it.

        Restoring by rename could replace a mapping published in the meantime, so
        the pass keeps the file under its private name instead.
        """
        from kiro_crew import session_pid
        from kiro_crew.session_pid import _prune_stale_session_pid_files

        _, txt, sig = self._live_mapping(tmp_path, monkeypatch, "live-token-bbb")
        real_check = session_pid._is_recycled_record
        calls: list[Path] = []

        def _read_then_republish(pid, path, expected):
            verdict = real_check(pid, path, expected)
            if not calls:
                fresh = txt.with_name("republish.tmp")
                fresh.write_text("new-owner-session\nlive-token-bbb", encoding="utf-8")
                os.replace(fresh, txt)
            calls.append(path)
            return verdict

        monkeypatch.setattr(session_pid, "_is_recycled_record", _read_then_republish)
        real_link = os.link

        def _no_links(*_a, **_k):
            raise PermissionError("hard links unsupported")

        monkeypatch.setattr(os, "link", _no_links)

        assert _prune_stale_session_pid_files() == 0
        assert not txt.exists()
        kept = list(tmp_path.glob(f".session_pid_{os.getpid()}.txt.retract-*"))
        assert len(kept) == 1
        assert kept[0].read_text(encoding="utf-8") == "new-owner-session\nlive-token-bbb"

        monkeypatch.setattr(os, "link", real_link)
        monkeypatch.setattr(session_pid, "_is_recycled_record", real_check)
        _prune_stale_session_pid_files()

        assert txt.read_text(encoding="utf-8") == "new-owner-session\nlive-token-bbb"
        assert not list(tmp_path.glob(".session_pid_*"))

    def test_a_sidecar_republished_mid_retraction_is_kept(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``.txt`` and ``.sig`` republished while the stale ``.txt`` is set aside stay.

        The publisher writes both after the stale record was moved, so the sidecar
        the sweep then finds is the new one and must not be removed.
        """
        from kiro_crew import session_pid
        from kiro_crew.session_pid import _prune_stale_session_pid_files

        _, txt, sig = self._live_mapping(tmp_path, monkeypatch, "live-token-bbb")
        real_sidecar = session_pid._retract_sidecar

        def _republish_then_retract(pid_file: Path) -> None:
            txt.write_text("new-owner-session\nlive-token-bbb", encoding="utf-8")
            sig.write_text("22", encoding="utf-8")
            real_sidecar(pid_file)

        monkeypatch.setattr(session_pid, "_retract_sidecar", _republish_then_retract)

        assert _prune_stale_session_pid_files() == 1
        assert txt.read_text(encoding="utf-8") == "new-owner-session\nlive-token-bbb"
        assert sig.read_text(encoding="utf-8") == "22"
        assert not list(tmp_path.glob(".session_pid_*"))

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
    def test_a_fifo_at_the_mapping_path_is_not_opened_blocking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A FIFO swapped in after the ``lstat`` must not park the sweep on ``open``."""
        from kiro_crew.session_pid import _is_recycled_record

        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda _pid: "live")
        regular = tmp_path / "vetted.txt"
        regular.write_text("previous-owner-session\nrecorded", encoding="utf-8")
        expected = os.lstat(regular)
        fifo = tmp_path / f"session_pid_{os.getpid()}.txt"
        os.mkfifo(fifo)

        box: dict[str, bool] = {}
        worker = threading.Thread(
            target=lambda: box.setdefault("v", _is_recycled_record(os.getpid(), fifo, expected)),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=5)
        if worker.is_alive():
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            worker.join(timeout=5)
            pytest.fail("the read blocked on a FIFO")
        assert box["v"] is False
        assert _is_recycled_record(os.getpid(), regular, expected) is True


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
