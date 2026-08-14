"""Sandboxed git execution for worktree operations.

Every git spawn in the worktree feature goes through :func:`run_git`, which
routes through the ``sandboxed_spawn_argv`` chokepoint (OS isolation +
credential-scrubbed env), matching ``git_coord.py``'s treatment of
agent-influenced git. A host with no sandbox backend and no explicit
``agent.sandbox_allow_unsandboxed_exec`` opt-in gets :exc:`SandboxUnavailable`
rather than an unisolated spawn.

The repo-supplied-code guards sit ON TOP of that, because isolation bounds what
a hook can reach but does not stop it running: the ``-c`` overrides in
:func:`git_no_repo_code` remove ``core.hooksPath`` and ``core.fsmonitor``, which
beat every config file.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess

from kiro_crew.sandbox import resource_limit_preexec, sandboxed_spawn_argv

# Wall-clock ceiling for each git invocation. `worktree add` copies a working
# tree, so it is not instant on a large repo, but it is local-only — a run
# longer than this means something is wedged (a lock, a hook prompting for
# input) and the request should fail rather than hold a connection open.
GIT_TIMEOUT = 120

# `core.hooksPath` sink. A NON-DIRECTORY, non-replaceable OS device: git finds no
# `post-checkout` under it and there is no directory anyone could drop one into.
#
# An in-repo sentinel is resolved relative to the repo, so whoever prepared the
# checkout could create it and put `post-checkout` inside — the suppression
# would become the execution vector. A gateway-owned `mkdtemp` directory moves
# the path out of the repo but leaves a same-uid, process-lifetime directory a
# compromised agent could chmod and populate between calls. `os.devnull` has no
# such window and needs no bookkeeping.
HOOKS_SINK = os.devnull

SANDBOX_REFUSAL = (
    "This host has no OS sandbox backend, so Kiro Crew will not run git for you. "
    "Create the worktree manually."
)

# Prefix every failure the sandbox launcher itself reports (see `sandbox.py`'s
# `sys.exit(f"sandbox: ...")` calls). Distinguishes "isolation could not be
# established" from a genuine git error.
SANDBOX_LAUNCHER_PREFIX = "sandbox: "

# STRICT, not the "standard" default. A caller may run `git config --includes`,
# and `include.path` is repo-controlled: a hostile checkout can point it at
# `~/.aws/credentials` (or `~/.netrc`, `~/.git-credentials`) and have git READ
# that file as config. "standard" leaves those visible; "strict" bind-mounts them
# away, along with `~/.ssh`. Nothing here needs a credential — refs are resolved
# locally and no remote is contacted — so strict costs the operation nothing.
SANDBOX_MODE = "strict"


class SandboxUnavailable(RuntimeError):
    """No OS sandbox backend, so the git spawn is refused rather than run bare."""


def git_no_repo_code() -> tuple[str, ...]:
    """Config overrides that stop the REPOSITORY supplying a program to run.

    ``-c`` beats every config file, so these hold even against a hostile
    ``.git/config``:

    * ``core.hooksPath`` -> :data:`HOOKS_SINK`, so no ``post-checkout`` (the one
      hook ``worktree add`` fires) can be found or planted.
    * ``core.fsmonitor=false`` -> repo config can otherwise name an arbitrary
      filesystem-monitor command that git spawns on index reads.

    Together these remove the repo-controlled-code-execution vector, which is
    what would otherwise argue for OS-sandbox isolation on this spawn.
    """
    return ("-c", f"core.hooksPath={HOOKS_SINK}", "-c", "core.fsmonitor=false")


def run_git(
    args: list[str], cwd: str, *, timeout: int | None = None
) -> subprocess.CompletedProcess[str]:
    """Run git with an argv list (never a shell) inside ``cwd``, OS-sandboxed.

    :exc:`SandboxUnavailable` is raised when the host has no sandbox backend and
    ``agent.sandbox_allow_unsandboxed_exec`` is unset; callers turn that into a
    503 telling the user to act manually. Fail CLOSED — a local convenience is
    not worth an unisolated spawn.

    The remaining protections are all here: an argv list with no shell, the POSIX
    resource-limit ceiling (``resource_limit_preexec`` returns ``None`` on
    Windows, where ``preexec_fn`` must be ``None``), a wall-clock timeout, and
    ``GIT_TERMINAL_PROMPT=0`` so a credential helper cannot block on an
    interactive prompt.
    """
    try:
        argv, env, cleanup = sandboxed_spawn_argv(
            ["git", *git_no_repo_code(), *args], mode=SANDBOX_MODE
        )
    except RuntimeError as exc:  # no sandbox backend and no explicit opt-in
        raise SandboxUnavailable(str(exc)) from exc
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT if timeout is None else timeout,
            check=False,
            preexec_fn=resource_limit_preexec(),
        )
    finally:
        if cleanup:
            with contextlib.suppress(OSError):
                os.unlink(cleanup)
    # The launcher can only discover SOME isolation failures in the child, after
    # `wrap_argv` has already returned: `unshare(NEWNS)` is permitted by the
    # backend probe but denied at exec time on hosts that restrict mount
    # namespaces (GitHub Actions runners are one — errno 1/EPERM). git never
    # runs, so without this the non-zero exit is misread downstream as "not a git
    # repository" or "cannot list worktrees". Surface it as the same refusal a
    # missing backend gets, so the user is told the truth.
    if proc.returncode != 0 and (proc.stderr or "").startswith(SANDBOX_LAUNCHER_PREFIX):
        raise SandboxUnavailable((proc.stderr or "").strip())
    return proc


def git_error(proc: subprocess.CompletedProcess[str]) -> str:
    """Condense git's stderr into a single-line message for the UI."""
    text = (proc.stderr or proc.stdout or "").strip()
    if not text:
        return "git failed"
    # Keep the first meaningful line; git prefixes most failures with "fatal:".
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:300]
    return "git failed"


