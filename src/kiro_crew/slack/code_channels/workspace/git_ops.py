"""Git plumbing for a code channel's workspace: worktree, branch, diff, cleanup.

Every git call here is BLOCKING and runs on a worker thread (the service wraps
each one in :func:`asyncio.to_thread`). Two runners, one policy:

* :func:`run_git` is the follow-up card's ``_run_git``
  (:mod:`kiro_crew.dashboard.handlers.worktree`) — OS-sandboxed, credential-scrubbed
  env, repo hooks / fsmonitor / config hooks disabled, wall-clock timeout. Used for
  the small probes whose output is a sha, a ref or a short listing.
* :func:`run_git_capped` is the same spawn with stdout hard-capped while it is
  read. ``git diff`` output is repository-sized and an agent decides its size, so
  an unbounded ``capture_output`` buffer would be a memory-DoS surface; the cap
  kills the child instead.

Worktree creation is the follow-up card's own ``_create_worktree_sync``: branch
claimed with a compare-and-create ``update-ref``, destination claimed with an
atomic ``mkdir``, checkout-filter repos refused, partial work unwound. Nothing in
that sequence is re-implemented here.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass

from kiro_crew.dashboard.handlers import worktree as _wt
from kiro_crew.git_config_hooks import ConfigHookScanError, config_hook_disable_args_sandboxed
from kiro_crew.sandbox import popen_limited, sandboxed_spawn_argv
from kiro_crew.validation import MAX_FOLLOWUP_BRANCH, is_valid_followup_branch

logger = logging.getLogger(__name__)

#: Raised by both runners when the host cannot isolate a git spawn.
SandboxUnavailable = _wt.SandboxUnavailable

#: Ceiling for one capped git read (a diff of a large tree is not instant).
_CAPPED_TIMEOUT = 60.0

#: ``--unified`` value that puts a whole file into one hunk (more lines than any
#: file the scan cap could hold).
_WHOLE_FILE_CONTEXT = 1_000_000_000

#: Branch namespace for code-channel worktrees, so they group in ``git branch``.
BRANCH_PREFIX = "slack/"

#: Longest title slug kept in a branch name; the channel hash follows it.
_MAX_TITLE_SLUG = 40

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    """One small sandboxed git call (see the module docstring)."""
    return _wt._run_git(args, cwd)


def run_git_capped(
    args: list[str], cwd: str, *, cap: int, timeout: float = _CAPPED_TIMEOUT
) -> tuple[int, bytes, bool]:
    """Run sandboxed git and keep at most ``cap`` bytes of stdout.

    Returns ``(returncode, stdout, truncated)``. ``truncated`` is True when the
    output overflowed ``cap`` (the child is then killed) or the run outlived
    ``timeout``; the bytes read so far are still returned. Same isolation as
    :func:`run_git`: sandbox chokepoint in strict mode, the follow-up card's
    ``-c`` overrides, config-defined hooks disabled by name, no terminal prompt.
    """
    try:
        hook_off = config_hook_disable_args_sandboxed(
            cwd, spawn_argv=sandboxed_spawn_argv, mode=_wt._SANDBOX_MODE
        )
    except ConfigHookScanError as exc:
        logger.warning("code channel git: config hooks could not be listed: %s", exc)
        return 128, b"", False
    except RuntimeError as exc:
        raise SandboxUnavailable(str(exc)) from exc
    try:
        argv, env, cleanup = sandboxed_spawn_argv(
            ["git", *_wt._git_no_repo_code(), *hook_off, *args], mode=_wt._SANDBOX_MODE
        )
    except RuntimeError as exc:
        raise SandboxUnavailable(str(exc)) from exc
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    try:
        try:
            proc = popen_limited(
                argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            logger.warning("code channel git could not start: %s", exc)
            return -9, b"", False
        buf = bytearray()
        overflow = False

        def _drain() -> None:
            nonlocal overflow
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    return
                if len(buf) + len(chunk) > cap:
                    buf.extend(chunk[: cap - len(buf)])
                    overflow = True
                    return
                buf.extend(chunk)

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()
        reader.join(timeout)
        timed_out = reader.is_alive()
        if timed_out or overflow:
            proc.kill()
            reader.join(5)
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = -9
        if timed_out:
            rc = rc or -9
        return rc, bytes(buf), timed_out or overflow
    finally:
        if cleanup:
            with contextlib.suppress(OSError):
                os.unlink(cleanup)


def channel_hash(team_id: str, channel_id: str) -> str:
    """The eight hex digits that tie a branch name to one channel."""
    return hashlib.sha256(f"{team_id}:{channel_id}".encode()).hexdigest()[:8]


def channel_branch(title: str, team_id: str, channel_id: str) -> str:
    """The branch a channel's worktree is cut on: ``slack/<title-slug>-<hash8>``.

    The hash is over ``team_id:channel_id``, so two channels with the same title
    on the same repo get distinct branches (and, because the worktree directory
    is derived from the branch's last segment, distinct directories), while the
    same channel always derives the same name — which is what lets a restart
    adopt the worktree it already made.
    """
    digest = channel_hash(team_id, channel_id)
    slug = _SLUG_STRIP_RE.sub("-", (title or "").lower()).strip("-")[:_MAX_TITLE_SLUG].strip("-")
    branch = f"{BRANCH_PREFIX}{slug or 'code-channel'}-{digest}"
    if len(branch) > MAX_FOLLOWUP_BRANCH or not is_valid_followup_branch(branch):
        branch = f"{BRANCH_PREFIX}code-channel-{digest}"
    return branch


def toplevel(path: str) -> str | None:
    """The git work-tree root containing ``path``, or ``None`` when it is not a repo."""
    return _wt._git_toplevel(path)


def checkout_filter(root: str) -> str:
    """A repo-declared checkout filter git would run (refusal reason), or ``""``."""
    return _wt._checkout_filter(root)


@dataclass(frozen=True)
class CreatedWorktree:
    """A worktree this module made or adopted for one channel."""

    path: str
    branch: str
    base_sha: str
    #: ``origin/HEAD`` when the repo declares a default branch, else ``""``: the
    #: diff base is then the recorded ``base_sha`` alone.
    base_ref: str
    adopted: bool


def is_channel_worktree(
    root: str, worktree: str, branch: str, team_id: str, channel_id: str
) -> bool:
    """Whether ``worktree`` is the registered worktree of THIS channel's own branch.

    The record that names a worktree is a file on disk; before the gateway reads
    files under it (outside any sandbox), the record is checked
    against git itself: the branch must be one :func:`channel_branch` derives for
    this channel, and git must list ``worktree`` as the tree holding that branch
    in ``root``. A record rewritten to point anywhere else matches neither.
    """
    if not (
        branch.startswith(BRANCH_PREFIX)
        and branch.endswith("-" + channel_hash(team_id, channel_id))
    ):
        return False
    held = _registered_path_for(root, branch)
    return held is not None and os.path.realpath(held) == os.path.realpath(worktree)


def resolve_commit(root: str, ref: str) -> str:
    """Commit sha for ``ref`` in ``root``, or ``""``."""
    return _wt._resolve_commit(root, ref)


def _registered_path_for(root: str, branch: str) -> str | None:
    """The registered worktree path holding ``branch``, if any."""
    trees = _wt._worktree_branches(root)
    if not trees:
        return None
    for path, held in trees.items():
        if held == branch and os.path.isdir(path):
            return path
    return None


def create_or_adopt_worktree(root: str, branch: str) -> CreatedWorktree | str:
    """Create the channel's worktree off the default branch, or adopt the one it has.

    Returns the worktree, or a short reason string when none could be made (the
    caller degrades to the original project). The branch name is
    channel-derived (:func:`channel_branch`), so a branch that already exists
    with a registered worktree is this channel's own from an earlier bind (a
    restart, a lost record) and is adopted rather than refused.
    """
    base_ref = _wt._resolve_base_ref(root)
    recorded_ref = "origin/HEAD" if base_ref == "origin/HEAD" else ""
    payload, status = _wt._create_worktree_sync(root, branch)
    if status == 200:
        path = str(payload.get("path") or "")
        sha = resolve_commit(root, f"refs/heads/{branch}")
        return CreatedWorktree(path, branch, sha, recorded_ref, bool(payload.get("reused")))
    existing = _registered_path_for(root, branch)
    if existing:
        # Resolve the base in the MAIN checkout: inside the worktree "HEAD" is
        # the channel's own branch, and merge-base HEAD HEAD answers HEAD.
        base_sha = resolve_commit(root, base_ref)
        sha = (merge_base(existing, "HEAD", base_sha) if base_sha else "") or base_sha
        return CreatedWorktree(existing, branch, sha, recorded_ref, True)
    return str(payload.get("error") or "worktree could not be created")[:300]


def merge_base(cwd: str, left: str, right: str) -> str:
    """``git merge-base left right`` or ``""``."""
    proc = run_git(["merge-base", "--end-of-options", left, right], cwd)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def current_branch(cwd: str) -> str:
    """The checked-out branch name, ``""`` when detached or unreadable."""
    proc = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def short_head(cwd: str) -> str:
    """Abbreviated HEAD sha, or ``""``."""
    proc = run_git(["rev-parse", "--short", "HEAD"], cwd)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def default_branch_name(cwd: str) -> str:
    """The remote's default branch name (``main``) from ``origin/HEAD``, or ``""``."""
    proc = run_git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd)
    name = proc.stdout.strip() if proc.returncode == 0 else ""
    return name.removeprefix("origin/")


