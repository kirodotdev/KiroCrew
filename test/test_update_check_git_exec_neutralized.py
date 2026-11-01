"""The update check's git calls cannot run a program the checkout's own config names.

``_check_git_checkout`` runs on the host at boot, on the 12-hourly poll and on
the dashboard's Check button, with ``cwd`` set to ``KIROCREW_PROJECT_DIR``. On a
source-checkout install that tree is agent-writable, so its ``.git/config`` and
``.gitattributes`` are agent input. Each test plants one exec vector that points
at a marker script and drives the check against a REAL git repository: the
marker must not appear. ``test_a_clean_checkout_still_reports_the_update`` is the
other half: the guards must not stop an ordinary checkout from being checked.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dashboard_updates_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolated_module_state,
)

from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform import update_capability, update_governance

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="marker programs are POSIX shell scripts"),
    pytest.mark.skipif(platform_compat.trusted_git_bin() is None, reason="needs a system git"),
]

_IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *_IDENTITY, *args], cwd=cwd, check=True, capture_output=True)


def _capability() -> update_capability.UpdateCapability:
    return update_capability.UpdateCapability(
        supported=True,
        managed_by=update_capability.MANAGED_BY_GIT,
        mode=update_capability.MODE_NOTIFY,
        can_download=True,
        can_apply=True,
        requires_restart=True,
    )


def _checkout(tmp_path: Path) -> Path:
    """A clone one commit behind its upstream, the commit bumping the version.

    The checkout tracks ``.gitattributes`` naming a ``pwn`` diff driver for
    CHANGELOG.md, so a test only has to define the driver in config.
    """
    upstream = tmp_path / "up.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "src" / "kiro_crew").mkdir(parents=True)
    (seed / "src" / "kiro_crew" / "__init__.py").write_text('__version__ = "0.0.1"\n')
    (seed / "CHANGELOG.md").write_text("# Changelog\n")
    (seed / ".gitattributes").write_text("CHANGELOG.md diff=pwn\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "first")
    _git(seed, "remote", "add", "origin", str(upstream))
    _git(seed, "push", "-q", "origin", "main")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(upstream), str(work))
    (seed / "src" / "kiro_crew" / "__init__.py").write_text('__version__ = "999.0.0"\n')
    with (seed / "CHANGELOG.md").open("a") as f:
        f.write("- a new release line\n")
    _git(seed, "commit", "-qam", "second")
    _git(seed, "push", "-q", "origin", "main")
    return work


def _marker_program(tmp_path: Path) -> tuple[Path, Path]:
    marker = tmp_path / "MARKER_RAN"
    program = tmp_path / "evil.sh"
    # Exits 0 and echoes its first file argument, so a vector that WOULD run
    # also lets git carry on rather than failing loudly at that step.
    program.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat "$1" 2>/dev/null\nexit 0\n')
    program.chmod(0o755)
    return program, marker


def _run_check(work: Path) -> dict:
    asyncio.run(updates._check_git_checkout(str(work), _capability()))
    return dict(updates._update_info)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    # The host's own GIT_SSH_COMMAND beats core.sshCommand, which would hide the
    # repository's value rather than test that the check neutralizes it.
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    # The shared autouse fixture stubs the preflight for the fake-spawn tests;
    # these tests drive real git, so they need the real refusal.
    monkeypatch.setattr(
        updates, "repo_exec_config_reason", update_governance.repo_exec_config_reason
    )
    work = _checkout(tmp_path)
    program, marker = _marker_program(tmp_path)
    return work, program, marker


def test_a_clean_checkout_still_reports_the_update(checkout):
    work, _program, marker = checkout
    info = _run_check(work)
    assert info["check_status"] == updates.CHECK_SUCCEEDED
    assert info["update_available"] is True
    assert info["latest_version"] == "999.0.0"
    assert info["commits_behind"] == 1
    assert info["changes"] == "- a new release line"
    assert not marker.exists()


@pytest.mark.parametrize(
    "key",
    ["core.fsmonitor", "diff.external"],
)
def test_a_pinned_fixed_key_program_does_not_run(checkout, key):
    """Pinned keys are closed and the check still answers."""
    work, program, marker = checkout
    _git(work, "config", key, str(program))
    info = _run_check(work)
    assert not marker.exists(), f"repo-local {key} ran on the host"
    assert info["check_status"] == updates.CHECK_SUCCEEDED
    assert info["changes"] == "- a new release line"


def test_repo_hooks_do_not_run(checkout):
    work, program, marker = checkout
    hooks = work / ".git" / "evil-hooks"
    hooks.mkdir()
    for name in ("reference-transaction", "post-index-change", "post-checkout", "pre-auto-gc"):
        (hooks / name).symlink_to(program)
    _git(work, "config", "core.hooksPath", str(hooks))
    # The default hooks directory too, which a pinned core.hooksPath bypasses.
    for name in ("reference-transaction", "post-index-change"):
        (work / ".git" / "hooks" / name).symlink_to(program)
    info = _run_check(work)
    assert not marker.exists(), "a repository hook ran on the host"
    assert info["check_status"] == updates.CHECK_SUCCEEDED


def test_repo_ssh_command_does_not_run(checkout, monkeypatch, tmp_path):
    work, program, marker = checkout
    # Stand a deterministic failing program in for the trusted ssh, so the fetch
    # neither resolves a hostname nor touches the network -- the planted
    # core.sshCommand must still not run. `core.sshCommand` is a program-valued
    # pin resolved through `trusted_system_bin("ssh")`; override that one name.
    stub = tmp_path / "stub-ssh"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    real = platform_compat.trusted_system_bin
    monkeypatch.setattr(
        platform_compat,
        "trusted_system_bin",
        lambda name: str(stub) if name == "ssh" else real(name),
    )
    _git(work, "config", "core.sshCommand", str(program))
    _git(work, "remote", "set-url", "origin", "ssh://git.example.invalid/repo.git")
    info = _run_check(work)
    assert not marker.exists(), "repo-local core.sshCommand ran on the host"
    # The stub ssh exits non-zero without a hostname lookup: an honest failure.
    assert info["check_status"] == updates.CHECK_FAILED


def test_a_commondir_redirect_is_refused(checkout, tmp_path):
    """A `.git/commondir` beside a plain checkout's `.git` directory is refused.

    A sandboxed agent can create this file even under a bind seal. Host git then
    reads config and hooks from the directory it names, bypassing the config
    preflight. The marker must not run, whatever the attacker's common dir says.
    """
    work, program, marker = checkout
    evil = tmp_path / "evilcommon"
    evil.mkdir()
    _git(evil, "init", "-q", "--bare")
    _git(evil, "config", "core.sshCommand", str(program))
    _git(evil, "config", "core.fsmonitor", str(program))
    (work / ".git" / "commondir").write_text(f"{evil}\n")
    info = _run_check(work)
    assert not marker.exists(), "a .git/commondir redirect ran the attacker's config"
    assert info["check_status"] == updates.CHECK_FAILED
    assert info["update_available"] is None


def _redirect_common_dir(work: Path, tmp_path: Path) -> None:
    """Point *work*'s ``.git/commondir`` at a CLEAN bare repo.

    Clean on purpose: nothing in the redirected config is a key the scope probes
    refuse, so only the commondir refusal itself can turn this checkout away.
    """
    common = tmp_path / "redirected-common"
    common.mkdir()
    _git(common, "init", "-q", "--bare")
    (work / ".git" / "commondir").write_text(f"{common}\n")


def test_the_commondir_file_itself_is_the_refusal(checkout, tmp_path):
    """The structural ``.git/commondir`` check answers before any git probe runs.

    The planted-program test above stays green with this check deleted, because
    the env pins already beat the redirected ``core.sshCommand``/``core.fsmonitor``.
    This one pins the check's own verdict.
    """
    work, _program, _marker = checkout
    _redirect_common_dir(work, tmp_path)
    assert (
        update_governance.repo_exec_config_reason(str(work))
        == "repository redirects its common git dir via .git/commondir"
    )


def test_a_redirected_common_dir_is_refused_without_the_file_check(checkout, tmp_path, monkeypatch):
    """git's own resolved common dir is the backstop when the file is not seen."""
    work, _program, _marker = checkout
    _redirect_common_dir(work, tmp_path)
    real_exists = os.path.exists
    monkeypatch.setattr(
        update_governance.os.path,
        "exists",
        lambda path: False if str(path).endswith("commondir") else real_exists(path),
    )
    reason = update_governance.repo_exec_config_reason(str(work))
    assert reason.startswith("common git dir is redirected to "), reason