def norm_path(path: str) -> str:
    """Normalized, case-folded form used to compare paths against git's output."""
    return os.path.normcase(os.path.normpath(path))


def git_toplevel(repo: str) -> str | None:
    """Return the work-tree root containing ``repo``, or None if not a repo."""
    probe = run_git(["rev-parse", "--show-toplevel"], repo)
    if probe.returncode != 0:
        return None
    top = probe.stdout.strip()
    return os.path.realpath(top) if top else None


def resolve_base_ref(root: str) -> str:
    """Pick the ref to branch from: the remote's default branch, else HEAD.

    ``origin/HEAD`` is the repo's own declaration of its default branch, which
    beats hardcoding "main" (repos whose default branch is named something else,
    or with a different primary remote layout, would otherwise fail). Falls back
    to ``HEAD`` so a repo with no remote — or no fetched ``origin/HEAD`` — still
    works, at the cost of branching from whatever is currently checked out.
    """
    probe = run_git(["rev-parse", "--verify", "--quiet", "origin/HEAD"], root)
    if probe.returncode == 0 and probe.stdout.strip():
        return "origin/HEAD"
    return "HEAD"


# One lock per repo root, so two same-repo mutations in this gateway never
# interleave their check/create/cleanup sequences. Cross-request atomicity does
# not depend on this (git's ref lock is what enforces it), but it removes the
# same-destination window between a "does dest exist" probe and `worktree add`,
# which is what lets cleanup treat an unregistered leftover directory as its own.
_REPO_LOCKS: dict[str, asyncio.Lock] = {}
_MAX_REPO_LOCKS = 64


def repo_lock(root: str) -> asyncio.Lock:
    """Return (creating if needed) the serialization lock for ``root``."""
    lock = _REPO_LOCKS.get(root)
    if lock is None:
        if len(_REPO_LOCKS) >= _MAX_REPO_LOCKS:
            # Drop idle entries so a long-lived gateway that has seen many repos
            # does not accumulate locks forever. Held locks are kept.
            for key in [k for k, v in _REPO_LOCKS.items() if not v.locked()]:
                del _REPO_LOCKS[key]
        lock = _REPO_LOCKS[root] = asyncio.Lock()
    return lock
