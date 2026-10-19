"""Spec Builder's git adapters: the audited, sandboxed git spawn and the worktree helpers.

Every git process this app runs against a user's repository goes through ``_git``, which
records the invocation in SEL before spawning and the outcome after it. The worktree
helpers create a spec's sibling worktree and branch, and roll them back while the
request still owns the spec. From the rest of the backend this module imports only
:mod:`repository` and :mod:`parsers`.
"""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import Any

from .parsers import _redact
from .repository import (
    _audit_tool,
    create_subprocess_limited,
    logger,
    sandboxed_spawn_argv,
    shielded_prepare_off_loop,
)

# ── git / worktree helpers ────────────────────────────────────────────────────


#: rc returned when git could not be executed at all (not installed, or the
#: sandbox refused the spawn). Distinct from git's own exit codes so a caller can
#: tell "not a repo" (rc 128) from "no git here".
#: How long to wait for a killed git process to actually exit before giving up
#: on the reap and logging it. SIGKILL is not negotiable, so this only ever
#: elapses when the process is stuck in an uninterruptible syscall.
_GIT_HALT_SECS = 5.0

_GIT_UNAVAILABLE = 127


def _prepare_git_spawn(argv: list[str]) -> tuple[list[str], Any, str | None]:
    """Build everything the sandboxed git spawn needs.

    BLOCKING -- call via ``asyncio.to_thread``. Returns
    ``(argv, env, cleanup_path)``. Still its own thread hop because
    ``sandboxed_spawn_argv`` probes the sandbox host and writes the scrubbed-env
    temp file; the resource limits are not built here, because
    ``create_subprocess_limited`` applies them after exec.
    """
    sandbox_argv, env, cleanup = sandboxed_spawn_argv(argv)
    return sandbox_argv, env, cleanup


