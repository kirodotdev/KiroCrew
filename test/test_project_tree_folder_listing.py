"""Tests for ``GET /api/project/tree?dir=``: one folder's own listing.

Split from ``test_project_tree.py`` (the file line cap); the app, the sandbox
passthrough and the project fixtures are that module's, imported so pytest
registers them here too.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_project_tree import (  # noqa: F401
    _make_app,
    _repo_template,
    mock_sel,
    passthrough_sandbox,
    plain_project,
    repo,
)

from kiro_crew.testing.links import make_dir_link


class TestProjectTreeFolderListing:
    """``?dir=``: one folder's listing, for a folder past the whole tree's cap."""

    @staticmethod
    async def _get(project: Path, **query: str) -> tuple[int, dict]:
        async with TestClient(TestServer(_make_app(str(project)))) as client:
            resp = await client.get("/api/project/tree", params={"path": str(project), **query})
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_a_folder_cut_by_the_cap_is_listed_on_its_own(
        self, plain_project, mock_sel, monkeypatch  # noqa: F811
    ):
        """The whole listing names ``big`` as cut; its own listing reaches the
        folder the cap hid, with paths still relative to the project root."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 4)
        big = plain_project / "big"
        for i in range(6):
            (big / f"f{i}.txt").parent.mkdir(parents=True, exist_ok=True)
            (big / f"f{i}.txt").write_text("x")
        (big / "zz_late" / "deep").mkdir(parents=True)
        (big / "zz_late" / "deep" / "leaf.txt").write_text("x")

        status, whole = await self._get(plain_project)
        assert status == 200
        assert "big" in whole["truncatedDirectories"]
        assert "big/zz_late/deep" not in whole["directories"]

        status, folder = await self._get(plain_project, dir="big/zz_late")
        assert status == 200
        assert folder["dir"] == "big/zz_late"
        assert folder["directories"] == ["big/zz_late/deep"]
        assert folder["paths"] == ["big/zz_late/deep/leaf.txt"]
        assert folder["truncatedDirectories"] == []

    @pytest.mark.asyncio
    async def test_a_folder_listing_inside_a_repository_is_project_relative(
        self, repo, mock_sel  # noqa: F811
    ):  # noqa: F811
        from kiro_crew import pinned_fs
        from kiro_crew.dashboard.handlers import files as files_mod

        status, data = await self._get(repo, dir="src")
        assert status == 200
        # git lists it where the folder can be pinned by descriptor; elsewhere
        # (Windows) the identity-checked walk does, with the same rows.
        pinnable = files_mod._PROJECT_TREE_SCANDIR_TAKES_FD and pinned_fs.supports_pinned_walk()
        assert data["repo"] is bool(pinnable)
        assert data["paths"] == ["src/mod.py"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw",
        ["..", "../outside", "a/../../outside", "./a", "a//b", "/etc", "a/", "a/.", "x\0y"],
    )
    async def test_a_dir_that_is_not_a_plain_inside_path_is_refused(
        self, plain_project, mock_sel, raw  # noqa: F811
    ):
        (plain_project / "a" / "b").mkdir(parents=True)
        (plain_project.parent / "outside").mkdir()
        (plain_project.parent / "outside" / "secret.txt").write_text("x")
        status, data = await self._get(plain_project, dir=raw)
        assert status == 403
        assert data["code"] == "dir_refused"
        assert "paths" not in data

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.sep != "/", reason="a colon is not a name character on Windows")
    async def test_a_folder_name_with_a_colon_is_listed_on_posix(
        self, plain_project, mock_sel  # noqa: F811
    ):  # noqa: F811
        (plain_project / "a:b").mkdir(parents=True)
        (plain_project / "a:b" / "f.txt").write_text("x")
        status, data = await self._get(plain_project, dir="a:b")
        assert status == 200
        assert data["paths"] == ["a:b/f.txt"]

    @pytest.mark.asyncio
    async def test_the_echoed_folder_takes_the_same_redaction_as_the_rows(
        self, plain_project, mock_sel  # noqa: F811
    ):
        secret = "AKIAIOSFODNN7EXAMPLE"
        (plain_project / secret).mkdir(parents=True)
        (plain_project / secret / "f.txt").write_text("x")
        status, data = await self._get(plain_project, dir=secret)
        assert status == 200
        assert secret not in json.dumps(data)
        assert data["paths"] == [f"{data['dir']}/f.txt"]

    @pytest.mark.asyncio
    async def test_a_link_out_of_the_project_is_never_followed(
        self, plain_project, mock_sel  # noqa: F811
    ):  # noqa: F811
        """A folder name inside the root that is a link to a directory outside it
        is refused, and so is any path beneath it: nothing outside is read."""
        outside = plain_project.parent / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("x")
        (outside / "sub").mkdir()
        plain_project.mkdir()
        make_dir_link(plain_project / "escape", outside)

        for raw in ("escape", "escape/sub"):
            status, data = await self._get(plain_project, dir=raw)
            assert status == 403, raw
            assert data["code"] == "dir_refused"
            assert "secret.txt" not in json.dumps(data)

    @pytest.mark.asyncio
    async def test_a_sensitive_folder_is_refused(
        self, plain_project, mock_sel, monkeypatch  # noqa: F811
    ):  # noqa: F811
        from kiro_crew.dashboard.handlers import files as files_mod

        (plain_project / "keys").mkdir(parents=True)
        real = os.path.realpath(plain_project / "keys")
        monkeypatch.setattr(files_mod, "is_sensitive_path", lambda p: p == real)
        status, data = await self._get(plain_project, dir="keys")
        assert status == 403
        assert data["code"] == "dir_refused"

    @pytest.mark.asyncio
    async def test_a_missing_folder_is_404(self, plain_project, mock_sel):  # noqa: F811
        plain_project.mkdir()
        (plain_project / "file.txt").write_text("x")
        for raw in ("gone", "file.txt"):
            status, data = await self._get(plain_project, dir=raw)
            assert status == 404, raw
            assert data["code"] == "dir_not_found"

    @pytest.mark.asyncio
    async def test_the_project_checks_still_run_first(self, tmp_path, mock_sel):  # noqa: F811
        """``dir`` cannot reach a project the allow-list does not name."""
        known = tmp_path / "known"
        known.mkdir()
        other = tmp_path / "other"
        (other / "sub").mkdir(parents=True)
        async with TestClient(TestServer(_make_app(str(known)))) as client:
            resp = await client.get("/api/project/tree", params={"path": str(other), "dir": "sub"})
            data = await resp.json()
        assert resp.status == 403
        assert data["code"] == "unknown_project_dir"


