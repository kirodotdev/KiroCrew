"""The workspace create must refuse a link at the workspace name, junction included.

Both callers resolve the path before ``materialize_workspace_dir`` sees it, and
resolving follows a link at the final name. So the function also takes the
unresolved ``leaf`` and refuses a symlink or a directory junction there, on every
platform. Its by-name arm (the one Windows takes) additionally refuses a junction
that appears at the name after resolution, including one that wins the EEXIST race.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import platform_compat
from kiro_crew.config import loader
from kiro_crew.config.loader import WorkspaceDirUnusable, materialize_workspace_dir


def _by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the by-name arm, the one Windows takes."""
    monkeypatch.setattr(loader.pinned_fs, "supports_pinned_walk", lambda: False)


def _fake_junction(monkeypatch: pytest.MonkeyPatch, entry: Path) -> None:
    """Make the OS junction oracle recognise *entry*, a real directory, as a junction."""
    monkeypatch.setattr(
        platform_compat, "_ISJUNCTION", lambda p: Path(p).resolve() == entry.resolve()
    )
    assert platform_compat.is_link_or_junction(entry)


def _materialize(path: Path) -> None:
    """Call it the way both callers do: resolve once, pass the unresolved leaf too."""
    materialize_workspace_dir(path.resolve(), leaf=path, display=str(path))


def _refused(path: Path) -> None:
    with pytest.raises(WorkspaceDirUnusable) as info:
        _materialize(path)
    assert info.value.code == "workspace_dir_not_a_directory"


@pytest.fixture(params=["pinned", "by-name"])
def arm(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "by-name":
        _by_name(monkeypatch)
    elif not loader.pinned_fs.supports_pinned_walk():
        pytest.skip("the pinned arm needs dir_fd and O_NOFOLLOW")
    return request.param


class TestLeafLink:
    @requires_symlinks
    def test_a_real_link_at_the_name_is_refused_after_resolution(
        self, tmp_path: Path, arm: str
    ) -> None:
        """Built with the product's own helper: a junction on Windows, a symlink elsewhere."""
        target = tmp_path / "real_target"
        target.mkdir()
        ws = tmp_path / "ws"
        platform_compat.symlink_or_junction(str(target), str(ws))
        assert ws.resolve() == target.resolve()
        _refused(ws)
        assert list(target.iterdir()) == []

    @requires_symlinks
    def test_a_link_reached_through_dotdot_over_a_missing_dir_is_refused(
        self, tmp_path: Path, arm: str
    ) -> None:
        """``nope/../linked`` with ``nope`` absent: lstat of the raw spelling fails."""
        target = tmp_path / "real_target"
        target.mkdir()
        platform_compat.symlink_or_junction(str(target), str(tmp_path / "linked"))
        leaf = tmp_path / "nope" / ".." / "linked"
        with pytest.raises(WorkspaceDirUnusable):
            materialize_workspace_dir(leaf.resolve(), leaf=leaf, display="nope/../linked")
        assert list(target.iterdir()) == []

    def test_a_junction_shaped_name_is_refused_on_every_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        _fake_junction(monkeypatch, ws)
        assert not ws.is_symlink()
        _refused(ws)

    def test_a_plain_directory_is_adopted_and_a_missing_one_created(
        self, tmp_path: Path, arm: str
    ) -> None:
        existing = tmp_path / "existing"
        existing.mkdir()
        (existing / "keep.txt").write_text("x", encoding="utf-8")
        _materialize(existing)
        assert (existing / "keep.txt").read_text(encoding="utf-8") == "x"
        fresh = tmp_path / "fresh"
        _materialize(fresh)
        assert fresh.is_dir()

    @requires_symlinks
    def test_a_linked_parent_is_still_followed(self, tmp_path: Path, arm: str) -> None:
        """Only the final name is screened; a link above it is resolved as before."""
        real_parent = tmp_path / "real_parent"
        real_parent.mkdir()
        linked_parent = tmp_path / "linked_parent"
        platform_compat.symlink_or_junction(str(real_parent), str(linked_parent))
        _materialize(linked_parent / "ws")
        assert (real_parent / "ws").is_dir()


class TestByNameRace:
    """A junction that appears at the name after resolution (Windows arm)."""

    @staticmethod
    def _race(monkeypatch: pytest.MonkeyPatch, ws: Path, *, as_junction: bool) -> None:
        real_mkdir = os.mkdir

        def racing_mkdir(path, *args, **kwargs):
            if Path(path).resolve() == ws.resolve():
                real_mkdir(path)
                if as_junction:
                    _fake_junction(monkeypatch, ws)
                raise FileExistsError(path)
            return real_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(loader.os, "mkdir", racing_mkdir)

    def test_a_junction_swapped_in_after_the_leaf_check_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leaf probe saw no link; by the adopt check a junction is there."""
        _by_name(monkeypatch)
        ws = tmp_path / "ws"
        ws.mkdir()
        _fake_junction(monkeypatch, ws)
        with pytest.raises(WorkspaceDirUnusable):
            materialize_workspace_dir(ws, leaf=tmp_path / "not-a-link", display="ws")

    def test_a_junction_planted_in_the_eexist_race_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _by_name(monkeypatch)
        ws = tmp_path / "ws"
        self._race(monkeypatch, ws, as_junction=True)
        _refused(ws)

    def test_a_racers_plain_directory_is_still_adopted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _by_name(monkeypatch)
        ws = tmp_path / "ws"
        self._race(monkeypatch, ws, as_junction=False)
        _materialize(ws)
        assert ws.is_dir()