def remote_url(cwd: str, remote: str = "origin") -> str:
    """The configured URL of ``remote`` (``""`` when absent)."""
    proc = run_git(["config", "--get", f"remote.{remote}.url"], cwd)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def has_remote_branch(cwd: str, branch: str, remote: str = "origin") -> bool:
    """Whether ``refs/remotes/<remote>/<branch>`` exists locally (i.e. it was pushed)."""
    if not branch:
        return False
    return bool(resolve_commit(cwd, f"refs/remotes/{remote}/{branch}"))


def untracked_files(cwd: str, *, cap: int) -> tuple[list[str], bool] | None:
    """Untracked, not-ignored paths (relative), with whether the list was cut short."""
    rc, out, truncated = run_git_capped(
        ["-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd,
        cap=cap,
    )
    if rc != 0 and not truncated:
        return None
    names = [os.fsdecode(n) for n in out.split(b"\0") if n]
    if truncated and names:
        names.pop()  # the last record may be cut mid-name
    return names, truncated


def tracked_diff(cwd: str, base: str, *, cap: int) -> tuple[bytes, bool] | None:
    """Unified diff of the working tree against ``base`` (committed + uncommitted).

    ``git diff <base>`` compares the working tree with ``base``, so one call holds
    both the branch's commits and its staged/unstaged edits. External diff
    drivers and textconv are off (both would run repo-configured programs), and
    binary files render as git's ``Binary files ... differ`` line.

    Every hunk carries the WHOLE file as context and renames are off, so each
    file section holds both complete versions: :func:`.diff.screen_tracked`
    checks those for credentials, then cuts the context back to git's three
    lines. One read means what is checked is what is published.
    """
    rc, out, truncated = run_git_capped(
        [
            "-c",
            "core.quotePath=false",
            # Pinned so repository config cannot change the shape the screen
            # parses (path prefixes, blank context lines, merged hunks).
            "-c",
            "diff.noprefix=false",
            "-c",
            "diff.mnemonicPrefix=false",
            "-c",
            "diff.suppressBlankEmpty=false",
            "-c",
            "diff.interHunkContext=0",
            "diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            f"--unified={_WHOLE_FILE_CONTEXT}",
            "--ignore-submodules=all",
            "--end-of-options",
            base,
            "--",
        ],
        cwd,
        cap=cap,
    )
    if rc != 0 and not truncated:
        return None
    return out, truncated
