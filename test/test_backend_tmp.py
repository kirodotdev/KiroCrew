"""Tests for :mod:`kiro_crew.mcp_gateway.backend_tmp` (issue #5064).

Everything runs against a monkeypatched data home under ``tmp_path``; the
real ``<data home>/run`` is never touched.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway import backend_tmp as bt


@pytest.fixture
def tmp_root(monkeypatch, tmp_path: Path) -> Path:
    """Point the module's data home at a fabricated directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(bt, "config_dir", lambda: home)
    return home / "run" / "mcp-tmp"


DIGEST = "a" * 64


class TestAllocate:
    def test_creates_private_dir_under_managed_root(self, tmp_root: Path) -> None:
        path = bt.allocate_backend_tmp(DIGEST)

        assert path.parent == tmp_root
        assert path.is_dir()
        assert path.name.startswith(DIGEST[:12] + "-")
        # Provisional owner recorded atomically-with-allocation: deletion is
        # only ever permitted for owned-and-dead dirs, so a dir must never
        # exist without an owner record.
        assert (path / bt.OWNER_FILENAME).read_text() == str(os.getpid())
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
            assert stat.S_IMODE(os.stat(tmp_root).st_mode) == 0o700

    def test_allocation_applies_platform_dacl_lockdown(self, tmp_root: Path, monkeypatch) -> None:
        # ``mode=0o700`` is a POSIX-bits no-op on Windows: under a permissive
        # custom data home the dir would inherit a readable DACL, exposing
        # token-bearing temp files. Allocation must route through the shim
        # (owner-only DACL with inheritance on Windows, chmod 0o700 on
        # POSIX) for BOTH the root and the fresh child dir.
        from kiro_crew import platform_compat as pc

        calls: list[str] = []
        real = pc.restrict_dir_to_owner

        def _spy(path):
            calls.append(str(path))
            real(path)

        monkeypatch.setattr(pc, "restrict_dir_to_owner", _spy)

        path = bt.allocate_backend_tmp(DIGEST)

        assert str(tmp_root) in calls
        assert str(path) in calls

    def test_two_allocations_of_one_digest_are_distinct(self, tmp_root: Path) -> None:
        # Per PROCESS, not per PoolKey: a connection-private backend can run
        # beside the shared one on the same key, and a digest-keyed dir would
        # let one process's shutdown sweep delete the other's live temp.
        a = bt.allocate_backend_tmp(DIGEST)
        b = bt.allocate_backend_tmp(DIGEST)
        assert a != b

    def test_probe_dirs_are_private_and_owned(self, tmp_root: Path) -> None:
        # Per-probe, never shared: a shared dir invites a sweep-vs-acquire
        # race; a private dir dies in the probe's own finally.
        a = bt.allocate_probe_tmp()
        b = bt.allocate_probe_tmp()
        assert a != b
        assert a.parent == b.parent == tmp_root
        assert (a / bt.OWNER_FILENAME).read_text() == str(os.getpid())


class TestTmpEnv:
    def test_triple_points_at_the_dir(self, tmp_path: Path) -> None:
        env = bt.tmp_env(tmp_path)
        assert env == {"TMPDIR": str(tmp_path), "TMP": str(tmp_path), "TEMP": str(tmp_path)}


class TestSweepOne:
    def test_removes_own_dir_with_contents(self, tmp_root: Path) -> None:
        path = bt.allocate_backend_tmp(DIGEST)
        (path / "litter").mkdir()
        (path / "litter" / "cache.bin").write_bytes(b"x")

        bt.sweep_backend_tmp(path)

        assert not path.exists()

    def test_refuses_a_path_outside_the_managed_root(self, tmp_root: Path, tmp_path: Path) -> None:
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("keep")

        bt.sweep_backend_tmp(victim)

        assert victim.exists() and (victim / "keep.txt").exists()

    def test_missing_dir_is_a_noop(self, tmp_root: Path) -> None:
        bt.sweep_backend_tmp(tmp_root / "gone-already")  # must not raise

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege")
    def test_symlink_in_place_of_dir_is_never_followed(
        self, tmp_root: Path, tmp_path: Path
    ) -> None:
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("keep")
        path = bt.allocate_backend_tmp(DIGEST)
        (path / bt.OWNER_FILENAME).unlink()
        path.rmdir()
        path.symlink_to(victim)

        bt.sweep_backend_tmp(path)

        assert victim.exists() and (victim / "keep.txt").exists()


def _age(path: Path, seconds: float) -> None:
    import time

    past = time.time() - seconds
    os.utime(path, (past, past))


