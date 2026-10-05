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


# ── the helper ──


def test_no_hooks_adds_nothing(repo: Path) -> None:
    assert gch.config_hook_names(repo) == []
    # No hook.* flags, but the submodule-recursion pins are always emitted.
    assert gch.config_hook_disable_args(repo) == [
        "-c",
        "submodule.recurse=false",
        "-c",
        "fetch.recurseSubmodules=false",
    ]


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
    assert any("enabled" in a for a in gch.config_hook_disable_args(repo))


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
        for i in range(gch._MAX_HOOK_NAMES + 1):
            fh.write(f'[hook "h{i}"]\n\tcommand = true\n')
    with pytest.raises(gch.ConfigHookScanError, match="limit"):
        gch.config_hook_names(repo)


def test_name_too_long_is_refused(repo: Path) -> None:
    """A hook name over _MAX_HOOK_NAME_BYTES bytes cannot be passed as a -c arg."""
    long_name = "x" * (gch._MAX_HOOK_NAME_BYTES + 1)
    with (repo / ".git" / "config").open("a") as fh:
        fh.write(f'[hook "{long_name}"]\n\tcommand = true\n')
    with pytest.raises(gch.ConfigHookScanError, match="bytes"):
        gch.config_hook_names(repo)


def test_empty_hook_name_is_disabled(repo: Path) -> None:
    """[hook ""] with event=pre-commit must not be silently skipped."""
    with (repo / ".git" / "config").open("a") as fh:
        fh.write('[hook ""]\n\tcommand = true\n\tevent = pre-commit\n')
    args = gch.config_hook_disable_args(repo)
    assert "hook..enabled=false" in args, args


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
        # Passthrough: the scan argvs run unwrapped (no OS sandbox backend needed, so this
        # holds on Windows CI too); the real `status` call is captured and short-circuited.
        seen.append(list(argv))
        if "status" in argv:
            raise Captured
        return list(argv), dict(os.environ), None

    monkeypatch.setattr(gitops, "sandboxed_spawn_argv", capture)
    with pytest.raises(Captured):
        await gitops._git(["status"], cwd=repo)
    real_call = next(a for a in seen if "status" in a)
    assert "hook.pwn.enabled=false" in real_call
    assert real_call.index("hook.pwn.enabled=false") < real_call.index("status")


