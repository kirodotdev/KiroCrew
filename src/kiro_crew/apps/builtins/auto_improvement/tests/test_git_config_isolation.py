"""The suite's git must not read the host's global config.

Fixture commits here land on ``main``. A developer host whose ``~/.gitconfig`` points
``core.hooksPath`` at a ``pre-commit`` that refuses such commits failed the whole suite,
while CI runners, which have no global hooks, stayed green.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def test_a_host_hook_that_refuses_commits_does_not_reach_a_fixture_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "host-home"
    hooks = home / "hooks"
    hooks.mkdir(parents=True)
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/bin/sh\necho 'host hook refused the commit' >&2\nexit 1\n", encoding="utf-8"
    )
    hook.chmod(0o755)
    (home / ".gitconfig").write_text(
        f"[core]\n\thooksPath = {hooks.as_posix()}\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "seed.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "commit", "-qm", "seed"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
