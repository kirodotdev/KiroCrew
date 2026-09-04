"""Tests for ``GET /api/project/tree``."""

from __future__ import annotations

import os
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_project_tree


class _Slot:
    def __init__(self, project: str) -> None:
        self.project = project


class _State:
    def __init__(self, *projects: str) -> None:
        self._slots = {f"s{i}": _Slot(p) for i, p in enumerate(projects)}


def _make_app(*known: str) -> web.Application:
    app = web.Application()
    app["state"] = _State(*known)
    app.router.add_get("/api/project/tree", api_project_tree)
    return app


@pytest.fixture(autouse=True)
def passthrough_sandbox(monkeypatch):
    """Run git unwrapped: CI runners have no sandbox backend, and the handlers
    fail CLOSED without one. The chokepoint's own behavior is covered by
    test_sandbox*/test_spawn_audit; these tests exercise the listing logic.
    """
    from kiro_crew.dashboard.handlers import files as files_mod

    monkeypatch.setattr(
        files_mod,
        "sandboxed_spawn_argv",
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
    root = tmp_path_factory.mktemp("tree-seed") / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "a.txt").write_text("line1\n")
    (root / "src").mkdir()
    (root / "src" / "mod.py").write_text("x = 1\n")
    (root / ".gitignore").write_text("ignored.log\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial commit")
    return root


@pytest.fixture()
def repo(tmp_path, _repo_template):
    root = tmp_path / "proj"
    shutil.copytree(_repo_template, root)
    return root


