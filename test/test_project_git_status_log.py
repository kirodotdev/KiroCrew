"""Tests for ``GET /api/project/git/status`` and ``GET /api/project/git/log``."""

from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from conftest import make_dir_link
from kiro_crew.dashboard.handlers import (
    api_project_git_log,
    api_project_git_status,
    api_project_tree,
)
from kiro_crew.security import redact
from kiro_crew.security.redaction import _PATH_SEGMENT_DISCRIMINATOR_SEP, _path_segment_label


def _clear_readonly_and_retry(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, onexc=_clear_readonly_and_retry)
    assert not os.path.lexists(path), f"fixture not removed: {path}"


def test_remove_tree_clears_readonly_file(tmp_path: Path) -> None:
    tree = tmp_path / "readonly-tree"
    tree.mkdir()
    read_only_file = tree / "read-only.txt"
    read_only_file.write_text("fixture")
    read_only_file.chmod(stat.S_IREAD)

    _remove_tree(tree)

    assert not os.path.lexists(tree)


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
    app.router.add_get("/api/project/tree", api_project_tree)
    return app


@pytest.fixture(autouse=True)
def passthrough_sandbox(monkeypatch):
    """Run Git unwrapped for parser tests.

    Production fails closed with 503 when the sandbox is unavailable. Only
    Git's own not-a-repository verdict produces ``repo: false``.
    """
    from kiro_crew.dashboard.handlers import files as files_mod

    monkeypatch.setattr(
        files_mod,
        "sandboxed_spawn_argv",
        lambda argv, mode="standard", **kw: (
            list(argv),
            dict(kw.get("env") or os.environ),
            None,
        ),
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


class _ProbeProcess:
    def __init__(self, returncode: int, stderr: bytes = b""):
        self.returncode = returncode
        self.stderr = io.BytesIO(stderr)
        self.killed = False
        self.waited = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        assert timeout == 5
        self.waited = True
        return self.returncode


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
    async def test_symlink_git_marker_to_real_repo_is_followed_by_git(
        self, repo, tmp_path, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        project = tmp_path / "linked-project"
        project.mkdir()
        marker = project / ".git"
        make_dir_link(marker, repo / ".git")
        marker_path = os.path.normcase(os.fspath(marker))
        target_path = os.path.normcase(os.fspath(repo / ".git"))
        visited: list[str] = []
        real_stat = os.stat
        real_lstat = os.lstat

        def record_stat(path, *args, **kwargs):
            visited.append(os.path.normcase(os.fspath(path)))
            return real_stat(path, *args, **kwargs)

        def record_lstat(path, *args, **kwargs):
            visited.append(os.path.normcase(os.fspath(path)))
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(files_mod.os, "stat", record_stat)
        monkeypatch.setattr(files_mod.os, "lstat", record_lstat)
        async with TestClient(TestServer(_make_app(str(project)))) as client:
            resp = await client.get(f"/api/project/git/status?path={project}")
            data = await resp.json()

        assert resp.status == 200
        assert data["repo"] is True
        assert marker_path not in visited
        assert target_path not in visited

    @pytest.mark.asyncio
    async def test_symlink_git_marker_to_missing_target_returns_repo_false(
        self, tmp_path, mock_sel
    ):
        project = tmp_path / "missing-target"
        project.mkdir()
        absent_target = tmp_path / "absent-target"
        absent_target.mkdir()
        make_dir_link(project / ".git", absent_target)
        absent_target.rmdir()

        async with TestClient(TestServer(_make_app(str(project)))) as client:
            resp = await client.get(f"/api/project/git/status?path={project}")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"repo": False, "files": []}

    @pytest.mark.asyncio
    async def test_lock_suffix_head_ref_matches_git_probe(self, repo, mock_sel):
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/bad.lock\n")
        probe_env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        status_probe = subprocess.run(
            ["git", "status", "--porcelain=v1", "-b"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=probe_env,
        )
        branch_probe = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=probe_env,
        )

        assert status_probe.returncode == 0
        assert status_probe.stdout == "## A  a.txt\n"
        assert branch_probe.returncode == 128
        assert branch_probe.stdout == ""

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()

        assert resp.status == 503
        assert data["code"] == "git_status_unavailable"

    @pytest.mark.asyncio
    async def test_corrupt_head_matches_git_probe(self, repo, mock_sel):
        (repo / ".git" / "HEAD").write_text("not a valid HEAD\n")
        probe = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
            },
        )

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()

        if probe.returncode == 0:
            assert resp.status == 503
            assert data["code"] == "git_status_unavailable"
        else:
            assert resp.status == 200
            assert data == {"repo": False, "files": []}

    @pytest.mark.parametrize("head_state", ("detached", "unborn"))
    @pytest.mark.asyncio
    async def test_valid_head_states_remain_repositories(
        self, repo, tmp_path, mock_sel, head_state
    ):
        if head_state == "detached":
            _git(repo, "checkout", "-q", "--detach", "HEAD")
            project = repo
        else:
            project = tmp_path / "unborn"
            project.mkdir()
            _git(project, "init", "-q", "-b", "trunk")

        async with TestClient(TestServer(_make_app(str(project)))) as client:
            resp = await client.get(f"/api/project/git/status?path={project}")
            data = await resp.json()

        assert resp.status == 200
        assert data["repo"] is True

    def test_repository_probe_uses_bounded_git_runner(self, repo, monkeypatch):
        from kiro_crew.dashboard.handlers import files as files_mod

        seen: dict[str, object] = {}

        def fake_run(args, *, cwd, env, timeout, cap, capture):
            seen.update(
                args=args,
                cwd=cwd,
                env=env,
                timeout=timeout,
                cap=cap,
                capture=capture,
            )
            return 128, "probe diagnostic", False

        monkeypatch.setattr(files_mod, "_run_git_bounded", fake_run)

        assert files_mod._probe_git_dir(str(repo), {"LC_ALL": "C"}) == (
            128,
            "probe diagnostic",
        )
        assert seen == {
            "args": ["git", "rev-parse", "--git-dir"],
            "cwd": str(repo),
            "env": {"LC_ALL": "C"},
            "timeout": 5,
            "cap": 4096,
            "capture": "stderr",
        }

    @pytest.mark.asyncio
    async def test_non_repository_probe_failure_is_unavailable(
        self, repo, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(
            files_mod,
            "popen_limited",
            lambda *_args, **_kwargs: _ProbeProcess(
                128, stderr=b"fatal: detected dubious ownership in repository"
            ),
        )
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()

        assert resp.status == 503
        assert data["code"] == "git_status_unavailable"

    @pytest.mark.parametrize("failure", ("sandbox", "spawn", "show-toplevel", "status"))
    @pytest.mark.asyncio
    async def test_operational_status_failure_is_not_reported_as_non_repo(
        self, repo, mock_sel, monkeypatch, failure
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        if failure == "sandbox":

            def refuse(*_args, **_kwargs):
                raise RuntimeError("sandbox unavailable")

            monkeypatch.setattr(files_mod, "sandboxed_spawn_argv", refuse)
        elif failure == "spawn":
            monkeypatch.setattr(
                files_mod,
                "popen_limited",
                MagicMock(side_effect=OSError("git unavailable")),
            )
        else:
            real_popen = files_mod.popen_limited
            target = "--show-toplevel" if failure == "show-toplevel" else "status"

            def fail_command(argv, *args, **kwargs):
                if target in argv:
                    raise OSError(f"git {target} unavailable")
                return real_popen(argv, *args, **kwargs)

            monkeypatch.setattr(files_mod, "popen_limited", fail_command)

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()

        assert resp.status == 503
        assert data == {
            "error": "Couldn't read the repository status.",
            "code": "git_status_unavailable",
        }

    @pytest.mark.asyncio
    async def test_permission_revocation_after_git_failure_is_unavailable(
        self, repo, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        real_popen = files_mod.popen_limited
        real_stat = files_mod.os.stat
        repo_path = os.path.normcase(os.fspath(repo))
        deny_repo_stat = False

        def guarded_stat(path, *args, **kwargs):
            if deny_repo_stat and os.path.normcase(os.fspath(path)) == repo_path:
                raise PermissionError("project traversal permission revoked")
            return real_stat(path, *args, **kwargs)

        def fail_status(argv, *args, **kwargs):
            nonlocal deny_repo_stat
            if "status" in argv:
                deny_repo_stat = True
                raise OSError("git status unavailable")
            return real_popen(argv, *args, **kwargs)

        monkeypatch.setattr(files_mod.os, "stat", guarded_stat)
        monkeypatch.setattr(files_mod, "popen_limited", fail_status)

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()

        assert os.path.lexists(repo), "fixture directory unexpectedly vanished"
        assert resp.status == 503
        assert data == {
            "error": "Couldn't read the repository status.",
            "code": "git_status_unavailable",
        }

    @pytest.mark.asyncio
    async def test_probe_pins_english_git_diagnostics(self, tmp_path, mock_sel, monkeypatch):
        from kiro_crew.dashboard.handlers import files as files_mod

        plain = tmp_path / "plain"
        plain.mkdir()
        seen: dict[str, object] = {}

        def fake_popen(_argv, **kwargs):
            seen.update(kwargs["env"])
            seen["stdout"] = kwargs["stdout"]
            seen["stderr"] = kwargs["stderr"]
            return _ProbeProcess(
                128, stderr=b"fatal: not a git repository (or any parent): .git"
            )

        monkeypatch.setattr(files_mod, "popen_limited", fake_popen)
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/git/status?path={plain}")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"repo": False, "files": []}
        assert seen["LC_ALL"] == "C"
        assert seen["LANGUAGE"] == "C"
        assert seen["stdout"] is subprocess.DEVNULL
        assert seen["stderr"] is subprocess.PIPE

    @pytest.mark.asyncio
    async def test_probe_path_text_does_not_claim_repository_absence(
        self, tmp_path, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        repo = tmp_path / "not a git repository"
        repo.mkdir()
        diagnostic = (
            f"fatal: detected dubious ownership in repository at '{repo}'\n"
        ).encode()
        monkeypatch.setattr(
            files_mod,
            "popen_limited",
            lambda *_args, **_kwargs: _ProbeProcess(128, stderr=diagnostic),
        )

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()

        assert resp.status == 503
        assert data == {
            "error": "Couldn't read the repository status.",
            "code": "git_status_unavailable",
        }

    @pytest.mark.asyncio
    async def test_non_utf8_probe_stderr_does_not_crash(
        self, tmp_path, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.setattr(
            files_mod,
            "popen_limited",
            lambda *_args, **_kwargs: _ProbeProcess(
                128, stderr=b"fatal: not a git repository \xff"
            ),
        )
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/git/status?path={plain}")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"repo": False, "files": []}

    @pytest.mark.asyncio
    async def test_probe_stderr_overflow_is_killed_and_unavailable(
        self, repo, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        proc = _ProbeProcess(
            128,
            stderr=b"fatal: not a git repository " + b"x" * 4096,
        )
        monkeypatch.setattr(files_mod, "popen_limited", lambda *_args, **_kwargs: proc)

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()

        assert resp.status == 503
        assert data["code"] == "git_status_unavailable"
        assert proc.killed is True
        assert proc.waited is True

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
    async def test_redaction_collision_files_stay_distinct(self, repo, mock_sel):
        """Two distinct changed files that redact() collapses to one path must
        BOTH appear in ``files``, as two distinct redacted entries.

        Real collision: two untracked files whose only differing segment is a
        credential-shaped token (distinct AKIA... ids, each 4-letter prefix + 16
        uppercase alphanumerics) both flatten to
        ``[REDACTED: credential]_model.txt`` under the whole-string redact().
        Each path is redacted with ``redact_path_segments`` so each member of
        the collision carries an opaque label keyed per gateway process --
        distinct between the two and stable across responses -- and neither
        vanishes; the de-dup behind it still guards a true collision, and the
        raw tokens never leak.
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
        # Both files survive, each redacted and distinct from the other, each
        # carrying exactly the keyed label of its own original segment...
        sep = _PATH_SEGMENT_DISCRIMINATOR_SEP
        redacted = [p for p in paths if p.startswith(f"[REDACTED: credential]_model.txt{sep}")]
        assert sorted(redacted) == sorted(
            f"[REDACTED: credential]_model.txt{sep}{_path_segment_label(f'{k}_model.txt')}"
            for k in (key_a, key_b)
        ), paths
        assert len(paths) == len(set(paths))
        # ...and the raw tokens never leak.
        assert key_a not in "\n".join(paths)
        assert key_b not in "\n".join(paths)

    @pytest.mark.asyncio
    async def test_a_credential_shaped_project_prefix_is_redacted_whole(self, repo, mock_sel):
        """When the project directory sits below the repo root and its own name
        is credential-shaped, status paths carry that prefix. The prefix is
        redacted the same way the tree root and ``repoRoot`` are (whole-string,
        no label), so the dashboard's prefix strip matches; only the part
        beneath it is labelled per segment."""
        key = "AKIAIOSFODNN7EXAMPLE"
        sub = repo / key
        sub.mkdir()
        (sub / "notes.txt").write_text("x\n")
        (sub / f"{key}_model.txt").write_text("y\n")
        async with TestClient(TestServer(_make_app(str(sub)))) as client:
            resp = await client.get(f"/api/project/git/status?path={sub}")
            data = await resp.json()
        assert data["repo"] is True
        paths = sorted(f["path"] for f in data["files"])
        sep = _PATH_SEGMENT_DISCRIMINATOR_SEP
        prefix = redact(key)
        assert sep not in prefix
        assert paths == sorted(
            [
                f"{prefix}/notes.txt",
                f"{prefix}/[REDACTED: credential]_model.txt{sep}{_path_segment_label(f'{key}_model.txt')}",
            ]
        ), paths
        assert key not in "\n".join(paths)

    @pytest.mark.asyncio
    async def test_a_token_straddling_the_prefix_join_falls_back_to_whole_path(
        self, repo, mock_sel, monkeypatch
    ):
        """The prefix and the part beneath it are redacted separately, so a
        token that straddles the joining slash is matched by neither half. The
        joined result must be a fixed point of the redactor; when it is not, the
        whole-path result wins, the same floor ``redact_path_segments`` applies
        to its own assembly."""
        from kiro_crew.dashboard.handlers import files as files_mod

        sub = repo / "SEC"
        sub.mkdir()
        (sub / "RET").write_text("x\n")

        def straddling_redactor(text: str) -> str:
            # Neither half is sensitive on its own; only the joined shape is.
            return text.replace("SEC/RET", "[REDACTED: straddle]")

        monkeypatch.setattr(files_mod, "redact", straddling_redactor)
        async with TestClient(TestServer(_make_app(str(sub)))) as client:
            resp = await client.get(f"/api/project/git/status?path={sub}")
            data = await resp.json()
        paths = [f["path"] for f in data["files"]]
        assert paths == ["[REDACTED: straddle]"], paths

    @pytest.mark.asyncio
    async def test_status_and_tree_label_the_same_path_identically(self, repo, mock_sel):
        """The dashboard joins the git-status response with the tree response
        by path (PierreWorkspaceTreeImpl), so one process must label a redacted
        path the same way in both -- including when the tree lists a colliding
        neighbour the status response does not carry."""
        key_a = "AKIAIOSFODNN7EXAMPLE"
        key_b = "AKIA" + "JKLMNOPQRSTUVWXY"
        (repo / f"{key_a}_model.txt").write_text("one\n")
        (repo / f"{key_b}_model.txt").write_text("two\n")
        _git(repo, "add", f"{key_b}_model.txt")
        _git(repo, "commit", "-qm", "track the neighbour")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            status = await resp.json()
            resp = await client.get(f"/api/project/tree?path={repo}")
            tree = await resp.json()
        status_paths = [f["path"] for f in status["files"]]
        sep = _PATH_SEGMENT_DISCRIMINATOR_SEP
        label_a = f"[REDACTED: credential]_model.txt{sep}{_path_segment_label(f'{key_a}_model.txt')}"
        label_b = f"[REDACTED: credential]_model.txt{sep}{_path_segment_label(f'{key_b}_model.txt')}"
        # Only key_a is changed, so status carries it alone...
        assert status_paths == [label_a]
        # ...while the tree carries both, and key_a's entry is byte-identical.
        assert label_a in tree["paths"]
        assert label_b in tree["paths"]
        assert set(status_paths) <= set(tree["paths"])

    @pytest.mark.asyncio
    async def test_a_true_redaction_collision_is_still_deduplicated(
        self, repo, mock_sel, monkeypatch
    ):
        """When the path helper (``redact_path_segments``) hands back the same
        string for two paths, the de-dup keeps one entry per
        (path, status, staged) so GitPanel never renders two rows under one key."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(
            files_mod,
            "redact_path_segments",
            lambda p, r=None: "[REDACTED: credential]_model.txt",
        )
        (repo / "one_model.txt").write_text("one\n")
        (repo / "two_model.txt").write_text("two\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        paths = [f["path"] for f in data["files"]]
        assert paths == ["[REDACTED: credential]_model.txt"]

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
    """A repo whose own config names a content-filter driver is reported as
    UNAVAILABLE: status re-hashes modified files through ``filter.<n>.clean``,
    so running any content-touching git against such a repo would execute a
    repository-supplied program on every poll.

    The refusal must not be spelled as an empty result. ``{"repo": true,
    "files": []}`` is what a genuinely clean repository returns, so a refusal
    wearing that shape makes the panel draw its green "clean" pill over a
    working tree it has never read."""

    @pytest.mark.asyncio
    async def test_status_refuses_clean_filter(self, repo, mock_sel):
        _git(repo, "config", "filter.evil.clean", "touch /tmp/pwned")
        (repo / "a.txt").write_text("modified\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert resp.status == 503
        assert data["code"] == "git_status_filter_refused"

    @pytest.mark.asyncio
    async def test_smudge_only_refusal_does_not_claim_a_program_ran(
        self, repo, mock_sel
    ):
        """The match is wider than execution, so the message must not assert it.

        ``smudge`` converts content on CHECKOUT; the status re-hash runs
        ``clean``. Refusing anyway is right -- we cannot prove the repo safe --
        but this config runs nothing during a status check, so a message saying
        a program runs "on every check" states a fact the guard does not have.
        The same gap is reached one further way: a driver no ``.gitattributes``
        path maps to. The probe-failure branch is a DIFFERENT fact and answers
        its own cause, so it must not appear in this body.
        """
        _git(repo, "config", "filter.evil.smudge", "touch /tmp/pwned")
        (repo / "a.txt").write_text("modified\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert resp.status == 503
        assert data["code"] == "git_status_filter_refused"
        assert data["cause"] == "declared"
        assert "declares" in data["error"]
        assert "every check" not in data["error"]
        # And ONLY this cause. Handing the reader both outcomes made the common
        # case -- a repo that does declare a driver -- state a disjunction the
        # guard had already resolved, which a reader can only partly follow.
        assert "could not be read" not in data["error"]

    @pytest.mark.asyncio
    async def test_unreadable_config_refuses_without_naming_a_driver_as_fact(
        self, repo, mock_sel, monkeypatch
    ):
        """The branch that refuses on ignorance, not on evidence.

        When the ``git config`` scope probe exits nonzero the guard refuses
        without having read any ``filter.*`` key, so this repository may declare
        nothing at all. Refusing is still right -- an unreadable scope cannot be
        proven filter-free -- but the 503 must not report a driver it never saw.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        real = files_mod._run_git_bounded

        def fail_config_list(cmd, **kwargs):
            if "config" in cmd and "--list" in cmd:
                return (1, "", "fatal: unable to read config file")
            return real(cmd, **kwargs)

        monkeypatch.setattr(files_mod, "_run_git_bounded", fail_config_list)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert resp.status == 503
        assert data["code"] == "git_status_filter_refused"
        assert data["cause"] == "unreadable"
        assert "could not be read" in data["error"]
        assert "declares" not in data["error"]

    @pytest.mark.asyncio
    async def test_status_filter_refusal_is_distinguishable_from_a_clean_repo(
        self, repo, tmp_path, mock_sel
    ):
        """The reported defect, asserted as the property that was violated.

        A caller cannot tell "we declined to read this repo" from "this repo
        has no changes" while both answers are the same bytes. Comparing
        the two responses directly is what keeps any future spelling of the
        refusal from collapsing back onto the clean answer -- an assertion on
        one literal body would not.
        """
        clean = tmp_path / "clean"
        shutil.copytree(repo, clean)
        async with TestClient(TestServer(_make_app(str(clean)))) as client:
            clean_resp = await client.get(f"/api/project/git/status?path={clean}")
            clean_body = await clean_resp.json()

        _git(repo, "config", "filter.evil.clean", "touch /tmp/pwned")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            refused_resp = await client.get(f"/api/project/git/status?path={repo}")
            refused_body = await refused_resp.json()

        # The clean control must really be the healthy answer, or the
        # comparison below could pass with both sides degraded.
        assert clean_resp.status == 200
        assert clean_body["repo"] is True
        assert clean_body["files"] == []

        assert refused_resp.status != clean_resp.status
        assert refused_body != clean_body

    @pytest.mark.asyncio
    async def test_status_checks_corrupt_head_before_filter_refusal(
        self, repo, mock_sel, monkeypatch
    ):
        """HEAD is probed before the filter guard.

        Both answers are the same 503, so the response body cannot witness the
        ordering; the guard never being reached is what does.
        """
        import kiro_crew.dashboard.handlers.files as files_mod

        called = False

        def _spy(*args, **kwargs):
            nonlocal called
            called = True
            return ""

        monkeypatch.setattr(files_mod, "_repo_filter_refusal_cause", _spy)
        _git(repo, "config", "filter.evil.clean", "touch /tmp/pwned")
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/bad.lock\n")

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()

        assert resp.status == 503
        assert data["code"] == "git_status_unavailable"
        assert called is False

    @pytest.mark.asyncio
    async def test_log_refuses_process_filter(self, repo, mock_sel):
        _git(repo, "config", "filter.evil.process", "evil-daemon")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}")
            data = await resp.json()
        assert resp.status == 503
        assert data["code"] == "git_log_filter_refused"

    @pytest.mark.asyncio
    async def test_a_policy_refusal_is_not_spelled_as_an_outage(
        self, repo, mock_sel, monkeypatch
    ):
        """The refusal and a genuine outage carry DIFFERENT codes.

        Both are 503 and neither is clean, which is the safety property. But a
        filter-driver refusal is permanent for the repo, caused by its own
        config, and no retry clears it, while an outage is transient and worth
        retrying. One code for both tells an LFS user their repository is broken
        on every poll, forever, so the caller must be able to tell them apart.
        """
        import kiro_crew.dashboard.handlers.files as files_mod

        _git(repo, "config", "filter.evil.clean", "touch /tmp/pwned")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            refused = await client.get(f"/api/project/git/status?path={repo}")
            refused_body = await refused.json()

        # A genuine outage on the same repo: the status command itself fails
        # while the filter probe reports nothing to refuse.
        monkeypatch.setattr(
            files_mod, "_repo_filter_refusal_cause", lambda *a, **k: ""
        )
        real = files_mod._run_git_bounded

        def fail_status(args, *a, **k):
            if "status" in args:
                return 1, "", False
            return real(args, *a, **k)

        monkeypatch.setattr(files_mod, "_run_git_bounded", fail_status)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            outage = await client.get(f"/api/project/git/status?path={repo}")
            outage_body = await outage.json()

        assert refused.status == outage.status == 503
        assert refused_body["code"] == "git_status_filter_refused"
        assert outage_body["code"] == "git_status_unavailable"

    @pytest.mark.asyncio
    async def test_log_filter_refusal_is_distinguishable_from_an_unborn_repo(
        self, repo, tmp_path, mock_sel
    ):
        """An empty commit list is what a repo with no commits legitimately
        returns, so the refusal must not be spelled that way either."""
        unborn = tmp_path / "unborn"
        unborn.mkdir()
        _git(unborn, "init", "-q", "-b", "trunk")
        async with TestClient(TestServer(_make_app(str(unborn)))) as client:
            unborn_resp = await client.get(f"/api/project/git/log?path={unborn}")
            unborn_body = await unborn_resp.json()

        _git(repo, "config", "filter.evil.process", "evil-daemon")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            refused_resp = await client.get(f"/api/project/git/log?path={repo}")
            refused_body = await refused_resp.json()

        # A repo with no commits still answers 200 with an empty list: that
        # state is legitimate and must NOT have been converted into an error.
        assert unborn_resp.status == 200
        assert unborn_body["commits"] == []

        assert refused_resp.status != unborn_resp.status
        assert refused_body != unborn_body

    @pytest.mark.asyncio
    async def test_clean_repo_is_not_refused(self, repo, mock_sel):
        """The probe only fires on filter drivers, not on ordinary config."""
        _git(repo, "config", "diff.noise.command", "irrelevant-but-not-a-filter")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert resp.status == 200
        assert data["repo"] is True


class TestWorktreeConfigScopeGate:
    """``extensions.worktreeConfig=true`` with no ``config.worktree`` on disk is
    a normal healthy state (git creates the file lazily; ``git worktree add``
    leaves it behind routinely). Probing the ``--worktree`` scope there exits
    128, which a fail-closed guard reads as "declares a filter driver",
    silently emptying the Git panel for filter-free repos."""

    @staticmethod
    def _guard(repo) -> str:
        from kiro_crew.dashboard.handlers.files import _repo_filter_refusal_cause

        env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        return _repo_filter_refusal_cause(["git"], str(repo), env)

    def test_extension_on_without_config_worktree_is_not_refused(self, repo):
        """The reported defect: extension on, file absent, no filter anywhere."""
        _git(repo, "config", "extensions.worktreeConfig", "true")
        git_dir = repo / ".git"
        assert not (git_dir / "config.worktree").exists()
        assert self._guard(repo) == ""

    def test_worktree_scoped_filter_is_still_refused(self, repo):
        """Writing a worktree-scoped key creates the file; a driver in it must
        still refuse — the gate narrows WHEN the scope is probed, never what a
        probed scope may declare."""
        _git(repo, "config", "extensions.worktreeConfig", "true")
        _git(repo, "config", "--worktree", "filter.evil.smudge", "sh -c ':'")
        assert self._guard(repo) == "declared"

    def test_worktree_scoped_extension_override_cannot_hide_its_own_scope(self, repo):
        """git takes the extension from the REPO config only, so a
        worktree-scoped ``extensions.worktreeConfig=false`` leaves the scope
        LIVE — but it wins a merged ``--get`` chain, so a probe without
        ``--local`` reads the extension as off and never lists the scope the
        driver hides in. The probe must read the scope git actually decides
        from."""
        _git(repo, "config", "extensions.worktreeConfig", "true")
        _git(repo, "config", "--worktree", "extensions.worktreeConfig", "false")
        _git(repo, "config", "--worktree", "filter.evil.smudge", "sh -c ':'")
        assert self._guard(repo) == "declared"

    def test_extension_on_with_empty_config_worktree_is_not_refused(self, repo):
        """File present and readable, no filter declared: probed, and clean."""
        _git(repo, "config", "extensions.worktreeConfig", "true")
        (repo / ".git" / "config.worktree").write_text("")
        assert self._guard(repo) == ""

    def test_garbled_config_worktree_still_refuses(self, repo):
        """Present-but-unreadable stays fail-closed: only the MISSING-file case
        is an empty scope; a scope git errors on cannot be proven filter-free."""
        _git(repo, "config", "extensions.worktreeConfig", "true")
        (repo / ".git" / "config.worktree").write_text("[broken\n")
        assert self._guard(repo) == "unreadable"

    @pytest.mark.skipif(
        os.name == "nt" or sys.platform == "darwin",
        reason="non-UTF-8 bytes are not legal NTFS or APFS/HFS+ name units",
    )
    def test_oversized_config_worktree_in_a_non_utf8_repo_path_stays_refused(self, tmp_path):
        """The reviewer's exact kill chain, end to end with real git.

        The repo path holds a byte that is not valid UTF-8, and
        ``config.worktree`` EXISTS but its key listing exceeds the bounded
        runner's 8 MiB stdout cap -- the listing probe is killed while
        ``rev-parse --absolute-git-dir`` still answers. The classifier must
        find the file through the surrogateescape-decoded path and keep the
        refusal: a replace-decoded path turns the byte into U+FFFD, the
        ``lstat`` misses the existing file, and the failed probe reads as the
        healthy empty scope -- executing the very filter driver the guard
        exists to refuse."""
        repo = tmp_path / os.fsdecode(b"wt-\xff")
        repo.mkdir()
        _git(repo, "init", "-q", "--template=", ".")
        _git(repo, "config", "extensions.worktreeConfig", "true")
        # One subsection name past the 8 MiB cap: --name-only prints it whole,
        # so the listing overflows and is killed; rev-parse is unaffected.
        (repo / ".git" / "config.worktree").write_text(
            '[s "' + "a" * (9 * 1024 * 1024) + '"]\n\tk = 1\n'
        )
        assert self._guard(repo) == "unreadable"

    def test_extension_off_never_probes_worktree_scope(self, repo, monkeypatch):
        """Without the extension the ``--worktree`` scope is never issued."""
        from kiro_crew.dashboard.handlers import files as files_mod

        real = files_mod._run_git_bounded
        seen: list[list[str]] = []

        def recording(argv, **kw):
            seen.append(list(argv))
            return real(argv, **kw)

        monkeypatch.setattr(files_mod, "_run_git_bounded", recording)
        assert self._guard(repo) == ""
        assert not any("--worktree" in argv for argv in seen)

    @pytest.mark.asyncio
    async def test_status_populates_with_extension_on_and_no_file(self, repo, mock_sel):
        """End to end: the panel shows the dirty file instead of going empty."""
        _git(repo, "config", "extensions.worktreeConfig", "true")
        (repo / "a.txt").write_text("modified\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert any(f["path"] == "a.txt" for f in data["files"])

    @pytest.mark.asyncio
    async def test_log_populates_with_extension_on_and_no_file(self, repo, mock_sel):
        _git(repo, "config", "extensions.worktreeConfig", "true")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert len(data["commits"]) >= 1


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
    async def test_windows_spawn_failure_after_directory_vanishes_returns_no_data(
        self, repo, mock_sel, monkeypatch
    ):
        """A Windows spawn error for a vanished cwd is absence, not outage."""
        from kiro_crew.dashboard.handlers import files as files_mod

        def vanish_then_fail(*_args, **_kwargs):
            _remove_tree(repo)
            error = FileNotFoundError(
                2, "[WinError 3] The system cannot find the path specified", str(repo)
            )
            error.winerror = 3
            raise error

        monkeypatch.setattr(files_mod, "popen_limited", vanish_then_fail)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            assert resp.status == 200
            data = await resp.json()
        assert data["repo"] is False
        assert data["files"] == []

    @pytest.mark.asyncio
    async def test_dir_removed_between_check_and_spawn_returns_no_data(self, repo, mock_sel, monkeypatch):
        """TOCTOU: the project dir can vanish after the isdir gate and before
        the git spawn. The endpoint must answer degraded, never 500."""
        from kiro_crew.dashboard.handlers import files as files_mod

        real_isdir = os.path.isdir

        def isdir_then_delete(path):
            ok = real_isdir(path)
            if ok and str(path) == str(repo):
                _remove_tree(repo)
            return ok

        monkeypatch.setattr(files_mod.os.path, "isdir", isdir_then_delete)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            assert resp.status == 200
            data = await resp.json()
        assert data["repo"] is False
        assert data["files"] == []


class TestListingCap:
    """The response caps the file listing at 500 and says when it did.

    Unless the flag reaches the caller, the cap reads as the total: a repo with
    900 changed files shows 500 and its list simply ends, which presents an
    undercount as a count.
    """

    @staticmethod
    def _add_untracked(repo, count: int) -> None:
        for i in range(count):
            (repo / f"f{i:04d}.txt").write_text("x\n")

    @pytest.mark.asyncio
    async def test_a_capped_listing_says_it_was_capped(self, repo, mock_sel):
        self._add_untracked(repo, 501)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert resp.status == 200
        assert len(data["files"]) == 500
        assert data["truncated"] is True

    @pytest.mark.asyncio
    async def test_a_listing_at_the_cap_is_not_marked_capped(self, repo, mock_sel):
        """Exactly 500 is complete: an off-by-one here would warn on a listing
        that is in fact whole, which trains the reader to ignore the warning."""
        self._add_untracked(repo, 500)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert resp.status == 200
        assert len(data["files"]) == 500
        assert "truncated" not in data

    @pytest.mark.asyncio
    async def test_a_clean_repo_is_not_marked_capped(self, repo, mock_sel):
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert resp.status == 200
        assert data["files"] == []
        assert "truncated" not in data
