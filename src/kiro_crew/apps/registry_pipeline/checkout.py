"""The git processes that fill a checkout, and the reads that describe one.

Fetch by branch or pinned commit into a destination this call created
(``_git_fetch_ref``), clone-or-pull with the origin and branch re-convergence rules
(``_git_clone_or_pull``), bounded reads of checkout-resident git metadata, and the
timeouts and process-group kill every registry git spawn uses.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.registry_pipeline.git_targets import (
    _GIT_EXEC_NEUTRALIZER_PAIRS,
    _git_output_is_auth_shaped,
    _git_target_is_unsupported,
    _git_transport_env,
    _loggable_git_transport_output,
    _redacted_git_failure_class,
    _same_git_target,
    _strip_git_target_userinfo,
)
from kiro_crew.apps.registry_pipeline.recovery import _move_checkout_aside
from kiro_crew.apps.registry_pipeline.sources import (
    _context_clone_sandbox_mode,
    is_clone_host_trusted,
)
from kiro_crew.apps.registry_pipeline.subprocess_env import anonymous_git_env, minimal_env
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)

# A git object name: sha1 (40 hex) or sha256 (64 hex) repository format.
#
# Anchored at ``\Z`` rather than ``$``: Python's ``$`` matches before a trailing
# newline, so ``$`` accepted a 40-hex value with ``"\n"`` appended. That matters at
# both readers -- this pattern validates a pin before it reaches a git argument
# vector, and it validates a SHA read back off disk in
# :func:`_resolved_clone_commit`, where a value that only looks like a commit would
# be reported as the landed one.
_COMMIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


# Timeout limits (seconds)
_CLONE_TIMEOUT = 60


# Config overrides that stop the REPOSITORY (or the user's global git config)
# supplying a program git runs, prepended to EVERY git invocation this module
# spawns -- not just the network one. ``git checkout`` fires ``post-checkout`` and
# ``git init`` honours ``init.templateDir``, so a repo shipping ``.githooks/`` plus a
# global ``core.hooksPath`` runs arbitrary code as the gateway user during the local
# steps that :func:`_git_transport_env` (network only) never reaches. ``-c`` on our
# own argv beats every config file. ``core.hooksPath`` points at ``os.devnull``, a
# non-directory OS device under which git finds no hook and into which no directory
# can be created (the ``_HOOKS_SINK`` reasoning in ``dashboard/handlers/worktree.py``);
# ``core.fsmonitor=false`` refuses a repo-named filesystem-monitor command git would
# otherwise spawn on index reads.
_HOOKS_NEUTRALIZER_ARGV: tuple[str, ...] = tuple(
    token for key, value in _GIT_EXEC_NEUTRALIZER_PAIRS for token in ("-c", f"{key}={value}")
)


async def _rmtree_force_settled(path: str | Path) -> None:
    """Remove *path* fully before cancellation can escape to a path reuser.

    Cancelling ``asyncio.to_thread`` does not stop its worker. Returning while
    that worker is still deleting is unsafe for update rollback: the caller may
    restore an old checkout at the same path, after which the orphaned worker
    can delete the restored tree. Retain and shield the executor future, absorb
    repeated cancellation until it settles, then propagate cancellation.
    """
    loop = asyncio.get_running_loop()
    worker = loop.run_in_executor(None, platform_compat.rmtree_force, path)
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.wait({worker})
            except asyncio.CancelledError:
                continue
        raise


async def _communicate_with_timeout(
    proc: asyncio.subprocess.Process,
    timeout: float,
) -> tuple[bytes, bytes]:
    """Communicate with a subprocess, killing its whole process tree on timeout.

    A timed-out ``git clone`` or ``/bin/sh -c <probe>`` can have descendants
    (SSH, a version-probe binary, ...). Killing only the immediate child with
    ``proc.kill()`` re-parents those grandchildren, so repeated timeouts leak
    processes. We instead signal the child's entire process group via
    ``platform_compat.kill_process_tree_async`` (killpg on POSIX, ``taskkill
    /T`` on Windows) and then reap the direct child. Callers MUST spawn the
    child with ``start_new_session`` (POSIX) / ``CREATE_NEW_PROCESS_GROUP``
    (Windows) so the group signal targets the child's own group and not the
    gateway's — every registry caller does. If the group kill fails
    (e.g. the child already exited, or it was never made a group leader) we
    fall back to a pid-scoped ``proc.kill()`` so the child is never left
    un-reaped. The reap itself goes through the shared
    ``platform_compat.kill_and_reap``, which drains the pipes via
    ``communicate()`` under a bound so a killed child blocked writing into a
    full pipe cannot hang the caller.
    """
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await platform_compat.kill_and_reap(proc)
        raise


def _resolved_clone_commit(clone_root: Path) -> str:
    """Return the commit SHA checked out in *clone_root*, or ``""`` if unknown.

    Reads git's own on-disk refs rather than spawning ``git rev-parse``: the SHA
    is recorded as provenance only, so resolving it must not add a subprocess —
    nor a new failure mode — to the install path.  Every read failure degrades to
    ``""`` (provenance without a commit) instead of failing the install.
    """
    git_dir = clone_root / ".git"
    # All three reads below are BOUNDED: HEAD, the loose ref, and packed-refs
    # all live inside the checkout, so their sizes are attacker-controlled (an
    # app's build script can rewrite them). This is the SAME `.git/HEAD` file
    # that :func:`_read_clone_branch` bounds — closing the memory-exhaustion
    # class at that call site alone would leave it open here, on the install
    # path, which is the round-11 "next call site" lesson.
    raw_head = _read_git_metadata_bounded(git_dir / "HEAD", _HEAD_READ_LIMIT)
    if raw_head is None:
        return ""
    head = raw_head.strip()
    if not head.startswith("ref:"):
        # Detached HEAD holds the SHA directly.
        return head if _COMMIT_SHA_RE.match(head) else ""
    ref = head[len("ref:") :].strip()
    # git writes this file, not the cloned repo — belt-and-braces so a ref can
    # never be read as a path outside the clone's own .git directory.
    if not ref or ref.startswith("/") or ".." in ref.split("/"):
        return ""
    raw_loose = _read_git_metadata_bounded(git_dir / ref, _HEAD_READ_LIMIT)
    if raw_loose is not None:
        loose = raw_loose.strip()
        if _COMMIT_SHA_RE.match(loose):
            return loose
    # A repacked clone keeps no loose ref file.
    packed = _read_git_metadata_bounded(git_dir / "packed-refs", _PACKED_REFS_READ_LIMIT)
    if packed is not None:
        for line in packed.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and _COMMIT_SHA_RE.match(parts[0]):
                return parts[0]
    return ""


async def _clone_origin_url(dest: Path) -> str | None:
    """Read *dest*'s ``origin`` remote URL. Returns None when unreadable.

    Local metadata read: no network, and ``anonymous_git_env`` so a credential
    helper is never invoked just to inspect a checkout. Routed through the
    sandbox chokepoint + cgroup scope like every other git spawn in this
    module — the argv is fixed, but *dest* is derived from an index-supplied
    app name, so the cwd is not ours to trust.
    """
    if not (dest / ".git").is_dir():
        return None
    origin_cmd, _cleanup = await wrap_argv_async(
        ["git", "remote", "get-url", "origin"],
        mode="strict",  # credential-free read; ~/.ssh stays hidden
        _prepare=wrap_argv,
    )
    origin_cmd = cgroup_scope_argv(origin_cmd)
    try:
        proc = await create_subprocess_limited(
            *origin_cmd,
            cwd=str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=anonymous_git_env(),
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
    except OSError:
        return None
    try:
        origin_out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        await _kill_process_group(proc)
        return None
    if proc.returncode != 0:
        return None
    return origin_out.decode(errors="replace").strip()


async def _clone_origin_matches(dest: Path, git_url: str) -> bool:
    """Whether *dest* is a checkout of the same repository as *git_url*.

    Fails closed: an unreadable origin, a missing remote, or an empty
    *git_url* to compare against all return False. Embedded userinfo is transport
    authentication rather than identity, so credential rotation remains a match.
    """
    if not git_url:
        return False
    origin = await _clone_origin_url(dest)
    return origin is not None and _same_git_target(origin, git_url)


# Upper bound on any single git-metadata read of a file INSIDE a checkout
# (``.git/HEAD``, a loose ref). Those files are agent-writable — an app's own
# build script can rewrite them — so their size is ATTACKER-controlled: an app
# can replace one with (or symlink it to) a multi-gigabyte / sparse file, and
# an unbounded read would load it into gateway memory. A well-formed value here
# is a single line (a ``ref:`` line or a 40/64-char SHA), tens of bytes, so a
# few-hundred-byte cap makes the read a no-op for hostile content while a real
# ref fits comfortably. This bound is applied at EVERY checkout-resident git
# read, not just one call site — the round-11 lesson is that gating a single
# caller leaves the primitive exploitable from the next one.
_HEAD_READ_LIMIT = 512  # bytes


# Upper bound on the ``.git/packed-refs`` read. It is line-oriented (one ref per
# line) so it can legitimately be larger than a single ref file, but it is still
# agent-writable checkout content, so the read is capped rather than unbounded.
# A shallow single-branch app clone packs a handful of refs; this ceiling covers
# a realistic repo while still refusing a hostile multi-megabyte replacement.
_PACKED_REFS_READ_LIMIT = 1 << 20  # 1 MiB


def _read_git_metadata_bounded(path: Path, limit: int) -> str | None:
    """Read at most *limit* bytes of a git-metadata file, or None on failure.

    The read is BOUNDED because *path* lives inside a checkout whose contents an
    app's build script can rewrite (see :data:`_HEAD_READ_LIMIT`): reading a
    ref file whole would let an oversized/sparse/symlinked replacement exhaust
    gateway memory. Content that exactly fills the bound is treated as
    truncated/hostile and returns None, so a caller never acts on a partial
    token. Missing file, unreadable, or non-UTF-8 all fail closed to None.

    The read is also SYMLINK-CONTAINED: *path* is a checkout-resident file whose
    name (``.git/HEAD``, a loose ref, ``packed-refs``) an app's build script can
    replace with a symlink pointing at a protected file (``~/.aws/credentials``,
    an SSH key). A bare ``open()`` would follow that link and read the target
    through the sensitive-path ceiling, so the read is routed through
    :func:`kiro_crew.hooks.safe_read_prefix`, which canonicalizes via ``realpath``
    and refuses a resolved target ``is_sensitive_path`` flags before any read,
    then opens the canonical path ``O_NOFOLLOW`` as TOCTOU defense against a
    final-component symlink swap. A rejected (or unreadable) path fails closed to
    None, so the size bound and the containment gate share one fail-closed exit.
    """
    from kiro_crew import hooks

    raw = hooks.safe_read_prefix(str(path), limit)
    if raw is None:
        # Rejected by the sensitive-path gate, a followed symlink refused
        # O_NOFOLLOW, missing, or otherwise unreadable — all fail closed.
        return None
    try:
        data = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Filling the bound means the real content was larger — refuse rather than
    # act on a value that may have been cut mid-token. Measured on the decoded
    # text so a multi-byte tail cannot slip a value past the bound.
    if len(data) >= limit:
        return None
    return data


def _read_clone_branch(clone_dir: Path) -> str | None:
    """Read the current branch of an existing git clone.

    Returns the branch name (e.g. ``"main"``), or None if the clone does not
    exist, is in detached HEAD state, or the branch cannot be determined.
    Reads ``.git/HEAD`` directly (stdlib-only, no subprocess spawn) — mirrors
    the fail-closed posture of :func:`_clone_origin_matches`.

    The read is BOUNDED to :data:`_HEAD_READ_LIMIT` bytes. ``.git/HEAD`` lives
    inside a checkout an app's build script can rewrite, so its size is
    attacker-controlled; reading it whole would let a multi-gigabyte or sparse
    replacement exhaust gateway memory. A well-formed HEAD fits in a few
    hundred bytes, so a ``ref:`` line that does not resolve within the bound is
    treated as malformed and fails closed (returns None). This is what closes
    the memory-exhaustion class at EVERY call site, not just the ones a caller
    happens to gate.

    A ``.git`` that is a *file* (worktree / submodule gitfile) rather than a
    directory also fails closed (``is_file()`` on the nested path returns
    False), so no fast path is attempted for those layouts.
    """
    head_file = clone_dir / ".git" / "HEAD"
    if not head_file.is_file():
        return None
    raw = _read_git_metadata_bounded(head_file, _HEAD_READ_LIMIT)
    if raw is None:
        # Missing, unreadable, non-UTF-8, or oversized (filled the bound) — the
        # bounded reader already failed closed on hostile/truncated content.
        return None
    head_content = raw.strip()
    # A normal branch checkout has HEAD = "ref: refs/heads/<branch>"
    _REF_PREFIX = "ref: refs/heads/"
    if head_content.startswith(_REF_PREFIX):
        branch = head_content[len(_REF_PREFIX) :]
        if not branch:
            return None
        return branch
    # Detached HEAD (raw SHA) or unexpected format — fail closed.
    return None


async def _clone_branch_matches(dest: Path, branch: str) -> bool:
    """Whether *dest* has *branch* checked out (exact string equality).

    Fails closed: an unreadable or detached HEAD, a missing ``.git/HEAD``,
    or an empty *branch* to compare against all return False — the caller
    must fall through to the throwaway clone so admission sees the correct
    branch's manifest.
    """
    if not branch:
        return False
    clone_branch = await asyncio.to_thread(_read_clone_branch, dest)
    return clone_branch == branch


_KILL_GRACE_PERIOD = 5  # seconds to wait after SIGTERM before SIGKILL


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Send SIGTERM to the process group, escalate to SIGKILL if needed.

    Routed through platform_compat (killpg on POSIX, taskkill /T on Windows) so
    the app-build timeout path doesn't AttributeError on win32.
    """
    # Async variants offload Windows taskkill to subprocess_executor so this
    # The build timeout path never blocks the event loop on taskkill.exe.
    # POSIX branch stays inline (os.killpg is non-blocking).
    try:
        await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGTERM)
    except OSError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_PERIOD)
    except asyncio.TimeoutError:
        await platform_compat.kill_and_reap(proc)


