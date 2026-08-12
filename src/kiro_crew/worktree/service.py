"""Repo-agnostic worktree inspection, recyclability verdicts, and removal.

This is the generalized form of what Dev Fleet does for the Kiro Crew checkout
alone: Dev Fleet pins the repository in a module-level constant and its base
branch to ``main``, so its logic cannot serve a user's own package. Here the
repository is a parameter and the base ref is the repo's own declaration
(``origin/HEAD``), so a pure-backend project with no build step is as well served
as a web one.

The recyclability verdict is the only safety-critical piece. ``dirty`` (a
non-empty ``git status --porcelain``, untracked files included) means work exists
ONLY in that directory: deleting it destroys work no commit holds, with no
recovery. So a dirty tree is never automatically recyclable, and a verdict that
could not be computed is not either — see :func:`prune_verdict`.

Merge detection needs no ``gh`` and no network. ``git cherry`` compares
*patch identity*, so a branch whose commits were squash-merged into the base
reports zero patch-unique commits while ``rev-list`` still counts them: that
combination — commits of its own, none of them patch-unique — is what identifies
landed work.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import asdict, dataclass, field

from kiro_crew.worktree.git_exec import git_error, norm_path, resolve_base_ref, run_git

# A tree with no commits of its own is only recyclable once it is old enough to
# be clearly abandoned rather than one the user just made and has not used yet.
STALE_EMPTY_AGE_S = 48 * 3600

# Verdict codes. Only `merged` and `empty` are recyclable; everything else,
# including every failure to determine the answer, holds the tree.
VERDICT_MERGED = "merged"
VERDICT_MERGED_DIRTY = "merged_dirty"
VERDICT_EMPTY = "empty"
VERDICT_FRESH = "fresh"
VERDICT_ACTIVE = "active"
VERDICT_DIRTY_CHECK_FAILED = "dirty_check_failed"
VERDICT_BASE_UNKNOWN = "base_unknown"

RECYCLABLE_VERDICTS = frozenset({VERDICT_MERGED, VERDICT_EMPTY})


@dataclass
class WorktreeInfo:
    """One worktree's state, as the dashboard needs to render and judge it."""

    path: str
    branch: str = ""
    head: str = ""
    detached: bool = False
    locked: bool = False
    is_main: bool = False
    # False when the base ref did not resolve. Every commit-count comparison is
    # meaningless then, so the verdict must not pretend otherwise.
    base_known: bool = True
    # None means "could not be determined" — distinct from False, and never
    # treated as clean.
    dirty: bool | None = None
    # Patch-unique commits vs the base ref, and raw commits vs it. They diverge
    # exactly when work landed upstream in a different shape (squash merge).
    ahead: int = 0
    own_commits: int = 0
    behind: int = 0
    age_s: int = 0
    size_bytes: int = 0
    verdict: str = VERDICT_BASE_UNKNOWN
    recyclable: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RepoWorktrees:
    """Every linked worktree of one repository, plus the base they judge against."""

    repo: str
    base_ref: str = ""
    base_sha: str = ""
    worktrees: list[WorktreeInfo] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "worktrees": [w.to_dict() for w in self.worktrees],
            "error": self.error,
        }


def _parse_worktree_list(stdout: str) -> list[dict]:
    """Parse ``worktree list --porcelain -z`` into one dict per registered tree.

    ``-z`` because a worktree path may itself contain a newline: with the
    line-oriented form such a path splits across records and never matches its
    registered entry. In ``-z`` mode git NUL-terminates every attribute and emits
    an extra NUL between entries, so empty fields are simply skipped.
    """
    trees: list[dict] = []
    current: dict | None = None
    for raw in stdout.split("\0"):
        if not raw:
            continue
        if raw.startswith("worktree "):
            current = {
                "path": raw[len("worktree ") :],
                "branch": "",
                "head": "",
                "detached": False,
                "locked": False,
            }
            trees.append(current)
            continue
        if current is None:
            continue
        if raw.startswith("branch "):
            ref = raw[len("branch ") :]
            current["branch"] = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        elif raw.startswith("HEAD "):
            current["head"] = raw[len("HEAD ") :][:7]
        elif raw == "detached":
            current["detached"] = True
        elif raw == "locked" or raw.startswith("locked "):
            current["locked"] = True
    return trees


