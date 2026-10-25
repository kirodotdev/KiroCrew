"""Each crewmate works in a git worktree of its own.

Two crewmates handed the same repository must not share a working tree or a
checked-out branch: one would commit the other's edits, or land a commit on the
branch the other has checked out. The gateway does only the three things a
model cannot do for itself here:

* give each crewmate one worktree per repository (:func:`ensure`), at
  ``<crewmate workspace>/worktrees/<crewmate-slug>/<repo>-<hash>``
  (:func:`path_for`), on its own branch ``crew/<crewmate-slug>/<repo>``
  (:func:`branch_for`), cut from the repository's default branch the first time
  and reused, as it is, after that;
* point the worker's ``slot.project`` at it at the start of each turn
  (:mod:`kiro_crew.dashboard.crewmate_worktree_scope`), with the repository's
  ``aws.project_profiles`` binding still applying there (:func:`source_path`);
* remove a crewmate's worktrees when the crewmate is removed
  (:func:`remove_crewmate_worktrees`): one with uncommitted or ignored files is
  kept and reported, the branch always stays, and the repository's own checkout
  is never touched.

Everything else is the worker's own git work, asked for in its prompt
(``worker.principles``): further branches under its prefix, rebasing onto the
default branch when it needs that branch's changes, checking before it pushes.

Every git call goes through the follow-up card's sandboxed runner
(:mod:`kiro_crew.dashboard.handlers.worktree`) with its repo-code guards: no
hooks, no fsmonitor, and a repository declaring a checkout filter is refused.
The folder a worktree goes in sits in the agent-writable workspace, so the
gateway makes (and, after a failed add, removes) it only through no-follow
descriptors. All functions here block; callers run them off the event loop.
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
from pathlib import PurePath

logger = logging.getLogger(__name__)

#: The folder under a crewmate's workspace that holds its worktrees.
WORKTREES_DIR = "worktrees"
#: Every crewmate branch starts with this, then the crewmate's slug.
BRANCH_PREFIX = "crew/"

# An absolute or home-relative path inside free text: a worker's brief.
_PATH_RE = re.compile(r"(?<![\w.~/-])((?:~|/)[^\s'\"`<>|;,()\[\]{}]+)")
# A Windows drive path (``D:\projects\tasklog`` or ``D:/projects``), unquoted.
_WIN_PATH_RE = re.compile(r"(?<![\w.~/\\-])([A-Za-z]:[\\/][^\s'\"`<>|;,()\[\]{}]*)")
# Any of those paths inside matching quotes, read whole so a space does not cut it.
_QUOTED_PATH_RE = re.compile(r"([\"'`])((?:~|/|[A-Za-z]:[\\/])[^\"'`\n]{0,400}?)\1")
_PATH_TRAILING = ".:!?*"
_MAX_PATHS = 20
_NAME_STRIP_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DOTS_RE = re.compile(r"\.{2,}")
#: Longest name segment kept as it is; a longer one is cut and keeps a digest of the whole.
_NAME_MAX = 40
#: Local branches tried, in order, when the repository names no ``origin/HEAD``.
_LOCAL_BASES = ("main", "master")  # wokeignore:rule=master


@dataclass(frozen=True)
class CrewmateWorktree:
    """A crewmate's worktree of one repository."""

    repo: str  # the repository's own checkout (the main worktree root)
    path: str  # this crewmate's worktree root
    branch: str
    cwd: str  # where the worker starts: the same subdirectory inside the worktree


class WorktreeRefused(RuntimeError):
    """The repository cannot be given a crewmate worktree (the reason says why)."""


def _wt():
    from kiro_crew.dashboard.handlers import worktree as wt  # circular: handlers import config

    return wt


def _name(text: str) -> str:
    """*text* as one safe path segment and legal git ref component, distinct per name.

    A name already safe and short is kept as it is. One the cleaning changed
    (``app+tools``, ``app.lock``, ``app..tools``) or cut keeps an 8-character
    digest of the whole, so two names never share a segment.
    """
    clean = _DOTS_RE.sub("-", _NAME_STRIP_RE.sub("-", text)).strip("-.")
    while clean.lower().endswith(".lock"):
        clean = clean[: -len(".lock")].rstrip("-.")
    clean = clean or "repo"
    if clean == text and len(clean) <= _NAME_MAX:
        return clean
    digest = hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return f"{clean[: _NAME_MAX - 9].rstrip('-.') or 'repo'}-{digest}"


