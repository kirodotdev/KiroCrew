"""The session tree keeps ONE ``$KIROCREW_SCRATCH`` across every process it spans.

The scratch root is masked for every sandboxed process and each spawn is handed
back only its own directory, so before this a dedicated subagent process, a
companion runtime and a recycled runtime's successor each got an EMPTY
``$KIROCREW_SCRATCH``: a brief the parent staged there was unreadable to the
child, and a runtime recycled for RSS mid-task lost the sessions' files. These
tests pin the four seams that close that: ``agent_scratch`` (env + window
validation), the two spawners (a second private window, the shared name), the
spawn-on-behalf-of paths (companion kwargs, dedicated kwargs, warm-pool bypass)
and the ``_bg`` successor (inherit + adopt).
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import agent_scratch as sc

#: A pid no live process can own on any supported platform (above every pid_max),
#: so a cleanup path that signals a fake reaches nothing on the runner.
_UNALLOCATABLE_PID = 99_999_999_999

# The dedicated-path tests drive ``SubagentManager`` spawn machinery, which
# refuses a spawn on a memory-pressured runner; pin the host reading so the
# test asserts on the seam, not on the runner (test_subagent_spawn_host_pin).
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture
def scratch_root(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sc, "config_dir", lambda: home)
    return home / "scratch"


class TestScratchEnvShared:
    def test_shared_names_the_tree_dir_and_keeps_temp_and_log_private(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(sc, "_CAN_CAP_LOGS", True)
        own = tmp_path / "own"
        shared = tmp_path / "tree"
        env = sc.scratch_env(own, shared=shared)
        # The prompt-visible name is the TREE's; temp files and the kiro-cli
        # log stay per-process, because two live processes appending to one
        # log is the sharing the pin exists to prevent.
        assert env["KIROCREW_SCRATCH"] == str(shared)
        assert {env["TMPDIR"], env["TMP"], env["TEMP"]} == {str(own)}
        assert env["KIRO_CHAT_LOG_FILE"] == str(own / "kiro-log" / "kiro-chat.log")

    def test_no_shared_is_the_first_process_shape(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sc, "_CAN_CAP_LOGS", False)
        assert sc.scratch_env(tmp_path) == sc.scratch_env(tmp_path, shared=None)
        assert sc.scratch_env(tmp_path)["KIROCREW_SCRATCH"] == str(tmp_path)


class TestSharedScratchWindow:
    def test_a_live_allocation_under_the_root_is_returned(self, scratch_root: Path) -> None:
        parent = sc.allocate_scratch("parent")
        assert sc.shared_scratch_window(parent) == parent

    def test_none_is_none(self, scratch_root: Path) -> None:
        assert sc.shared_scratch_window(None) is None

    def test_a_swept_allocation_is_dropped_not_recreated(self, scratch_root: Path) -> None:
        parent = sc.allocate_scratch("parent")
        shutil.rmtree(parent)
        assert sc.shared_scratch_window(parent) is None
        assert not parent.exists()

    def test_a_failed_refresh_refuses_only_a_tree_the_sweep_could_take(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """Without the mtime hold a dead-owner tree can be swept under the mount,
        so it is refused; a live-owner or unowned tree is never swept and is
        still handed out."""
        dead = sc.allocate_scratch("runtime")
        sc.record_owner(dead, 1001)
        live = sc.allocate_scratch("runtime")
        sc.record_owner(live, 1002)
        unowned = sc.allocate_scratch("runtime")
        (unowned / sc.OWNER_FILENAME).unlink()
        monkeypatch.setattr(sc, "_pgroup_alive", lambda pid: pid == 1002)

        def refuse(*args, **kwargs):
            raise PermissionError("utime refused")

        monkeypatch.setattr(sc.os, "utime", refuse)
        assert sc.shared_scratch_window(dead) is None
        assert sc.shared_scratch_window(live) == live
        assert sc.shared_scratch_window(unowned) == unowned
        assert dead.exists()  # refused, not removed

    def test_a_file_at_the_name_is_refused(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        bogus = scratch_root / "parent-deadbeef"
        bogus.write_text("not a dir", encoding="utf-8")
        assert sc.shared_scratch_window(bogus) is None

    def test_a_link_under_the_root_is_never_followed(
        self, scratch_root: Path, tmp_path: Path
    ) -> None:
        scratch_root.mkdir(parents=True)
        target = tmp_path / "elsewhere"
        target.mkdir()
        link = scratch_root / "parent-cafebabe"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
        assert sc.shared_scratch_window(link) is None

    def test_a_directory_outside_the_root_is_refused(
        self, scratch_root: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        assert sc.shared_scratch_window(outside) is None
        nested = scratch_root / "a" / "b"
        nested.mkdir(parents=True)
        assert sc.shared_scratch_window(nested) is None


class TestAdoptOwner:
    """A successor joins the marker beside its draining predecessor."""

    def test_adoption_names_both_and_the_sweep_keeps_the_tree_while_either_lives(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        alive = {1001, 2002}
        monkeypatch.setattr(sc, "_pgroup_alive", lambda pid: pid in alive)
        tree = sc.allocate_scratch("runtime")
        sc.record_owner(tree, 1001)  # the predecessor, still draining
        assert sc.adopt_owner(tree, 2002) == "recorded"
        assert sc._read_owner_pids(tree / sc.OWNER_FILENAME) == (1001, 2002)

        alive.discard(1001)
        old = time.time() - 2 * sc._UNOWNED_GRACE_SECONDS
        for entry in (tree, tree / sc.OWNER_FILENAME):
            os.utime(entry, (old, old))
        # Predecessor dead, successor alive: kept. This is the case a REPLACED
        # marker gets wrong in one direction or the other.
        assert sc.sweep_dead_scratch() == 0 and tree.exists()
        alive.clear()
        alive.add(1001)
        assert sc.sweep_dead_scratch() == 0 and tree.exists()
        alive.clear()
        assert sc.sweep_dead_scratch() == 1 and not tree.exists()

    def test_adopting_twice_is_idempotent(self, scratch_root: Path, monkeypatch) -> None:
        monkeypatch.setattr(sc, "_pgroup_alive", lambda pid: True)
        tree = sc.allocate_scratch("runtime")
        sc.record_owner(tree, 1001)
        assert sc.adopt_owner(tree, 2002) == "recorded"
        assert sc.adopt_owner(tree, 2002) == "recorded"
        assert sc._read_owner_pids(tree / sc.OWNER_FILENAME) == (1001, 2002)

    def test_dead_pids_are_pruned_on_adoption_so_a_chained_tree_stays_bounded(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        tree = sc.allocate_scratch("runtime")
        sc.record_owner(tree, 1001)
        alive = {1001}
        monkeypatch.setattr(sc, "_pgroup_alive", lambda pid: pid in alive)
        assert sc.adopt_owner(tree, 2002) == "recorded"
        alive = {2002}  # the predecessor drained and exited
        assert sc.adopt_owner(tree, 3003) == "recorded"
        assert sc._read_owner_pids(tree / sc.OWNER_FILENAME) == (2002, 3003)

    def test_concurrent_adopters_both_land_in_the_marker(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """Siblings of one fan-out adopt the same tree from executor threads."""
        import threading

        monkeypatch.setattr(sc, "_pgroup_alive", lambda pid: True)
        tree = sc.allocate_scratch("parent")
        sc.record_owner(tree, 1001)
        pids = list(range(5000, 5040))
        threads = [threading.Thread(target=sc.adopt_owner, args=(tree, pid)) for pid in pids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert set(sc._read_owner_pids(tree / sc.OWNER_FILENAME)) == {1001, *pids}

    def test_a_linked_marker_is_refused(self, scratch_root: Path, tmp_path: Path) -> None:
        tree = sc.allocate_scratch("runtime")
        marker = tree / sc.OWNER_FILENAME
        marker.unlink()
        target = tmp_path / "elsewhere"
        target.write_text("1", encoding="utf-8")
        try:
            marker.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
        assert sc.adopt_owner(tree, 2002) == "refused"
        assert target.read_text(encoding="utf-8") == "1"

    def test_a_marker_linked_to_an_endless_device_is_never_read(self, scratch_root: Path) -> None:
        """The marker is in the agent's own dir; an unbounded read of it would
        hand that process the gateway's memory."""
        tree = sc.allocate_scratch("runtime")
        marker = tree / sc.OWNER_FILENAME
        marker.unlink()
        try:
            marker.symlink_to(Path("/dev/zero"))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
        assert sc.adopt_owner(tree, 2002) == "refused"
        assert marker.is_symlink()  # untouched, not replaced, not followed

    def test_an_oversized_or_garbled_marker_is_garbled_not_rewritten(
        self, scratch_root: Path
    ) -> None:
        """Distinct from a planted link (``"refused"``): a garbled marker steers
        nothing, and the tree it sits in is one the sweep skips for good, so
        the spawner may proceed -- a fatal answer would let any agent process
        the tree is mounted into veto every later spawn on it by scribbling
        over the marker."""
        tree = sc.allocate_scratch("runtime")
        marker = tree / sc.OWNER_FILENAME
        marker.write_bytes(b"1\n" * (sc._OWNER_MARKER_MAX_BYTES + 8))
        assert sc.adopt_owner(tree, 2002) == "garbled"
        assert marker.stat().st_size > sc._OWNER_MARKER_MAX_BYTES  # left for a human
        marker.write_text("not a pid", encoding="utf-8")
        assert sc.adopt_owner(tree, 2002) == "garbled"
        assert marker.read_text(encoding="utf-8") == "not a pid"
        # The sweep reads the same bounded way and leaves both for a human.
        old = time.time() - 2 * sc._UNOWNED_GRACE_SECONDS
        for entry in (tree, marker):
            os.utime(entry, (old, old))
        assert sc.sweep_dead_scratch() == 0 and tree.exists()

    def test_a_pid_no_process_can_have_is_refused_not_probed(self, scratch_root: Path) -> None:
        """``os.killpg`` raises OverflowError past the C int, which is not the
        OSError the liveness probe handles: an agent-written marker must not
        reach it."""
        tree = sc.allocate_scratch("runtime")
        marker = tree / sc.OWNER_FILENAME
        for bad in ("99999999999999999999", "0", "-5", f"{sc._PID_MAX + 1}"):
            marker.write_text(bad, encoding="utf-8")
            assert sc.adopt_owner(tree, 2002) == "garbled", bad
            assert marker.read_text(encoding="utf-8") == bad
        marker.write_text(str(sc._PID_MAX), encoding="utf-8")
        assert sc._read_owner_pids(marker) == (sc._PID_MAX,)

    def test_an_unowned_tree_stays_unowned_when_joined(self, scratch_root: Path) -> None:
        """A marker naming only the joiner would make the allocator's tree
        reclaimable the hour after the joiner exits; unowned is never swept."""
        tree = sc.allocate_scratch("runtime")
        (tree / sc.OWNER_FILENAME).unlink()
        assert sc.adopt_owner(tree, 2002) == "recorded"
        assert not (tree / sc.OWNER_FILENAME).exists()

    def test_a_failed_adoption_leaves_the_tree_unowned_not_predecessor_owned(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        tree = sc.allocate_scratch("runtime")
        sc.record_owner(tree, 1001)

        def boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(sc, "atomic_write", boom)
        assert sc.adopt_owner(tree, 2002) == "unwritable"
        # Unowned is never swept; predecessor-only would be swept the hour
        # after the predecessor exits, under the live successor.
        assert not (tree / sc.OWNER_FILENAME).exists()

    def test_the_sweep_still_reclaims_a_single_dead_owner(self, scratch_root: Path, monkeypatch):
        tree = sc.allocate_scratch("runtime")
        sc.record_owner(tree, 1001)
        monkeypatch.setattr(sc, "_pgroup_alive", lambda pid: False)
        old = time.time() - 2 * sc._UNOWNED_GRACE_SECONDS
        for entry in (tree, tree / sc.OWNER_FILENAME):
            os.utime(entry, (old, old))
        assert sc.sweep_dead_scratch() == 1

    def test_a_marker_that_is_not_utf8_is_garbled_not_fatal(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """``UnicodeDecodeError`` is a ``ValueError``: the sweep skips the tree
        like any garbled marker and still reclaims the dead tree beside it;
        adoption refuses it rather than rewriting it."""
        garbled = sc.allocate_scratch("runtime")
        (garbled / sc.OWNER_FILENAME).write_bytes(b"\xff\xfe1001\n")
        dead = sc.allocate_scratch("runtime")
        sc.record_owner(dead, 1002)
        monkeypatch.setattr(sc, "_pgroup_alive", lambda pid: False)
        old = time.time() - 2 * sc._UNOWNED_GRACE_SECONDS
        for tree in (garbled, dead):
            for entry in (tree, tree / sc.OWNER_FILENAME):
                os.utime(entry, (old, old))

        assert sc.sweep_dead_scratch() == 1
        assert garbled.exists() and not dead.exists()
        assert sc.adopt_owner(garbled, 2002) == "garbled"
        assert (garbled / sc.OWNER_FILENAME).read_bytes() == b"\xff\xfe1001\n"

    @pytest.mark.skipif(sys.platform == "win32", reason="FIFOs are a POSIX shape")
    def test_a_fifo_planted_at_the_marker_does_not_park_the_reader(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A FIFO passes the link check and ``O_NOFOLLOW``; a blocking open with no
        writer would hang the sweep or a spawn's adopt forever. The read must
        return at once and treat it as not-a-marker."""
        import threading

        tree = sc.allocate_scratch("runtime")
        marker = tree / sc.OWNER_FILENAME
        marker.unlink()
        os.mkfifo(marker)
        # Read on a daemon thread with a bounded join: a regression here is a
        # reader parked on the FIFO forever, which must FAIL this test fast
        # rather than hang the worker (class-6 lost run).
        outcome: list[object] = []

        def read() -> None:
            try:
                outcome.append(sc._read_owner_pids(marker))
            except Exception as exc:  # noqa: BLE001 - the outcome IS the assertion
                outcome.append(exc)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(5)
        assert not reader.is_alive(), "the marker read parked on the FIFO"
        assert isinstance(outcome[0], ValueError) and "not a regular file" in str(outcome[0])
        assert sc.adopt_owner(tree, 2002) == "garbled"
        monkeypatch.setattr(sc, "_pgroup_alive", lambda pid: False)
        old = time.time() - 2 * sc._UNOWNED_GRACE_SECONDS
        os.utime(tree, (old, old))
        assert sc.sweep_dead_scratch() == 0 and tree.exists()

    def test_a_marker_is_read_to_eof_not_in_one_read(self, scratch_root: Path, monkeypatch) -> None:
        """A short read hands back a prefix; a prefix naming only dead pids
        would read as a dead owner over the live pid the tail names."""
        tree = sc.allocate_scratch("runtime")
        marker = tree / sc.OWNER_FILENAME
        marker.write_text("1001\n1002\n1003\n", encoding="utf-8")
        real_read = os.read
        monkeypatch.setattr(sc.os, "read", lambda fd, n: real_read(fd, min(n, 1)))
        assert sc._read_owner_pids(marker) == (1001, 1002, 1003)
        # The cap still holds when every read is short.
        marker.write_bytes(b"1\n" * (sc._OWNER_MARKER_MAX_BYTES + 8))
        with pytest.raises(ValueError):
            sc._read_owner_pids(marker)


class TestAnInheritedTreeIsHeldAgainstTheSweep:
    """Between validation and adoption the new user is not in the marker and the
    allocator may be dead: an hourly sweep landing there would delete the tree
    under the spawn that is mounting it. Validation marks the tree ACTIVE (its
    mtime), which is the sweep's own idle rule; nothing has to be released."""

    @staticmethod
    def _dead_idle(tree: Path, monkeypatch) -> None:
        monkeypatch.setattr(sc, "_pgroup_alive", lambda pid: False)
        old = time.time() - 2 * sc._UNOWNED_GRACE_SECONDS
        for entry in (tree, tree / sc.OWNER_FILENAME):
            os.utime(entry, (old, old))

    def test_a_validated_window_survives_a_sweep_for_the_grace_window(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        tree = sc.allocate_scratch("runtime")
        sc.record_owner(tree, 1001)
        self._dead_idle(tree, monkeypatch)
        assert sc.shared_scratch_window(tree) == tree
        assert sc.sweep_dead_scratch() == 0 and tree.exists()
        # Nothing to release: the hold is the mtime, and the sweep reclaims the
        # tree once the grace window has passed with no user in the marker.
        old = time.time() - 2 * sc._UNOWNED_GRACE_SECONDS
        os.utime(tree, (old, old))
        assert sc.sweep_dead_scratch() == 1 and not tree.exists()

    def test_two_heirs_validating_the_same_tree_both_refresh_it(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        tree = sc.allocate_scratch("runtime")
        sc.record_owner(tree, 1001)
        self._dead_idle(tree, monkeypatch)
        assert sc.shared_scratch_window(tree) == tree  # heir A
        assert sc.shared_scratch_window(tree) == tree  # heir B, same tree
        assert sc.sweep_dead_scratch() == 0 and tree.exists()

    def test_the_sweep_rechecks_under_the_lock_before_removing(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A touch that lands after the sweep's idle reading but before its
        rmtree still wins: the final look is under the same lock."""
        tree = sc.allocate_scratch("runtime")
        sc.record_owner(tree, 1001)
        self._dead_idle(tree, monkeypatch)
        real_read = sc._read_owner_pids

        def touch_then_read(marker: Path):
            os.utime(tree, None)  # a spawn validates while the sweep judges
            return real_read(marker)

        monkeypatch.setattr(sc, "_read_owner_pids", touch_then_read)
        assert sc.sweep_dead_scratch() == 0 and tree.exists()

    def test_validation_never_holds_the_marker_io_lock(self, scratch_root: Path) -> None:
        """The sweep lock covers lstat/utime and rmtree only, never marker I/O,
        so an adoption mid-write cannot stall validation and vice versa."""
        assert sc._SWEEP_LOCK is not sc._ADOPT_LOCK
        tree = sc.allocate_scratch("runtime")
        with sc._ADOPT_LOCK:  # an adoption mid-write
            assert sc.shared_scratch_window(tree) == tree  # returns without waiting


class TestSpawnersMountTheTreeWindow:
    """Both spawners hand the sandbox two windows and name the tree's in the env."""

    @staticmethod
    def _install_capture(monkeypatch, module, tmp_path: Path, spawn_attr: str):
        macos_dir = tmp_path / "Kiro CLI.app" / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True)
        executable = macos_dir / "kiro-cli"
        executable.write_bytes(b"#!/bin/sh\n")
        executable.chmod(0o755)
        (macos_dir / "kiro-cli-chat").write_bytes(b"sibling")
        wrapped: dict[str, object] = {}

        class _StopSpawn(Exception):
            pass

        def capture_wrap(argv, mode, **kwargs):
            wrapped.update(argv=list(argv), mode=mode, kwargs=kwargs)
            return ["/usr/bin/sandbox-wrapper", *argv], None

        async def stop_spawn(*args, **kwargs):
            wrapped["spawn_kwargs"] = kwargs
            raise _StopSpawn()

        async def resolve_installed(*, environ=None, home=None):
            return str(executable)

        import kiro_crew.acp.client as client_mod

        monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", resolve_installed)
        monkeypatch.setattr(module, "wrap_argv", capture_wrap)
        monkeypatch.setattr(
            module, "assert_voice_runtime_outside_agent_workspace", MagicMock(), raising=False
        )
        monkeypatch.setattr(
            module, "cgroup_scope_argv", lambda argv: ["/usr/bin/cgroup-wrapper", *argv]
        )

        async def _unbound_workspace(work_dir):
            return work_dir, None

        monkeypatch.setattr(
            module, "bind_voice_safe_agent_workspace_async", _unbound_workspace, raising=False
        )
        monkeypatch.setattr(module, spawn_attr, stop_spawn)
        return wrapped, _StopSpawn

    @pytest.mark.asyncio
    async def test_runtime_spawn_adds_the_tree_dir_as_a_second_window(
        self, scratch_root: Path, tmp_path: Path, monkeypatch
    ) -> None:
        import kiro_crew.acp.runtime as runtime_mod
        from kiro_crew.acp.runtime import AcpRuntime

        wrapped, stop = self._install_capture(
            monkeypatch, runtime_mod, tmp_path, "create_subprocess_limited"
        )
        tree = sc.allocate_scratch("parent")
        runtime = AcpRuntime(work_dir=tmp_path / "workspace", shared_scratch=tree)
        with pytest.raises(stop):
            await runtime.spawn()

        own, second = tuple(wrapped["kwargs"]["extra_private_dirs"])  # type: ignore[index]
        assert Path(own).parent == scratch_root and Path(own) != tree
        assert Path(second) == tree
        env = wrapped["spawn_kwargs"]["env"]  # type: ignore[index]
        assert env["KIROCREW_SCRATCH"] == str(tree)
        assert env["TMPDIR"] == own
        # Spawns made on behalf of sessions on this runtime inherit the TREE.
        assert runtime.work_scratch_dir == tree

    @pytest.mark.asyncio
    async def test_runtime_spawn_drops_a_swept_tree_dir_and_keeps_its_own(
        self, scratch_root: Path, tmp_path: Path, monkeypatch
    ) -> None:
        import kiro_crew.acp.runtime as runtime_mod
        from kiro_crew.acp.runtime import AcpRuntime

        wrapped, stop = self._install_capture(
            monkeypatch, runtime_mod, tmp_path, "create_subprocess_limited"
        )
        scratch_root.mkdir(parents=True, exist_ok=True)
        gone = scratch_root / "parent-00000000"  # never allocated, i.e. already swept
        runtime = AcpRuntime(work_dir=tmp_path / "workspace", shared_scratch=gone)
        with pytest.raises(stop):
            await runtime.spawn()

        (own,) = tuple(wrapped["kwargs"]["extra_private_dirs"])  # type: ignore[index]
        env = wrapped["spawn_kwargs"]["env"]  # type: ignore[index]
        assert env["KIROCREW_SCRATCH"] == own == env["TMPDIR"]
        assert not gone.exists()
        assert runtime.work_scratch_dir == Path(own)

    @pytest.mark.asyncio
    async def test_client_spawn_adds_the_tree_dir_as_a_second_window(
        self, scratch_root: Path, tmp_path: Path, monkeypatch
    ) -> None:
        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import AcpClient

        wrapped, stop = self._install_capture(
            monkeypatch, client_mod, tmp_path, "create_subprocess_limited"
        )
        tree = sc.allocate_scratch("parent")
        client = AcpClient(work_dir=tmp_path / "workspace", shared_scratch=tree)
        with pytest.raises(stop):
            await client._spawn()

        own, second = tuple(wrapped["kwargs"]["extra_private_dirs"])  # type: ignore[index]
        assert Path(own).parent == scratch_root and Path(own) != tree
        assert Path(second) == tree
        env = wrapped["spawn_kwargs"]["env"]  # type: ignore[index]
        assert env["KIROCREW_SCRATCH"] == str(tree)
        assert env["TMPDIR"] == own
        assert client.work_scratch_dir == tree

    @pytest.mark.asyncio
    async def test_a_root_clients_respawn_joins_its_previous_directory(
        self, scratch_root: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """A root (no inherited tree) that respawns after its process exited is
        still the same session tree: its children mounted the directory the
        previous process exposed and the work it staged is there. The new
        process joins it as the tree rather than starting an empty one."""
        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import AcpClient

        wrapped, stop = self._install_capture(
            monkeypatch, client_mod, tmp_path, "create_subprocess_limited"
        )
        client = AcpClient(work_dir=tmp_path / "workspace")
        previous = sc.allocate_scratch("session")  # what the first process exposed
        client._scratch_dir = previous
        with pytest.raises(stop):
            await client._spawn()

        own, second = tuple(wrapped["kwargs"]["extra_private_dirs"])  # type: ignore[index]
        assert Path(second) == previous and Path(own) != previous
        env = wrapped["spawn_kwargs"]["env"]  # type: ignore[index]
        assert env["KIROCREW_SCRATCH"] == str(previous)
        assert client.work_scratch_dir == previous


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the fake-process spawn prelude is posix-shaped; the mechanism is platform-neutral",
)
class TestAnInheritingRuntimeJoinsTheMarkerOnceLive:
    """The adoption runs AFTER the process exists, past where the sandbox-capture
    harness stops, so this drives ``spawn`` up to the handshake with a fake
    process (the ``test_acp_spawn_offload`` prelude) and reads what reached
    ``agent_scratch``."""

    @staticmethod
    def _drive(monkeypatch, tmp_path: Path, tree: Path, adopt_outcome: str):
        from test_acp_spawn_offload import TestRuntimeShieldSurvivesAFailedAppend

        import kiro_crew.acp.runtime as runtime_mod
        from kiro_crew.acp.runtime import AcpRuntime

        class _StopSpawn(Exception):
            pass

        mock_proc = MagicMock()
        mock_proc.pid = _UNALLOCATABLE_PID
        mock_proc.returncode = None
        mock_proc.stderr = None
        mock_proc.stdout = None
        TestRuntimeShieldSurvivesAFailedAppend._patch_prelude(monkeypatch, tmp_path, mock_proc)
        # No native agent tree here either (the offload module's autouse fixture).
        from kiro_crew.acp import skill_projection

        monkeypatch.setattr(
            skill_projection,
            "prepare_native_skill_projection",
            lambda work_dir: skill_projection.NativeSkillProjection({"kirocrew": "kirocrew-view"}),
        )
        calls: list[tuple[str, Path, int]] = []
        fake_scratch = SimpleNamespace(
            allocate_scratch=lambda _label: None,
            shared_scratch_window=lambda path: path,
            record_owner=lambda path, pid: calls.append(("record", path, pid)) or "recorded",
            adopt_owner=lambda path, pid: calls.append(("adopt", path, pid)) or adopt_outcome,
            ScratchBoundaryError=sc.ScratchBoundaryError,
            SharedScratchJoinError=sc.SharedScratchJoinError,
        )
        monkeypatch.setattr(runtime_mod, "agent_scratch", fake_scratch)
        monkeypatch.setattr(runtime_mod, "register_protected_pid", lambda pid: None)
        monkeypatch.setattr(runtime_mod, "_track_pid", lambda pid: None)
        monkeypatch.setattr(runtime_mod, "_track_session_pid", lambda pid: None)

        async def _no_reader(_self) -> None:
            return None

        monkeypatch.setattr(AcpRuntime, "_reader_loop", _no_reader, raising=True)
        monkeypatch.setattr(
            AcpRuntime, "_send_and_await", AsyncMock(side_effect=_StopSpawn()), raising=True
        )
        kill = AsyncMock()
        monkeypatch.setattr(AcpRuntime, "kill", kill, raising=True)
        runtime = AcpRuntime(work_dir=tmp_path / "workspace", shared_scratch=tree)
        return runtime, calls, kill, _StopSpawn

    @pytest.mark.asyncio
    async def test_the_live_process_is_added_to_the_trees_marker(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        tree = tmp_path / "scratch" / "parent-abcdef01"
        runtime, calls, _kill, stop = self._drive(monkeypatch, tmp_path, tree, "recorded")
        with pytest.raises(stop):
            await runtime.spawn()
        assert ("adopt", tree, _UNALLOCATABLE_PID) in calls

    @pytest.mark.asyncio
    async def test_a_garbled_marker_is_a_warning_not_a_reap(self, tmp_path: Path, monkeypatch):
        """The tree is mounted read-write into every agent it serves; one that
        scribbles over the marker must not be able to veto every later spawn."""
        tree = tmp_path / "scratch" / "parent-abcdef01"
        runtime, calls, _kill, stop = self._drive(monkeypatch, tmp_path, tree, "garbled")
        # The harness ends the spawn AFTER the adopt step; reaching it proves the
        # adopt outcome did not raise (compare the "stale" case below).
        with pytest.raises(stop):
            await runtime.spawn()
        assert ("adopt", tree, _UNALLOCATABLE_PID) in calls
        assert runtime.work_scratch_dir == tree

    @pytest.mark.asyncio
    async def test_a_marker_that_cannot_be_joined_reaps_the_spawn(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """ "stale" leaves the marker naming only the other users: their exit would
        read as a dead owner over this runtime's live use."""
        from kiro_crew.agent_scratch import SharedScratchJoinError

        tree = tmp_path / "scratch" / "parent-abcdef01"
        runtime, calls, kill, _stop = self._drive(monkeypatch, tmp_path, tree, "stale")
        # The subclass names WHICH marker could not be joined, so a caller that
        # inherits on a slot's behalf can abandon the inherit on this and only this.
        with pytest.raises(SharedScratchJoinError, match="could not be joined"):
            await runtime.spawn()
        kill.assert_awaited()


class TestSpawnsOnBehalfOfAParent:
    def test_companion_runtime_kwargs_carry_the_parents_tree_dir(self, tmp_path: Path) -> None:
        from kiro_crew.session_allocation import _collect_parent_runtime_kwargs

        tree = tmp_path / "scratch" / "runtime-abcdef01"
        provider = MagicMock()
        provider._client = MagicMock(
            _sandbox_mode="auto",
            _extra_env={},
            _mcp_gateway_overlay=None,
            _mcp_gateway_socket=None,
            backend="kiro",
        )
        provider.client = provider._client
        provider.tool_search_settings = None
        provider.work_scratch_dir = tree
        owner = MagicMock()
        owner.get_provider = MagicMock(return_value=provider)

        kwargs = _collect_parent_runtime_kwargs(owner, "parent")
        assert kwargs["shared_scratch"] == tree

    def test_a_parent_without_a_path_contributes_nothing(self) -> None:
        from kiro_crew.session_allocation import (
            _collect_parent_runtime_kwargs,
            parent_work_scratch_dir,
        )

        provider = MagicMock()  # every attribute is a Mock, none is a Path
        provider._client = MagicMock(backend="kiro")
        provider.client = provider._client
        provider.tool_search_settings = None
        owner = MagicMock()
        owner.get_provider = MagicMock(return_value=provider)
        assert parent_work_scratch_dir(owner, "parent") is None
        assert "shared_scratch" not in _collect_parent_runtime_kwargs(owner, "parent")
        owner.get_provider = MagicMock(return_value=None)
        assert parent_work_scratch_dir(owner, "gone") is None

    def test_the_capability_is_read_off_the_provider_not_probed_on_its_client(
        self, tmp_path: Path
    ) -> None:
        """H14: the ABC property is the contract; a client-side path alone is not read."""
        from kiro_crew.session_allocation import parent_work_scratch_dir

        tree = tmp_path / "scratch" / "chat-1-abcdef01"
        provider = SimpleNamespace(
            work_scratch_dir=tree, client=SimpleNamespace(work_scratch_dir=tmp_path / "other")
        )
        owner = MagicMock()
        owner.get_provider = MagicMock(return_value=provider)
        assert parent_work_scratch_dir(owner, "parent") == tree

    @pytest.mark.asyncio
    async def test_a_dedicated_subagent_bypasses_the_warm_pool_to_mount_the_tree(
        self, tmp_path: Path
    ) -> None:
        """A pooled child's mounts were fixed at pre-spawn with no parent."""
        from kiro_crew.session import SessionManager

        cfg = MagicMock()
        cfg.session.pool_size = 2
        cfg.session.pool_agent = "kirocrew"
        cfg.session.pool_ttl_secs = 1800
        cfg.session.timeout_secs = 3600
        cfg.agent.default_agent = ""
        cfg.agent.model = "auto"

        def _provider():
            p = MagicMock()
            p.start = AsyncMock()
            p.shutdown = AsyncMock()
            p.is_process_alive = MagicMock(return_value=True)
            p.exit_code = None
            p.cwd = ""
            return p

        factory = MagicMock(side_effect=lambda *a, **kw: _provider())
        with patch("kiro_crew.session.default_project_dir", return_value=""):
            mgr = SessionManager(cfg, provider_factory=factory)
        mgr._drain_and_claim = AsyncMock(return_value=_provider())
        mgr._schedule_replenish = MagicMock()
        tree = tmp_path / "scratch" / "runtime-abcdef01"
        try:
            await mgr.get_or_create("subagent:s1", agent="kirocrew", shared_scratch=tree)
        finally:
            await mgr.close_all()

        mgr._drain_and_claim.assert_not_awaited()
        factory.assert_called_once()
        assert factory.call_args.kwargs["shared_scratch"] == tree

    def _run_dedicated(self, sessions: MagicMock) -> dict:
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.execution_context import execution_for_store
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_approval_policy = MagicMock(return_value="")
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        ctx_builder = MagicMock()
        ctx_builder.build_message = MagicMock(return_value=("msg", None))
        ctx_builder.hooks.auto_approve_subagent_tools = False
        captured: dict = {}
        mock_client = MagicMock()

        async def fake_get_or_create(key, agent=None, approval_policy="", **kwargs):
            captured.update(kwargs)
            return mock_client, True, False

        sessions.get_or_create = fake_get_or_create

        async def fake_stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE)

        mock_client.stream = fake_stream
        cfg = KiroCrewConfig(agent=AgentConfig())
        runner = SubagentManager(sessions=sessions, ctx_builder=ctx_builder)
        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="sub1",
            task="test",
            parent_session_key="parent-key",
            model="some-model",  # a per-spawn model forces the dedicated path
        )
        runner._log_spawned(info)
        with (
            patch.object(runner, "_should_use_session_sharing", return_value=False),
            patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
        ):
            asyncio.run(runner._run_inner(info, "subagent:sub1"))
        return captured

    def test_a_dedicated_subagent_is_handed_its_parents_tree_dir(self, tmp_path: Path) -> None:
        tree = tmp_path / "scratch" / "runtime-abcdef01"
        sessions = MagicMock()
        sessions.parent_work_scratch_dir = MagicMock(return_value=tree)
        captured = self._run_dedicated(sessions)
        sessions.parent_work_scratch_dir.assert_called_once_with("parent-key")
        assert captured["shared_scratch"] == tree

    def test_a_parent_without_scratch_hands_the_child_nothing(self) -> None:
        sessions = MagicMock()
        sessions.parent_work_scratch_dir = MagicMock(return_value=None)
        captured = self._run_dedicated(sessions)
        assert "shared_scratch" not in captured


class TestTheKiroDedicatedPathCarriesTheTree:
    """``AcpProvider.start`` on the kiro backend swaps the placeholder client for
    an ``AcpRuntime`` it constructs itself; the inherited directory must reach
    THAT constructor, not only the placeholder's kwargs."""

    @staticmethod
    def _provider(tree: Path):
        from kiro_crew.providers.acp import AcpProvider

        with patch("kiro_crew.providers.acp.AcpClient"):
            provider = AcpProvider(acp_backend="", shared_scratch=tree)
        provider._client = MagicMock()
        provider._client.backend = ""
        provider._client._work_dir = "/tmp/ws"
        provider._client._agent = "kirocrew"
        provider._client._sandbox_mode = "auto"
        provider._client._extra_env = {}
        provider._client._mcp_gateway_overlay = None
        provider._client._mcp_gateway_socket = None
        provider._client._model = "auto"
        provider._client._resume_session_id = None
        return provider

    @pytest.mark.asyncio
    async def test_the_runtime_the_provider_spawns_is_built_with_the_tree(
        self, tmp_path: Path
    ) -> None:
        tree = tmp_path / "scratch" / "runtime-abcdef01"
        provider = self._provider(tree)
        handle = MagicMock()
        handle.session_id = "kiro-sess-1"
        handle.store_session_config = MagicMock()
        handle.set_model = AsyncMock()
        runtime = MagicMock()
        runtime.pid = _UNALLOCATABLE_PID
        runtime.spawn = AsyncMock()
        runtime.create_session = AsyncMock(return_value=handle)
        runtime.work_scratch_dir = tree
        with (
            patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime) as ctor,
            patch(
                "kiro_crew.providers.acp.AcpSessionProvider",
                side_effect=lambda h, rt, **kw: SimpleNamespace(
                    _handle=h, _runtime=rt, resumed=False, work_scratch_dir=rt.work_scratch_dir
                ),
            ),
        ):
            await provider._start_kiro_runtime()
        assert ctor.call_count == 1
        assert ctor.call_args.kwargs["shared_scratch"] == tree
        # The capability answers the LIVE process's directory after the swap.
        assert provider.work_scratch_dir == tree

    @pytest.mark.asyncio
    async def test_a_swept_inherited_tree_is_replaced_by_the_live_one_for_the_restart(
        self, tmp_path: Path
    ) -> None:
        """The runtime drops an inherited window that was swept and falls back to
        its own directory. The provider must then remember THAT directory: a
        restart that re-sent the swept path would drop it again and start a
        third tree, losing what the first runtime staged."""
        swept = tmp_path / "scratch" / "runtime-00000000"  # never allocated
        own = tmp_path / "scratch" / "subagent-live-11111111"
        provider = self._provider(swept)
        handle = MagicMock()
        handle.session_id = "kiro-sess-1"
        handle.store_session_config = MagicMock()
        handle.set_model = AsyncMock()

        def build(**kwargs):
            runtime = MagicMock()
            runtime.pid = _UNALLOCATABLE_PID
            runtime.spawn = AsyncMock()
            runtime.create_session = AsyncMock(return_value=handle)
            # spawn() dropped the swept inherit: the live tree is the own dir.
            runtime.work_scratch_dir = own
            return runtime

        with (
            patch("kiro_crew.providers.acp.AcpRuntime", side_effect=build) as ctor,
            patch(
                "kiro_crew.providers.acp.AcpSessionProvider",
                side_effect=lambda h, rt, **kw: SimpleNamespace(
                    _handle=h,
                    _runtime=rt,
                    resumed=False,
                    work_scratch_dir=rt.work_scratch_dir,
                    # What the restart reads off the client it replaces.
                    backend="",
                    _work_dir="/tmp/ws",
                    _agent="kirocrew",
                    _sandbox_mode="auto",
                    _extra_env={},
                    _mcp_gateway_overlay=None,
                    _mcp_gateway_socket=None,
                    _model="auto",
                    _resume_session_id=None,
                ),
            ),
        ):
            await provider._start_kiro_runtime()
            assert ctor.call_args.kwargs["shared_scratch"] == swept
            assert provider._shared_scratch == own  # recorded off the live process
            await provider._start_kiro_runtime()  # the restart
        assert ctor.call_count == 2
        assert ctor.call_args.kwargs["shared_scratch"] == own

    def test_the_capability_is_declared_on_the_provider_contract(self) -> None:
        """H14: read off ``LLMProvider``, never probed for a private name."""
        from kiro_crew.providers.base import LLMProvider

        assert isinstance(vars(LLMProvider)["work_scratch_dir"], property)

        class _Bare(LLMProvider):  # a conforming adapter that threads nothing
            pass

        assert _Bare.work_scratch_dir.fget(MagicMock()) is None


class TestBgRuntimeSuccessorInheritsTheTree:
    """A recycled ``_bg`` runtime's replacement takes over the sessions' work dir."""

    @staticmethod
    def _cfg():
        from kiro_crew.config import KiroCrewConfig

        c = KiroCrewConfig()
        c.session.timeout_secs = 2
        return c

    @staticmethod
    def _factory():
        async def _empty(_command: str):
            if False:  # pragma: no cover
                yield None

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.is_process_alive = lambda: True
            m.context_usage_pct = lambda: 0.0
            m.context_usage_unknown = lambda: False
            m.context_window_tokens = lambda: 0
            m.has_active_turn = lambda: False
            m.runtime_info = lambda: (None, None)
            m.stream_command = MagicMock(side_effect=_empty)
            return m

        return factory

    @staticmethod
    def _fresh_runtime():
        rt = AsyncMock()
        rt.spawn = AsyncMock()
        rt.is_alive = lambda: True
        rt.create_session = AsyncMock(return_value=object())
        return rt

    @pytest.mark.asyncio
    async def test_a_stale_runtimes_successor_inherits_and_adopts(self, tmp_path: Path) -> None:
        from kiro_crew.session import SessionManager

        mgr = SessionManager(self._cfg(), provider_factory=self._factory())
        tree = tmp_path / "scratch" / "runtime-abcdef01"
        stale = AsyncMock()
        stale.is_alive = lambda: True
        stale.has_active_or_initializing_sessions = lambda: True
        stale._is_stale = AsyncMock(return_value="rss")
        stale.kill = AsyncMock()
        stale.pid = _UNALLOCATABLE_PID
        stale.work_scratch_dir = tree
        mgr._bg_runtime = stale

        fresh = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=fresh) as ctor:
            await mgr.get_bg_session()

        assert ctor.call_count == 1
        assert ctor.call_args.kwargs["shared_scratch"] == tree
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_runtime_that_dies_in_create_session_still_hands_its_tree_on(
        self, tmp_path: Path
    ) -> None:
        """The retry clears the slot before the second iteration reads it."""
        from kiro_crew.acp.runtime import AcpRuntimeDead
        from kiro_crew.session import SessionManager

        mgr = SessionManager(self._cfg(), provider_factory=self._factory())
        tree = tmp_path / "scratch" / "runtime-abcdef01"
        alive = {"now": True}
        dying = AsyncMock()
        dying.is_alive = lambda: alive["now"]
        dying.has_active_or_initializing_sessions = lambda: False
        dying._is_stale = AsyncMock(return_value=None)
        dying.kill = AsyncMock()
        dying.pid = _UNALLOCATABLE_PID
        dying.work_scratch_dir = tree

        async def _die(**_kw):
            alive["now"] = False  # the process is gone by the time the call fails
            raise AcpRuntimeDead("gone")

        dying.create_session = AsyncMock(side_effect=_die)
        mgr._bg_runtime = dying

        fresh = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=fresh) as ctor:
            await mgr.get_bg_session()

        assert ctor.call_count == 1
        assert ctor.call_args.kwargs["shared_scratch"] == tree
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_replacement_that_fails_to_spawn_does_not_lose_the_tree(
        self, tmp_path: Path
    ) -> None:
        """The stale runtime is detached and the slot cleared BEFORE the
        replacement spawns; a spawn failure must not leave the next call with
        nothing to inherit."""
        from kiro_crew.acp.runtime import AcpRuntimeDead
        from kiro_crew.session import SessionManager

        mgr = SessionManager(self._cfg(), provider_factory=self._factory())
        tree = tmp_path / "scratch" / "runtime-abcdef01"
        stale = AsyncMock()
        stale.is_alive = lambda: True
        stale.has_active_or_initializing_sessions = lambda: False
        stale._is_stale = AsyncMock(return_value="age")
        stale.kill = AsyncMock()
        stale.pid = _UNALLOCATABLE_PID
        stale.work_scratch_dir = tree
        mgr._bg_runtime = stale

        failing = self._fresh_runtime()
        failing.spawn = AsyncMock(side_effect=AcpRuntimeDead("spawn failed"))
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=failing):
            with pytest.raises(AcpRuntimeDead):
                await mgr.get_bg_session()
        assert mgr._bg_runtime is None or not mgr._bg_runtime.is_alive()

        fresh = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=fresh) as ctor:
            await mgr.get_bg_session()
        assert ctor.call_args.kwargs["shared_scratch"] == tree
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_tree_whose_marker_cannot_be_joined_is_abandoned_not_fatal(
        self, tmp_path: Path
    ) -> None:
        """The marker lives inside a directory mounted read-write into every
        agent on the runtime, so a tampered marker must not lock the ``_bg``
        slot out of ever getting a replacement: the inherit is dropped and the
        replacement proceeds with its own directory."""
        from kiro_crew.session import SessionManager

        mgr = SessionManager(self._cfg(), provider_factory=self._factory())
        tree = tmp_path / "scratch" / "runtime-abcdef01"
        stale = AsyncMock()
        stale.is_alive = lambda: True
        stale.has_active_or_initializing_sessions = lambda: False
        stale._is_stale = AsyncMock(return_value="age")
        stale.kill = AsyncMock()
        stale.pid = _UNALLOCATABLE_PID
        stale.work_scratch_dir = tree
        mgr._bg_runtime = stale

        refusing = self._fresh_runtime()
        refusing.spawn = AsyncMock(
            side_effect=sc.SharedScratchJoinError("owner marker replaced with a link")
        )
        recovered = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", side_effect=[refusing, recovered]) as ctor:
            await mgr.get_bg_session()
        assert [c.kwargs["shared_scratch"] for c in ctor.call_args_list] == [tree, None]
        assert mgr._bg_runtime is recovered
        assert mgr._background_runtime.state.inherited_scratch is None
        # A later replacement starts from the recovered runtime's own tree, not
        # the abandoned one.
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_own_marker_failure_keeps_the_inherited_tree(self, tmp_path: Path) -> None:
        """A plain ``ScratchBoundaryError`` is the replacement's OWN marker
        (record_owner), which says nothing about the inherited tree: the
        failure propagates and the inherit is kept for the next call."""
        from kiro_crew.session import SessionManager

        mgr = SessionManager(self._cfg(), provider_factory=self._factory())
        tree = tmp_path / "scratch" / "runtime-abcdef01"
        stale = AsyncMock()
        stale.is_alive = lambda: True
        stale.has_active_or_initializing_sessions = lambda: False
        stale._is_stale = AsyncMock(return_value="age")
        stale.kill = AsyncMock()
        stale.pid = _UNALLOCATABLE_PID
        stale.work_scratch_dir = tree
        mgr._bg_runtime = stale

        own_failure = self._fresh_runtime()
        own_failure.spawn = AsyncMock(
            side_effect=sc.ScratchBoundaryError("own owner marker replaced with a link")
        )
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=own_failure) as ctor:
            with pytest.raises(sc.ScratchBoundaryError):
                await mgr.get_bg_session()
        assert ctor.call_count == 1  # no second, tree-less spawn
        assert mgr._background_runtime.state.inherited_scratch == tree
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_first_runtime_of_a_gateway_starts_its_own_tree(self) -> None:
        from kiro_crew.session import SessionManager

        mgr = SessionManager(self._cfg(), provider_factory=self._factory())
        assert mgr._bg_runtime is None
        fresh = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=fresh) as ctor:
            await mgr.get_bg_session()

        assert ctor.call_args.kwargs["shared_scratch"] is None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_live_runtimes_tree_is_recorded_at_the_slot_not_at_the_next_read(
        self, tmp_path: Path
    ) -> None:
        """A backend switch retires the runtime without another acquisition
        reading it as a predecessor; the tree must already be on the state or
        the switch back starts empty and the sweep takes the forgotten tree."""
        from kiro_crew.session import SessionManager

        mgr = SessionManager(self._cfg(), provider_factory=self._factory())
        tree = tmp_path / "scratch" / "runtime-fresh0001"
        fresh = self._fresh_runtime()
        fresh.work_scratch_dir = tree
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=fresh):
            await mgr.get_bg_session()
        assert mgr._background_runtime.state.inherited_scratch == tree
        # Retired by a path that never re-reads the slot; the next replacement
        # still inherits the tree the retired runtime exposed.
        mgr._bg_runtime = None
        successor = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=successor) as ctor:
            await mgr.get_bg_session()
        assert ctor.call_args.kwargs["shared_scratch"] == tree
        await mgr.close_all()