class TestBootSweep:
    def test_dead_and_idle_reclaimed_dead_but_active_kept(self, tmp_root: Path) -> None:
        # Deletion needs BOTH signals: the recorded owner is the LAUNCHER
        # pid, and a wrapper launcher can exit while its server child lives
        # on writing temp files (which keeps the dir mtime fresh).
        dead_idle = bt.allocate_backend_tmp(DIGEST)
        bt.record_owner(dead_idle, 2**22 - 1)  # almost surely dead
        _age(dead_idle, 2 * bt._UNOWNED_GRACE_SECONDS)
        _age(dead_idle / bt.OWNER_FILENAME, 2 * bt._UNOWNED_GRACE_SECONDS)
        dead_active = bt.allocate_backend_tmp("b" * 64)
        bt.record_owner(dead_active, 2**22 - 1)  # dead owner, fresh mtime

        removed = bt.sweep_all_backend_tmp()

        assert not dead_idle.exists()
        assert dead_active.exists(), "fresh mtime reads as in-use: kept"
        assert removed == 1

    def test_oversized_dead_tree_is_still_reclaimed(self, tmp_root: Path) -> None:
        # Regression: an entry cap in the tree-idle walk that bailed to
        # ``fallback`` (= now) made every tree larger than the cap read as
        # permanently ACTIVE, so a dead backend that wrote enough entries
        # could never be reclaimed -- the exact storage-exhaustion defect
        # this module exists to fix. 10,050 entries exceeds the removed
        # 10,000 cap; the walk is uncapped now and must find the true
        # (ancient) newest mtime.
        import time

        big = bt.allocate_backend_tmp(DIGEST)
        bt.record_owner(big, 2**22 - 1)  # almost surely dead
        past = time.time() - 2 * bt._UNOWNED_GRACE_SECONDS
        for d in range(10):
            sub = big / f"d{d}"
            sub.mkdir()
            for f in range(1005):
                p = sub / f"f{f}"
                p.touch()
                os.utime(p, (past, past))
            os.utime(sub, (past, past))
        _age(big, 2 * bt._UNOWNED_GRACE_SECONDS)
        _age(big / bt.OWNER_FILENAME, 2 * bt._UNOWNED_GRACE_SECONDS)

        removed = bt.sweep_all_backend_tmp()

        assert not big.exists()
        assert removed == 1

    def test_deep_fresh_write_keeps_the_dir(self, tmp_root: Path) -> None:
        # A live process writing through an already-open fd never touches the
        # top DIRECTORY's mtime -- the idle signal must be tree-newest, so a
        # fresh file deep inside keeps the dir even when the top is aged and
        # the recorded owner is dead (the Windows-reachable wrapper case,
        # where no group probe exists).
        path = bt.allocate_backend_tmp(DIGEST)
        bt.record_owner(path, 2**22 - 1)  # dead owner
        nested = path / "cache" / "blobs"
        nested.mkdir(parents=True)
        (nested / "live.bin").write_bytes(b"x")
        for p in (path, path / "cache", nested, path / bt.OWNER_FILENAME):
            _age(p, 2 * bt._UNOWNED_GRACE_SECONDS)
        os.utime(nested / "live.bin")  # the one fresh entry

        assert bt.sweep_all_backend_tmp() == 0
        assert path.exists()

    def test_live_owner_is_kept_even_when_idle(self, tmp_root: Path) -> None:
        live = bt.allocate_backend_tmp(DIGEST)
        # POSIX: our own process GROUP id -- the probe is group-scoped (the
        # launcher pid doubles as the pgid under start_new_session), and
        # under pytest-xdist os.getpid() is not a group leader. Windows: the
        # probe routes through pid_exists, so a live PID is the right ref.
        live_ref = os.getpid() if sys.platform == "win32" else os.getpgrp()
        bt.record_owner(live, live_ref)
        _age(live, 2 * bt._UNOWNED_GRACE_SECONDS)

        assert bt.sweep_all_backend_tmp() == 0
        assert live.exists()

    def test_unowned_dir_is_never_deleted(self, tmp_root: Path) -> None:
        # Allocation writes a provisional owner atomically-with-creation, so
        # an ownerless dir indicates a state this code did not produce --
        # deleting on absence of evidence is how live data gets lost.
        path = bt.allocate_backend_tmp(DIGEST)
        (path / bt.OWNER_FILENAME).unlink()
        _age(path, 10 * bt._UNOWNED_GRACE_SECONDS)

        assert bt.sweep_all_backend_tmp() == 0
        assert path.exists()

    def test_garbled_owner_is_left_for_a_human(self, tmp_root: Path) -> None:
        path = bt.allocate_backend_tmp(DIGEST)
        (path / bt.OWNER_FILENAME).write_text("not-a-pid")
        _age(path, 2 * bt._UNOWNED_GRACE_SECONDS)

        assert bt.sweep_all_backend_tmp() == 0
        assert path.exists()

    def test_crashed_probe_dir_is_reclaimed_when_dead_and_idle(self, tmp_root: Path) -> None:
        # A probe normally cleans its own dir in its finally; one whose
        # cleanup never ran (crash) is reclaimed by the generic owner-dead +
        # idle path once its recording process is gone.
        probe = bt.allocate_probe_tmp()
        (probe / bt.OWNER_FILENAME).write_text(str(2**22 - 1))
        _age(probe, 2 * bt._UNOWNED_GRACE_SECONDS)
        _age(probe / bt.OWNER_FILENAME, 2 * bt._UNOWNED_GRACE_SECONDS)
        assert bt.sweep_all_backend_tmp() == 1
        assert not probe.exists()

    def test_missing_root_returns_zero(self, tmp_root: Path) -> None:
        assert bt.sweep_all_backend_tmp() == 0

    def test_stray_file_is_left_alone(self, tmp_root: Path) -> None:
        tmp_root.mkdir(parents=True)
        stray = tmp_root / "not-a-dir.txt"
        stray.write_text("stray")

        removed = bt.sweep_all_backend_tmp()

        assert removed == 0 and stray.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege")
    def test_symlink_child_is_never_followed(self, tmp_root: Path, tmp_path: Path) -> None:
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("keep")
        tmp_root.mkdir(parents=True)
        (tmp_root / "evil-link").symlink_to(victim)

        removed = bt.sweep_all_backend_tmp()

        assert removed == 0
        assert victim.exists() and (victim / "keep.txt").exists()
