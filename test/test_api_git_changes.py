"""Tests for api_git_changes handler in dashboard/handlers/files.py."""

from __future__ import annotations

import json
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers.files import (
    _discover_git_repos,
    _git_status_label,
    api_git_changes,
)

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _req(dir_path: str = "") -> make_mocked_request:
    url = f"/api/git-changes?dir={dir_path}" if dir_path else "/api/git-changes"
    return make_mocked_request("GET", url)


def _mock_sel():
    sel = MagicMock()
    sel.log_api_access = MagicMock()
    return sel


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True)
    (path / "init.txt").write_text("x")
    subprocess.run(["git", "add", "init.txt"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, check=True)


class TestGitStatusLabel:
    def test_untracked(self):
        assert _git_status_label("??") == "untracked"

    def test_conflicted(self):
        assert _git_status_label("UU") == "conflicted"
        assert _git_status_label("AA") == "conflicted"
        assert _git_status_label("DD") == "conflicted"

    def test_worktree_column_preferred(self):
        assert _git_status_label(" M") == "modified"
        assert _git_status_label(" D") == "deleted"

    def test_index_column_fallback(self):
        assert _git_status_label("A ") == "added"
        assert _git_status_label("M ") == "modified"


class TestApiGitChanges:
    @pytest.mark.asyncio
    async def test_missing_dir_returns_400(self):
        with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
            resp = await api_git_changes(_req(""))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_nonexistent_dir_returns_empty(self):
        with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
            resp = await api_git_changes(_req("/tmp/nonexistent_dir_abc123"))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["repos"] == []

    @pytest.mark.asyncio
    async def test_sensitive_dir_returns_403(self, tmp_path):
        with patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True), \
             patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
            resp = await api_git_changes(_req(str(tmp_path)))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_non_git_dir_returns_no_repos(self, tmp_path):
        (tmp_path / "plain.txt").write_text("x")
        with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
            resp = await api_git_changes(_req(str(tmp_path)))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["repos"] == []

    @pytest.mark.asyncio
    @requires_git
    async def test_clean_repo_reports_empty_files(self, tmp_path):
        _git_repo(tmp_path)
        with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
            resp = await api_git_changes(_req(str(tmp_path)))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert len(body["repos"]) == 1
        repo = body["repos"][0]
        assert repo["files"] == []
        assert repo["branch"]
        assert repo["name"] == tmp_path.name

    @pytest.mark.asyncio
    @requires_git
    async def test_modified_and_untracked_files(self, tmp_path):
        _git_repo(tmp_path)
        (tmp_path / "init.txt").write_text("changed\nlines\n")
        (tmp_path / "new.txt").write_text("brand new")
        with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
            resp = await api_git_changes(_req(str(tmp_path)))
        body = json.loads(resp.body)
        files = {f["rel"]: f for f in body["repos"][0]["files"]}
        assert files["init.txt"]["status"] == "modified"
        assert files["init.txt"]["staged"] is False
        # numstat counts: init.txt went from "x" (1 line) to 2 lines.
        assert files["init.txt"]["additions"] == 2
        assert files["init.txt"]["deletions"] == 1
        assert files["new.txt"]["status"] == "untracked"
        # Untracked files have no numstat entry — counts absent.
        assert "additions" not in files["new.txt"]
        assert files["new.txt"]["path"].endswith("new.txt")

    @pytest.mark.asyncio
    @requires_git
    async def test_staged_file_flagged(self, tmp_path):
        _git_repo(tmp_path)
        (tmp_path / "staged.txt").write_text("s")
        subprocess.run(["git", "add", "staged.txt"], cwd=tmp_path, capture_output=True, check=True)
        with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
            resp = await api_git_changes(_req(str(tmp_path)))
        body = json.loads(resp.body)
        files = {f["rel"]: f for f in body["repos"][0]["files"]}
        assert files["staged.txt"]["status"] == "added"
        assert files["staged.txt"]["staged"] is True

    @pytest.mark.asyncio
    @requires_git
    async def test_multi_repo_workspace_children(self, tmp_path):
        _git_repo(tmp_path / "repo_a")
        _git_repo(tmp_path / "repo_b")
        (tmp_path / "repo_b" / "init.txt").write_text("changed")
        with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
            resp = await api_git_changes(_req(str(tmp_path)))
        body = json.loads(resp.body)
        by_name = {r["name"]: r for r in body["repos"]}
        assert set(by_name) == {"repo_a", "repo_b"}
        assert by_name["repo_a"]["files"] == []
        assert by_name["repo_b"]["files"][0]["rel"] == "init.txt"

    @pytest.mark.asyncio
    @requires_git
    async def test_src_layout_discovered(self, tmp_path):
        _git_repo(tmp_path / "src" / "PackageA")
        with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
            resp = await api_git_changes(_req(str(tmp_path)))
        body = json.loads(resp.body)
        assert [r["name"] for r in body["repos"]] == ["PackageA"]

    @pytest.mark.asyncio
    @requires_git
    async def test_linked_worktree_discovered(self, tmp_path):
        # Linked worktrees represent .git as a FILE (gitdir pointer), not a dir.
        _git_repo(tmp_path / "main_repo")
        wt = tmp_path / "wt"
        subprocess.run(
            ["git", "worktree", "add", "-b", "feature", str(wt)],
            cwd=tmp_path / "main_repo", capture_output=True, check=True,
        )
        (wt / "init.txt").write_text("changed in worktree")
        with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
            resp = await api_git_changes(_req(str(wt)))
        body = json.loads(resp.body)
        assert len(body["repos"]) == 1
        files = {f["rel"]: f for f in body["repos"][0]["files"]}
        assert files["init.txt"]["status"] == "modified"

    @pytest.mark.asyncio
    @requires_git
    async def test_filename_with_whitespace_survives(self, tmp_path):
        # -z parsing: no quote-stripping, no .strip() — edge whitespace intact.
        _git_repo(tmp_path)
        (tmp_path / " padded name .txt").write_text("x")
        with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
            resp = await api_git_changes(_req(str(tmp_path)))
        body = json.loads(resp.body)
        rels = [f["rel"] for f in body["repos"][0]["files"]]
        assert " padded name .txt" in rels

    @pytest.mark.asyncio
    @requires_git
    async def test_sel_audit_logging(self, tmp_path):
        _git_repo(tmp_path)
        mock_sel = _mock_sel()
        with patch("kiro_crew.dashboard.handlers.files._sel", return_value=mock_sel):
            await api_git_changes(_req(str(tmp_path)))
        mock_sel.log_api_access.assert_called_once()
        assert mock_sel.log_api_access.call_args[1]["operation"] == "git_changes"
        assert mock_sel.log_api_access.call_args[1]["outcome"] == "allowed"


class TestDiscoverGitRepos:
    @requires_git
    def test_repo_itself(self, tmp_path):
        _git_repo(tmp_path)
        assert _discover_git_repos(str(tmp_path)) == [str(tmp_path.resolve())]

    def test_empty_dir(self, tmp_path):
        assert _discover_git_repos(str(tmp_path)) == []

    @requires_git
    def test_hidden_children_skipped(self, tmp_path):
        _git_repo(tmp_path / ".hidden_repo")
        assert _discover_git_repos(str(tmp_path)) == []
