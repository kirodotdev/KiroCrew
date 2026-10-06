"""Tests for the Git panel's branch switcher routes.

``GET /api/project/git/branches`` and ``POST /api/project/git/switch`` run real
git (through the worktree module's sandboxed ``_run_git``) against a throwaway
repository, so these tests skip where the OS sandbox cannot run git, exactly as
``test_worktree_create.py`` does.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import tempfile
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import sandbox as sandbox_mod
from kiro_crew.dashboard.handlers import git_branches as gb
from kiro_crew.dashboard.handlers.worktree import SandboxUnavailable, _run_git

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}

_REDACTABLE = "feat/ghp_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def _git(*args: str, cwd, **env: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, **_GIT_ENV, **env},
    ).stdout.strip()


@functools.lru_cache(maxsize=1)
def _sandbox_exec_reason() -> str:
    sandbox_mod.reset_backend()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            proc = _run_git(["--version"], tmp)
        except SandboxUnavailable as exc:
            return str(exc) or "sandbox unavailable"
        except OSError as exc:  # pragma: no cover - no git binary at all
            return f"git unavailable: {exc}"
    return "" if proc.returncode == 0 else (proc.stderr or "git failed").strip()


@pytest.fixture(scope="session", autouse=True)
def _sandbox_backend_session_boundary():
    yield
    sandbox_mod.reset_backend()


@pytest.fixture(scope="session")
def _repo_template(tmp_path_factory):
    """``main`` (current) plus ``older``, ``feature``, and three remote refs."""
    template = tmp_path_factory.mktemp("branches-seed") / "proj"
    template.mkdir()
    _git("init", "-q", "-b", "main", cwd=template)
    _git("config", "gc.auto", "0", cwd=template)
    _git("config", "maintenance.auto", "false", cwd=template)
    (template / "README.md").write_text("hi\n")
    _git("add", "README.md", cwd=template)
    _git(
        "commit",
        "-q",
        "-m",
        "init",
        cwd=template,
        GIT_AUTHOR_DATE="2020-01-01T00:00:00Z",
        GIT_COMMITTER_DATE="2020-01-01T00:00:00Z",
    )
    _git("branch", "older", cwd=template)
    _git("switch", "-q", "-c", "feature", cwd=template)
    (template / "README.md").write_text("feature\n")
    _git("commit", "-q", "-am", "feature work", cwd=template)
    _git("switch", "-q", "main", cwd=template)
    head = _git("rev-parse", "HEAD", cwd=template)
    feature = _git("rev-parse", "feature", cwd=template)
    # A configured remote (never contacted) so tracking can be set up against it.
    _git("remote", "add", "origin", "https://example.invalid/repo.git", cwd=template)
    # Remote-tracking refs without a network: one with no local twin, one whose
    # short name matches a local branch, and the remote's HEAD symref.
    _git("update-ref", "refs/remotes/origin/remote-only", feature, cwd=template)
    _git("update-ref", "refs/remotes/origin/feature", feature, cwd=template)
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/feature", cwd=template)
    _git("update-ref", f"refs/heads/{_REDACTABLE}", head, cwd=template)
    return template


@pytest.fixture
def repo(tmp_path, request):
    reason = _sandbox_exec_reason()
    if reason:
        pytest.skip(f"sandboxed git cannot run on this host: {reason[:120]}")
    root = tmp_path / "proj"
    shutil.copytree(request.getfixturevalue("_repo_template"), root)
    return root


def _make_app(*projects, user: str = "owner") -> web.Application:
    @web.middleware
    async def claims(request: web.Request, handler):
        request["app"] = ""
        request["user"] = user
        return await handler(request)

    app = web.Application(middlewares=[claims])
    state = MagicMock()
    state.owner_id = "owner"
    state._slots = {f"chat-{i}": MagicMock(project=str(p)) for i, p in enumerate(projects)}
    app["state"] = state
    app.router.add_get("/api/project/git/branches", gb.api_project_git_branches)
    app.router.add_post("/api/project/git/switch", gb.api_project_git_switch)
    return app


async def _client(*projects, user: str = "owner") -> TestClient:
    client = TestClient(TestServer(_make_app(*projects, user=user)))
    await client.start_server()
    return client


async def _switch(client: TestClient, path, **body):
    resp = await client.post("/api/project/git/switch", json={"path": str(path), **body})
    return resp.status, await resp.json()


def _current(root) -> str:
    return _git("branch", "--show-current", cwd=root)


class TestListBranches:
    @pytest.mark.asyncio
    async def test_lists_local_newest_first_and_marks_current(self, repo):
        client = await _client(repo)
        try:
            resp = await client.get("/api/project/git/branches", params={"path": str(repo)})
            body = await resp.json()
        finally:
            await client.close()
        assert resp.status == 200
        assert body["repo"] is True
        assert body["current"] == "main"
        names = [r["name"] for r in body["local"]]
        # `feature` carries the only newer commit, so it sorts first.
        assert names[0] == "feature"
        assert set(names) >= {"main", "older", "feature"}
        current = [r for r in body["local"] if r["current"]]
        assert [r["name"] for r in current] == ["main"]
        feature = next(r for r in body["local"] if r["name"] == "feature")
        assert feature["subject"] == "feature work"
        assert feature["author"] == "Test"
        assert feature["switchable"] is True
        assert "switchBlocked" not in body

    @pytest.mark.asyncio
    async def test_remote_rows_skip_head_symref_and_local_twins(self, repo):
        client = await _client(repo)
        try:
            resp = await client.get("/api/project/git/branches", params={"path": str(repo)})
            body = await resp.json()
        finally:
            await client.close()
        assert [r["name"] for r in body["remote"]] == ["origin/remote-only"]

    @pytest.mark.asyncio
    async def test_redacted_name_is_listed_but_not_switchable(self, repo):
        client = await _client(repo)
        try:
            resp = await client.get("/api/project/git/branches", params={"path": str(repo)})
            body = await resp.json()
        finally:
            await client.close()
        redacted = [r for r in body["local"] if "REDACTED" in r["name"]]
        assert len(redacted) == 1
        assert redacted[0]["switchable"] is False
        assert _REDACTABLE not in str(body)

    @pytest.mark.asyncio
    async def test_filter_driver_blocks_switching_but_still_lists(self, repo):
        _git("config", "filter.lfs.smudge", "git-lfs smudge -- %f", cwd=repo)
        client = await _client(repo)
        try:
            resp = await client.get("/api/project/git/branches", params={"path": str(repo)})
            body = await resp.json()
            status, switched = await _switch(client, repo, branch="feature")
        finally:
            await client.close()
        assert resp.status == 200
        assert body["switchBlocked"] == "filter"
        assert body["local"]
        assert status == 409
        assert switched["code"] == "git_switch_filter_refused"
        assert _current(repo) == "main"

    @pytest.mark.asyncio
    async def test_unknown_project_is_refused(self, repo, tmp_path):
        client = await _client(tmp_path / "elsewhere")
        try:
            resp = await client.get("/api/project/git/branches", params={"path": str(repo)})
            body = await resp.json()
        finally:
            await client.close()
        assert resp.status == 403
        assert body["code"] == "unknown_project_dir"

    @pytest.mark.asyncio
    async def test_non_repository_answers_repo_false(self, repo, tmp_path):
        # `repo` only applies the sandbox skip; the project below is no repository.
        plain = tmp_path / "plain"
        plain.mkdir()
        client = await _client(plain)
        try:
            resp = await client.get("/api/project/git/branches", params={"path": str(plain)})
            body = await resp.json()
        finally:
            await client.close()
        assert resp.status == 200
        assert body == {"repo": False, "local": [], "remote": []}

    @pytest.mark.asyncio
    async def test_failed_probe_that_is_not_an_absence_is_an_outage(self, repo, monkeypatch):
        # Dubious ownership (or a corrupt .git) is not "no repository": the
        # picker must not say so under a chip that shows the repo's branch.
        def dubious(args, cwd, **kwargs):
            return subprocess.CompletedProcess(
                args, 128, "", "fatal: detected dubious ownership in repository at '/x'\n"
            )

        monkeypatch.setattr(gb, "_run_git", dubious)
        client = await _client(repo)
        try:
            resp = await client.get("/api/project/git/branches", params={"path": str(repo)})
            body = await resp.json()
        finally:
            await client.close()
        assert resp.status == 503
        assert body["code"] == "git_branches_unavailable"

    @pytest.mark.asyncio
    async def test_long_subject_and_author_are_bounded(self, repo):
        _git("switch", "-q", "-c", "wordy", cwd=repo)
        (repo / "w.txt").write_text("w\n")
        _git("add", "w.txt", cwd=repo)
        _git("commit", "-q", "-m", "s" * 5000, cwd=repo, GIT_AUTHOR_NAME="a" * 5000)
        _git("switch", "-q", "main", cwd=repo)
        client = await _client(repo)
        try:
            resp = await client.get("/api/project/git/branches", params={"path": str(repo)})
            body = await resp.json()
        finally:
            await client.close()
        wordy = next(r for r in body["local"] if r["name"] == "wordy")
        assert len(wordy["subject"]) == gb.MAX_ROW_TEXT
        assert len(wordy["author"]) == gb.MAX_ROW_TEXT
        assert wordy["subject"].endswith("\u2026")

    @pytest.mark.asyncio
    async def test_listing_output_over_the_cap_is_refused_not_buffered(self, repo, monkeypatch):
        monkeypatch.setattr(gb, "MAX_LIST_OUTPUT", 64)
        client = await _client(repo)
        try:
            resp = await client.get("/api/project/git/branches", params={"path": str(repo)})
            body = await resp.json()
        finally:
            await client.close()
        assert resp.status == 503
        assert body["code"] == "git_branches_unavailable"


class TestSwitch:
    @pytest.mark.asyncio
    async def test_switches_to_existing_local_branch(self, repo):
        client = await _client(repo)
        try:
            status, body = await _switch(client, repo, branch="feature")
        finally:
            await client.close()
        assert status == 200, body
        assert body == {"ok": True, "branch": "feature", "previous": "main"}
        assert _current(repo) == "feature"
        assert (repo / "README.md").read_text() == "feature\n"

    @pytest.mark.asyncio
    async def test_creates_branch_at_head_without_upstream(self, repo):
        client = await _client(repo)
        try:
            status, body = await _switch(client, repo, branch="feat/new-thing", create=True)
        finally:
            await client.close()
        assert status == 200, body
        assert _current(repo) == "feat/new-thing"
        assert _git("rev-parse", "HEAD", cwd=repo) == _git("rev-parse", "main", cwd=repo)
        with pytest.raises(subprocess.CalledProcessError):
            _git("rev-parse", "--abbrev-ref", "feat/new-thing@{upstream}", cwd=repo)

    @pytest.mark.asyncio
    async def test_create_refuses_an_existing_name(self, repo):
        client = await _client(repo)
        try:
            status, body = await _switch(client, repo, branch="feature", create=True)
        finally:
            await client.close()
        assert status == 409
        assert body["code"] == "git_branch_exists"
        assert _current(repo) == "main"

    @pytest.mark.asyncio
    async def test_tracks_a_remote_branch(self, repo):
        client = await _client(repo)
        try:
            status, body = await _switch(
                client, repo, branch="remote-only", track="origin/remote-only"
            )
        finally:
            await client.close()
        assert status == 200, body
        assert _current(repo) == "remote-only"
        assert (
            _git("rev-parse", "--abbrev-ref", "remote-only@{upstream}", cwd=repo)
            == "origin/remote-only"
        )

    @pytest.mark.asyncio
    async def test_missing_branch_is_404(self, repo):
        client = await _client(repo)
        try:
            status, body = await _switch(client, repo, branch="nope")
            track_status, track_body = await _switch(
                client, repo, branch="nope", track="origin/nope"
            )
        finally:
            await client.close()
        assert (status, body["code"]) == (404, "git_branch_not_found")
        assert (track_status, track_body["code"]) == (404, "git_branch_not_found")

    @pytest.mark.asyncio
    async def test_conflicting_local_changes_are_refused_and_kept(self, repo):
        (repo / "README.md").write_text("my edit\n")
        client = await _client(repo)
        try:
            status, body = await _switch(client, repo, branch="feature")
        finally:
            await client.close()
        assert status == 409
        assert body["code"] == "git_switch_dirty"
        assert _current(repo) == "main"
        assert (repo / "README.md").read_text() == "my edit\n"

    @pytest.mark.asyncio
    async def test_ignored_local_file_the_target_tracks_is_refused_and_kept(self, repo):
        # `tracked-there` tracks secret.env; on main it is ignored and holds
        # local content git's default switch would silently replace.
        _git("switch", "-q", "-c", "tracked-there", cwd=repo)
        (repo / "secret.env").write_text("branch copy\n")
        _git("add", "secret.env", cwd=repo)
        _git("commit", "-q", "-m", "track secret.env", cwd=repo)
        _git("switch", "-q", "main", cwd=repo)
        (repo / ".git" / "info" / "exclude").write_text("secret.env\n")
        (repo / "secret.env").write_text("my local value\n")
        client = await _client(repo)
        try:
            status, body = await _switch(client, repo, branch="tracked-there")
        finally:
            await client.close()
        assert status == 409
        assert body["code"] == "git_switch_dirty"
        assert _current(repo) == "main"
        assert (repo / "secret.env").read_text() == "my local value\n"

    @pytest.mark.asyncio
    async def test_every_git_call_runs_in_the_c_locale(self, repo, monkeypatch):
        # The classifier reads English stderr; a translated user locale would
        # turn a dirty refusal into a generic failure.
        seen: list[dict | None] = []
        real = gb._run_git

        def recording(args, cwd, **kwargs):
            seen.append(kwargs.get("env_overrides"))
            return real(args, cwd, **kwargs)

        monkeypatch.setattr(gb, "_run_git", recording)
        (repo / "README.md").write_text("my edit\n")
        client = await _client(repo)
        try:
            status, body = await _switch(client, repo, branch="feature")
        finally:
            await client.close()
        assert (status, body["code"]) == (409, "git_switch_dirty")
        assert seen
        assert all(env and env.get("LC_ALL") == "C" for env in seen)

    @pytest.mark.asyncio
    async def test_non_conflicting_local_changes_carry_over(self, repo):
        (repo / "notes.txt").write_text("scratch\n")
        client = await _client(repo)
        try:
            status, _ = await _switch(client, repo, branch="older")
        finally:
            await client.close()
        assert status == 200
        assert _current(repo) == "older"
        assert (repo / "notes.txt").read_text() == "scratch\n"

    @pytest.mark.asyncio
    async def test_switching_to_current_branch_is_a_no_op(self, repo):
        client = await _client(repo)
        try:
            status, body = await _switch(client, repo, branch="main")
        finally:
            await client.close()
        assert status == 200
        assert body["branch"] == "main"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("branch", ["-f", "-", "@{-1}", "a b", "x\ny", ""])
    async def test_option_and_shorthand_names_are_refused(self, repo, branch):
        client = await _client(repo)
        try:
            status, body = await _switch(client, repo, branch=branch)
        finally:
            await client.close()
        assert status == 400
        assert body["code"] == "invalid_branch"
        assert _current(repo) == "main"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("branch", ["foo..bar", "foo.lock", "HEAD", "feat/", "a~1"])
    async def test_create_requires_a_valid_new_name(self, repo, branch):
        client = await _client(repo)
        try:
            status, body = await _switch(client, repo, branch=branch, create=True)
        finally:
            await client.close()
        assert status == 400
        assert body["code"] == "invalid_branch"

    @pytest.mark.asyncio
    async def test_create_and_track_together_are_refused(self, repo):
        client = await _client(repo)
        try:
            status, body = await _switch(
                client, repo, branch="x", create=True, track="origin/remote-only"
            )
        finally:
            await client.close()
        assert status == 400
        assert body["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_non_owner_is_refused_before_git_runs(self, repo, monkeypatch):
        calls: list = []
        monkeypatch.setattr(gb, "_switch_sync", lambda *a: calls.append(a) or ({}, 200))
        client = await _client(repo, user="someone-else")
        try:
            status, body = await _switch(client, repo, branch="feature")
        finally:
            await client.close()
        assert status in (401, 403)
        assert calls == []
        assert _current(repo) == "main"

    @pytest.mark.asyncio
    async def test_repo_root_above_the_granted_directory_is_refused(self, repo):
        sub = repo / "sub"
        sub.mkdir()
        client = await _client(sub)
        try:
            status, body = await _switch(client, sub, branch="feature")
        finally:
            await client.close()
        assert status == 403
        assert body["code"] == "repo_root_outside_project"
        assert _current(repo) == "main"

    @pytest.mark.asyncio
    async def test_work_tree_redirected_outside_the_project_is_refused(self, repo, tmp_path):
        """``core.worktree`` would make the checkout write files somewhere else."""
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        (decoy / "README.md").write_text("not yours\n")
        _git("config", "core.worktree", str(decoy), cwd=repo)
        client = await _client(repo)
        try:
            status, body = await _switch(client, repo, branch="feature")
        finally:
            await client.close()
        assert status == 403
        assert body["code"] == "repo_root_outside_project"
        assert (decoy / "README.md").read_text() == "not yours\n"

    @pytest.mark.asyncio
    async def test_unknown_project_is_refused(self, repo, tmp_path):
        client = await _client(tmp_path / "elsewhere")
        try:
            status, body = await _switch(client, repo, branch="feature")
        finally:
            await client.close()
        assert status == 403
        assert body["code"] == "unknown_project_dir"


class TestPlainRefName:
    @pytest.mark.parametrize("name", ["main", "feat/x", "release-1.2", "user@host/x"])
    def test_accepts_ordinary_names(self, name):
        assert gb._is_plain_ref_name(name)

    @pytest.mark.parametrize("name", ["", "-x", "-", "@{-1}", "a b", "a\tb", "x" * 201])
    def test_rejects_options_shorthands_and_whitespace(self, name):
        assert not gb._is_plain_ref_name(name)