async def _halt_git(proc: Any, subcommand: str) -> None:
    """Stop a git process this app spawned, and reap it.

    Awaiting ``communicate()`` is the only thing that ties the child's lifetime to
    the request. Drop that await -- gateway shutdown, a client disconnect, any
    cancellation -- and git keeps running to completion detached from the handler
    that asked for it. For a read-only subcommand that only wastes a process, but
    ``worktree add`` MUTATES the user's repository: the worktree and branch appear
    after the request they belonged to is gone, and nothing reports them.

    kill() first and unconditionally, because it is synchronous: whatever happens to
    this coroutine next, the mutation is already stopped. The reap is shielded for
    the reason the kill is not -- the usual trigger here IS cancellation, and an
    unshielded await would be cancelled at once, leaving behind the zombie it came
    to collect.
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        return  # already gone; nothing to reap
    try:
        await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=_GIT_HALT_SECS))
    except asyncio.TimeoutError:
        logger.warning("git %s did not exit after kill", subcommand)
    except ProcessLookupError:
        pass


async def _git(cwd: str, *args: str) -> tuple[int, str, str]:
    """Run a git command (argv exec, no shell) in *cwd*. Returns (rc, out, err).

    Routed through ``sandboxed_spawn_argv`` with a scrubbed env and the resource
    -limit preexec, mirroring ``git_coord._git``. The working directory here is
    caller-supplied (and the branch name derives from a spec name), so this is
    an agent-influenced spawn in the sense of the spawn-audit tripwire — it must
    stay routed rather than being added to the benign allowlist.

    Every invocation and every outcome is recorded in SEL through
    ``_audit_tool``. A process this app spawns on the user's repository must be
    reconstructable from the audit log: without it, a worktree create/remove left
    no tool-invocation trail at all, only the app-level ``spec_worktree_*``
    entries, which say nothing about what git actually ran or whether it failed.
    """
    subcommand = args[0] if args else ""
    # Off-loop because a critical audit is a synchronous write, and audit-or-deny:
    # git is only spawned once the record has actually landed.
    if not await asyncio.to_thread(_audit_tool, "invoked", subcommand, cwd, critical=True):
        # Fail closed: no audit record, no spawn. Callers already treat a non-zero
        # rc as "not a git repo", so this degrades the feature (no worktree, no
        # branch detection) instead of running an unaudited process.
        logger.warning("refusing to run git %s: invocation could not be audited", subcommand)
        return _GIT_UNAVAILABLE, "", "git unavailable: audit unavailable"
    try:
        # Off-loop: the sandbox backend probe can shell out (subprocess.run) the
        # first time it runs on a host, and it writes the scrubbed-env temp file.
        # Neither is the cheap in-memory call it looks like.
        argv, env, cleanup = await shielded_prepare_off_loop(
            functools.partial(_prepare_git_spawn, ["git", "-C", cwd, *args])
        )
    except Exception as exc:
        # Sandbox unavailable / argv build failure: report it, do not 500 the
        # caller. Every caller already treats a non-zero rc as "not a git repo".
        _audit_tool("error", subcommand, cwd, error=type(exc).__name__)
        return _GIT_UNAVAILABLE, "", f"git unavailable: {type(exc).__name__}"
    proc: Any = None
    try:
        # Limits are applied after exec by a shim, avoiding preexec_fn in the
        # multithreaded gateway process.
        proc = await create_subprocess_limited(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await proc.communicate()
    except FileNotFoundError:
        # No git on this host. Browsing a folder calls _repo_info, so letting
        # this propagate turned the project picker's first request into an HTTP
        # 500 on any machine without git installed — the app is usable without
        # it (the worktree option simply isn't offered), so degrade instead.
        # (the finally below removes the temp env file)
        _audit_tool("error", subcommand, cwd, error="FileNotFoundError")
        return _GIT_UNAVAILABLE, "", "git is not installed"
    except BaseException as exc:  # spawn failure, cancellation, timeout
        _audit_tool("error", subcommand, cwd, error=type(exc).__name__)
        await _halt_git(proc, subcommand)
        raise
    finally:
        if cleanup:
            # Off-loop too: same class as the probe above, and this one runs on
            # EVERY git call. Shielded so a cancelled turn still removes the
            # temp env file (it holds the scrubbed environment) instead of
            # leaking it into the user's temp dir.
            await asyncio.shield(asyncio.to_thread(_unlink_quietly, cleanup))
    rc = proc.returncode or 0
    _audit_tool("success" if rc == 0 else "failure", subcommand, cwd, rc=rc)
    return (
        rc,
        out.decode(errors="replace").strip(),
        err.decode(errors="replace").strip(),
    )


async def _repo_info(path: str) -> dict:
    """Probe *path*: is it inside a git repo? Return root + branch details."""
    rc, out, _ = await _git(path, "rev-parse", "--show-toplevel")
    if rc != 0 or not out:
        return {"is_git": False}
    root = out
    _, branch, _ = await _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    # Default base: origin/main, then the legacy default-branch name, then HEAD.
    # The legacy ref has to be spelled literally to resolve in a user's own repo
    # that still uses it, so the inclusive-language rule is suppressed here the
    # same way security.py suppresses it for the protected-branch patterns.
    base = ""
    for cand in ("origin/main", "origin/master"):  # wokeignore:rule=master
        rc2, _, _ = await _git(root, "rev-parse", "--verify", "--quiet", cand)
        if rc2 == 0:
            base = cand
            break
    return {"is_git": True, "root": root, "branch": branch, "default_base": base or branch}


def _unlink_quietly(path: str) -> None:
    """Remove a file, ignoring absence and errors.

    BLOCKING -- call via ``asyncio.to_thread``.
    """
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


async def _rollback_worktree_if_ours(
    name: str,
    *,
    was_ours: bool,
    repo_root: str,
    created_worktree: str,
    worktree_branch: str,
) -> bool:
    """Undo a created worktree ONLY while this request still owns the name.

    ``_remove_worktree`` is ``worktree remove --force`` plus ``branch -D``, so a
    rollback that fires after a concurrent delete + same-name recreate would
    discard the REPLACEMENT spec's uncommitted work and hard-delete its branch.

    ``was_ours`` is the identity-pinned index pop's own answer. A False pop means
    the name does not refer to our create, and the worktree path is derived
    from the name (``<repo>-wt-<name>``), so it is not ours to remove either.
    Leaving it is the safe failure: an orphaned worktree is recoverable by hand,
    deleted work is not.

    Returns True when the worktree was actually removed.
    """
    if not created_worktree:
        return False
    if not was_ours:
        logger.warning(
            "spec %s: leaving worktree %s in place -- the index entry is no longer ours",
            name,
            created_worktree,
        )
        return False
    await _remove_worktree(repo_root, created_worktree, worktree_branch)
    return True


async def _remove_worktree(repo_root: str, worktree_path: str, branch: str = "") -> None:
    """Best-effort rollback of a worktree this request just created.

    Called only on a create path that already succeeded in making the worktree
    and then failed a later validation — without this the request 400s and
    leaves an orphaned worktree + branch behind for the user to clean up by
    hand. Prunes before deleting the branch, since a leftover registration
    keeps the branch checked-out from git's point of view. ``branch`` is passed
    in rather than derived: the worktree dir is ``<repo>-wt-<name>`` while the
    branch is ``spec/<name>``, so deriving one from the other is wrong.
    """
    if not repo_root or not worktree_path:
        return
    try:
        await _git(repo_root, "worktree", "remove", "--force", worktree_path)
        await _git(repo_root, "worktree", "prune")
        if branch:
            await _git(repo_root, "branch", "-D", branch)
    except Exception:  # pragma: no cover - rollback must never mask the real error
        logger.debug("worktree rollback failed for %s", worktree_path, exc_info=True)


async def _create_worktree(repo_root: str, spec_name: str) -> tuple[str, str] | str:
    """Create a dedicated worktree + branch for a spec off the repo's default base.

    Returns (worktree_path, branch) on success, or an error string. The worktree
    lands as a SIBLING of the repo (``<repo>-wt-<spec>``), branch ``spec/<name>``,
    mirroring the worktree-per-feature convention.
    """
    root = Path(repo_root)
    wt_path = root.parent / f"{root.name}-wt-{spec_name}"
    branch = f"spec/{spec_name}"
    # Off-loop: a stat against a caller-chosen repo root, which can sit on a
    # stalled network mount. It is the last filesystem call in this module that
    # still ran on the event loop -- every other one is inside a helper marked
    # BLOCKING and invoked through to_thread.
    if await asyncio.to_thread(wt_path.exists):
        return f"worktree path already exists: {wt_path}"
    info = await _repo_info(repo_root)
    base = info.get("default_base") or "HEAD"
    rc, _, err = await _git(repo_root, "worktree", "add", str(wt_path), "-b", branch, base)
    if rc != 0:
        return _redact(err.splitlines()[-1] if err else f"git worktree add failed (rc={rc})")
    return (str(wt_path), branch)