def test_a_textconv_driver_named_by_gitattributes_does_not_run(checkout):
    work, program, marker = checkout
    _git(work, "config", "diff.pwn.textconv", str(program))
    info = _run_check(work)
    assert not marker.exists(), "a .gitattributes diff driver ran on the host"
    assert info["check_status"] == updates.CHECK_FAILED
    assert info["update_available"] is None


def test_included_config_is_read_by_the_guards(checkout):
    """``include.path`` reaches both the pins (fixed keys) and the refusal (named ones)."""
    work, program, marker = checkout
    included = work / ".git" / "extra.cfg"
    included.write_text(f"[core]\n\tfsmonitor = {program}\n[diff]\n\texternal = {program}\n")
    _git(work, "config", "include.path", str(included))
    info = _run_check(work)
    assert not marker.exists()
    assert info["check_status"] == updates.CHECK_SUCCEEDED

    included.write_text(f'[diff "pwn"]\n\ttextconv = {program}\n')
    info = _run_check(work)
    assert not marker.exists(), "an included textconv driver ran on the host"
    assert info["check_status"] == updates.CHECK_FAILED


def test_include_if_config_is_read_by_the_refusal(checkout):
    work, program, marker = checkout
    included = work / ".git" / "cond.cfg"
    included.write_text(f'[diff "pwn"]\n\ttextconv = {program}\n')
    _git(work, "config", "includeIf.onbranch:main.path", str(included))
    info = _run_check(work)
    assert not marker.exists(), "an includeIf textconv driver ran on the host"
    assert info["check_status"] == updates.CHECK_FAILED


