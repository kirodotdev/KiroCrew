"""The starting repo for the Slack Code channel workspace tests.

``build_repo_template`` lays out a bare remote and a clone on ``main`` whose
``origin/HEAD`` resolves. The tests' session fixture builds it once and each test
copies it. It runs before the per-test git floor applies, so its git calls use the
scrubbed env: no host config, templates or hooks reach it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from push_guard_helpers import _fixture_git_env


def build_repo_template(base: Path) -> None:
    """Write ``base/remote.git`` (bare) and ``base/proj`` (a clone of it)."""
    env = _fixture_git_env()

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

    remote = base / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=base)
    root = base / "proj"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("gc.auto", "0")):
        git("config", key, value, cwd=root)
    (root / "README.md").write_text("hello\n", newline="\n")
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("x = 1\n", newline="\n")
    git("add", ".", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    git("remote", "add", "origin", "https://user:s3cret@github.com/acme/widgets.git", cwd=root)
    # Push through a local URL, then keep the https form as the configured URL.
    git("push", "-q", str(remote), "main:main", cwd=root)
    git("fetch", "-q", str(remote), "+refs/heads/*:refs/remotes/origin/*", cwd=root)
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=root)
