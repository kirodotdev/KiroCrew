"""Config-defined git hooks (``hook.<name>.command``, git 2.54+) on the app's host-side git.

``core.hooksPath`` only moves the hook directory, so a hook named in a repository's
``.git/config`` still runs. ``spine.git_safety`` and the gate's git argv disable each
such hook by name, through core's ``kiro_crew.git_config_hooks``. The argv tests run
on any git; the firing test needs git 2.54+, the first version that runs config
hooks, and skips below it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


def _git_version() -> tuple[int, int]:
    out = subprocess.run(["git", "--version"], capture_output=True, check=True).stdout.decode()
    m = re.search(r"(\d+)\.(\d+)", out)
    assert m, out
    return int(m.group(1)), int(m.group(2))


needs_config_hooks = pytest.mark.skipif(
    _git_version() < (2, 54), reason="git < 2.54 has no config-defined hooks"
)

_ENV = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


@pytest.fixture(autouse=True)
def _isolated_git_config(tmp_path, _floor_monkeypatch):
    """No host global/system config: only the hooks a test plants exist.

    Patches through the isolation floor's own ``MonkeyPatch`` (``_floor_monkeypatch``),
    not the shared ``monkeypatch``: a test that calls ``monkeypatch.undo()`` mid-way must
    not also drop this fixture's git-config pins.
    """
    _floor_monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    _floor_monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    _floor_monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for key, value in _ENV.items():
        _floor_monkeypatch.setenv(key, value)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "init")
    return root


def _plant(repo: Path, marker: Path, name: str = "pwn") -> None:
    """A config hook on every event the host-side helpers trigger."""
    for event in ("pre-commit", "post-commit", "post-index-change", "reference-transaction"):
        _git(repo, "config", "--add", f"hook.{name}.event", event)
    # `#` ends the command so git's appended hook arguments are ignored.
    _git(repo, "config", f"hook.{name}.command", f"echo {name} >> '{marker}' #")


def test_git_safety_argv_carries_the_disable(repo: Path, tmp_path: Path) -> None:
    from kiro_crew.apps.builtins.auto_improvement.spine import gate, git_safety

    _plant(repo, tmp_path / "marker")
    assert "hook.pwn.enabled=false" in git_safety.git_argv(repo, "status")
    assert "hook.pwn.enabled=false" in gate._git_argv(repo, "add", "-A")


def test_git_safety_scan_failure_is_a_git_safety_error(repo: Path) -> None:
    from kiro_crew.apps.builtins.auto_improvement.spine import git_safety

    with (repo / ".git" / "config").open("a") as fh:
        fh.write('[hook "a=b"]\n\tcommand = true\n')
    with pytest.raises(git_safety.GitSafetyError):
        git_safety.hook_off_args(repo)


@needs_config_hooks
def test_git_safety_commit_does_not_fire(repo: Path, tmp_path: Path) -> None:
    from kiro_crew.apps.builtins.auto_improvement.spine import gate

    marker = tmp_path / "marker"
    _plant(repo, marker)
    (repo / "a.txt").write_text("b\n")
    for args in (("add", "-A"), ("commit", "-qm", "x"), ("status", "--porcelain")):
        subprocess.run(gate._git_argv(repo, *args), check=True, capture_output=True)
    assert not marker.exists()