@pytest.mark.parametrize("remote", ["origin", "fork"])
def test_a_remote_upload_pack_program_does_not_run(checkout, remote):
    """``origin``'s pin loses to the repository (git keeps the first value), so it is refused."""
    work, program, marker = checkout
    if remote != "origin":
        _git(work, "remote", "add", remote, str(work.parent / "up.git"))
        _git(work, "fetch", "-q", remote)
        _git(work, "branch", "-q", f"--set-upstream-to={remote}/main")
    _git(work, "config", f"remote.{remote}.uploadpack", str(program))
    info = _run_check(work)
    assert not marker.exists(), f"remote.{remote}.uploadpack ran on the host"
    assert info["check_status"] == updates.CHECK_FAILED


def test_fetch_all_cannot_reach_another_remotes_upload_pack(checkout):
    work, program, marker = checkout
    _git(work, "remote", "add", "extra", str(work.parent / "up.git"))
    _git(work, "config", "remote.extra.uploadpack", str(program))
    _git(work, "config", "fetch.all", "true")
    _run_check(work)
    assert not marker.exists(), "fetch.all reached another remote's uploadpack"


def test_a_git_shim_on_path_is_not_run(checkout, monkeypatch, tmp_path):
    work, program, marker = checkout
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    (shim_dir / "git").symlink_to(program)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    info = _run_check(work)
    assert not marker.exists(), "a PATH-planted git ran instead of the system one"
    assert info["check_status"] == updates.CHECK_SUCCEEDED