class TestProjectTree:
    @pytest.mark.asyncio
    async def test_missing_path_is_400(self, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/project/tree")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_project_is_403(self, tmp_path, mock_sel):
        known = tmp_path / "known"
        known.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        async with TestClient(TestServer(_make_app(str(known)))) as client:
            resp = await client.get(f"/api/project/tree?path={other}")
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_vanished_directory_response_is_redacted(self, tmp_path, mock_sel, monkeypatch):
        """A known project dir deleted between the allow-list match and the stat.

        The early return still echoes the path, so it goes through the same egress
        redaction as the listing below it -- a project directory can carry a
        credential-shaped segment, and this arm is reachable, not defensive.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        known = tmp_path / "AKIAIOSFODNN7EXAMPLE"
        known.mkdir()
        monkeypatch.setattr(files_mod.os.path, "isdir", lambda p: False)
        async with TestClient(TestServer(_make_app(str(known)))) as client:
            resp = await client.get(f"/api/project/tree?path={known}")
            data = await resp.json()
        assert data["paths"] == []
        assert "AKIAIOSFODNN7EXAMPLE" not in data["root"]

    @pytest.mark.asyncio
    async def test_git_repo_lists_tracked_and_untracked(self, repo, mock_sel):
        (repo / "untracked.md").write_text("hi\n")
        (repo / "ignored.log").write_text("nope\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/tree?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert "a.txt" in data["paths"]
        assert "src/mod.py" in data["paths"]
        assert "untracked.md" in data["paths"]
        assert "ignored.log" not in data["paths"]

    @pytest.mark.asyncio
    async def test_listing_disables_the_repo_writable_fsmonitor_hook(
        self, repo, mock_sel, monkeypatch
    ):
        # `core.fsmonitor` names a command git SPAWNS and lives in the
        # repository's own config, which an agent can write — so a tree listing
        # must not let it run. Pinned on the argv because the flag is invisible
        # in the response: a listing with the hook enabled looks identical.
        seen: list[list[str]] = []
        from kiro_crew.dashboard.handlers import files as files_mod

        real = files_mod._run_git_bounded

        def spy(argv, **kwargs):
            seen.append(list(argv))
            return real(argv, **kwargs)

        monkeypatch.setattr(files_mod, "_run_git_bounded", spy)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/tree?path={repo}")
            assert resp.status == 200
        ls_argv = next(a for a in seen if "ls-files" in a)
        assert "core.fsmonitor=" in ls_argv
        assert ls_argv.index("-c") < ls_argv.index("ls-files")

    @pytest.mark.asyncio
    async def test_non_repo_walk_skips_heavy_and_hidden_dirs(self, tmp_path, mock_sel):
        plain = tmp_path / "plain"
        (plain / "node_modules" / "dep").mkdir(parents=True)
        (plain / "node_modules" / "dep" / "index.js").write_text("x")
        (plain / ".hidden").mkdir()
        (plain / ".hidden" / "secret.txt").write_text("x")
        (plain / "docs").mkdir()
        (plain / "docs" / "readme.md").write_text("x")
        (plain / "top.txt").write_text("x")
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()
        assert data["repo"] is False
        assert sorted(data["paths"]) == ["docs/readme.md", "top.txt"]

    @pytest.mark.asyncio
    async def test_redaction_collision_paths_are_deduplicated(self, repo, mock_sel):
        """Two genuinely-different paths that redact() collapses to one string
        must not appear twice in the listing.

        Uses a real ls-files collision: two files whose only differing segment is
        a credential-shaped token (distinct AKIA... ids, each 4-letter prefix +
        16 uppercase alphanumerics) both flatten to
        ``[REDACTED: credential]_model.txt``. Without server-side de-dup the
        dashboard tree hands @pierre/trees two adjacent identical entries and its
        ``appendPresortedPaths`` throws ``Duplicate path``.
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
            resp = await client.get(f"/api/project/tree?path={repo}")
            data = await resp.json()
        paths = data["paths"]
        # Both filenames collapsed to the same redacted placeholder...
        assert "[REDACTED: credential]_model.txt" in paths
        # ...but it appears exactly once (first occurrence kept), and the raw
        # credential-shaped tokens never leak.
        assert paths.count("[REDACTED: credential]_model.txt") == 1
        assert len(paths) == len(set(paths))
        assert key_a not in "\n".join(paths)
        assert key_b not in "\n".join(paths)
        # Non-colliding entries survive unchanged.
        assert "a.txt" in paths
        assert "src/mod.py" in paths

    @pytest.mark.asyncio
    async def test_walk_caps_entries_and_flags_truncation(self, tmp_path, mock_sel, monkeypatch):
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 2)
        plain = tmp_path / "plain"
        plain.mkdir()
        for name in ("a.txt", "b.txt", "c.txt"):
            (plain / name).write_text("x")
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()
        assert data["truncated"] is True
        assert len(data["paths"]) == 2

    @pytest.mark.asyncio
    async def test_cap_does_not_drop_the_whole_tracked_block(self, tmp_path, mock_sel, monkeypatch):
        """A fat UNTRACKED subtree must not evict every tracked file.

        ``git ls-files --cached --others`` emits all untracked entries as one
        complete block and only then the tracked ones, so capping with a plain
        prefix cut never reaches the tracked block once untracked alone fill it
        -- the whole source tree loses its rows. The listing is sorted before
        the cut so the budget is spent by path, not by which block git happened
        to emit first.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 20)
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", ".")
        tracked = ("README.md", "docs/real.py", "src/real.py")
        for rel in tracked:
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
        # Untracked (NOT ignored) and larger than the cap on its own. Named to
        # sort after every tracked path, so a sorted cut reaches them all.
        fat = repo / "zz_vendor" / "deep"
        fat.mkdir(parents=True)
        for i in range(40):
            (fat / f"u{i:04d}.txt").write_text("x")

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/tree?path={repo}")
            data = await resp.json()

        assert data["repo"] is True
        assert data["truncated"] is True
        assert len(data["paths"]) == 20
        # The point of the fix: every tracked file keeps a row.
        for rel in tracked:
            assert rel in data["paths"], f"{rel} was evicted by the untracked block"
        # ...and the fix does not merely invert the loss: the untracked subtree
        # still spends the remaining budget, so it keeps a row too.
        assert any(p.startswith("zz_vendor/") for p in data["paths"])
