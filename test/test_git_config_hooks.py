"""Config-defined git hooks (``hook.<name>.command``, git 2.54+) on host-side git.

``core.hooksPath`` only moves the hook directory, so a hook named in a repository's
``.git/config`` still runs. Every host-side git call over an agent-writable tree must
disable each such hook by name. Two kinds of test:

* argv tests run on any git: ``git config`` lists a ``hook.*`` key whatever the
  version, so the disable flag either reaches the spawned argv or it does not.
* firing tests need git 2.54+, the first version that runs config hooks, and skip
  below it. They prove the flag actually stops the hook.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew import git_config_hooks as gch


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
def _isolated_git_config(tmp_path, monkeypatch):
    """No host global/system config: only the hooks a test plants exist."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)


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


# ── the helper ──


def test_no_hooks_adds_nothing(repo: Path) -> None:
    assert gch.config_hook_names(repo) == []
    assert gch.config_hook_disable_args(repo) == []


def test_lists_every_scope_and_include(repo: Path, tmp_path: Path, monkeypatch) -> None:
    included = tmp_path / "inc.cfg"
    included.write_text('[hook "fromInclude"]\n\tcommand = true\n\tevent = pre-commit\n')
    _git(repo, "config", "include.path", str(included))
    _git(repo, "config", "hook.local.command", "true")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        '[hook "fromGlobal"]\n\tcommand = true\n\tevent = pre-commit\n'
    )
    monkeypatch.delenv("GIT_CONFIG_GLOBAL")
    monkeypatch.setenv("HOME", str(home))
    with (repo / ".git" / "config").open("a") as fh:
        fh.write('[hook "Mixed Case.dot"]\n\tcommand = true\n')
    assert sorted(gch.config_hook_names(repo)) == [
        "Mixed Case.dot",
        "fromGlobal",
        "fromInclude",
        "local",
    ]
    assert gch.config_hook_disable_pairs(repo)[0][0].endswith(".enabled")


def test_two_part_hook_setting_is_not_a_name(repo: Path) -> None:
    _git(repo, "config", "hook.jobs", "2")
    assert gch.config_hook_names(repo) == []


def test_name_with_equals_is_refused(repo: Path) -> None:
    # `-c` splits on the first `=`, so this name could not be disabled.
    with (repo / ".git" / "config").open("a") as fh:
        fh.write('[hook "a=b"]\n\tcommand = true\n\tevent = pre-commit\n')
    with pytest.raises(gch.ConfigHookScanError, match="'='"):
        gch.config_hook_disable_args(repo)


def test_too_many_names_is_refused(repo: Path) -> None:
    with (repo / ".git" / "config").open("a") as fh:
        for i in range(gch.MAX_HOOK_NAMES + 1):
            fh.write(f'[hook "h{i}"]\n\tcommand = true\n')
    with pytest.raises(gch.ConfigHookScanError, match="limit"):
        gch.config_hook_names(repo)


def test_unreadable_config_is_refused(repo: Path) -> None:
    (repo / ".git" / "config").write_text("[broken\n")
    with pytest.raises(gch.ConfigHookScanError):
        gch.config_hook_names(repo)


def test_missing_directory_adds_nothing(tmp_path: Path) -> None:
    assert gch.config_hook_names(tmp_path / "absent") == []


def test_missing_git_binary_adds_nothing(repo: Path, tmp_path: Path) -> None:
    assert gch.config_hook_names(repo, git=str(tmp_path / "no-such-git")) == []


@needs_config_hooks
def test_baseline_hooks_path_alone_does_not_stop_a_config_hook(repo: Path, tmp_path: Path) -> None:
    """Positive control: without the disable flags the planted hook really fires."""
    marker = tmp_path / "marker"
    _plant(repo, marker)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            f"core.hooksPath={os.devnull}",
            "commit",
            "-qm",
            "x",
            "--allow-empty",
        ],
        check=True,
        capture_output=True,
    )
    assert marker.exists()


@needs_config_hooks
def test_disable_args_stop_the_hook_even_with_an_event_named_hook(
    repo: Path, tmp_path: Path
) -> None:
    # `hook.<event>.command` makes git 2.55+ treat `hook.<event>.enabled=false` as a
    # per-hook switch, which is why the helper disables by NAME instead.
    marker = tmp_path / "marker"
    _plant(repo, marker)
    _git(repo, "config", "hook.pre-commit.command", "true")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            f"core.hooksPath={os.devnull}",
            *gch.config_hook_disable_args(repo),
            "commit",
            "-qm",
            "x",
            "--allow-empty",
        ],
        check=True,
        capture_output=True,
    )
    assert not marker.exists()


# ── auto_improvement (git_safety) ──


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


# ── md_notebook ──


def _md_use_path_git(monkeypatch) -> None:
    from kiro_crew.apps.builtins.md_notebook import git_ops

    monkeypatch.setattr(git_ops, "_git_bin", lambda: shutil.which("git"))
    # TRUSTED_PATH would hide a newer git a developer put first on PATH.
    monkeypatch.setattr(git_ops, "TRUSTED_PATH", os.environ["PATH"])