@pytest.mark.asyncio
async def test_papyrus_git_scans_without_a_trusted_git(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """The sandboxed scan works without a 'trusted' git in a fixed system dir: with
    trusted_git_bin() returning None, the planted hook is still disabled."""
    from kiro_crew import platform_compat
    from kiro_crew.apps.builtins.papyrus.backend import gitops

    monkeypatch.setattr(platform_compat, "trusted_git_bin", lambda: None)
    _plant(repo, tmp_path / "marker")
    seen: list[list[str]] = []

    class Captured(Exception):
        pass

    def capture(argv, *a, **kw):
        seen.append(list(argv))
        if "status" in argv:
            raise Captured
        return list(argv), dict(os.environ), None

    monkeypatch.setattr(gitops, "sandboxed_spawn_argv", capture)
    with pytest.raises(Captured):
        await gitops._git(["status"], cwd=repo)
    real_call = next(a for a in seen if "status" in a)
    assert "hook.pwn.enabled=false" in real_call


# ── dev_fleet ──


def test_dev_fleet_rewrites_only_git(repo: Path, tmp_path: Path) -> None:
    from kiro_crew.apps.builtins.dev_fleet import runtime

    _plant(repo, tmp_path / "marker")
    assert runtime._with_config_hooks_off(["npm", "ci"], str(repo)) == ["npm", "ci"]
    via_cwd = runtime._with_config_hooks_off(["git", "fetch"], str(repo))
    assert via_cwd == [
        "git",
        "-c",
        "hook.pwn.enabled=false",
        "-c",
        "submodule.recurse=false",
        "-c",
        "fetch.recurseSubmodules=false",
        "fetch",
    ]
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
        # Passthrough the scan argvs (no backend needed); the real `worktree` call raises as
        # a sandbox outage would, which is what `_run_git` turns into SandboxUnavailable.
        seen.append(list(argv))
        if "worktree" in argv:
            raise RuntimeError("captured")
        return list(argv), dict(os.environ), None

    monkeypatch.setattr(wt, "sandboxed_spawn_argv", capture)
    with pytest.raises(wt.SandboxUnavailable):
        wt._run_git(["worktree", "list"], str(repo))
    real_call = next(a for a in seen if "worktree" in a)
    assert "hook.pwn.enabled=false" in real_call


def test_worktree_run_git_scan_failure_reads_as_git_failure(repo: Path, monkeypatch) -> None:
    """A hook name that cannot be disabled (`=` in it) fails the scan closed: the git
    call is refused and reads as a git failure, never run with the hook enabled."""
    from kiro_crew.dashboard.handlers import worktree as wt

    with (repo / ".git" / "config").open("a") as fh:
        fh.write('[hook "a=b"]\n\tcommand = true\n')
    monkeypatch.setattr(
        wt, "sandboxed_spawn_argv", lambda argv, **kw: (list(argv), dict(os.environ), None)
    )
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


def test_scan_uses_the_caller_s_git_binary(repo: Path, monkeypatch) -> None:
    """The scan runs the SAME git the caller passes (its own real-call binary), not a
    substituted trusted path -- scanning a different binary than the call would protect
    nothing and would break hosts whose git lives outside the fixed system dirs."""
    seen: list[str] = []
    real = gch._run

    def spy(argv, **kw):
        seen.append(argv[0])
        return real(argv, **kw)

    monkeypatch.setattr(gch, "_run", spy)
    gch.config_hook_names(repo, git="git")
    assert seen and all(a == "git" for a in seen)


def _passthrough_spawn(argv, *, mode, env):
    """A stand-in for `sandboxed_spawn_argv`: returns the argv unwrapped (what a real
    backend does under an outer sandbox) so the scan runs the caller's own git."""
    return list(argv), dict(env) if env is not None else dict(os.environ), None


def test_sandboxed_scan_produces_flags(tmp_path: Path, monkeypatch) -> None:
    """The sandboxed entry point disables a planted hook, using the git the caller's own
    spawn resolves -- with no dependency on a 'trusted' system git existing."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "trusted_git_bin", lambda: None, raising=False)
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "hook.pwn.command", "true")
    _git(repo, "config", "hook.pwn.event", "pre-commit")
    args = gch.config_hook_disable_args_sandboxed(
        repo, spawn_argv=_passthrough_spawn, mode="standard"
    )
    assert "hook.pwn.enabled=false" in args


def test_sandboxed_scan_fails_closed_when_the_sandbox_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """If the spawn cannot be confined (no backend, no opt-in), the sandbox's own error
    propagates unchanged, so the caller refuses the git call the same way it refuses a
    real-call spawn failure -- never running the real call with hooks enabled."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "hook.pwn.command", "true")

    def no_backend(argv, *, mode, env):
        raise RuntimeError("no sandbox backend and sandbox_allow_unsandboxed_exec is unset")

    with pytest.raises(RuntimeError, match="no sandbox backend"):
        gch.config_hook_disable_args_sandboxed(repo, spawn_argv=no_backend, mode="strict")


def test_sandboxed_scan_fails_closed_on_unreadable_config(tmp_path: Path, monkeypatch) -> None:
    """A config the scan cannot read (vs. a sandbox outage) refuses as ConfigHookScanError."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".git" / "config").write_text("[broken\n")
    with pytest.raises(gch.ConfigHookScanError):
        gch.config_hook_disable_args_sandboxed(repo, spawn_argv=_passthrough_spawn, mode="strict")


def test_sandboxed_scan_bare_path_git_is_never_run_unsandboxed(tmp_path: Path, monkeypatch) -> None:
    """Every scan git argv is handed to the caller's spawn wrapper -- the module never
    spawns a bare-PATH git itself on the sandboxed path."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "hook.pwn.command", "true")
    wrapped_argvs: list[list[str]] = []

    def spy_spawn(argv, *, mode, env):
        wrapped_argvs.append(list(argv))
        return list(argv), dict(os.environ), None

    gch.config_hook_disable_args_sandboxed(repo, spawn_argv=spy_spawn, mode="strict")
    assert wrapped_argvs, "the scan spawned nothing"
    assert all(a[0] == "git" for a in wrapped_argvs)


def test_disable_args_pin_submodule_recursion(repo: Path) -> None:
    """No child git may run in a submodule (where a hook we cannot scan by name could
    live, e.g. a deinitialized submodule's gitdir): fetch/checkout recursion and the
    status/diff submodule check are all pinned off."""
    args = gch.config_hook_disable_args(repo)
    assert "submodule.recurse=false" in args
    assert "fetch.recurseSubmodules=false" in args


# ── submodule hooks: a child git in a submodule reads the submodule's own config ──
#
# `config_hook_names` finds each gitlinked submodule (`git ls-files -s`, mode 160000) and
# runs git IN it to list its hook names, covering an absorbed submodule (gitdir under
# `.git/modules`) and a non-absorbed nested `sub/.git` alike. The `-c hook.<name>.enabled`
# `=false` flags reach the child git through `GIT_CONFIG_PARAMETERS`.


def _absorbed_submodule(parent: Path, name: str = "subpwn") -> Path:
    """Superproject with a committed submodule; a hook in the absorbed modules/ gitdir."""
    sub = parent / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", "-b", "main")
    (sub / "f").write_text("x\n")
    _git(sub, "add", "f")
    _git(sub, "commit", "-qm", "init")
    sup = parent / "sup"
    sup.mkdir()
    _git(sup, "init", "-q", "-b", "main")
    _git(sup, "-c", "protocol.file.allow=always", "submodule", "add", str(sub), "sub")
    _git(sup, "commit", "-qm", "add sub")
    with (sup / ".git" / "modules" / "sub" / "config").open("a") as fh:
        fh.write(f'[hook "{name}"]\n\tcommand = true\n\tevent = pre-commit\n')
    return sup


def _nonabsorbed_submodule(parent: Path, name: str = "nonabs") -> Path:
    """Superproject with a staged gitlink whose nested sub/.git is a real directory."""
    sup = parent / "sup"
    sup.mkdir()
    _git(sup, "init", "-q", "-b", "main")
    (sup / "t").write_text("t\n")
    _git(sup, "add", "t")
    _git(sup, "commit", "-qm", "init")
    sub = sup / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", "-b", "main")
    (sub / "f").write_text("s\n")
    _git(sub, "add", "f")
    _git(sub, "commit", "-qm", "init")
    _git(sub, "config", f"hook.{name}.command", "true")
    _git(sub, "config", f"hook.{name}.event", "pre-commit")
    subprocess.run(
        ["git", "-C", str(sup), "-c", "protocol.file.allow=always", "add", "sub"],
        capture_output=True,
    )
    return sup


def test_absorbed_submodule_hook_is_listed(tmp_path: Path) -> None:
    assert "subpwn" in gch.config_hook_names(_absorbed_submodule(tmp_path))


def test_absorbed_submodule_hook_from_subdirectory_cwd(tmp_path: Path) -> None:
    """A cwd deep in the work tree still finds the submodule (ls-files from toplevel)."""
    sup = _absorbed_submodule(tmp_path)
    assert "subpwn" in gch.config_hook_names(sup / "sub")


def test_nonabsorbed_submodule_hook_is_listed(tmp_path: Path) -> None:
    assert "nonabs" in gch.config_hook_names(_nonabsorbed_submodule(tmp_path))


def test_disable_args_include_submodule_hook(tmp_path: Path) -> None:
    args = gch.config_hook_disable_args(_absorbed_submodule(tmp_path))
    assert "hook.subpwn.enabled=false" in args


def test_no_submodule_is_empty(repo: Path) -> None:
    assert gch.config_hook_names(repo) == []


@needs_config_hooks
def test_absorbed_submodule_hook_blocked_end_to_end(tmp_path: Path) -> None:
    """On git 2.54+: a dirty submodule's post-index-change hook must not run under the
    disable args (they reach the submodule child via GIT_CONFIG_PARAMETERS)."""
    sup = _absorbed_submodule(tmp_path, name="subpwn")
    marker = tmp_path / "m"
    cfg = sup / ".git" / "modules" / "sub" / "config"
    text = (
        cfg.read_text()
        .replace("\tcommand = true\n", f'\tcommand = echo F >> "{marker.as_posix()}" #\n')
        .replace("\tevent = pre-commit\n", "\tevent = post-index-change\n")
    )
    cfg.write_text(text)
    (sup / "sub" / "f").write_text("dirty\n")
    args = gch.config_hook_disable_args(sup)
    subprocess.run(
        [
            "git",
            "-C",
            str(sup),
            "-c",
            f"core.hooksPath={os.devnull}",
            *args,
            "status",
            "--porcelain",
        ],
        check=True,
        capture_output=True,
    )
    assert not marker.exists()


# ── nested submodules: a child git spawns a grandchild git, which reads ITS own config ──
#
# `git status` recurses: the superproject spawns a child git in each submodule, and that
# child spawns a grandchild git in each of ITS submodules. Every level reads its own
# agent-writable config, so `config_hook_names` must enumerate every level. The disable
# flags reach a grandchild through `GIT_CONFIG_PARAMETERS` (verified two levels on 2.55).


def _nested_submodule(parent: Path, name: str = "deephook") -> Path:
    """sup -> childsub -> gcsub, with a config hook in the checked-out GRANDCHILD."""
    gc = parent / "gc"
    gc.mkdir()
    _git(gc, "init", "-q", "-b", "main")
    (gc / "f").write_text("x\n")
    _git(gc, "add", "f")
    _git(gc, "commit", "-qm", "init")

    child = parent / "child"
    child.mkdir()
    _git(child, "init", "-q", "-b", "main")
    (child / "g").write_text("y\n")
    _git(child, "add", "g")
    _git(child, "commit", "-qm", "init")
    _git(child, "-c", "protocol.file.allow=always", "submodule", "add", str(gc), "gcsub")
    _git(child, "commit", "-qm", "add gc")

    sup = parent / "sup"
    sup.mkdir()
    _git(sup, "init", "-q", "-b", "main")
    (sup / "h").write_text("z\n")
    _git(sup, "add", "h")
    _git(sup, "commit", "-qm", "init")
    _git(sup, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "childsub")
    _git(sup, "commit", "-qm", "add child")
    _git(sup, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive")

    # The hook belongs to the CHECKED-OUT grandchild (its own gitdir), not the source repo.
    deep = sup / "childsub" / "gcsub"
    _git(deep, "config", f"hook.{name}.command", "true")
    _git(deep, "config", f"hook.{name}.event", "post-index-change")
    return sup


def test_nested_submodule_hook_is_listed(tmp_path: Path) -> None:
    """A hook two levels down (submodule of a submodule) is enumerated."""
    assert "deephook" in gch.config_hook_names(_nested_submodule(tmp_path))


def test_nested_submodule_hook_in_disable_args(tmp_path: Path) -> None:
    assert "hook.deephook.enabled=false" in gch.config_hook_disable_args(
        _nested_submodule(tmp_path)
    )


@needs_config_hooks
def test_nested_submodule_hook_blocked_end_to_end(tmp_path: Path) -> None:
    """git 2.54+: a dirty grandchild submodule's post-index-change hook must not fire on a
    superproject `git status` once the disable args are passed."""
    sup = _nested_submodule(tmp_path, name="deephook")
    marker = tmp_path / "m"
    deep = sup / "childsub" / "gcsub"
    _git(deep, "config", "hook.deephook.command", f'echo F >> "{marker.as_posix()}" #')
    (deep / "f").write_text("dirty\n")  # make the grandchild refresh its index
    args = gch.config_hook_disable_args(sup)
    assert "hook.deephook.enabled=false" in args
    subprocess.run(
        ["git", "-C", str(sup), "-c", f"core.hooksPath={os.devnull}", *args, "status"],
        check=True,
        capture_output=True,
    )
    assert not marker.exists()


# ── config_hook_disable_args_sandboxed: scan runs through the caller's own sandbox ──


def test_too_many_nested_repos_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """The repo-count bound must RAISE, not return a partial list. A LIFO walk that just
    stopped at the limit would leave the deepest repo's hook enabled on a `git status`
    that still recurses into it."""
    monkeypatch.setattr(gch, "_MAX_SUBMODULE_REPOS", 1)
    sup = _nested_submodule(tmp_path)
    with pytest.raises(gch.ConfigHookScanError, match="nested repositories"):
        gch.config_hook_names(sup)


@pytest.mark.skipif(os.name == "nt", reason="Windows strips trailing spaces from filenames")
def test_toplevel_path_with_trailing_space_still_finds_submodule(tmp_path: Path) -> None:
    """A work tree whose own path ends in a space must not lose its submodules: only the
    `rev-parse --show-toplevel` terminating newline is stripped, never the path's spaces."""
    sub = tmp_path / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", "-b", "main")
    (sub / "f").write_text("x\n")
    _git(sub, "add", "f")
    _git(sub, "commit", "-qm", "init")
    sup = tmp_path / "sup "  # the repo's OWN directory name ends in a space
    sup.mkdir()
    _git(sup, "init", "-q", "-b", "main")
    _git(sup, "-c", "protocol.file.allow=always", "submodule", "add", str(sub), "sub")
    _git(sup, "commit", "-qm", "add sub")
    with (sup / ".git" / "modules" / "sub" / "config").open("a") as fh:
        fh.write('[hook "spacepwn"]\n\tcommand = true\n\tevent = pre-commit\n')
    assert "spacepwn" in gch.config_hook_names(sup)


@pytest.mark.asyncio
async def test_sandboxed_scan_async_produces_flags(tmp_path: Path) -> None:
    """The async entry (used by papyrus) runs the scan off-loop and returns the same flags."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "hook.pwn.command", "true")
    _git(repo, "config", "hook.pwn.event", "pre-commit")
    args = await gch.config_hook_disable_args_sandboxed_async(
        repo, spawn_argv=_passthrough_spawn, mode="standard"
    )
    assert "hook.pwn.enabled=false" in args
    assert "submodule.recurse=false" in args
    assert "fetch.recurseSubmodules=false" in args
