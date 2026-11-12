"""Tests for ``GET /api/project/tree?shallow=1``: one level per request.

The app, the sandbox passthrough and the project fixtures are
``test_project_tree.py``'s, imported so pytest registers them here too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_project_tree import (  # noqa: F401
    _git,
    _make_app,
    _repo_template,
    mock_sel,
    passthrough_sandbox,
    plain_project,
    repo,
)

from kiro_crew.dashboard.file_api.project_tree import _project_tree_shallow_git


async def _get(project: Path, **query: str) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(str(project)))) as client:
        resp = await client.get("/api/project/tree", params={"path": str(project), **query})
        return resp.status, await resp.json()


class TestShallowWalk:
    @pytest.mark.asyncio
    async def test_reads_the_root_alone_and_names_its_folders_lazy(
        self, plain_project, mock_sel  # noqa: F811
    ):
        (plain_project / "a" / "deep").mkdir(parents=True)
        (plain_project / "a" / "deep" / "leaf.txt").write_text("x")
        (plain_project / "b").mkdir()
        (plain_project / "top.txt").write_text("x")

        status, data = await _get(plain_project, shallow="1")
        assert status == 200
        assert data["paths"] == ["top.txt"]
        assert data["directories"] == ["a", "b"]
        assert data["lazyDirectories"] == ["a", "b"]
        # Not read is not cut: no folder is reported truncated or hidden-only.
        assert data["truncated"] is False
        assert data["truncatedDirectories"] == []
        assert data["hiddenOnlyDirectories"] == []

    @pytest.mark.asyncio
    async def test_a_folder_listing_is_one_level_too(self, plain_project, mock_sel):  # noqa: F811
        (plain_project / "a" / "deep" / "deeper").mkdir(parents=True)
        (plain_project / "a" / "deep" / "deeper" / "leaf.txt").write_text("x")
        (plain_project / "a" / "one.txt").write_text("x")

        status, data = await _get(plain_project, shallow="1", dir="a")
        assert status == 200
        assert data["dir"] == "a"
        assert data["paths"] == ["a/one.txt"]
        assert data["directories"] == ["a/deep"]
        assert data["lazyDirectories"] == ["a/deep"]

    @pytest.mark.asyncio
    async def test_without_the_flag_the_whole_tree_is_listed(
        self, plain_project, mock_sel  # noqa: F811
    ):
        (plain_project / "a" / "deep").mkdir(parents=True)
        (plain_project / "a" / "deep" / "leaf.txt").write_text("x")

        status, data = await _get(plain_project)
        assert status == 200
        assert data["paths"] == ["a/deep/leaf.txt"]
        assert data["lazyDirectories"] == []

    @pytest.mark.asyncio
    async def test_the_project_allow_list_still_applies(self, tmp_path, mock_sel):  # noqa: F811
        known = tmp_path / "known"
        known.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        async with TestClient(TestServer(_make_app(str(known)))) as client:
            resp = await client.get(
                "/api/project/tree", params={"path": str(other), "shallow": "1"}
            )
        assert resp.status == 403

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="POSIX permission bits; root reads any folder",
    )
    async def test_a_folder_that_cannot_be_opened_is_named_unreadable_not_lazy(
        self, plain_project, mock_sel  # noqa: F811
    ):
        locked = plain_project / "locked"
        locked.mkdir(parents=True)
        (locked / "x.txt").write_text("x")
        (plain_project / "open").mkdir()
        locked.chmod(0)
        try:
            status, data = await _get(plain_project, shallow="1")
        finally:
            locked.chmod(0o755)
        assert status == 200
        assert data["directories"] == ["locked", "open"]
        assert data["unreadableDirectories"] == ["locked"]
        assert data["lazyDirectories"] == ["open"]


class TestShallowGit:
    @pytest.mark.asyncio
    async def test_a_repository_lists_one_level_and_keeps_gitignore(
        self, repo, mock_sel  # noqa: F811
    ):
        """One level where folders can be pinned; whole where they cannot (Windows)."""
        from kiro_crew.dashboard.handlers import files as files_mod

        pinnable = (
            files_mod._PROJECT_TREE_SCANDIR_TAKES_FD and files_mod.pinned_fs.supports_pinned_walk()
        )
        (repo / "ignored.log").write_text("x")
        (repo / "src" / "pkg").mkdir()
        (repo / "src" / "pkg" / "inner.py").write_text("x")
        status, data = await _get(repo, shallow="1")
        assert status == 200
        assert data["repo"] is True
        if pinnable:
            assert sorted(data["paths"]) == [".gitignore", "a.txt"]
            assert data["directories"] == ["src"]
            assert data["lazyDirectories"] == ["src"]
        else:
            assert sorted(data["paths"]) == [
                ".gitignore",
                "a.txt",
                "src/mod.py",
                "src/pkg/inner.py",
            ]
            assert data["directories"] == ["src", "src/pkg"]
            assert data["lazyDirectories"] == []
        assert "ignored.log" not in data["paths"]

    @pytest.mark.asyncio
    async def test_hidden_files_ride_a_one_level_listing(self, repo, mock_sel):  # noqa: F811
        """``hidden=1`` adds the ignored dot-files; ``shallow=1`` still reads one level."""
        from kiro_crew.dashboard.handlers import files as files_mod

        (repo / ".gitignore").write_text("ignored.log\n.local\n")
        (repo / ".local").write_text("x")
        (repo / "src" / ".local").write_text("x")
        status, data = await _get(repo, shallow="1", hidden="1")
        assert status == 200
        assert ".local" in data["paths"]
        if files_mod._PROJECT_TREE_SCANDIR_TAKES_FD and files_mod.pinned_fs.supports_pinned_walk():
            # One level: the folder's own dot-file waits for the folder's listing.
            assert data["lazyDirectories"] == ["src"]
            assert "src/.local" not in data["paths"]
        status, plain = await _get(repo, shallow="1")
        assert ".local" not in plain["paths"]

    @pytest.mark.asyncio
    async def test_hidden_files_ride_a_walked_folder_too(
        self, repo, mock_sel, monkeypatch  # noqa: F811
    ):
        """Where a folder listing walks (Windows), ``hidden=1`` keeps its ignored dot-files."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_SCANDIR_TAKES_FD", False)
        (repo / ".gitignore").write_text("ignored.log\n.local\n")
        (repo / "src" / ".local").write_text("x")
        (repo / "src" / "ignored.log").write_text("x")
        (repo / "src" / "pkg").mkdir()
        (repo / "src" / "pkg" / "inner.py").write_text("x")
        status, data = await _get(repo, shallow="1", hidden="1", dir="src")
        assert status == 200
        assert "src/.local" in data["paths"]
        assert "src/ignored.log" not in data["paths"]
        assert data["lazyDirectories"] == ["src/pkg"]
        status, plain = await _get(repo, shallow="1", dir="src")
        assert "src/.local" not in plain["paths"]

    @pytest.mark.asyncio
    async def test_a_walked_folder_still_hides_what_git_ignores(
        self, repo, mock_sel, monkeypatch  # noqa: F811
    ):
        """Where folders cannot be pinned (Windows) the walk lists them, not git."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_SCANDIR_TAKES_FD", False)
        (repo / "src" / "ignored.log").write_text("x")
        (repo / "src" / "kept.py").write_text("x")
        status, data = await _get(repo, shallow="1", dir="src")
        assert status == 200
        assert data["repo"] is False
        assert sorted(data["paths"]) == ["src/kept.py", "src/mod.py"]

    def test_split_keeps_direct_files_and_first_segments(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "docs").mkdir()
        files, nested, lazy = _project_tree_shallow_git(
            ["a.txt", "src/mod.py", "src/pkg/x.py", "docs/i.md"],
            {"vendor", "src/sub"},
            str(tmp_path),
        )
        assert files == ["a.txt"]
        assert nested == {"vendor"}
        assert lazy == {"src", "docs"}

    def test_a_deleted_folder_keeps_its_nested_repositories_whole(self, tmp_path):
        """A folder not on disk is never listed on its own, so nothing under it waits."""
        (tmp_path / "src").mkdir()
        files, nested, lazy = _project_tree_shallow_git(
            ["vendor/README.md", "src/mod.py"], {"vendor/lib", "src/sub"}, str(tmp_path)
        )
        assert files == ["vendor/README.md"]
        assert nested == {"vendor/lib"}
        assert lazy == {"src"}

    @pytest.mark.asyncio
    async def test_a_folder_deleted_on_disk_keeps_its_tracked_rows(
        self, repo, mock_sel  # noqa: F811
    ):
        """git's index still names files under a folder the agent removed."""
        import shutil

        shutil.rmtree(repo / "src")
        status, data = await _get(repo, shallow="1")
        assert status == 200
        assert "src/mod.py" in data["paths"]
        assert data["lazyDirectories"] == []

    @pytest.mark.asyncio
    async def test_where_folders_cannot_be_pinned_a_repository_lists_whole(
        self, repo, mock_sel, monkeypatch  # noqa: F811
    ):
        """There a folder listing walks, which cannot name every tracked path."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_SCANDIR_TAKES_FD", False)
        status, data = await _get(repo, shallow="1")
        assert status == 200
        assert "src/mod.py" in data["paths"]
        assert data["lazyDirectories"] == []
