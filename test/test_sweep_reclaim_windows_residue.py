"""The three idle sweeps reclaim trees holding a junction or a read-only file.

``agent_scratch``, ``mcp_gateway.backend_tmp`` and ``work_root`` each walk a
tree for its newest mtime and then delete it. Two kinds of residue that test
runs and git leave on Windows must not make a dead tree permanent:

* a directory JUNCTION -- ``lstat`` reports it as a plain directory, so a walk
  that tests ``S_ISDIR`` alone descends into it, and a dangling one makes
  ``scandir`` raise, which the walk reads as "active";
* a READ-ONLY file (every git object) -- plain ``rmtree`` cannot unlink it on
  Windows, and a partial delete takes the owner/allocation marker with it, so
  the leftover has no evidence and no later sweep touches it.

Each sweep is judged from a moment well past every grace window instead of by
ageing files: Windows ``utime`` cannot set a junction's own mtime without
going through it. Everything runs against a monkeypatched data home.
"""

from __future__ import annotations

import os
import stat
import sys
import time
import types
from pathlib import Path

import pytest

from kiro_crew import agent_scratch as sc
from kiro_crew import platform_compat
from kiro_crew import work_root as wr
from kiro_crew.mcp_gateway import backend_tmp as bt

_WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="directory junctions")

_DEAD_PID = 2**22 - 1  # almost surely no such process
_DIGEST = "a" * 64
#: Later than every grace window the three sweeps use (a week at most).
_LATER = 30 * 24 * 3600.0


def _later() -> float:
    return time.time() + _LATER


def _sweep_scratch() -> int:
    return sc.sweep_dead_scratch(now=_later())


def _sweep_work() -> int:
    return wr.sweep_work_root(now=_later())


def _sweep_backend(monkeypatch) -> int:
    # The only clock this sweep reads; it takes no ``now``.
    with monkeypatch.context() as patch:
        patch.setattr(bt, "time", types.SimpleNamespace(time=_later))
        return bt.sweep_all_backend_tmp()


def _make_junction(target: Path, link: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


def _plant_dangling_junction(tree: Path, tmp_path: Path) -> Path:
    target = tmp_path / "gone-target"
    target.mkdir()
    link = tree / "nested" / "junction"
    link.parent.mkdir(parents=True)
    _make_junction(target, link)
    target.rmdir()  # dangling, as a test that cleaned up its target leaves it
    return link


def _plant_readonly_file(tree: Path) -> Path:
    path = tree / ".git" / "objects" / "ab" / "cdef"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"blob")
    os.chmod(path, stat.S_IREAD)
    return path


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    for module in (sc, bt, wr):
        monkeypatch.setattr(module, "config_dir", lambda: home)
    return home


def _scratch_dead(label: str) -> Path:
    path = sc.allocate_scratch(label)
    sc.record_owner(path, _DEAD_PID)
    return path


def _backend_dead() -> Path:
    path = bt.allocate_backend_tmp(_DIGEST)
    bt.record_owner(path, _DEAD_PID)
    return path


class TestDanglingJunction:
    @_WINDOWS_ONLY
    def test_agent_scratch_reclaims_it(self, home: Path, tmp_path: Path) -> None:
        path = _scratch_dead("junc")
        link = _plant_dangling_junction(path, tmp_path)

        assert _sweep_scratch() == 1
        assert not os.path.lexists(path)
        assert not os.path.lexists(link)

    @_WINDOWS_ONLY
    def test_backend_tmp_reclaims_it(self, home: Path, tmp_path: Path, monkeypatch) -> None:
        path = _backend_dead()
        _plant_dangling_junction(path, tmp_path)

        assert _sweep_backend(monkeypatch) == 1
        assert not os.path.lexists(path)

    @_WINDOWS_ONLY
    def test_work_root_reclaims_it(self, home: Path, tmp_path: Path) -> None:
        path = wr.allocate_work("k")
        _plant_dangling_junction(path, tmp_path)

        assert _sweep_work() == 1
        assert not os.path.lexists(path)


