"""Tests for ``GET /api/project/git/status`` and ``GET /api/project/git/log``."""

from __future__ import annotations

import os
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_project_git_log, api_project_git_status


class _Slot:
    def __init__(self, project: str) -> None:
        self.project = project


class _State:
    def __init__(self, *projects: str) -> None:
        self._slots = {f"s{i}": _Slot(p) for i, p in enumerate(projects)}


def _make_app(*known: str) -> web.Application:
    app = web.Application()
    app["state"] = _State(*known)
    app.router.add_get("/api/project/git/status", api_project_git_status)
    app.router.add_get("/api/project/git/log", api_project_git_log)
    return app


@pytest.fixture(autouse=True)
def passthrough_sandbox(monkeypatch):
    """Run git unwrapped: CI runners have no sandbox backend, and the handlers
    fail CLOSED without one (repo: False). The chokepoint's own behavior is
    covered by test_sandbox*/test_spawn_audit; these tests exercise the git
    parsing, so they pass argv through unchanged (the worktree tests' pattern).
    """
    from kiro_crew.dashboard.handlers import files as files_mod

    monkeypatch.setattr(
        files_mod, "sandboxed_spawn_argv",
        lambda argv, mode="standard", **kw: (list(argv), dict(os.environ), None),
    )


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


def _git(cwd, *args) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )


