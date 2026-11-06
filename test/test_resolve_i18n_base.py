"""`.github/scripts/resolve-i18n-base.sh` fetches the base without changing the clone's depth.

CI checks out shallow, so the script fetches the base at depth 1. Contributors
also run it from a full clone to mirror a CI step, and there a depth-limited
fetch would make the repository, and every worktree sharing it, shallow.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew.testing.wait import default_timeout

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "resolve-i18n-base.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("git") is None,
    reason="runs the script's bash against real git; Linux/macOS runners cover it",
)


def _git_env(tmp: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=str(tmp / "gitconfig"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    env.update(git_maintenance_off_entries(env))
    return env


def _run(env: dict[str, str], cwd: Path, *argv: str) -> str:
    out = subprocess.run(
        list(argv),
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    return out.stdout.strip()


def _commit(env: dict[str, str], repo: Path, n: int) -> None:
    (repo / "f.txt").write_text(f"{n}\n", encoding="utf-8")
    _run(env, repo, "git", "add", "f.txt")
    _run(env, repo, "git", "commit", "-q", "-m", f"c{n}")


def _clone_then_move_main(tmp: Path, *clone_args: str) -> tuple[dict[str, str], Path, Path]:
    """An origin with three commits, a clone of it, then two more commits on main."""
    env = _git_env(tmp)
    origin = tmp / "origin"
    _run(env, tmp, "git", "init", "-q", "-b", "main", str(origin))
    _run(env, origin, "git", "config", "commit.gpgsign", "false")
    for n in range(3):
        _commit(env, origin, n)
    clone = tmp / "clone"
    _run(env, tmp, "git", "clone", "-q", *clone_args, origin.as_uri(), str(clone))
    for n in range(3, 5):
        _commit(env, origin, n)
    return env, origin, clone


def test_a_full_clone_stays_full(tmp_path: Path) -> None:
    env, origin, clone = _clone_then_move_main(tmp_path)

    assert _run(env, clone, "bash", str(SCRIPT), "origin/main") == "origin/main"

    assert _run(env, clone, "git", "rev-parse", "origin/main") == _run(
        env, origin, "git", "rev-parse", "main"
    )
    assert _run(env, clone, "git", "rev-parse", "--is-shallow-repository") == "false"


def test_a_shallow_clone_fetches_the_base_at_depth_one(tmp_path: Path) -> None:
    env, origin, clone = _clone_then_move_main(tmp_path, "--depth=1")

    assert _run(env, clone, "bash", str(SCRIPT), "origin/main") == "origin/main"

    assert _run(env, clone, "git", "rev-parse", "origin/main") == _run(
        env, origin, "git", "rev-parse", "main"
    )
    assert _run(env, clone, "git", "rev-list", "--count", "origin/main") == "1"