def branch_for(slug: str, repo: str) -> str:
    """The branch a crewmate of *slug* works on in *repo*."""
    return f"{BRANCH_PREFIX}{_name(slug)}/{_name(os.path.basename(repo))}"


def _root(workspace_dir: str, slug: str) -> str:
    """The folder that holds crewmate *slug*'s worktrees."""
    return os.path.join(os.path.realpath(workspace_dir), WORKTREES_DIR, _name(slug))


def path_for(workspace_dir: str, slug: str, repo: str) -> str:
    """The worktree crewmate *slug* gets for *repo*, one per repository.

    Under the crewmate's workspace, in a folder of its own slug (two crewmates may
    share a workspace), named for the repository and a digest of its path (two
    repositories may share a name).
    """
    return os.path.join(_root(workspace_dir, slug), _folder_name(repo))


def _folder_name(repo: str) -> str:
    """The last segment of every crewmate worktree of *repo*: its name and path digest."""
    real = os.path.realpath(repo)
    digest = hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return f"{_name(os.path.basename(real))}-{digest}"


def _absolute_here(path: str) -> bool:
    """Whether *path*, ``~`` expanded, is absolute on the platform the gateway runs on."""
    try:
        return os.path.isabs(os.path.expanduser(path))
    except (TypeError, ValueError):
        return False


def paths_in(text: str) -> list[str]:
    """Absolute, ``~`` or Windows drive paths named in *text*, in order, at most a bounded few.

    A quoted path is read whole, spaces included (``"/projects/My Repo"``); an
    unquoted one ends at the first space. Only a path absolute on THIS platform is
    kept: ``C:\\tools\\x`` on a POSIX gateway would resolve against its own cwd.
    """
    text = text or ""
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for match in _QUOTED_PATH_RE.finditer(text):
        taken.append(match.span())
        found.append((match.start(), match.group(2).strip()))
    # Windows paths first, so the ``/tools/x`` inside ``C:/tools/x`` is not a path of its own.
    for regex in (_WIN_PATH_RE, _PATH_RE):
        for match in regex.finditer(text):
            if any(a <= match.start() < b for a, b in taken):
                continue
            taken.append(match.span())
            found.append((match.start(), match.group(1).rstrip(_PATH_TRAILING)))
    out: list[str] = []
    for _start, candidate in sorted(found):
        if len(candidate) > 1 and candidate not in out and _absolute_here(candidate):
            out.append(candidate)
        if len(out) >= _MAX_PATHS:
            break
    return out


def _nearest_dir(real: str) -> str:
    """*real*, or its nearest existing ancestor: a brief may name a file not made yet."""
    current = real
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            return current
        current = parent
    return current


def repo_root(path: str) -> str | None:
    """The repository checkout *path* belongs to, else ``None``.

    Read from git's common directory, so a path inside any worktree of a
    repository (another crewmate's included) maps to that repository's own
    checkout. A bare repository, or a path not absolute here, answers ``None``.
    """
    if not _absolute_here(path):
        return None
    start = _nearest_dir(os.path.realpath(os.path.expanduser(path)))
    if not os.path.isdir(start):
        return None
    proc = _wt()._run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], start)
    common = proc.stdout.strip() if proc.returncode == 0 else ""
    if not common or os.path.basename(os.path.normpath(common)) != ".git":
        return None
    return os.path.realpath(os.path.dirname(os.path.normpath(common)))


def _contains(parent: str, child: str) -> bool:
    """Whether *child* is *parent* or inside it; paths on different drives are not."""
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:
        return False