class TestLiveJunctionIsNotWalked:
    """A junction's TARGET is another tree: its activity is not this tree's, and
    deleting this tree must leave the target alone."""

    @_WINDOWS_ONLY
    def test_a_busy_target_does_not_keep_a_dead_scratch_tree(
        self, home: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        precious = outside / "precious.txt"
        precious.write_text("keep", encoding="utf-8")
        later = _later()
        os.utime(precious, (later, later))  # "written" at the sweep's own moment
        path = _scratch_dead("live-junc")
        _make_junction(outside, path / "link")

        assert sc.sweep_dead_scratch(now=later) == 1
        assert not os.path.lexists(path)
        assert precious.read_text(encoding="utf-8") == "keep"

    @_WINDOWS_ONLY
    def test_a_junction_directly_under_the_managed_root_is_not_swept(
        self, home: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / sc.OWNER_FILENAME).write_text(str(_DEAD_PID), encoding="utf-8")
        (outside / "precious.txt").write_text("keep", encoding="utf-8")
        root = sc.scratch_root()
        root.mkdir(parents=True)
        _make_junction(outside, root / "evil")

        assert _sweep_scratch() == 0
        assert (outside / "precious.txt").read_text(encoding="utf-8") == "keep"


class TestReadOnlyFile:
    def test_agent_scratch_reclaims_it(self, home: Path) -> None:
        path = _scratch_dead("ro")
        _plant_readonly_file(path)

        assert _sweep_scratch() == 1
        assert not os.path.lexists(path)

    def test_backend_tmp_reclaims_it(self, home: Path, monkeypatch) -> None:
        path = _backend_dead()
        _plant_readonly_file(path)

        assert _sweep_backend(monkeypatch) == 1
        assert not os.path.lexists(path)

    def test_work_root_reclaims_it(self, home: Path) -> None:
        path = wr.allocate_work("k")
        _plant_readonly_file(path)

        assert _sweep_work() == 1
        assert not os.path.lexists(path)


def _partial_rmtree(marker_name: str):
    """An ``rmtree_force`` that deletes the marker and then fails, like a real
    rmtree stopping at a file it cannot remove after it already took the
    marker (which sorts first)."""

    def fake(path: "str | os.PathLike[str]") -> bool:
        os.unlink(Path(path) / marker_name)
        return False

    return fake


class TestPartialDeleteKeepsTheEvidence:
    """A delete that stops part way must leave the tree reclaimable later."""

    def test_agent_scratch_restores_the_owner(self, home: Path, monkeypatch) -> None:
        path = _scratch_dead("partial")
        with monkeypatch.context() as patch:
            patch.setattr(platform_compat, "rmtree_force", _partial_rmtree(sc.OWNER_FILENAME))
            assert _sweep_scratch() == 0
        assert (path / sc.OWNER_FILENAME).read_text().split() == [str(_DEAD_PID)]

        assert _sweep_scratch() == 1
        assert not os.path.lexists(path)

    def test_backend_tmp_restores_the_owner(self, home: Path, monkeypatch) -> None:
        path = _backend_dead()
        with monkeypatch.context() as patch:
            patch.setattr(platform_compat, "rmtree_force", _partial_rmtree(bt.OWNER_FILENAME))
            patch.setattr(bt, "time", types.SimpleNamespace(time=_later))
            assert bt.sweep_all_backend_tmp() == 0
        assert (path / bt.OWNER_FILENAME).read_text().strip() == str(_DEAD_PID)

        assert _sweep_backend(monkeypatch) == 1
        assert not os.path.lexists(path)

    def test_work_root_restores_the_marker(self, home: Path, monkeypatch) -> None:
        path = wr.allocate_work("k")
        with monkeypatch.context() as patch:
            patch.setattr(platform_compat, "rmtree_force", _partial_rmtree(wr._MARKER_NAME))
            assert _sweep_work() == 0
        assert os.path.lexists(path / wr._MARKER_NAME)

        assert _sweep_work() == 1
        assert not os.path.lexists(path)


#: Reparse tags as Windows reports them: a junction and a symlink carry the
#: name-surrogate bit; a cloud-files placeholder directory does not.
_TAG_JUNCTION = 0xA0000003
_TAG_SYMLINK = 0xA000000C
_TAG_CLOUD = 0x9000001A


class TestNameSurrogateOnly:
    """Only a link to another name is skipped. A reparse directory that holds its
    own data (a cloud placeholder, a dedup or container-isolation directory) is
    still part of the tree, and a fresh write inside it keeps the tree."""

    def test_the_helper_reads_the_name_surrogate_bit(self) -> None:
        def info(tag: "int | None") -> object:
            return (
                types.SimpleNamespace()
                if tag is None
                else types.SimpleNamespace(st_reparse_tag=tag)
            )

        assert platform_compat.lstat_is_name_surrogate(info(_TAG_JUNCTION))
        assert platform_compat.lstat_is_name_surrogate(info(_TAG_SYMLINK))
        assert not platform_compat.lstat_is_name_surrogate(info(_TAG_CLOUD))
        assert not platform_compat.lstat_is_name_surrogate(info(0))
        assert not platform_compat.lstat_is_name_surrogate(info(None))  # POSIX

    @staticmethod
    def _tag_as_cloud(monkeypatch, directory: Path) -> None:
        """Make ``os.lstat`` report *directory* as a cloud placeholder dir."""
        real = os.lstat
        target = os.path.normcase(str(directory))

        def lstat(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if os.path.normcase(os.fspath(path)) != target:
                return result
            return types.SimpleNamespace(
                st_mode=result.st_mode, st_mtime=result.st_mtime, st_reparse_tag=_TAG_CLOUD
            )

        monkeypatch.setattr(os, "lstat", lstat)

    @staticmethod
    def _fresh_write_inside(tree: Path) -> Path:
        cloud = tree / "synced"
        cloud.mkdir()
        later = _later()
        fresh = cloud / "being-written.bin"
        fresh.write_bytes(b"x")
        os.utime(fresh, (later, later))  # written at the sweep's own moment
        return cloud

    def test_agent_scratch_keeps_the_tree(self, home: Path, monkeypatch) -> None:
        path = _scratch_dead("cloud")
        self._tag_as_cloud(monkeypatch, self._fresh_write_inside(path))

        assert _sweep_scratch() == 0
        assert path.is_dir()

    def test_backend_tmp_keeps_the_tree(self, home: Path, monkeypatch) -> None:
        path = _backend_dead()
        self._tag_as_cloud(monkeypatch, self._fresh_write_inside(path))

        assert _sweep_backend(monkeypatch) == 0
        assert path.is_dir()

    def test_work_root_keeps_the_tree(self, home: Path, monkeypatch) -> None:
        path = wr.allocate_work("k")
        self._tag_as_cloud(monkeypatch, self._fresh_write_inside(path))

        assert _sweep_work() == 0
        assert path.is_dir()


class TestReadOnlyHookOnWindows:
    """The rmtree retry hook clears the read-only attribute, never through a link."""

    @_WINDOWS_ONLY
    def test_a_junction_is_retried_without_a_chmod(self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "junction"
        _make_junction(target, link)
        chmods: list[str] = []
        monkeypatch.setattr(os, "chmod", lambda p, *_a, **_k: chmods.append(str(p)))

        platform_compat._clear_readonly_and_retry(os.rmdir, str(link), OSError("denied"))

        assert chmods == []
        assert not os.path.lexists(link) and target.is_dir()

    @_WINDOWS_ONLY
    def test_a_read_only_file_keeps_its_other_bits_and_is_retried(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        victim = tmp_path / "blob"
        victim.write_bytes(b"x")
        os.chmod(victim, stat.S_IREAD)
        modes: list[int] = []
        real = os.chmod
        monkeypatch.setattr(os, "chmod", lambda p, m, *a, **k: (modes.append(m), real(p, m))[1])

        platform_compat._clear_readonly_and_retry(os.unlink, str(victim), OSError("denied"))

        assert not victim.exists()
        assert modes and all(m & stat.S_IREAD and m & stat.S_IWRITE for m in modes)
