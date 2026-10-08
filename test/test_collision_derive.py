"""Tests for the collision-index git derivation (repo_id / repo_rel_path)."""

from __future__ import annotations

import os
import subprocess

import pytest

from kiro_crew.dashboard.collision_derive import (
    canonicalize_remote,
    derive_repo_context,
    repo_rel_for,
)


@pytest.fixture(autouse=True)
def _git_through_sandbox_passthrough(monkeypatch):
    """Route ``collision_derive``'s git probe around the OS sandbox for tests.

    ``_run_git`` wraps every ``git`` call through ``sandboxed_spawn_argv`` and, by
    design, DROPS to None when that wrap is unavailable (a host with no OS sandbox
    backend — e.g. a Linux/Windows CI shard without one). The positive-path tests
    below assert that a real repo derives a non-None result, so they must exercise
    the derive logic itself rather than the host's sandbox availability. Replace
    the wrap with a passthrough that returns the real argv unwrapped (the pattern
    used for the exemplar routed caller in ``test_dashboard_worktree_coverage``):
    real ``git`` runs, the derive logic is verified everywhere, and no backend is
    required. A test that specifically needs the no-backend branch
    (``TestNoSandboxBackendDegradesToNone``, ``test_git_failure_returns_none``)
    monkeypatches the symbol again itself, which overrides this per-test.
    """
    import kiro_crew.dashboard.collision_derive as cd

    def _passthrough(argv, mode="standard", **_kw):
        return list(argv), {}, None

    monkeypatch.setattr(cd, "sandboxed_spawn_argv", _passthrough)


def _coords(abs_file, cwd):
    """Test helper: full derive via the split API (context + per-path relpath)."""
    ctx = derive_repo_context(cwd)
    if ctx is None:
        return None
    rel = repo_rel_for(ctx.repo_root, abs_file)
    if rel is None:
        return None
    return ctx.repo_id, rel, ctx.repo_root


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, cwd=str(cwd))


def _init_repo(path, *, remote=None):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    if remote:
        _git(path, "remote", "add", "origin", remote)
    return path


@pytest.fixture(autouse=True)
def _skip_without_git():
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git not available")


class TestCanonicalizeRemote:
    def test_scp_and_https_forms_match(self):
        scp = canonicalize_remote("git@github.com:org/repo.git")
        https = canonicalize_remote("https://github.com/org/repo")
        assert scp == https == "github.com/org/repo"

    def test_strips_dotgit_and_lowercases_host_preserving_path_case(self):
        # Host is lowercased (DNS is case-insensitive); PATH case is preserved,
        # and .git + trailing slash are stripped.
        assert canonicalize_remote("https://Host/Org/Repo.git/") == "host/Org/Repo"

    def test_case_distinct_paths_do_not_merge(self):
        # On a case-sensitive host, org/Repo and org/repo are distinct repos;
        # folding them would emit a false collision (GPT-review Finding C).
        assert canonicalize_remote("git@host:org/Repo") != canonicalize_remote("git@host:org/repo")
        # But the HOST case is still folded, so scp/https forms match.
        assert canonicalize_remote("git@HOST:o/r") == canonicalize_remote("https://host/o/r")

    def test_empty(self):
        assert canonicalize_remote("") == ""
        assert canonicalize_remote(None) == ""


class TestDeriveRepoCoords:
    def test_basic_derive_with_remote(self, tmp_path):
        repo = _init_repo(tmp_path / "r", remote="git@github.com:org/repo.git")
        f = repo / "src" / "app.py"
        f.parent.mkdir(parents=True)
        f.write_text("x", encoding="utf-8")
        rc = _coords(str(f), str(repo))
        assert rc is not None
        repo_id, rel, root = rc
        assert repo_id == "github.com/org/repo"
        assert rel == os.path.join("src", "app.py")
        assert root == os.path.realpath(str(repo))

    def test_no_remote_falls_back_to_common_dir(self, tmp_path):
        repo = _init_repo(tmp_path / "r")  # no origin
        f = repo / "a.py"
        f.write_text("x", encoding="utf-8")
        rc = _coords(str(f), str(repo))
        assert rc is not None
        repo_id, rel, _ = rc
        assert repo_id == os.path.realpath(str(repo / ".git"))
        assert rel == "a.py"

    def test_out_of_tree_write_is_dropped(self, tmp_path):
        repo = _init_repo(tmp_path / "r", remote="git@h:o/r.git")
        outside = tmp_path / "outside.py"
        outside.write_text("x", encoding="utf-8")
        assert _coords(str(outside), str(repo)) is None

    def test_path_above_root_is_dropped(self, tmp_path):
        repo = _init_repo(tmp_path / "nested" / "r", remote="git@h:o/r.git")
        above = tmp_path / "nested" / "sibling.py"
        above.write_text("x", encoding="utf-8")
        assert _coords(str(above), str(repo)) is None

    def test_non_repo_cwd_returns_none(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert derive_repo_context(str(plain)) is None

    def test_git_failure_returns_none(self, tmp_path, monkeypatch):
        # A git subprocess that raises (not just nonzero) must be swallowed to
        # None, not propagate. Covers the _run_git except branch.
        import kiro_crew.dashboard.collision_derive as cd

        def _boom(*a, **k):
            raise OSError("git exploded")

        monkeypatch.setattr(cd.subprocess, "run", _boom)
        assert cd.derive_repo_context(str(tmp_path)) is None

    def test_two_worktrees_share_repo_id_differ_in_root(self, tmp_path):
        repo = _init_repo(tmp_path / "r", remote="git@github.com:org/repo.git")
        (repo / "a.py").write_text("x", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        wt = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", str(wt))
        rc_main = _coords(str(repo / "a.py"), str(repo))
        rc_wt = _coords(str(wt / "a.py"), str(wt))
        assert rc_main is not None and rc_wt is not None
        # repo_id matches (origin remote) -> the two worktrees collide on a.py;
        # worktree root differs.
        assert rc_main[0] == rc_wt[0] == "github.com/org/repo"
        assert rc_main[1] == rc_wt[1] == "a.py"
        assert rc_main[2] != rc_wt[2]

    def test_two_worktrees_share_common_dir_when_no_remote(self, tmp_path):
        repo = _init_repo(tmp_path / "r")  # no origin
        (repo / "a.py").write_text("x", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        wt = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", str(wt))
        rc_main = _coords(str(repo / "a.py"), str(repo))
        rc_wt = _coords(str(wt / "a.py"), str(wt))
        assert rc_main is not None and rc_wt is not None
        # No remote -> common dir; a worktree's common dir is the main repo's
        # .git, so the two still share repo_id (the multi-worktree match).
        assert rc_main[0] == rc_wt[0]


class TestNoSandboxBackendDegradesToNone:
    """On a host with no OS sandbox backend, the git probe must DROP (return
    None), never raise into the per-turn flush — the module's never-raise
    contract. Reproduces the Windows-CI SandboxUnavailableError path."""

    def test_derive_returns_none_when_sandbox_backend_unavailable(self, tmp_path, monkeypatch):
        import kiro_crew.dashboard.collision_derive as cd

        def _boom(*_a, **_k):
            raise cd.SandboxUnavailableError("no backend (test)", "no_backend", "probe: not Linux")

        monkeypatch.setattr(cd, "sandboxed_spawn_argv", _boom)
        # A real repo cwd, but the sandbox wrap refuses: derive must drop to None
        # rather than propagate SandboxUnavailableError.
        assert cd.derive_repo_context(str(tmp_path)) is None
        assert cd._run_git(str(tmp_path), "rev-parse", "--show-toplevel") is None