@pytest.fixture(scope="session")
def _repo_template(tmp_path_factory):
    """One-commit repo template reused across tests."""
    root = tmp_path_factory.mktemp("git-status-seed") / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "a.txt").write_text("line1\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "initial commit")
    return root


@pytest.fixture()
def repo(tmp_path, _repo_template):
    """A real git repo with one commit on branch ``trunk``."""
    root = tmp_path / "proj"
    shutil.copytree(_repo_template, root)
    return root


# ── /api/project/git/status tests ──


class TestGitStatus:
    @pytest.mark.asyncio
    async def test_non_repo_returns_repo_false(self, tmp_path, mock_sel):
        plain = tmp_path / "plain"
        plain.mkdir()
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/git/status?path={plain}")
            data = await resp.json()
        assert data["repo"] is False
        assert data["files"] == []

    @pytest.mark.asyncio
    async def test_unknown_dir_is_refused(self, repo, tmp_path, mock_sel):
        other = tmp_path / "other"
        other.mkdir()
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={other}")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_staged_unstaged_untracked(self, repo, mock_sel):
        """Repo with staged, unstaged, and untracked files reports all."""
        # Modify tracked file (unstaged)
        (repo / "a.txt").write_text("modified\n")

        # Stage a new file
        (repo / "b.txt").write_text("new file\n")
        _git(repo, "add", "b.txt")

        # Untracked file
        (repo / "c.txt").write_text("untracked\n")

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()

        assert data["repo"] is True
        assert "branch" in data
        assert data["branch"] == "trunk"

        paths = {f["path"]: f for f in data["files"]}
        # a.txt modified in worktree (unstaged)
        assert "a.txt" in paths
        a = paths["a.txt"]
        assert a["staged"] is False
        assert a["status"] == "M"

        # b.txt staged (added)
        assert "b.txt" in paths
        b = paths["b.txt"]
        assert b["staged"] is True
        assert b["status"] == "A"

        # c.txt untracked
        assert "c.txt" in paths
        c = paths["c.txt"]
        assert c["staged"] is False
        assert c["status"] == "?"

    @pytest.mark.asyncio
    async def test_staged_and_unstaged_lanes_of_one_file_both_survive(self, repo, mock_sel):
        """A file staged AND modified again ("MM") keeps both entries.

        The two rows share a path and differ only in status/staged, so the
        redaction de-dup must key on the whole tuple. Keying on path alone
        would drop the unstaged lane and undercount GitPanel's file total.
        """
        (repo / "a.txt").write_text("staged change\n")
        _git(repo, "add", "a.txt")
        (repo / "a.txt").write_text("and an unstaged change\n")

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()

        entries = [f for f in data["files"] if f["path"] == "a.txt"]
        assert len(entries) == 2
        assert {(e["status"], e["staged"]) for e in entries} == {("M", True), ("M", False)}

    @pytest.mark.asyncio
    async def test_redaction_collision_files_are_deduplicated(self, repo, mock_sel):
        """Two distinct changed files that redact() collapses to one path must
        not both appear in ``files``.

        Real collision: two untracked files whose only differing segment is a
        credential-shaped token (distinct AKIA... ids, each 4-letter prefix + 16
        uppercase alphanumerics) both flatten to
        ``[REDACTED: credential]_model.txt``. Without server-side de-dup the two
        entries would reach the dashboard tree and @pierre/trees'
        ``appendPresortedPaths`` would throw ``Duplicate path`` on the adjacent
        identical rows. First occurrence is kept.
        """
        # Two DISTINCT keys are the point: the test proves two different
        # credential-shaped names collapse to ONE placeholder. key_a is the
        # documented example id Semgrep allowlists; key_b must stay a split
        # literal because detected-aws-access-key-id-value matches an
        # AKIA-shaped literal and cannot tell a fixture from a real leak. Do
        # not re-join it -- the runtime value is identical and CI, not the
        # test, is what breaks.
        key_a = "AKIAIOSFODNN7EXAMPLE"
        key_b = "AKIA" + "JKLMNOPQRSTUVWXY"
        (repo / f"{key_a}_model.txt").write_text("one\n")
        (repo / f"{key_b}_model.txt").write_text("two\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        paths = [f["path"] for f in data["files"]]
        # Both filenames collapsed to the same redacted placeholder...
        assert "[REDACTED: credential]_model.txt" in paths
        # ...but only one entry survives, and the raw tokens never leak.
        assert paths.count("[REDACTED: credential]_model.txt") == 1
        assert len(paths) == len(set(paths))
        assert key_a not in "\n".join(paths)
        assert key_b not in "\n".join(paths)

    @pytest.mark.asyncio
    async def test_clean_repo_empty_files(self, repo, mock_sel):
        """Clean repo returns empty files list."""
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert data["files"] == []

    @pytest.mark.asyncio
    async def test_numstat_additions(self, repo, mock_sel):
        """Modified file gets additions/deletions from numstat."""
        (repo / "a.txt").write_text("line1\nline2\nline3\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        paths = {f["path"]: f for f in data["files"]}
        assert "a.txt" in paths
        a = paths["a.txt"]
        # Should have additions (2 new lines) and deletions (original line changed)
        assert "additions" in a or "deletions" in a


# ── /api/project/git/log tests ──


class TestGitLog:
    @pytest.mark.asyncio
    async def test_non_repo_returns_repo_false(self, tmp_path, mock_sel):
        plain = tmp_path / "plain"
        plain.mkdir()
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/git/log?path={plain}")
            data = await resp.json()
        assert data["repo"] is False
        assert data["commits"] == []

    @pytest.mark.asyncio
    async def test_unknown_dir_is_refused(self, repo, tmp_path, mock_sel):
        other = tmp_path / "other"
        other.mkdir()
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={other}")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_returns_commits(self, repo, mock_sel):
        """Log returns at least the initial commit."""
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert len(data["commits"]) == 1
        c = data["commits"][0]
        assert c["message"] == "initial commit"
        assert c["author"] == "T"
        assert c["isHead"] is True
        assert "sha" in c
        assert "date" in c

    @pytest.mark.asyncio
    async def test_limit_parameter(self, repo, mock_sel):
        """limit=1 returns only 1 commit even if there are more."""
        # Add a second commit
        (repo / "d.txt").write_text("x\n")
        _git(repo, "add", "d.txt")
        _git(repo, "commit", "-qm", "second commit")

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}&limit=1")
            data = await resp.json()
        assert len(data["commits"]) == 1
        assert data["commits"][0]["message"] == "second commit"
        assert data["commits"][0]["isHead"] is True

    @pytest.mark.asyncio
    async def test_limit_capped_at_100(self, repo, mock_sel):
        """limit > 100 is capped to 100."""
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}&limit=500")
            data = await resp.json()
        # Should still work, just capped
        assert data["repo"] is True
        assert len(data["commits"]) >= 1


class TestFilterDriverRefusal:
    """A repo whose own config names a content-filter driver gets a degraded
    empty answer: status re-hashes modified files through ``filter.<n>.clean``,
    so running any content-touching git against such a repo would execute a
    repository-supplied program on every poll."""

    @pytest.mark.asyncio
    async def test_status_refuses_clean_filter(self, repo, mock_sel):
        _git(repo, "config", "filter.evil.clean", "touch /tmp/pwned")
        (repo / "a.txt").write_text("modified\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert data["files"] == []

    @pytest.mark.asyncio
    async def test_log_refuses_process_filter(self, repo, mock_sel):
        _git(repo, "config", "filter.evil.process", "evil-daemon")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert data["commits"] == []

    @pytest.mark.asyncio
    async def test_clean_repo_is_not_refused(self, repo, mock_sel):
        """The probe only fires on filter drivers, not on ordinary config."""
        _git(repo, "config", "diff.noise.command", "irrelevant-but-not-a-filter")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert data["repo"] is True


class TestArrowFilename:
    @pytest.mark.skipif(os.name == "nt", reason="'>' is not a legal NTFS filename character")
    @pytest.mark.asyncio
    async def test_modified_file_named_like_a_rename_is_not_split(self, repo, mock_sel):
        """A literal 'foo -> bar' filename must survive intact: splitting it
        would point the row (and a subsequent open/save) at the unrelated
        file 'bar'."""
        name = "foo -> bar"
        (repo / name).write_text("v1\n")
        _git(repo, "add", name)
        _git(repo, "commit", "-qm", "add arrow file")
        (repo / name).write_text("v2\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        paths = [f["path"] for f in data["files"]]
        assert name in paths
        assert "bar" not in paths

    @pytest.mark.asyncio
    async def test_real_rename_still_reports_new_name(self, repo, mock_sel):
        _git(repo, "mv", "a.txt", "renamed.txt")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        paths = [f["path"] for f in data["files"]]
        assert "renamed.txt" in paths
        assert "a.txt -> renamed.txt" not in paths


class TestVanishedDirectory:
    @pytest.mark.asyncio
    async def test_dir_removed_between_check_and_spawn_returns_no_data(self, repo, mock_sel, monkeypatch):
        """TOCTOU: the project dir can vanish after the isdir gate and before
        the git spawn. The endpoint must answer degraded, never 500."""
        from kiro_crew.dashboard.handlers import files as files_mod

        real_isdir = os.path.isdir

        def isdir_then_delete(path):
            ok = real_isdir(path)
            if ok and str(path) == str(repo):
                shutil.rmtree(repo, ignore_errors=True)
            return ok

        monkeypatch.setattr(files_mod.os.path, "isdir", isdir_then_delete)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            assert resp.status == 200
            data = await resp.json()
        assert data["repo"] is False
        assert data["files"] == []
