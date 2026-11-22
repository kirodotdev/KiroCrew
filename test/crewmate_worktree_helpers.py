# SPDX-License-Identifier: Apache-2.0
"""Git repositories, and an edition runtime, for the Bot worktree tests.

A PLAIN module, not a ``conftest.py``: each fixture is declared with an explicit
``name=`` so a module importing the ``*_fixture`` symbol does not collide with the
test parameter of the same name (flake8 F811).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from test_worktree_create import _passthrough_spawn, _sandbox_exec_reason


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture(name="git_here")
def git_here_fixture(monkeypatch, request):
    """Git for these tests: plain, except in a case that asks for ``sandboxed_git``.

    The worktree logic is what these cases test; the sandbox wrapper itself is
    tested in ``test_worktree_create``. A sandboxed git costs about 0.3 s a spawn
    on macOS (``sandbox-exec`` compiles the Seatbelt profile on every exec) and each
    git call here is four spawns with its hook scan, so the sandbox stays off the
    cases that do not need it. The Linux CI runners have no backend and run every
    case plain. A case that names ``sandboxed_git`` keeps the host's real sandbox
    where it has one, so the composition stays covered end to end.
    Windows is skipped up front, before any probe: the sandbox fails closed there
    ("not Linux"), and these cases are written for POSIX paths, permissions and links.
    """
    if sys.platform == "win32":
        pytest.skip("sandboxed git does not run on Windows (no backend)")
    if "sandboxed_git" not in request.fixturenames or _sandbox_exec_reason():
        from kiro_crew.dashboard.handlers import worktree as wt

        monkeypatch.setattr(wt, "sandboxed_spawn_argv", _passthrough_spawn)


@pytest.fixture(name="sandboxed_git")
def sandboxed_git_fixture():
    """Named by a case that runs its git under the host's real sandbox (see ``git_here``)."""


def _init(root, branch: str = "main") -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root.parent, "init", "-q", "-b", branch, str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return str(root)


@pytest.fixture(name="repo")
def repo_fixture(tmp_path, git_here):
    root = tmp_path / "shared" / "tasklog"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    return _init(root)


def _not_a_worktree_reason(path: str) -> str:
    """Git's reason the checkout at *path* cannot find its repository, or ``""``."""
    proc = subprocess.run(
        ["git", "-C", path, "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return "" if proc.returncode == 0 else proc.stderr.strip()


def _commit_in(path: str, name: str, message: str) -> None:
    with open(os.path.join(path, name), "w", encoding="utf-8") as fh:
        fh.write(message + "\n")
    _git(path, "add", name)
    _git(path, "commit", "-q", "-m", message)


@pytest.fixture(name="edition_runtime")
def edition_runtime_fixture():
    """``install(runtime)`` composes *runtime* as the edition's ``agent_runtime``.

    The active context is reset after the test.
    """
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.context import reset_context, set_context

    def install(runtime) -> None:
        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, agent_runtime=runtime))

    yield install
    reset_context()
