"""Shared git fixtures for the Review Fix test modules.

``_repo`` builds a throwaway git repository with one committed file so tests
can exercise candidate worktrees, patches, and scoped commits without touching
the operator's machine. It lives here — not in either test module — because
pytest collects each ``test/test_*.py`` as a top-level module, so importing one
test module from another fails under sharded collection.
"""

from __future__ import annotations

import subprocess

import pytest


@pytest.fixture(autouse=True)
def unsandboxed_git(monkeypatch):
    """Run Task Runner git spawns without the OS sandbox wrapper.

    These modules exercise real ``git`` through ``git_coord._git``, whose
    chokepoint fails closed on hosts with no sandbox backend — GitHub's Ubuntu
    runners deny unprivileged user namespaces via AppArmor and the Windows
    runners have none — so an unwrapped fixture would make them red for a
    property of the CI host rather than the code under test. Swapping the
    chokepoint for a passthrough keeps the real git subprocess behavior under
    test while making host sandbox capability irrelevant.

    Both halves of the chokepoint are patched: ``git_coord._git`` awaits
    ``sandboxed_spawn_argv_async`` (which only delegates preparation to the
    sync ``sandboxed_spawn_argv`` via ``_prepare``), so the async patch is what
    actually bypasses the failing capability probe on such hosts; the sync
    patch keeps callers that invoke the prepare step directly covered.
    """

    def passthrough(argv, mode="standard", **kwargs):
        return list(argv), None, None

    async def passthrough_async(argv, mode="standard", **kwargs):
        return list(argv), None, None

    monkeypatch.setattr("kiro_crew.git_coord.sandboxed_spawn_argv", passthrough)
    monkeypatch.setattr("kiro_crew.git_coord.sandboxed_spawn_argv_async", passthrough_async)


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "feature/fix")
    # Pin EOL handling off: the module under test runs git with
    # GIT_CONFIG_NOSYSTEM=1 while these setup calls see the host system
    # config, and a host autocrlf would otherwise make the committed blob's
    # EOLs diverge from what the working-tree diff later compares against.
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Review Fix Test")
    (repo / "target.txt").write_text("before\n", encoding="utf-8")
    (repo / "unrelated.txt").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "--", "target.txt", "unrelated.txt")
    _git(repo, "commit", "-m", "initial")
    return repo
