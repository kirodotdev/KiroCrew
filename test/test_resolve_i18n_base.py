"""`.github/scripts/resolve-i18n-base.sh` fetches the base without changing the clone's depth.

CI checks out shallow, so the script fetches the base at depth 1. Contributors
also run it from a full clone to mirror a CI step, and there a depth-limited
fetch would make the repository, and every worktree sharing it, shallow.
"""

from __future__ import annotations

import json
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
    # A CI host's own event must not reach the script: the tests that want one set it.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GITHUB_EVENT_"))}
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


def _pull_request_repo(tmp: Path) -> tuple[dict[str, str], Path, dict[str, str]]:
    """An origin whose PR merge commit was built on a newer main than the event's base.

    Returns the env, the origin, and the commits by name: ``stale_base`` (what
    ``pull_request.base.sha`` still says on a re-run), ``built_on`` (the main the
    merge commit was made on), ``pr_head`` and ``merge``.
    """
    env = _git_env(tmp)
    origin = tmp / "origin"
    _run(env, tmp, "git", "init", "-q", "-b", "main", str(origin))
    _run(env, origin, "git", "config", "commit.gpgsign", "false")
    _run(env, origin, "git", "config", "uploadpack.allowAnySHA1InWant", "true")
    _commit(env, origin, 0)
    stale_base = _run(env, origin, "git", "rev-parse", "HEAD")
    _run(env, origin, "git", "checkout", "-q", "-b", "pr")
    (origin / "pr.txt").write_text("pr\n", encoding="utf-8")
    _run(env, origin, "git", "add", "pr.txt")
    _run(env, origin, "git", "commit", "-q", "-m", "pr")
    pr_head = _run(env, origin, "git", "rev-parse", "HEAD")
    _run(env, origin, "git", "checkout", "-q", "main")
    for n in range(1, 3):
        _commit(env, origin, n)
    built_on = _run(env, origin, "git", "rev-parse", "HEAD")
    _run(env, origin, "git", "merge", "-q", "--no-ff", "-m", "merge", "pr")
    merge = _run(env, origin, "git", "rev-parse", "HEAD")
    _run(env, origin, "git", "update-ref", "refs/pull/1/merge", merge)
    _run(env, origin, "git", "reset", "-q", "--hard", built_on)
    return (
        env,
        origin,
        {"stale_base": stale_base, "built_on": built_on, "pr_head": pr_head, "merge": merge},
    )


def _shallow_checkout(env: dict[str, str], tmp: Path, origin: Path, ref: str) -> Path:
    """What ``actions/checkout`` leaves: one commit at depth 1, detached."""
    clone = tmp / "clone"
    _run(env, tmp, "git", "init", "-q", str(clone))
    _run(env, clone, "git", "remote", "add", "origin", origin.as_uri())
    _run(env, clone, "git", "fetch", "-q", "--depth=1", "origin", ref)
    _run(env, clone, "git", "checkout", "-q", "--detach", "FETCH_HEAD")
    return clone


def _pull_request_event(
    env: dict[str, str], tmp: Path, pr_head: str, event: str = "pull_request"
) -> dict[str, str]:
    path = tmp / "event.json"
    path.write_text(json.dumps({"pull_request": {"head": {"sha": pr_head}}}), encoding="utf-8")
    return {**env, "GITHUB_EVENT_NAME": event, "GITHUB_EVENT_PATH": str(path)}


def test_a_pull_request_merge_checkout_measures_from_the_merge_commits_first_parent(
    tmp_path: Path,
) -> None:
    env, origin, c = _pull_request_repo(tmp_path)
    clone = _shallow_checkout(env, tmp_path, origin, "refs/pull/1/merge")
    pr_env = _pull_request_event(env, tmp_path, c["pr_head"])

    resolved = _run(pr_env, clone, "bash", str(SCRIPT), c["stale_base"])

    # The stale base would charge main's c1 and c2 to the PR; the first parent
    # leaves exactly the PR's own file.
    assert resolved == c["built_on"]
    assert _run(env, clone, "git", "diff", "--name-only", resolved, "HEAD") == "pr.txt"


def test_a_pr_head_checkout_keeps_the_base_it_was_given(tmp_path: Path) -> None:
    # HEAD is a merge, but not one OF the PR head: the PR's own tip merged main in,
    # so its first parent is the PR's previous commit, never the base.
    env, origin, c = _pull_request_repo(tmp_path)
    clone = _shallow_checkout(env, tmp_path, origin, "refs/pull/1/merge")
    pr_env = _pull_request_event(env, tmp_path, c["merge"])

    assert _run(pr_env, clone, "bash", str(SCRIPT), c["stale_base"]) == c["stale_base"]


def test_a_push_keeps_the_base_it_was_given(tmp_path: Path) -> None:
    env, origin, c = _pull_request_repo(tmp_path)
    clone = _shallow_checkout(env, tmp_path, origin, "refs/pull/1/merge")
    push_env = _pull_request_event(env, tmp_path, c["pr_head"], event="push")

    assert _run(push_env, clone, "bash", str(SCRIPT), c["stale_base"]) == c["stale_base"]
