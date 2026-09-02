"""Tests for :mod:`kiro_crew.agent_scratch` (issue #5063).

Everything runs against a monkeypatched data home under ``tmp_path``; the
real ``<data home>/scratch`` is never touched.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from kiro_crew import agent_scratch as sc


@pytest.fixture
def scratch_root(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sc, "config_dir", lambda: home)
    return home / "scratch"


class TestAllocate:
    def test_creates_private_dir_under_managed_root(self, scratch_root: Path) -> None:
        path = sc.allocate_scratch("chat-31")

        assert path.parent == scratch_root
        assert path.is_dir()
        assert path.name.startswith("chat-31-")
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o700

    def test_label_is_sanitized_and_bounded(self, scratch_root: Path) -> None:
        path = sc.allocate_scratch("weird/label with spaces!" + "x" * 100)
        assert "/" not in path.name and " " not in path.name
        assert len(path.name) <= 40 + 1 + 8  # label cap + dash + token

    def test_two_allocations_are_distinct(self, scratch_root: Path) -> None:
        assert sc.allocate_scratch("a") != sc.allocate_scratch("a")


class TestEnv:
    def test_exports_temp_triple_and_scratch_alias(self, tmp_path: Path) -> None:
        env = sc.scratch_env(tmp_path)
        value = str(tmp_path)
        assert env == {"TMPDIR": value, "TMP": value, "TEMP": value, "KIROCREW_SCRATCH": value}


class TestLivenessSweep:
    @staticmethod
    def _age(path: Path, seconds: float) -> None:
        past = time.time() - seconds
        os.utime(path, (past, past))

    def test_dead_and_idle_reclaimed_live_or_active_kept(self, scratch_root: Path) -> None:
        dead_idle = sc.allocate_scratch("dead")
        sc.record_owner(dead_idle, 2**22 - 1)  # almost surely dead
        self._age(dead_idle, 2 * sc._UNOWNED_GRACE_SECONDS)
        self._age(dead_idle / sc.OWNER_FILENAME, 2 * sc._UNOWNED_GRACE_SECONDS)
        dead_active = sc.allocate_scratch("active")
        sc.record_owner(dead_active, 2**22 - 1)  # dead owner, fresh mtime
        live = sc.allocate_scratch("live")
        # POSIX: our own process GROUP id -- the probe is group-scoped (the
        # launcher pid doubles as the pgid under start_new_session), and
        # under pytest-xdist os.getpid() is not a group leader. Windows: the
        # probe routes through pid_exists, so a live PID is the right ref.
        live_ref = os.getpid() if sys.platform == "win32" else os.getpgrp()
        sc.record_owner(live, live_ref)
        self._age(live, 2 * sc._UNOWNED_GRACE_SECONDS)

        removed = sc.sweep_dead_scratch()

        assert not dead_idle.exists(), "dead owner + idle content is reclaimed"
        assert dead_active.exists(), "fresh mtime reads as in-use: kept"
        assert live.exists(), "a live owner is never touched, however idle"
        assert removed == 1

    def test_deep_fresh_write_keeps_the_dir(self, scratch_root: Path) -> None:
        # A live process writing through an already-open fd never touches the
        # top DIRECTORY's mtime -- the idle signal must be tree-newest, so a
        # fresh file deep inside keeps the dir even when the top is aged and
        # the recorded owner is dead (the Windows-reachable wrapper case,
        # where no group probe exists).
        path = sc.allocate_scratch("deep")
        sc.record_owner(path, 2**22 - 1)  # dead owner
        nested = path / "clone" / "src"
        nested.mkdir(parents=True)
        (nested / "live-output.log").write_text("still writing")
        # Age every PARENT dir (top + intermediate); the deep file stays fresh.
        for p in (path, path / "clone", nested, path / sc.OWNER_FILENAME):
            self._age(p, 2 * sc._UNOWNED_GRACE_SECONDS)
        os.utime(nested / "live-output.log")  # the one fresh entry

        assert sc.sweep_dead_scratch() == 0
        assert path.exists()

    def test_allocation_records_a_provisional_owner(self, scratch_root: Path) -> None:
        path = sc.allocate_scratch("prov")
        assert (path / sc.OWNER_FILENAME).read_text() == str(os.getpid())

    def test_unowned_dir_is_never_deleted(self, scratch_root: Path) -> None:
        # Allocation writes a provisional owner atomically-with-creation, so
        # an ownerless dir indicates a state this code did not produce --
        # deleting on absence of evidence is how live work gets lost.
        path = sc.allocate_scratch("unowned")
        (path / sc.OWNER_FILENAME).unlink()
        self._age(path, 10 * sc._UNOWNED_GRACE_SECONDS)

        assert sc.sweep_dead_scratch() == 0
        assert path.exists()

    def test_garbled_owner_file_is_left_for_a_human(self, scratch_root: Path) -> None:
        path = sc.allocate_scratch("garbled")
        (path / sc.OWNER_FILENAME).write_text("not-a-pid")
        self._age(path, 2 * sc._UNOWNED_GRACE_SECONDS)

        removed = sc.sweep_dead_scratch()

        assert path.exists() and removed == 0

    def test_missing_root_returns_zero(self, scratch_root: Path) -> None:
        assert sc.sweep_dead_scratch() == 0

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege")
    def test_symlink_child_is_never_followed(self, scratch_root: Path, tmp_path: Path) -> None:
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "keep").write_text("keep")
        scratch_root.mkdir(parents=True, exist_ok=True)
        (scratch_root / "evil").symlink_to(victim)

        removed = sc.sweep_dead_scratch()

        assert removed == 0
        assert victim.exists() and (victim / "keep").exists()