def _patch_unique_ahead(path: str, base_ref: str) -> int | None:
    """Commits on this tree's HEAD that are not present in ``base_ref`` by patch id.

    ``git cherry`` prefixes a patch-unique commit with ``+`` and one already
    upstream with ``-``, which is what makes squash-merged work show as zero.
    """
    proc = run_git(["cherry", base_ref, "HEAD"], path)
    if proc.returncode != 0:
        return None
    return sum(1 for line in proc.stdout.splitlines() if line.startswith("+"))


def _ahead_behind(path: str, base_sha: str) -> tuple[int, int] | None:
    """``(own_commits, behind)`` against ``base_sha`` in ONE git call.

    ``--left-right --count A...B`` prints "<in A not B> <in B not A>", so with
    A=base and B=HEAD the pair is exactly (behind, own commits). Two separate
    ``rev-list --count`` invocations produced the same two numbers at twice the
    process cost, which dominates on a sandboxed spawn.
    """
    proc = run_git(["rev-list", "--count", "--left-right", f"{base_sha}...HEAD"], path)
    if proc.returncode != 0:
        return None
    parts = proc.stdout.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    behind, own = int(parts[0]), int(parts[1])
    return own, behind


def _dir_size(path: str) -> int:
    """Apparent size of ``path``'s contents; best effort, never raises."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def prune_verdict(info: WorktreeInfo) -> tuple[str, bool]:
    """Classify ``info`` and say whether the tree may be recycled.

    Fail CLOSED at every uncertainty. An unknown base ref or an undeterminable
    dirty state yields a non-recyclable verdict, because "we could not tell" and
    "there is nothing to lose" must never collapse into the same answer — the
    consequence of confusing them is deleting work that exists nowhere else.
    """
    if info.is_main:
        return VERDICT_ACTIVE, False
    if info.dirty is None:
        return VERDICT_DIRTY_CHECK_FAILED, False
    if not info.base_known:
        return VERDICT_BASE_UNKNOWN, False

    # Commits of its own, none of them patch-unique -> the work is upstream,
    # whether merged as-is or squashed.
    if info.own_commits > 0 and info.ahead == 0:
        if info.dirty:
            return VERDICT_MERGED_DIRTY, False
        return VERDICT_MERGED, True

    if info.own_commits == 0 and not info.dirty:
        if info.age_s > STALE_EMPTY_AGE_S:
            return VERDICT_EMPTY, True
        return VERDICT_FRESH, False

    return VERDICT_ACTIVE, False


def inspect_worktree(
    path: str,
    base_sha: str,
    *,
    is_main: bool = False,
    branch: str = "",
    head: str = "",
) -> WorktreeInfo:
    """Gather one worktree's git state and classify it. Never raises on git error.

    ``base_sha`` must be a resolved COMMIT, not a ref name. A symbolic base such
    as ``HEAD`` would be re-evaluated inside this worktree — against its own tip —
    so every commit count would come back zero and unmerged work would read as
    already landed.

    ``branch`` and ``head`` come from ``worktree list --porcelain``, which already
    reports both for every registered tree. Re-deriving them here would cost two
    more sandboxed spawns per tree for information the caller is holding.
    """
    info = WorktreeInfo(path=path, is_main=is_main, branch=branch, head=head)

    status = run_git(["status", "--porcelain"], path)
    if status.returncode != 0:
        info.error = git_error(status)
    else:
        info.dirty = any(line.strip() for line in status.stdout.splitlines())

    if not base_sha:
        info.base_known = False
        info.error = info.error or "base ref could not be resolved"
    else:
        ahead = _patch_unique_ahead(path, base_sha)
        counts = _ahead_behind(path, base_sha)
        if ahead is None or counts is None:
            info.base_known = False
            info.error = info.error or "commit counts could not be read"
        else:
            info.ahead = ahead
            info.own_commits, info.behind = counts

    # Age is measured from the worktree directory's creation, not its mtime: a
    # build writing into the tree would otherwise keep resetting the clock and a
    # genuinely abandoned tree would never reach the stale threshold.
    info.age_s = _worktree_age_s(path)
    info.verdict, info.recyclable = prune_verdict(info)
    return info


def _worktree_age_s(path: str) -> int:
    """Seconds since the worktree directory was created, best effort.

    ``st_birthtime`` where the platform has it, else ``st_ctime`` (inode change
    time on POSIX, creation on Windows) — for a directory git created and never
    re-linked, close enough to decide "clearly abandoned". Returns 0 when it
    cannot be read, which keeps a tree OUT of the stale-empty class rather than
    into it.
    """
    try:
        st = os.stat(path)
    except OSError:
        return 0
    created = getattr(st, "st_birthtime", None) or st.st_ctime
    return max(0, int(time.time() - created))


def list_worktrees(root: str, *, with_size: bool = False) -> RepoWorktrees:
    """Every worktree registered against ``root``, judged against its base ref.

    The main worktree is included and flagged ``is_main`` so the caller can show
    it as context, but it is never recyclable.
    """
    result, entries = _prepare(root)
    if result.error:
        return result
    root_norm = norm_path(root)
    for entry in entries:
        result.worktrees.append(
            _inspect_entry(entry, result.base_sha, root_norm, with_size)
        )
    return result


def _prepare(root: str) -> tuple[RepoWorktrees, list[dict]]:
    """Per-repo half: list the trees and resolve the base to a commit."""
    result = RepoWorktrees(repo=root)
    listing = run_git(["worktree", "list", "--porcelain", "-z"], root)
    if listing.returncode != 0:
        result.error = git_error(listing)
        return result, []

    result.base_ref = resolve_base_ref(root)
    # Resolve the base to a COMMIT here, in the main repo, and compare every
    # worktree against that. A ref name would be re-resolved inside each worktree,
    # and `HEAD` (the fallback when the repo has no `origin/HEAD`) means a
    # different commit in every one of them.
    base = run_git(["rev-parse", "--verify", "--quiet", f"{result.base_ref}^{{commit}}"], root)
    result.base_sha = base.stdout.strip() if base.returncode == 0 else ""
    return result, _parse_worktree_list(listing.stdout)


def _inspect_entry(
    entry: dict, base_sha: str, root_norm: str, with_size: bool
) -> WorktreeInfo:
    """Per-tree half. Touches only this tree, so it is safe on a worker thread."""
    path = entry["path"]
    if not os.path.isdir(path):
        # Registered but gone from disk (a manual `rm -rf`). Reported so the UI
        # can offer a prune, not silently dropped.
        return WorktreeInfo(
            path=path,
            branch=entry["branch"],
            head=entry["head"],
            base_known=False,
            error="registered but missing on disk",
        )
    info = inspect_worktree(
        path,
        base_sha,
        is_main=norm_path(path) == root_norm,
        branch=entry["branch"],
        head=entry["head"],
    )
    info.detached = entry["detached"] or not entry["branch"]
    info.locked = entry["locked"]
    if with_size:
        info.size_bytes = _dir_size(path)
    return info


# Every git probe is a sandboxed spawn, so a repo with several trees costs real
# wall-clock. Serve a recent answer instead of re-measuring on every open; the
# window is short enough that a tree the user just dirtied still reads as dirty
# on their next glance.
CACHE_TTL_S = 10.0
_MAX_CACHED_REPOS = 32
_cache: dict[tuple[str, bool], tuple[float, RepoWorktrees]] = {}


def invalidate_cache(root: str = "") -> None:
    """Drop cached listings for ``root`` (or all of them when empty).

    Called after a mutation so the next read reflects it rather than the window.
    """
    if not root:
        _cache.clear()
        return
    for key in [k for k in _cache if k[0] == root]:
        del _cache[key]


async def list_worktrees_cached(
    root: str, *, with_size: bool = False, ttl: float = CACHE_TTL_S
) -> RepoWorktrees:
    """:func:`list_worktrees` with a TTL cache and per-tree parallelism.

    The per-repo listing must finish before any tree can be judged (it supplies
    the base commit), but the trees themselves are independent directories, so
    their probes run concurrently instead of serially.
    """
    key = (root, with_size)
    hit = _cache.get(key)
    now = time.time()
    if hit is not None and now - hit[0] < ttl:
        return hit[1]

    result, entries = await asyncio.to_thread(_prepare, root)
    if result.error:
        return result
    root_norm = norm_path(root)
    result.worktrees = list(
        await asyncio.gather(
            *(
                asyncio.to_thread(_inspect_entry, entry, result.base_sha, root_norm, with_size)
                for entry in entries
            )
        )
    )
    if len(_cache) >= _MAX_CACHED_REPOS:
        _cache.clear()
    _cache[key] = (now, result)
    return result


def find_worktree(root: str, path: str) -> WorktreeInfo | None:
    """The registered worktree of ``root`` at ``path``, or None if not registered.

    Registration is the identity check every mutation goes through: a path that
    git does not list against this repository is not something this module will
    touch, however plausible it looks on disk.
    """
    target = norm_path(path)
    for info in list_worktrees(root).worktrees:
        if norm_path(info.path) == target:
            return info
    return None


def remove_worktree(root: str, path: str, *, force: bool = False) -> tuple[dict, int]:
    """Remove the worktree at ``path``. Returns ``(json_body, http_status)``.

    Refuses unless the tree is registered against ``root``, and refuses the main
    worktree outright. A dirty tree needs ``force``: the confirmation the user
    gave in the UI is what that flag encodes, so the default can stay safe.

    The branch is deliberately left in place. Deleting a ref is the step that can
    lose commits, and nothing here needs it — a leftover branch costs bytes,
    while a wrongly deleted one costs work.
    """
    info = find_worktree(root, path)
    if info is None:
        return {
            "error": "not a registered worktree of this repository",
            "code": "not_registered",
        }, 404
    if info.is_main:
        return {"error": "refusing to remove the main worktree", "code": "main_worktree"}, 400
    if info.dirty is None and not force:
        return {
            "error": "could not determine whether the worktree is clean",
            "code": "dirty_check_failed",
        }, 409
    if info.dirty and not force:
        return {
            "error": "worktree has uncommitted changes",
            "code": "worktree_dirty",
            "verdict": info.verdict,
            "dirty": True,
        }, 409

    target = info.path
    args = ["worktree", "remove", target]
    if force:
        args.insert(2, "--force")
    proc = run_git(args, root)
    if proc.returncode != 0:
        run_git(["worktree", "prune"], root)
        if os.path.isdir(target):
            return {"error": git_error(proc), "code": "git_failed"}, 400
    if os.path.isdir(target):
        # `worktree remove` refused but the caller forced: drop the directory,
        # then deregister it so git does not keep listing a path that is gone.
        if not force:
            return {"error": git_error(proc), "code": "git_failed"}, 400
        shutil.rmtree(target, ignore_errors=True)
    run_git(["worktree", "prune"], root)
    return {"ok": True, "path": target, "branch": info.branch}, 200


def repo_disk_bytes(root: str) -> int:
    """Total apparent size of every linked (non-main) worktree of ``root``."""
    total = 0
    for info in list_worktrees(root).worktrees:
        if info.is_main or not os.path.isdir(info.path):
            continue
        total += _dir_size(info.path)
    return total


__all__ = [
    "RECYCLABLE_VERDICTS",
    "RepoWorktrees",
    "STALE_EMPTY_AGE_S",
    "VERDICT_ACTIVE",
    "VERDICT_BASE_UNKNOWN",
    "VERDICT_DIRTY_CHECK_FAILED",
    "VERDICT_EMPTY",
    "VERDICT_FRESH",
    "VERDICT_MERGED",
    "VERDICT_MERGED_DIRTY",
    "WorktreeInfo",
    "find_worktree",
    "inspect_worktree",
    "list_worktrees",
    "prune_verdict",
    "remove_worktree",
    "repo_disk_bytes",
]