class TestEveryProcessSpawnSeamIsAccountedFor:
    """The fix rests on every seam that starts a kiro-cli process on a session's
    behalf threading ``shared_scratch``; a seam that forgets reproduces the
    original bug with no red test. So enumerate the seams structurally: every
    ``AcpRuntime(`` / ``AcpClient(`` construction in ``src/`` either passes
    ``shared_scratch`` (directly, or via a ``**kwargs`` its caller filled) or is
    a STANDALONE process listed below with the reason it has no session tree to
    join. A new construction site fails this test until it is placed."""

    _SRC = Path(sc.__file__).resolve().parent
    _CALL = re.compile(r"\b(AcpRuntime|AcpClient)\(")
    # Processes that belong to no session tree: nothing spawned them on a
    # session's behalf, so there is no parent directory to share.
    _STANDALONE = {
        "apps/builtins/code_review_sage/sage_lib/review_pool.py": (
            "app-owned review workers; no chat session is their parent"
        ),
        "knowledge/llm_pool.py": "knowledge-indexing helper process; no session parent",
    }

    @classmethod
    def _constructions(cls) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for path in sorted(cls._SRC.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for match in cls._CALL.finditer(text):
                line_start = text.rfind("\n", 0, match.start()) + 1
                line = text[line_start : text.find("\n", match.start())]
                stripped = line.lstrip()
                if stripped.startswith(("#", "class ", "def ", "async def ")):
                    continue
                if "isinstance(" in line or '"""' in line or "MagicMock" in line:
                    continue
                # The call's argument text, to its balanced close.
                depth, i = 1, match.end()
                while depth and i < len(text):
                    depth += {"(": 1, ")": -1}.get(text[i], 0)
                    i += 1
                found.append((path.relative_to(cls._SRC).as_posix(), text[match.end() : i - 1]))
        return found

    def test_the_enumeration_sees_the_seams_this_fix_threads(self) -> None:
        rels = {rel for rel, _ in self._constructions()}
        assert {"providers/acp.py", "session_background.py"} <= rels

    def test_every_construction_threads_the_tree_or_is_a_placed_standalone(self) -> None:
        unplaced = []
        for rel, args in self._constructions():
            code = re.sub(r"#[^\n]*", "", args)  # a comment is not an argument
            if re.search(r"\bshared_scratch\s*=", code) or "**" in code:
                continue
            if rel in self._STANDALONE:
                continue
            unplaced.append(rel)
        assert unplaced == [], (
            "a kiro-cli process is constructed without the session tree's "
            "shared_scratch and is not a placed standalone: " + ", ".join(unplaced)
        )

    def test_every_kwargs_fed_construction_has_a_caller_that_fills_the_tree(self) -> None:
        """``AcpClient(**kwargs)`` in AcpProvider is filled by its own __init__;
        the parent-side collectors that feed the dedicated and pooled arms
        name the key too."""
        from kiro_crew import session_allocation
        from kiro_crew.config import loader
        from kiro_crew.providers import acp as acp_provider
        from kiro_crew.subagent_manager import run as sub_run

        assert '"shared_scratch": shared_scratch' in inspect.getsource(acp_provider.AcpProvider)
        assert 'kwargs["shared_scratch"]' in inspect.getsource(
            session_allocation._collect_parent_runtime_kwargs
        )
        assert 'extra_kwargs["shared_scratch"]' in inspect.getsource(sub_run)
        assert "shared_scratch=shared_scratch" in inspect.getsource(loader)

    def test_the_standalone_list_names_only_files_that_still_construct(self) -> None:
        rels = {rel for rel, _ in self._constructions()}
        stale = sorted(set(self._STANDALONE) - rels)
        assert stale == [], f"standalone entries with no construction left: {stale}"