@pytest.mark.asyncio
async def test_md_notebook_run_git_carries_the_disable(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    from kiro_crew.apps.builtins.md_notebook import git_ops

    _md_use_path_git(monkeypatch)
    _plant(repo, tmp_path / "marker")
    seen: list[tuple[str, ...]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kw):
        seen.append(argv)
        return await real(*argv, **kw)

    monkeypatch.setattr(git_ops.asyncio, "create_subprocess_exec", spy)
    await git_ops.run_git(["status", "--porcelain"], str(repo))
    assert seen and "hook.pwn.enabled=false" in seen[-1]
    assert seen[-1].index("hook.pwn.enabled=false") < seen[-1].index("status")


@pytest.mark.asyncio
async def test_md_notebook_scan_failure_is_a_git_error(repo: Path, monkeypatch) -> None:
    from kiro_crew.apps.builtins.md_notebook import git_ops

    _md_use_path_git(monkeypatch)
    with (repo / ".git" / "config").open("a") as fh:
        fh.write('[hook "a=b"]\n\tcommand = true\n')
    with pytest.raises(git_ops.GitError):
        await git_ops.run_git(["status"], str(repo))


@needs_config_hooks
@pytest.mark.asyncio
async def test_md_notebook_commit_does_not_fire(repo: Path, tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.apps.builtins.md_notebook import git_ops

    _md_use_path_git(monkeypatch)
    marker = tmp_path / "marker"
    _plant(repo, marker)
    (repo / "a.txt").write_text("b\n")
    await git_ops.run_git(["add", "-A"], str(repo))
    await git_ops.run_git(["commit", "-qm", "x"], str(repo))
    assert not marker.exists()


# ── papyrus ──


@pytest.mark.asyncio
async def test_papyrus_git_carries_the_disable(repo: Path, tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.apps.builtins.papyrus.backend import gitops

    _plant(repo, tmp_path / "marker")
    seen: list[list[str]] = []

    class Captured(Exception):
        pass

    def capture(argv, *a, **kw):
        seen.append(list(argv))
        raise Captured

    monkeypatch.setattr(gitops, "sandboxed_spawn_argv", capture)
    with pytest.raises(Captured):
        await gitops._git(["status"], cwd=repo)
    assert seen and "hook.pwn.enabled=false" in seen[0]
    assert seen[0].index("hook.pwn.enabled=false") < seen[0].index("status")


# ── dev_fleet ──


def test_dev_fleet_rewrites_only_git(repo: Path, tmp_path: Path) -> None:
    from kiro_crew.apps.builtins.dev_fleet import runtime

    _plant(repo, tmp_path / "marker")
    assert runtime._with_config_hooks_off(["npm", "ci"], str(repo)) == ["npm", "ci"]
    via_cwd = runtime._with_config_hooks_off(["git", "fetch"], str(repo))
    assert via_cwd == ["git", "-c", "hook.pwn.enabled=false", "fetch"]
    git = shutil.which("git")
    assert git
    via_dash_c = runtime._with_config_hooks_off([git, "-C", str(repo), "remote"], None)
    assert via_dash_c[1:3] == ["-c", "hook.pwn.enabled=false"]


@pytest.mark.asyncio
async def test_dev_fleet_run_cmd_carries_the_disable(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    from kiro_crew.apps.builtins.dev_fleet import runtime

    _plant(repo, tmp_path / "marker")
    seen: list[list[str]] = []

    def capture(cmd, *a, **kw):
        seen.append(list(cmd))
        raise RuntimeError("captured")

    monkeypatch.setattr(runtime, "sandboxed_spawn_argv", capture)
    rc, _out, err = await runtime._run_cmd(["git", "-C", str(repo), "status"])
    assert rc == -1 and "captured" in err
    assert seen and "hook.pwn.enabled=false" in seen[0]


@pytest.mark.asyncio
async def test_dev_fleet_run_cmd_refuses_on_scan_failure(repo: Path, monkeypatch) -> None:
    from kiro_crew.apps.builtins.dev_fleet import runtime

    with (repo / ".git" / "config").open("a") as fh:
        fh.write('[hook "a=b"]\n\tcommand = true\n')
    monkeypatch.setattr(runtime, "sandboxed_spawn_argv", lambda *a, **k: pytest.fail("spawned"))
    rc, _out, err = await runtime._run_cmd(["git", "-C", str(repo), "status"])
    assert rc == -1 and "hook" in err


# ── dashboard worktree ──


def test_worktree_run_git_carries_the_disable(repo: Path, tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import worktree as wt

    _plant(repo, tmp_path / "marker")
    seen: list[list[str]] = []

    def capture(argv, *a, **kw):
        seen.append(list(argv))
        raise RuntimeError("captured")

    monkeypatch.setattr(wt, "sandboxed_spawn_argv", capture)
    with pytest.raises(wt.SandboxUnavailable):
        wt._run_git(["worktree", "list"], str(repo))
    assert seen and "hook.pwn.enabled=false" in seen[0]


def test_worktree_run_git_scan_failure_reads_as_git_failure(repo: Path, monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import worktree as wt

    with (repo / ".git" / "config").open("a") as fh:
        fh.write('[hook "a=b"]\n\tcommand = true\n')
    monkeypatch.setattr(wt, "sandboxed_spawn_argv", lambda *a, **k: pytest.fail("spawned"))
    proc = wt._run_git(["worktree", "list"], str(repo))
    assert proc.returncode != 0 and "hook" in proc.stderr


# ── update_governance ──


def test_update_governance_refuses_a_repo_hook(repo: Path) -> None:
    from kiro_crew.platform import update_governance as ug

    assert ug.repo_exec_config_reason(str(repo)) == ""
    _git(repo, "config", "hook.pwn.command", "true")
    assert "hook.pwn.command" in ug.repo_exec_config_reason(str(repo))


def test_update_governance_ignores_a_global_hook(repo: Path, tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.platform import update_governance as ug

    glob = tmp_path / "global.cfg"
    glob.write_text('[hook "mine"]\n\tcommand = true\n\tevent = pre-commit\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(glob))
    assert ug.repo_exec_config_reason(str(repo)) == ""