def source_path(cwd: str) -> str:
    """*cwd* as the same path in the repository's own checkout, when it is in a crewmate worktree.

    ``<workspace>/worktrees/<slug>/<repo>-<hash>/src`` answers ``<repo>/src``;
    any other path answers itself, so ``aws.project_profiles`` binds a worker in
    its worktree exactly as it would bind it in the checkout. It is read from git
    each time and nothing is remembered, so it holds after a restart: the folder
    git names as the work tree's top must sit at ``worktrees/<slug>/``, carry the
    name :func:`path_for` gives that repository, and be registered by that
    repository as one of its worktrees. A repository cloned inside the worktree
    is its own top, so it answers itself. A path with no ``worktrees`` folder in
    it runs no git. Never raises; blocks, so an async caller runs it off the loop.
    """
    if not cwd or not _absolute_here(cwd):
        return cwd
    try:
        real = os.path.realpath(os.path.expanduser(cwd))
    except (OSError, ValueError):
        return cwd
    if WORKTREES_DIR not in PurePath(real).parts[:-2]:
        return cwd
    try:
        return _source_of(real) or cwd
    except Exception:  # noqa: BLE001 - no sandbox, no git: the path matches as itself
        logger.debug("crewmate worktree source of %s unknown", cwd, exc_info=True)
        return cwd


def _source_of(real: str) -> str | None:
    """The checkout path *real* mirrors, else ``None`` (see :func:`source_path`)."""
    wt = _wt()
    top = wt._git_toplevel(_nearest_dir(real))
    if not top or not _contains(top, real):
        return None
    parts = PurePath(top).parts
    if len(parts) < 3 or parts[-3] != WORKTREES_DIR:
        return None
    repo = repo_root(top)
    if repo is None or repo == top or os.path.basename(top) != _folder_name(repo):
        return None
    trees = wt._worktree_branches(repo)
    if not trees or wt._norm_path(top) not in trees:
        return None
    rel = os.path.relpath(real, top)
    return repo if rel == "." else os.path.join(repo, rel)


