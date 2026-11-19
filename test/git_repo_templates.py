"""Git repos built once and copied per test, for the Windows-slow test files.

A git spawn costs about 0.17 s on the Windows CI runner (run 44376034204: 51
``test_one_hostile_config_key_*`` cases of ~37 spawns each took 6.4 s apiece),
so a fixture that rebuilds the same repo for every test pays that on each spawn.
A fixture here builds the repo ONCE per session and each test gets its own
``copytree`` of it, so no test can reach another's repo. The template is never
handed to a test. Anything that names an absolute path (a worktree, a remote)
is still made per test, with real git, after the copy.

The template builders run before the per-test git floor applies, so they use
the scrubbed env from :func:`push_guard_helpers._fixture_git_env`: no host
config, templates or hooks reach them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from push_guard_helpers import _fixture_git_env


def copy_repo(template: Path, dest: Path) -> Path:
    """A private copy of *template* at *dest* (symlinks kept as links)."""
    return Path(shutil.copytree(template, dest, symlinks=True))


def _surface_git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_fixture_git_env(),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture(scope="session")
def _surface_root_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``proj``: one commit on ``main`` holding ``README.md``, user and gc.auto set.

    The same repo ``slack_code_channel_surface_helpers.worktree_fixture`` builds
    with six git spawns per test; here it is built once.
    """
    root = tmp_path_factory.mktemp("surface-root") / "proj"
    root.mkdir()
    _surface_git("init", "-q", "-b", "main", cwd=root)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("gc.auto", "0")):
        _surface_git("config", key, value, cwd=root)
    (root / "README.md").write_text("hello\n")
    _surface_git("add", ".", cwd=root)
    _surface_git("commit", "-q", "-m", "init", cwd=root)
    return root


@pytest.fixture(name="worktree")
def surface_worktree_fixture(tmp_path: Path, _surface_root_template: Path) -> tuple[Path, Path]:
    """``(root, tree)`` like ``worktree_fixture``: a copied root plus a real channel worktree.

    The worktree records absolute paths in both repos, so it is added per test
    with real git (``git worktree add``), never copied.
    """
    from slack_code_channel_surface_helpers import BRANCH, _channel_tree, _git

    root = copy_repo(_surface_root_template, tmp_path / "proj")
    tree = _channel_tree(root)  # where a bind from this root cuts it
    _git("worktree", "add", "-q", "-b", BRANCH, str(tree), cwd=root)
    return root, tree