class TestProjectTreeFolderListingIsPinned:
    """An ancestor swapped for a link after ``?dir=`` is checked reads nothing outside."""

    @staticmethod
    def _swap_after_pin(monkeypatch, project: Path, outside: Path) -> None:
        from kiro_crew.dashboard.handlers import files as files_mod

        real_pin = files_mod._project_tree_pin

        def pin_then_swap(base, segments):
            identity = real_pin(base, segments)
            (project / "a").rename(project / "a-moved")
            make_dir_link(project / "a", outside)
            return identity

        monkeypatch.setattr(files_mod, "_project_tree_pin", pin_then_swap)

    @pytest.mark.asyncio
    async def test_the_walk_reads_nothing_behind_a_swapped_ancestor(
        self, plain_project, mock_sel, monkeypatch  # noqa: F811
    ):
        (plain_project / "a" / "b").mkdir(parents=True)
        (plain_project / "a" / "b" / "inside.txt").write_text("x")
        outside = plain_project.parent / "outside"
        (outside / "b").mkdir(parents=True)
        (outside / "b" / "secret.txt").write_text("x")
        self._swap_after_pin(monkeypatch, plain_project, outside)

        async with TestClient(TestServer(_make_app(str(plain_project)))) as client:
            resp = await client.get(
                "/api/project/tree", params={"path": str(plain_project), "dir": "a/b"}
            )
            data = await resp.json()
        assert resp.status == 200
        assert "secret.txt" not in json.dumps(data)
        assert data["paths"] == []
        assert data["unreadableDirectories"] == ["a/b"]

    @pytest.mark.asyncio
    async def test_git_lists_nothing_behind_a_swapped_ancestor(
        self, repo, mock_sel, monkeypatch  # noqa: F811
    ):  # noqa: F811
        (repo / "a" / "b").mkdir(parents=True)
        (repo / "a" / "b" / "inside.txt").write_text("x")
        outside = repo.parent / "outside"
        (outside / "b").mkdir(parents=True)
        (outside / "b" / "secret.txt").write_text("x")
        self._swap_after_pin(monkeypatch, repo, outside)

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get("/api/project/tree", params={"path": str(repo), "dir": "a/b"})
            data = await resp.json()
        assert resp.status == 200
        assert "secret.txt" not in json.dumps(data)

    @pytest.mark.asyncio
    async def test_a_platform_that_cannot_pin_lists_with_the_walk_not_git(
        self, repo, mock_sel, monkeypatch  # noqa: F811
    ):
        """Windows: git walks into a junction, so a folder listing skips git.

        Simulated by turning the descriptor pin off; the by-name walk then
        compares each folder's identity and names the swapped one unreadable.
        """
        from kiro_crew import pinned_fs
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_SCANDIR_TAKES_FD", False)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        (repo / "a" / "b").mkdir(parents=True)
        (repo / "a" / "b" / "inside.txt").write_text("x")
        outside = repo.parent / "outside"
        (outside / "b").mkdir(parents=True)
        (outside / "b" / "secret.txt").write_text("x")
        self._swap_after_pin(monkeypatch, repo, outside)

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get("/api/project/tree", params={"path": str(repo), "dir": "a/b"})
            data = await resp.json()
        assert resp.status == 200
        assert data["repo"] is False
        assert "secret.txt" not in json.dumps(data)