def _plain(text: str) -> bool:
    """Whether *text* (a subfolder that becomes the printed project) has no control character or bracket."""
    return not any(ord(c) < 0x20 or c in "\x7f[]" for c in text)


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(dest: str) -> threading.Lock:
    """One lookup at a time per worktree: two worker turns of one crewmate may race."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(dest, threading.Lock())


def ensure(slug: str, workspace_dir: str, path: str) -> CrewmateWorktree | None:
    """The crewmate's own worktree for the repository *path* is in, made if missing.

    ``None`` when *path* is in no git repository, or in one that contains the
    crewmate's own workspace (a home folder under git is not a repository it was
    handed). Raises :class:`WorktreeRefused` when the gateway will not make it (a
    checkout filter, an unlistable worktree table, something else at the place).
    One git already registers at the place is used as it is, whatever branch the
    worker has checked out there: that is the worker's own work.
    """
    wt = _wt()
    repo = repo_root(path)
    if repo is None or _contains(repo, os.path.realpath(workspace_dir)):
        return None
    dest = path_for(workspace_dir, slug, repo)
    with _lock(dest):
        trees = wt._worktree_branches(repo)
        if trees is None:
            raise WorktreeRefused("git could not list this repository's worktrees")
        registered = trees.get(wt._norm_path(dest))
        if registered is None or not os.path.isdir(dest):
            if os.path.lexists(dest):
                raise WorktreeRefused(f"{dest} exists and is not a worktree of {repo}")
            if registered is not None:
                # Its folder was deleted by hand: drop the stale registration.
                wt._run_git(["worktree", "prune"], repo)
            registered = branch_for(slug, repo)
            _create(wt, repo, dest, registered, workspace_dir, slug)
    return CrewmateWorktree(
        repo=repo, path=dest, branch=registered, cwd=_mapped_cwd(wt, repo, dest, path)
    )


def _mapped_cwd(wt, repo: str, dest: str, path: str) -> str:
    """The folder in *dest* matching the subfolder *path* names; the worktree root otherwise."""
    start = _nearest_dir(os.path.realpath(os.path.expanduser(path)))
    top = wt._git_toplevel(start) or repo
    rel = os.path.relpath(start, top)
    if rel == "." or rel.startswith("..") or not _plain(rel):
        return dest
    cwd = os.path.join(dest, rel)
    # A link inside the worktree (``docs -> ../../repo``) would lead back out of it.
    if not os.path.isdir(cwd) or not _contains(os.path.realpath(dest), os.path.realpath(cwd)):
        return dest
    return cwd


def _default_branch(wt, repo: str) -> str:
    """The ref the first cut comes from: ``origin/HEAD``, else a usual default branch name, else ``HEAD``.

    Fully qualified, so a tag named like the branch is never taken for it.
    """
    for ref in ("refs/remotes/origin/HEAD", *(f"refs/heads/{n}" for n in _LOCAL_BASES)):
        if wt._resolve_commit(repo, ref):
            return ref
    return "HEAD"


def _create(wt, repo: str, dest: str, branch: str, workspace_dir: str, slug: str) -> None:
    """Add *dest* on *branch*, cutting the branch from the default branch when it is new."""
    offending = wt._checkout_filter(repo)
    if offending:
        raise WorktreeRefused(f"the repository configures a checkout filter ({offending})")
    claimed = False
    base_sha = ""
    if not wt._resolve_commit(repo, f"refs/heads/{branch}"):
        base_sha = wt._resolve_commit(repo, _default_branch(wt, repo))
        if not base_sha:
            raise WorktreeRefused("the repository has no commit to branch from")
        if not wt._claim_branch(repo, branch, base_sha):
            raise WorktreeRefused(f"could not create {branch}")
        claimed = True
    try:
        made = _make_dest(workspace_dir, slug, dest)
    except (OSError, WorktreeRefused):
        if claimed:
            wt._cleanup_partial(repo, dest, branch, claimed=True, created=False, base_sha=base_sha)
        raise
    try:
        proc = wt._run_git(["worktree", "add", dest, branch], repo)
    except (subprocess.TimeoutExpired, OSError):
        _unwind_add(wt, repo, dest, branch, made, claimed=claimed, base_sha=base_sha)
        raise
    if proc.returncode != 0 or not os.path.exists(os.path.join(dest, ".git")):
        _unwind_add(wt, repo, dest, branch, made, claimed=claimed, base_sha=base_sha)
        raise WorktreeRefused(wt._git_error(proc) if proc.returncode else "worktree add failed")
    logger.info("crewmate worktree %s on %s from %s", dest, branch, repo)


def _make_dest(workspace_dir: str, slug: str, dest: str) -> tuple[int, int]:
    """Make the empty folder *dest* without following a link; its ``(dev, ino)``.

    ``worktrees/<slug>`` sits in the agent-writable workspace and this runs in the
    gateway: each part is made and opened with a no-follow descriptor from the
    workspace down (``pinned_fs.open_pinned_descendant_dir``) and *dest* is made
    relative to the last one. A link raises :class:`WorktreeRefused`.
    """
    from kiro_crew.pinned_fs import PinnedPathRefusal, open_pinned_descendant_dir

    workspace = os.path.realpath(workspace_dir)
    os.makedirs(workspace, exist_ok=True)  # configured, not the agent's
    name = os.path.basename(dest)
    try:
        with open_pinned_descendant_dir(
            workspace, (WORKTREES_DIR, _name(slug)), what="crewmate worktree folder", create=True
        ) as dir_fd:
            if dir_fd is None:  # Windows: the walk lstat-refused every link
                os.mkdir(dest)
                made = os.lstat(dest)
            else:
                os.mkdir(name, dir_fd=dir_fd)
                made = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileExistsError:
        raise WorktreeRefused(f"{dest} already exists") from None
    except PinnedPathRefusal as exc:
        raise WorktreeRefused(f"{dest} is not a plain folder: {exc}") from None
    return made.st_dev, made.st_ino


def _unwind_add(
    wt, repo: str, dest: str, branch: str, made: tuple[int, int], *, claimed: bool, base_sha: str
) -> None:
    """Undo a failed ``worktree add``: the folder this lookup made, then git's records.

    The folder goes by descriptor, and only when it is still the one made (its
    ``(dev, ino)``), so a parent swapped for a link in between leads to nothing
    being removed; never by a by-name ``rmtree``. Then ``_cleanup_partial`` with
    ``created=False`` prunes the registration and drops the branch this lookup cut.
    """
    from kiro_crew import pinned_fs

    def _approve(root_fd: int, _tree: object) -> str | None:
        opened = os.fstat(root_fd)
        return None if (opened.st_dev, opened.st_ino) == made else "not the folder made"

    try:
        pinned_fs.remove_tree_pinned(
            os.path.realpath(dest),
            what="half-made crewmate worktree",
            approve=_approve,
            refusal=OSError,
        )
    except OSError:
        logger.warning("crewmate worktree %s left in place after a failed add", dest, exc_info=True)
    wt._cleanup_partial(repo, dest, branch, claimed=claimed, created=False, base_sha=base_sha)


def _ignored_files(wt, path: str) -> str:
    """The first ignored paths in the worktree at *path*, ``""`` when none; ``"unknown"`` if git cannot say."""
    proc = wt._run_git(
        ["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"], path
    )
    if proc.returncode != 0:
        return "unknown"
    found = [p for p in proc.stdout.split("\0") if p]
    return ", ".join(found[:5]) + (f" and {len(found) - 5} more" if len(found) > 5 else "")


def remove_crewmate_worktrees(workspace_dir: str, slug: str) -> tuple[list[str], list[str]]:
    """Remove crewmate *slug*'s worktrees; ``(removed, kept)``.

    Found by listing ``<workspace>/worktrees/<slug>`` and asking git which
    repository each belongs to and whether it registers it there; anything git
    does not register as a worktree of that repository is left untouched. Each
    goes with ``git worktree remove`` (never ``--force``): one with uncommitted
    changes, ignored files (results, a data file) or a repository that configures
    a checkout filter is kept and listed in ``kept``. Branches stay, and the
    repository's own checkout is never touched. A link where the folder should be
    is not followed.
    """
    wt = _wt()
    root = _root(workspace_dir, slug)
    if os.path.islink(os.path.dirname(root)) or os.path.islink(root) or not os.path.isdir(root):
        return [], []
    removed: list[str] = []
    kept: list[str] = []
    for item in sorted(os.scandir(root), key=lambda e: e.name):
        if item.is_symlink() or not item.is_dir(follow_symlinks=False):
            continue
        dest = item.path
        try:
            repo = repo_root(dest)
            trees = wt._worktree_branches(repo) if repo else None
            if (
                not repo
                or repo == os.path.realpath(dest)
                or not trees
                or (wt._norm_path(dest) not in trees)
            ):
                logger.warning("crewmate worktree %s kept: not a registered worktree", dest)
                kept.append(dest)
                continue
            why = ""
            if wt._checkout_filter(dest):
                why = "the repository configures a checkout filter"
            elif ignored := _ignored_files(wt, dest):
                why = f"ignored files ({ignored})"
            if why:
                logger.warning("crewmate worktree %s kept: %s", dest, why)
                kept.append(dest)
                continue
            proc = wt._run_git(["worktree", "remove", dest], repo)
        except Exception:  # noqa: BLE001 - no sandbox, no git: keep it
            logger.warning("crewmate worktree %s could not be removed", dest, exc_info=True)
            kept.append(dest)
            continue
        if proc.returncode == 0:
            removed.append(dest)
        else:
            logger.warning("crewmate worktree %s kept: %s", dest, wt._git_error(proc))
            kept.append(dest)
    with contextlib.suppress(OSError):
        os.rmdir(root)  # only once empty; rmdir never follows a link
    return removed, kept


def remove_for_member(name: str, config) -> None:
    """Remove the worktrees of crewmate *name* (its record read from *config*). Best-effort.

    Called when the crewmate is deleted; never raises, so it cannot block that.
    """
    from kiro_crew.config import loader
    from kiro_crew.members import member_slug

    record = getattr(config, "agents", {}).get(name)
    if record is None:
        return
    try:
        workspace = str(loader.workspace_dir_for(getattr(record, "workspace", "") or None))
        _removed, kept = remove_crewmate_worktrees(workspace, member_slug(name, config))
    except Exception:  # noqa: BLE001 - never blocks the crewmate's removal
        logger.warning("worktrees of %s were not removed", name, exc_info=True)
        return
    if kept:
        logger.warning("kept %d worktree(s) of %s: %s", len(kept), name, kept)