async def _git_fetch_ref(
    git_url: str,
    ref: str,
    dest: Path,
    log_lines: list[str],
    *,
    checkout_branch: str = "",
    credential_target: str | None = None,
    clone_env: dict[str, str],
    sandbox_mode: str,
    mask_local_git_config: bool = False,
    forward_ssh_auth_sock: bool = False,
) -> dict[str, Any] | None:
    """Materialise *dest* from one remote ref. Returns None on success.

    ``git clone --branch`` cannot take a commit id -- it exits 128 with
    ``Remote branch <sha> not found in upstream origin`` -- so a pinned entry
    needs fetch-by-SHA instead. The published catalog pins every third-party app
    to a commit, and this is the only path that honours that pin.

    Only the fetch invocation receives the one-shot credential rewrite. Init,
    remote setup and checkout run with *clone_env*: a remote tree can select an
    inherited filter driver through ``.gitattributes``, and an existing checkout
    can select hooks or other executable config. Keeping the credential out of
    every worktree operation is therefore the security boundary, not merely a
    subprocess-configuration detail.

    ``git remote add origin`` is not optional bookkeeping. ``git init`` + ``git
    fetch <url> <sha>`` leaves NO origin remote, and the update path reads it
    (:func:`_clone_origin_url`); without it every later update fails closed with
    ``unreadable_clone_origin`` and deliberately does NOT delete the checkout, so
    the app installs once and then needs manual cleanup to ever update again.

    ``--filter=blob:none`` is deliberately absent. A server that does not support
    it merely warns and sends everything, and a server that DOES support it turns
    the app's source tree into a partial clone whose later file reads become lazy
    network fetches -- during the build, off the install path's error handling.

    ``--template=`` is likewise load-bearing: the owner-designated posture uses
    :func:`minimal_env`, which does not disable the user's global git config, so a
    configured ``init.templateDir`` would install hooks into this repository and
    the checkout below would then execute ``post-checkout``.

    *mask_local_git_config* additionally runs the LOCAL steps (init, remote add,
    checkout, branch) with ``GIT_CONFIG_NOSYSTEM=1`` / ``GIT_CONFIG_GLOBAL=devnull``
    so no operator-configured ``filter.<name>.smudge`` program resolves for a
    driver the repository selects through ``.gitattributes``. It is OFF by default
    because masking also disables Git LFS: an install would check out LFS pointer
    files instead of content. Only the pre-install store-art prewarm -- which reads
    a handful of image files out of a throwaway checkout and never builds or runs
    the tree -- opts in; every install path keeps *clone_env* as is.
    """
    transport_target = credential_target or git_url
    if _git_target_is_unsupported(transport_target):
        return {
            "ok": False,
            "error": (
                "git clone target contains an unsupported query or fragment or an "
                "ambiguous Git transport identity"
            ),
        }

    if _strip_git_target_userinfo(git_url) != git_url:
        raise ValueError("_git_fetch_ref requires a credential-free git_url")
    credential_target = credential_target or git_url
    if _strip_git_target_userinfo(credential_target) != git_url:
        raise ValueError("credential_target does not match git_url")
    credentialed_transport = credential_target != git_url
    if credentialed_transport and credential_target.partition("://")[0].lower() not in {
        "http",
        "https",
    }:
        return {
            "ok": False,
            "name": dest.name,
            "error": "embedded git credentials require an HTTP(S) target",
        }

    async def run(
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout: int,
        network: bool = False,
    ) -> tuple[int, str]:
        # Prepend the hooks/fsmonitor neutralizer to EVERY git spawn (network or
        # not): the init/checkout steps run with *clone_env*, which carries no such
        # override, so without this a repo-shipped ``post-checkout`` executes during
        # checkout. ``argv[0]`` is always ``git``; the ``-c`` overrides go right after
        # it so they precede the subcommand.
        argv = [argv[0], *_HOOKS_NEUTRALIZER_ARGV, *argv[1:]]
        fwd = forward_ssh_auth_sock and network  # the agent socket is for the network step only
        sandboxed, _cleanup = await wrap_argv_async(
            argv, mode=sandbox_mode, forward_ssh_auth_sock=fwd, _prepare=wrap_argv
        )
        sandboxed = cgroup_scope_argv(sandboxed)
        if network:
            process_env = _git_transport_env(credential_target, git_url, clone_env)
        elif mask_local_git_config:
            # Prewarm only. The local steps (init, checkout, branch) need no
            # credential helper, so they run with system and global git config
            # disabled. A repository selects filter drivers by name through
            # ``.gitattributes``; the PROGRAM behind that name comes from the
            # operator's global/system config, and with those files out of scope no
            # ``filter.<name>.smudge`` resolves, so a checkout can execute nothing a
            # repository chose. The network step keeps the operator's config for
            # its credential helpers. Installs do NOT take this branch: the same
            # masking disables Git LFS and would check out pointer files.
            process_env = {
                **clone_env,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            }
        else:
            process_env = clone_env
        proc = await create_subprocess_limited(
            *sandboxed,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            env=process_env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            return 124, "timed out"
        except asyncio.CancelledError:
            await _kill_process_group(proc)
            raise
        return (
            proc.returncode or 0,
            _loggable_git_transport_output(
                out.decode(errors="replace").strip(),
                credentialed=credentialed_transport if network else False,
            ),
        )

    # Hardening applied to the network step. None of these are set anywhere on
    # the existing clone paths; the fetch is where an attacker-influenced URL
    # meets git, so they start here rather than nowhere.
    hardening = [
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "submodule.recurse=false",
        "-c",
        "fetch.recurseSubmodules=no",
    ]
    # Destination lifecycle, stated as one invariant because three reviewer findings
    # were three exits from the same mistake:
    #
    #   THIS FUNCTION MAY DELETE `dest` IF AND ONLY IF THIS INVOCATION CREATED IT
    #   AND DID NOT SUCCEED -- and that is decided in ONE place, not per branch.
    #
    # The earlier drafts put cleanup on each failure branch, so each fix closed one
    # exit and left the others: adopting a non-checkout destination (deleted the
    # user's files), a moved-aside restore that skipped on cancellation, and finally
    # a spawn exception between `git init` and `git remote add` that left a `.git`
    # directory with no origin -- which then wedges every later attempt on
    # `unreadable_clone_origin`, a fail-closed path that deliberately does not clean
    # up after itself.
    #
    # Cleanup therefore belongs to the LIFETIME of the thing created, not to the
    # enumeration of ways to fail. The `finally` below covers return, raise and
    # cancellation without any branch having to remember.
    created_here = False
    succeeded = False
    try:
        if (dest / ".git").is_dir():
            pass  # a checkout we can fetch into; never ours to delete
        elif dest.exists():
            # Refuse rather than adopt. `not (dest / ".git").is_dir()` answers "is
            # there a checkout here", which is NOT "am I creating this": a plain
            # directory of the user's files, or a `.git` FILE from a worktree link,
            # would read as fresh.
            log_lines.append(
                f"Refusing to fetch into {dest}: it exists but is not a git checkout "
                "(remove or fix it manually and retry)"
            )
            return {
                "ok": False,
                "name": dest.name,
                # Human sentence in `error`, machine slug in `code`: the install
                # banner renders `result.error`, never `result.message`.
                "code": "destination_not_a_checkout",
                "error": (
                    "The destination exists but is not a git checkout. Remove or fix it "
                    "manually and retry the install."
                ),
            }
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Set BEFORE the spawn: `git init` may create the directory and then
            # fail, or the spawn itself may raise, and either way the directory is
            # ours to discard.
            created_here = True
            code, out = await run(["git", "init", "--quiet", "--template=", str(dest)], timeout=15)
            if code != 0:
                log_lines.append(f"git init failed (exit {code}): {out}")
                return {"ok": False, "name": dest.name, "error": "git init failed"}
            code, out = await run(["git", "remote", "add", "origin", git_url], cwd=dest, timeout=15)
            if code != 0:
                log_lines.append(f"git remote add failed (exit {code}): {out}")
                return {"ok": False, "name": dest.name, "error": "git remote add failed"}

        refspec = (
            f"+refs/heads/{checkout_branch}:refs/remotes/origin/{checkout_branch}"
            if checkout_branch
            else ref
        )
        description = f"branch {checkout_branch}" if checkout_branch else f"commit {ref[:12]}"
        log_lines.append(f"Fetching {_strip_git_target_userinfo(git_url)} at {description}...")
        code, out = await run(
            [
                "git",
                *hardening,
                "fetch",
                "--no-auto-maintenance",
                "--no-tags",
                "--depth",
                "1",
                git_url,
                refspec,
            ],
            cwd=dest,
            timeout=_CLONE_TIMEOUT,
            network=True,
        )
        log_lines.append(out)
        if code != 0:
            # Fail closed. A server that will not serve this object (unreachable
            # commit, or one no ref contains) must not degrade into "install the
            # default branch instead" -- that is the pin silently not applying.
            return {
                "ok": False,
                "name": dest.name,
                "error": f"git fetch failed (exit {code})",
            }

        checkout_cmd = (
            ["git", "checkout", "--quiet", "-B", checkout_branch, f"origin/{checkout_branch}"]
            if checkout_branch
            else ["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"]
        )
        code, out = await run(checkout_cmd, cwd=dest, timeout=15)
        if code != 0:
            log_lines.append(f"git checkout failed (exit {code}): {out}")
            return {
                "ok": False,
                "name": dest.name,
                "error": "git checkout of fetched tree failed",
            }

        if checkout_branch:
            code, out = await run(
                [
                    "git",
                    "branch",
                    "--set-upstream-to",
                    f"origin/{checkout_branch}",
                    checkout_branch,
                ],
                cwd=dest,
                timeout=15,
            )
            if code != 0:
                log_lines.append(f"git branch tracking setup failed (exit {code}): {out}")
                return {
                    "ok": False,
                    "name": dest.name,
                    "error": "git branch tracking setup failed",
                }
            succeeded = True
            return None

        # The pin only becomes real here. `_resolved_clone_commit` degrades to "" on
        # any read failure, so an empty answer is a FAILURE rather than a pass: the
        # whole point is refusing to build a tree we cannot identify.
        landed = await asyncio.to_thread(_resolved_clone_commit, dest)
        if landed != ref:
            log_lines.append(
                f"pinned commit not honoured: asked for {ref}, checkout reports "
                f"{landed or '<unknown>'} — refusing to install"
            )
            return {
                "ok": False,
                "name": dest.name,
                "error": "pinned commit verification failed",
            }
        succeeded = True
        return None
    finally:
        # `created_here` means the destination is this call's own `git init`, so a
        # failed fetch leaves a repository holding read-only pack files -- the same
        # removal requirement as the clone path in `_git_clone_or_pull`.
        if created_here and not succeeded:
            await _rmtree_force_settled(dest)


async def _git_fetch_commit(
    git_url: str,
    commit: str,
    dest: Path,
    log_lines: list[str],
    *,
    credential_target: str | None = None,
    clone_env: dict[str, str],
    sandbox_mode: str,
    forward_ssh_auth_sock: bool = False,
) -> dict[str, Any] | None:
    """Materialise *dest* at exactly *commit*. Returns None on success."""
    return await _git_fetch_ref(
        git_url,
        commit,
        dest,
        log_lines,
        credential_target=credential_target,
        clone_env=clone_env,
        sandbox_mode=sandbox_mode,
        forward_ssh_auth_sock=forward_ssh_auth_sock,
    )


async def _git_fetch_branch(
    git_url: str,
    branch: str,
    dest: Path,
    log_lines: list[str],
    *,
    credential_target: str | None = None,
    clone_env: dict[str, str],
    sandbox_mode: str,
    mask_local_git_config: bool = False,
) -> dict[str, Any] | None:
    """Fetch and check out *branch* without exposing credentials to checkout.

    *mask_local_git_config* is forwarded to :func:`_git_fetch_ref`; see there. It
    is for the pre-install store-art prewarm only and stays off for installs.
    """
    return await _git_fetch_ref(
        git_url,
        branch,
        dest,
        log_lines,
        checkout_branch=branch,
        credential_target=credential_target,
        clone_env=clone_env,
        sandbox_mode=sandbox_mode,
        mask_local_git_config=mask_local_git_config,
    )


async def _git_clone_or_pull(
    git_url: str,
    branch: str,
    dest: Path,
    log_lines: list[str],
    *,
    credential_target: str | None = None,
    index_originated: bool = False,
    pending_cleanup: list[Path] | None = None,
    restorable_stale: list[Path] | None = None,
    commit: str = "",
) -> dict[str, Any] | None:
    """Clone credential-free *git_url*, or fast-forward it if already present.

    Returns None on success, or a ``{"ok": False, ...}`` error dict on failure.

    ``credential_target`` may carry embedded userinfo for the network request,
    but it is deliberately separate from *git_url*: the latter is repository
    identity and the only target allowed into sandbox argv, diagnostics, stored
    origin, and logs. The raw target reaches only the per-network-call transport
    environment created after :func:`wrap_argv` returns.

    If *pending_cleanup* is provided (a mutable list), any moved-aside directory
    that should be deleted after the caller's full install transaction succeeds
    is appended to it. The caller is responsible for cleaning up these paths
    on the happy path; on failure, the old checkout has already been restored
    by this function's finally block.

    If *restorable_stale* is provided (a mutable list), a moved-aside checkout
    that is the SAME repository as the active one (a branch drift, not a
    different repo) is additionally appended here. Only a path in BOTH
    *pending_cleanup* and *restorable_stale* is safe to hand back as
    ``restore_from`` on a later rejection — restoring an origin-mismatched
    move-aside would give the build the exact tree an earlier gate refused.

    *index_originated* selects the credential posture (confused-deputy defense —
    see :func:`anonymous_git_env`). When ``False`` (the default: a bundled /
    owner-designated install) the clone keeps the gateway's ambient git/ssh
    identity via :func:`minimal_env`. When ``True`` (the repo URL came from an
    owner-configured *external* registry index — index-controlled content, not a
    repo the owner typed) the clone runs **credential-free** via
    :func:`anonymous_git_env` and forces the ``strict`` OS sandbox (``~/.ssh``
    hidden), so a hostile index entry pointing at a private *sibling* repo on the
    owner's own trusted forge cannot be read with the gateway's identity.
    """
    transport_target = credential_target or git_url
    if _git_target_is_unsupported(transport_target):
        return {
            "ok": False,
            "error": (
                "git clone target contains an unsupported query or fragment or an "
                "ambiguous Git transport identity"
            ),
        }
    if _strip_git_target_userinfo(git_url) != git_url:
        raise ValueError("_git_clone_or_pull requires a credential-free git_url")
    credential_target = credential_target or git_url
    if _strip_git_target_userinfo(credential_target) != git_url:
        raise ValueError("credential_target does not match git_url")
    credentialed_transport = credential_target != git_url

    clone_env = anonymous_git_env() if index_originated else minimal_env()
    sandbox_mode = "strict" if index_originated else _context_clone_sandbox_mode(git_url)
    forward_sock = not index_originated  # an index entry never reaches the owner's ssh-agent
    # SSRF gate: refuse to clone/pull from a host the owner does not explicitly
    # trust (public forge or configured registry). The git_url may originate
    # from an untrusted external registry index; this prevents a clone against
    # a loopback/internal destination it could inject. is_clone_host_trusted()
    # loads config from disk, so run it off the event loop.
    if not await asyncio.to_thread(is_clone_host_trusted, git_url):
        log_lines.append(
            "Refusing clone: host of "
            f"{_strip_git_target_userinfo(git_url)!r} is not a trusted forge/registry"
        )
        return {
            "ok": False,
            # Human sentence in `error`, machine slug in `code`: the install
            # banner renders `result.error`, never `result.message`.
            "code": "untrusted_clone_host",
            "error": (
                "Refusing to clone from an untrusted host "
                "(not a public forge or configured registry)."
            ),
        }
    # Track a moved-aside directory if we need to preserve the old checkout
    # during origin-mismatch re-clone (delete-after-success pattern).
    moved_aside: Path | None = None
    # Whether *moved_aside* is the same repository (restore it on failure) rather
    # than a different one moved out of the way (retain it, never restore).
    moved_aside_is_restorable = False

    if dest.is_dir() and (dest / ".git").is_dir():
        # The credential posture was decided from *git_url* — but a persisted
        # clone pulls from ITS OWN `origin`, which can be a different URL
        # (e.g. a registry replaced with the same app name leaves the old
        # clone behind). Never run a credentialed pull against an unverified
        # remote: require the existing origin to resolve to the same clone
        # target as the vetted git_url. Userinfo is deliberately ignored here:
        # credential rotation must not turn the same repository into a rebind.
        # Any other mismatch moves the stale clone aside and re-clones from the
        # URL the posture decision was actually made for.
        #
        # The same origin check gates the manifest that admission ran on (see
        # _fetch_app_manifest), so the re-clone below cannot swap in code that
        # was admitted under a different repo's manifest.
        #
        # The mismatched clone is NEVER built from or pulled from — fail-closed.
        existing_origin = await _clone_origin_url(dest)
        if existing_origin is None:
            # Unreadable origin (corrupt .git/config, missing remote, etc.).
            # Fail-closed WITHOUT destroying the checkout — the user may
            # have local edits and the checkout might be the correct repo
            # with a broken config. Never enter the destructive
            # move-aside/re-clone path on an ambiguous signal.
            log_lines.append(
                f"Cannot read origin remote of existing checkout at {dest}; "
                "refusing to replace it (fix the checkout manually and retry)"
            )
            return {
                "ok": False,
                "name": dest.name,
                # Human sentence in `error`, machine slug in `code`: the App
                # Store install banner renders `result.error` and never
                # `result.message`, so the slug must not sit in `error`.
                "code": "unreadable_clone_origin",
                "error": (
                    "The existing checkout's origin remote is unreadable. "
                    f"Remove or fix it manually at {dest} and retry the install."
                ),
            }
        if not _same_git_target(existing_origin, git_url):
            # Parity with the branch-mismatch path below: say WHY the checkout
            # is being replaced before doing it, naming the mismatched origin,
            # so the install log records the re-clone reason instead of a bare
            # move-aside line.
            log_lines.append(
                "Existing clone origin "
                f"{_strip_git_target_userinfo(existing_origin)!r} does not match "
                f"{_strip_git_target_userinfo(git_url)!r}; moving aside stale clone "
                "for re-clone"
            )
            # Move aside with an atomic same-filesystem rename into a sibling
            # temp path under the app-sources root. If rename fails (e.g. locked
            # files on Windows), return fail-closed without deleting dest.
            moved_aside = await _move_checkout_aside(dest, log_lines)
            if moved_aside is None:
                log_lines.append(f"Refusing to build from the stale clone at {dest}")
                return {
                    "ok": False,
                    "name": dest.name,
                    "code": "stale_clone_not_removed",
                    "error": (
                        "A checkout of a different repository is present and could not be "
                        f"moved aside (a file at {dest} may be locked or in use). "
                        "Remove it manually and retry the install."
                    ),
                }
        elif credentialed_transport and not commit:
            # Never run a credentialed `git pull` inside an app-controlled
            # checkout. Pull can merge/checkout and invoke hooks or named filter
            # drivers from that repository while the raw URL rewrite is present.
            # Preserve the verified same-origin tree, then materialise a clean
            # replacement whose fetch alone receives the credential.
            log_lines.append(
                "Credentialed branch update requires an isolated fetch; moving "
                "the existing checkout aside for a restorable replacement"
            )
            moved_aside = await _move_checkout_aside(dest, log_lines)
            if moved_aside is None:
                return {
                    "ok": False,
                    "name": dest.name,
                    "code": "existing_checkout_not_moved_aside",
                    "error": (
                        "The existing app checkout could not be moved aside, so a "
                        "credentialed update cannot be performed safely. Remove or "
                        "move it manually and retry the install."
                    ),
                }
            moved_aside_is_restorable = True

    if not commit and dest.is_dir() and (dest / ".git").is_dir():
        # Branch re-convergence only applies to branch-tracking entries. A
        # commit-pinned install never reuses the existing tree (the `if commit:`
        # block below moves it aside and re-fetches detached regardless), so
        # reading its branch here is pointless work — and pointless attack
        # surface: the read touches ``.git/HEAD`` inside a checkout the app's
        # own build script can rewrite. Skipping it when `commit` is set keeps
        # the reconvergence read off the pinned-update path entirely (the read
        # itself is also bounded in :func:`_read_clone_branch`, so the fast
        # path at :func:`_clone_branch_matches` is safe too).
        #
        # Origin is verified — but the checked-out branch may have drifted
        # (e.g. a registry entry changed from branch A to branch B). If so,
        # the same move-aside/re-clone treatment applies: do NOT checkout in
        # place (local edits would be carried over silently), move the old
        # checkout aside so it is preserved for manual recovery, then fall
        # through to a fresh clone of the correct branch.
        #
        # IMPORTANT: Only move aside when a CONCRETE branch name was read AND
        # it differs from the requested branch. When the read returns None
        # (detached HEAD, unreadable .git/HEAD, gitfile layout) we fall
        # through to the pull path — this is the pre-PR behavior for that
        # checkout (non-destructive). Detached HEAD is the normal healthy
        # state for tag-pinned entries (and for any commit-pinned checkout,
        # which is always fetched detached — see :func:`_git_fetch_commit`);
        # treating it as a confirmed mismatch would destroy a working
        # checkout on every update cycle.
        clone_branch = await asyncio.to_thread(_read_clone_branch, dest)
        if clone_branch is None:
            # Unknown branch state — do not destroy the checkout.
            log_lines.append(
                f"Cannot determine branch of existing checkout at {dest} "
                f"(detached HEAD or unreadable .git/HEAD); skipping "
                f"branch re-convergence and proceeding with pull"
            )
        elif clone_branch != branch:
            log_lines.append(
                f"Existing clone branch {clone_branch!r} does not match "
                f"requested branch {branch!r}; moving aside for re-clone"
            )
            moved_aside = await _move_checkout_aside(dest, log_lines)
            if moved_aside is None:
                log_lines.append(
                    f"Refusing to build from the checkout on the wrong branch at {dest}"
                )
                return {
                    "ok": False,
                    "name": dest.name,
                    # Human sentence in `error`, machine slug in `code`: the
                    # install banner renders `result.error`, never `.message`.
                    "code": "stale_clone_not_removed",
                    "error": (
                        "A checkout on the wrong branch is present and could not be "
                        "moved aside. Remove it manually and retry the install."
                    ),
                }
            # Origin was already verified identical above, so this is the SAME
            # repository the user was on — only its branch drifted. That makes
            # it restorable on a later build/install failure, exactly like the
            # pinned-install move-aside below: a failed transaction must put
            # the user's own (possibly edited) branch-A tree back rather than
            # strand it as an undiscoverable `.stale-*` sibling.
            moved_aside_is_restorable = True

    if dest.is_dir() and (dest / ".git").is_dir():
        if commit:
            # A PINNED INSTALL NEVER REUSES AN EXISTING TREE.
            #
            # Four review rounds landed here, each a different way of trusting
            # on-disk state: adopting a destination that was not a checkout, an
            # exception leaving a half-built one, HEAD equality standing in for
            # contents, and finally content `git status` cannot report at all. The
            # last one is why no cleanliness check can close this class: `.git/`
            # is never reported by any `git status` variant, so an added
            # `.git/hooks/post-checkout` is invisible, and an untracked
            # `sitecustomize.py` executes on interpreter start if the tree lands on
            # `sys.path`. Both run attacker code while the receipt records the pin.
            #
            # So the pin's meaning is restored structurally instead: the tree the
            # build sees is one this call fetched, not one it inspected. The old
            # checkout is moved aside (never deleted) and the fresh-checkout path
            # below fetches into an empty destination created by
            # `git init --template=`, which is also what keeps hooks absent.
            #
            # The cost is one shallow fetch of a single commit per pinned reinstall.
            # The fast path it replaces was buying that round-trip with trust in an
            # agent-writable directory.
            moved_aside = await _move_checkout_aside(dest, log_lines)
            # This one is the SAME repository with the user's own edits, so a failed
            # transaction must put it back. The origin-mismatch move above is a
            # DIFFERENT repository's checkout: restoring that would hand the build the
            # very tree the mismatch gate refused, so it is only ever retained. One
            # list carrying both meanings is what made a failed pinned update either
            # lose the user's edits or resurrect the wrong repo, depending on which
            # rule won.
            moved_aside_is_restorable = moved_aside is not None
            if moved_aside is None:
                return {
                    "ok": False,
                    "name": dest.name,
                    # Human sentence in `error`, machine slug in `code`: the
                    # install banner renders `result.error`, never `.message`.
                    "code": "existing_checkout_not_moved_aside",
                    "error": (
                        "The existing app checkout could not be moved aside, so a "
                        "pinned install cannot be performed safely. Remove or move it "
                        "manually and retry the install."
                    ),
                }
            # Fall through: `dest` is gone, so the pinned fetch below
            # creates it fresh inside the try/finally that owns restoration.
        else:
            # Already cloned from the verified origin AND branch (or the branch
            # state was unknown and re-convergence was skipped) — fetch and
            # fast-forward. (The origin-mismatch gate above guarantees this
            # checkout's origin is the same normalized target as git_url: a
            # mismatched checkout was moved aside and never reused. Pull from the
            # current registry URL through the command-scoped rewrite, so rotated
            # credentials take effect without ever being persisted in origin.)
            log_lines.append(
                f"Updating {_strip_git_target_userinfo(git_url)} (branch: {branch})..."
            )
            # Route through wrap_argv (OS sandbox) THEN cgroup_scope_argv, matching
            # the fresh-clone path below — the cgroup DoS ceiling is the outermost
            # layer but must not replace the wrap_argv sandbox on this
            # agent-influenced git spawn. The hooks/fsmonitor neutralizer is spliced
            # right after ``git`` so a repo-shipped hook cannot run during the pull.
            pull_cmd, _cleanup = await wrap_argv_async(
                ["git", *_HOOKS_NEUTRALIZER_ARGV, "pull", "--ff-only", git_url, branch],
                mode=sandbox_mode,
                forward_ssh_auth_sock=forward_sock,
                _prepare=wrap_argv,
            )
            pull_cmd = cgroup_scope_argv(pull_cmd)
            pull_env = _git_transport_env(credential_target, git_url, clone_env)
            proc = await create_subprocess_limited(
                *pull_cmd,
                cwd=str(dest),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
                env=pull_env,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
                log_lines.append(
                    _loggable_git_transport_output(
                        stdout.decode(errors="replace").strip(),
                        credentialed=credentialed_transport,
                    )
                )
                if proc.returncode != 0:
                    # Fail closed: installing whatever the checkout happens to hold
                    # while persisting the catalog URL as its provenance would
                    # record a source the installed code was never fetched from.
                    log_lines.append(f"git pull failed (exit {proc.returncode}) — aborting")
                    return {
                        "ok": False,
                        "error": (
                            f"git pull failed (exit {proc.returncode}); "
                            "not installing stale code"
                        ),
                    }
            except asyncio.TimeoutError:
                await _kill_process_group(proc)
                log_lines.append("git pull timed out — aborting")
                return {
                    "ok": False,
                    "error": "git pull timed out; not installing stale code",
                }
            return None

    # Fresh checkout.
    #
    # Both the pinned and the branch path run inside the SAME try/finally below,
    # which owns moved-aside restoration. The first draft gave the pinned path its
    # own restore, which skipped on a spawn exception or cancellation -- and the two
    # copies could nest, stranding the user's old checkout. One restoration path,
    # exercised by both.
    clone_succeeded = False
    try:
        if commit:
            result = await _git_fetch_commit(
                git_url,
                commit,
                dest,
                log_lines,
                credential_target=credential_target,
                clone_env=clone_env,
                sandbox_mode=sandbox_mode,
                forward_ssh_auth_sock=forward_sock,
            )
            if result is not None:
                return result
            clone_succeeded = True
            return None

        if credentialed_transport:
            result = await _git_fetch_branch(
                git_url,
                branch,
                dest,
                log_lines,
                credential_target=credential_target,
                clone_env=clone_env,
                sandbox_mode=sandbox_mode,
            )
            if result is not None:
                return result
            clone_succeeded = True
            return None

        log_lines.append(f"Cloning {_strip_git_target_userinfo(git_url)} (branch: {branch})...")
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone_cmd = [
            "git",
            *_HOOKS_NEUTRALIZER_ARGV,
            "clone",
            "--depth",
            "1",
            "--branch",
            branch,
            "--single-branch",
            git_url,
            str(dest),
        ]
        sandboxed_cmd, _cleanup = await wrap_argv_async(
            clone_cmd, mode=sandbox_mode, forward_ssh_auth_sock=forward_sock, _prepare=wrap_argv
        )
        sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
        transport_env = _git_transport_env(credential_target, git_url, clone_env)

        proc = await create_subprocess_limited(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            env=transport_env,
        )
        # `rmtree_force`, never `shutil.rmtree(..., ignore_errors=True)`: what a
        # half-finished `git clone` leaves at *dest* is a git checkout, and git
        # creates `.git/objects/pack/*.{pack,idx,rev}` READ-ONLY. On Windows that
        # is the FILE_ATTRIBUTE_READONLY bit, so the unlink raises, `ignore_errors`
        # swallows it, and the tree stays on disk while this returns an error the
        # caller reads as "nothing was left behind".
        #
        # Only the FRESH-INSTALL path reaches the three removals below; when an
        # existing checkout was moved aside the `finally` owns the unwind and
        # already copes with a surviving tree. That asymmetry is the bug: with
        # nothing moved aside, an undeletable partial clone is never noticed, and
        # the next install finds `dest/.git` present with a matching origin and
        # takes the fast-forward branch instead -- `git pull` in a repo the clone
        # never finished, which fails, so every retry of that install fails too.
        clone_output = ""
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_CLONE_TIMEOUT)
            clone_output = _loggable_git_transport_output(
                stdout.decode(errors="replace").strip(),
                credentialed=credentialed_transport,
            )
            log_lines.append(clone_output)
        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            await _rmtree_force_settled(dest)
            return {"ok": False, "name": dest.name, "error": "git clone timed out"}
        except asyncio.CancelledError:
            await _kill_process_group(proc)
            await _rmtree_force_settled(dest)
            raise
        if proc.returncode != 0:
            await _rmtree_force_settled(dest)
            if index_originated:
                # The clone ran credential-free (anonymous_git_env + strict
                # sandbox) because the repo URL came from an external registry
                # index whose repo differs from the registry URL, so owner
                # credentials were withheld (confused-deputy defense). A private
                # sibling repo therefore fails to clone. BUT the credential-
                # posture remedy ("private app repos must live inside the
                # registry repo") is only honest when a withheld credential is a
                # plausible cause: gate it on an auth-shaped failure class. A
                # typo'd branch, a DNS blip, or a deleted public repo returns the
                # bare honest failure instead — being told to restructure
                # repositories for a transient error is the misleading-remedy
                # defect this gate closes.
                #
                # No raw clone output ever reaches the banner: the classifiers
                # return only booleans, and the appended failure class is a
                # CONSTANT allowlisted label, so credential-bearing or path-
                # bearing stderr cannot leak (PR-1418 lesson).
                if _git_output_is_auth_shaped(clone_output):
                    remedy_lead = "so owner credentials are withheld"
                elif "repository not found" in clone_output.lower():
                    # A private repo the caller cannot see reads as "repository
                    # not found", so on this credential-free clone a withheld
                    # credential is a *possible* (not certain) cause: keep the
                    # hint, softened to "a likely cause". Deliberately NARROW —
                    # a *branch* not found ("Remote branch X not found") is a
                    # definite typo, not a posture signal, so the bare token
                    # "not found" is excluded.
                    remedy_lead = "so a likely cause is that owner credentials are withheld"
                else:
                    remedy_lead = ""
                if remedy_lead:
                    # Human sentence in `error`, machine slug in `code`: the
                    # install banner renders `result.error`, never `.message`.
                    return {
                        "ok": False,
                        "name": dest.name,
                        "code": "git_clone_failed_no_credentials",
                        "error": (
                            "Git clone failed (cloned without credentials because "
                            "this app's repo URL differs from the registry URL, "
                            f"{remedy_lead}). Private app repos must live inside "
                            "the registry repo — see the monorepo layout in "
                            "docs/app-kit/publishing-guide.md."
                        ),
                    }
                # Not auth-shaped: bare honest failure, with the redacted
                # (allowlisted, constant) failure class when one is recognized.
                failure_class = _redacted_git_failure_class(clone_output)
                if failure_class:
                    return {
                        "ok": False,
                        "name": dest.name,
                        "code": "git_clone_failed",
                        "error": f"Git clone failed: {failure_class}.",
                    }
                return {
                    "ok": False,
                    "name": dest.name,
                    "code": "git_clone_failed",
                    "error": "git clone failed",
                }
            return {
                "ok": False,
                "name": dest.name,
                "code": "git_clone_failed",
                "error": "git clone failed",
            }
        clone_succeeded = True
        return None
    finally:
        if moved_aside is not None:
            if clone_succeeded:
                # Clone verified — but do NOT delete moved_aside yet.
                # The caller's build/install step has not run; if it fails
                # the user loses their old (possibly locally modified) code.
                # Instead, surface the path for the caller to clean up
                # after the full install transaction succeeds.
                if pending_cleanup is not None:
                    pending_cleanup.append(moved_aside)
                # Reported separately, because only a same-repository move may be
                # restored: putting an origin-mismatched checkout back would give the
                # build the tree that gate refused.
                if moved_aside_is_restorable and restorable_stale is not None:
                    restorable_stale.append(moved_aside)
            else:
                # Clone did NOT succeed — remove any partial dest and restore
                # the old checkout so the user's code is not stranded.
                await _rmtree_force_settled(dest)
                # If dest still exists the removal genuinely could not finish:
                # `rmtree_force` clears the read-only bit, but a file another
                # process holds OPEN still refuses to unlink on Windows. Move IT
                # aside so the restore rename cannot collide. Keep the path inside
                # app-sources.
                if dest.exists():
                    partial_name = f"{dest.name}.partial-{uuid.uuid4().hex[:8]}"
                    partial_aside = dest.with_name(partial_name)
                    try:
                        await asyncio.to_thread(dest.rename, partial_aside)
                        log_lines.append(
                            f"Undeletable partial clone moved to {partial_aside}; "
                            "remove it manually when the lock is released"
                        )
                    except OSError as move_exc:
                        log_lines.append(
                            f"Cannot remove or move partial clone at {dest}: "
                            f"{move_exc}; old checkout remains at {moved_aside}"
                        )
                        # Cannot restore — bail out of the restore attempt.
                        moved_aside = None  # skip the rename below
                    else:
                        # Refresh mtime so the retention clock starts now
                        # (best-effort — harmless if it fails).
                        try:
                            await asyncio.to_thread(os.utime, partial_aside)
                        except OSError:
                            pass
                if moved_aside is not None:
                    try:
                        await asyncio.to_thread(moved_aside.rename, dest)
                    except OSError as exc:
                        log_lines.append(
                            f"Cannot restore moved-aside checkout at "
                            f"{moved_aside}: {exc}; recover your files from "
                            f"{moved_aside}"
                        )