def _plant_after_the_refusal(work: Path, program: Path, vector: str, monkeypatch) -> None:
    """Plant *vector* so that only the fetch, not the refusal, sees it.

    The agent can rewrite ``.git/config`` between the refusal's read and the
    fetch's read. Answering the refusal "clean" models the agent winning that
    race every time, so what is left is the fetch's own flags and environment.
    """
    monkeypatch.setattr(updates, "repo_exec_config_reason", lambda _proj: "")
    upstream = str(work.parent / "up.git")
    if vector == "origin-uploadpack":
        _git(work, "config", "remote.origin.uploadpack", str(program))
    elif vector == "tracked-uploadpack":
        _git(work, "remote", "add", "fork", upstream)
        _git(work, "fetch", "-q", "fork")
        _git(work, "branch", "-q", "--set-upstream-to=fork/main")
        _git(work, "config", "remote.fork.uploadpack", str(program))
    elif vector == "fetch-all-uploadpack":
        _git(work, "remote", "add", "extra", upstream)
        _git(work, "config", "remote.extra.uploadpack", str(program))
        _git(work, "config", "fetch.all", "true")
    elif vector == "git-proxy":
        _git(work, "config", "core.gitProxy", str(program))
        _git(work, "remote", "set-url", "origin", "git://127.0.0.1:9/repo.git")
    elif vector in ("remote-vcs", "helper-url"):
        helper_dir = program.parent / "helpers"
        helper_dir.mkdir()
        (helper_dir / "git-remote-pwn").symlink_to(program)
        monkeypatch.setenv("PATH", f"{helper_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        if vector == "remote-vcs":
            _git(work, "config", "remote.origin.vcs", "pwn")
        else:
            _git(work, "remote", "set-url", "origin", "pwn::anything")
    elif vector == "textconv":
        _git(work, "config", "diff.pwn.textconv", str(program))
    elif vector == "hooks-path":
        hooks = work / ".git" / "evil-hooks"
        hooks.mkdir()
        for ev in ("reference-transaction", "post-index-change", "post-checkout", "pre-auto-gc"):
            (hooks / ev).symlink_to(program)
        _git(work, "config", "core.hooksPath", str(hooks))
    elif vector == "config-hook":
        # git 2.54+ config-defined hooks: no key to pin and no fetch flag closes
        # them, so isolating the fetch is the only close. The ref write fires
        # reference-transaction; a non-isolated fetch runs these.
        for ev in ("reference-transaction", "post-index-change"):
            _git(work, "config", f"hook.{ev}.command", str(program))
    else:
        raise AssertionError(vector)


@pytest.mark.parametrize(
    "vector",
    [
        "origin-uploadpack",
        "tracked-uploadpack",
        "fetch-all-uploadpack",
        "git-proxy",
        "remote-vcs",
        "helper-url",
        "textconv",
        "hooks-path",
        "config-hook",
    ],
)
def test_a_vector_written_after_the_refusal_still_does_not_run(checkout, monkeypatch, vector):
    """The fetch runs isolated, so a vector raced in after the preflight -- a
    hook most of all, which no pin or flag can close -- never runs on the host."""
    work, program, marker = checkout
    _plant_after_the_refusal(work, program, vector, monkeypatch)
    _run_check(work)
    assert not marker.exists(), f"{vector} ran on the host once the refusal was raced"


def test_a_linked_worktree_checkout_still_fetches(checkout, tmp_path):
    """A linked worktree (`.git` is a pointer file) still checks and updates.

    Its object store and tracking refs live in the common git dir, which the
    helper resolves through `git rev-parse --git-path objects` rather than
    assuming `<proj>/.git`. Without that, the check reported a failed fetch on
    every poll for worktree and submodule installs.
    """
    work, _program, marker = checkout
    _git(work, "branch", "--track", "wt", "origin/main")
    wt = work.parent / "worktree"
    _git(work, "worktree", "add", str(wt), "wt")
    info = _run_check(wt)
    assert not marker.exists()
    assert info["check_status"] == updates.CHECK_SUCCEEDED
    assert info["update_available"] is True
    assert info["latest_version"] == "999.0.0"


def test_a_relative_local_remote_resolves_against_the_checkout(checkout):
    """A relative local remote URL resolves against the checkout, not the
    scratch git dir the fetch runs under."""
    work, _program, marker = checkout
    _git(work, "remote", "set-url", "origin", "../up.git")
    info = _run_check(work)
    assert not marker.exists()
    assert info["check_status"] == updates.CHECK_SUCCEEDED
    assert info["update_available"] is True


def test_a_shallow_checkout_still_fetches(checkout, tmp_path):
    """The isolated fetch carries the checkout's shallow boundary, so a shallow
    clone one commit behind still checks."""
    work, _program, marker = checkout
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{work.parent / 'up.git'}", str(shallow))
    # Upstream merges a side branch rooted BELOW the clone's shallow boundary,
    # so the fetch's walk reaches past the boundary commit.
    seed = work.parent / "seed"
    _git(seed, "checkout", "-q", "-b", "side", "HEAD~1")
    (seed / "side.txt").write_text("side\n")
    _git(seed, "add", "side.txt")
    _git(seed, "commit", "-qm", "side")
    _git(seed, "checkout", "-q", "main")
    _git(seed, "merge", "-q", "--no-edit", "side")
    _git(seed, "push", "-q", "origin", "main")
    info = _run_check(shallow)
    assert not marker.exists()
    assert info["check_status"] == updates.CHECK_SUCCEEDED
    assert info["update_available"] is True
