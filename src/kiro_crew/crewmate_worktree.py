"""Each crewmate works in a git worktree of its own.

Two crewmates handed the same repository must not share a working tree or a
checked-out branch: one would commit the other's edits, or land a commit on the
branch the other has checked out. The gateway does only the three things a
model cannot do for itself here:

* give each crewmate one worktree per repository (:func:`ensure`), at
  ``<crewmate workspace>/worktrees/<crewmate-slug>/<repo>-<hash>``
  (:func:`path_for`), on its own branch ``<prefix><crewmate-slug>/<repo>``
  (:func:`branch_for`; the prefix is ``agent.member_branch_prefix``, ``crew/``
  unless set), cut from the repository's default branch the first time and
  reused, as it is, after that; a repository the edition works in place
  (``AgentRuntime.works_in_place``) gets none, so the worker's project stays put;
* point the worker's ``slot.project`` at it at the start of each turn
  (:mod:`kiro_crew.dashboard.crewmate_worktree_scope`), with the repository's
  ``aws.project_profiles`` binding still applying there (:func:`source_path`);
* remove a crewmate's worktrees when the crewmate is removed
  (:func:`remove_crewmate_worktrees`): one with uncommitted or ignored files is
  kept and reported, the branch always stays, and the repository's own checkout
  is never touched.

The module also makes the worktree an isolated ``spawn_run`` worker runs in
(``isolate: "worktree"``, :func:`make_run_worktree`): detached at the HEAD of the
checkout the task's ``cwd`` is in, BESIDE the repository as
``<parent>/<repo>-wt-<run id>`` and only under an allowed root of
``agent.subagent_cwd_allowed_roots``; and removes it once the run's retention
ends, only while it is clean (:func:`remove_run_worktree`). :func:`source_path`
maps such a worktree back to the repository too, so a binding there still applies.

Everything else is the worker's own git work, asked for in its prompt
(``worker.principles``): further branches under its prefix, rebasing onto the
default branch when it needs that branch's changes, checking before it pushes.

Every git call goes through the follow-up card's sandboxed runner
(:mod:`kiro_crew.dashboard.handlers.worktree`) with its repo-code guards: no
hooks, no fsmonitor, and a repository declaring a checkout filter is refused.
The folder a worktree goes in sits in the agent-writable workspace, so the
gateway makes (and, after a failed add, removes) it only through no-follow
descriptors. On a platform that cannot pin a tree walk (Windows), where that
descriptor-bound removal refuses, the empty half-made folder is instead removed
by a guarded ``os.rmdir`` of that same folder while it is still the one this
lookup made. All functions here block; callers run them off the event loop.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import PurePath

from kiro_crew.subprocess_utf8 import utf8_path_stdout

logger = logging.getLogger(__name__)

#: The folder under a crewmate's workspace that holds its worktrees.
WORKTREES_DIR = "worktrees"
#: Every crewmate branch starts with this unless ``agent.member_branch_prefix`` names another.
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
#: Between a repository's name and a run id in a run worktree's folder name
#: (``<repo>-wt-<run id>``); also what keeps :func:`source_path` from running git.
RUN_WORKTREE_INFIX = "-wt-"
#: A run id's shape: ``kiro_crew.subagent._RUN_ID_HEX_CHARS`` lowercase hex characters.
_RUN_ID_RE = re.compile(r"[0-9a-f]{16}")
#: Most commits a run worktree's report names; the count covers the rest.
_RUN_COMMITS_SHOWN = 20


@dataclass(frozen=True)
class CrewmateWorktree:
    """A crewmate's worktree of one repository."""

    repo: str  # the repository's own checkout (the main worktree root)
    path: str  # this crewmate's worktree root
    branch: str
    cwd: str  # where the worker starts: the same subdirectory inside the worktree


@dataclass(frozen=True)
class RunWorktree:
    """The worktree one isolated ``spawn_run`` run works in."""

    source_cwd: str  # the cwd it was made from, as given; a retry or recovery starts there
    path: str  # ``<parent of the repository>/<repo name>-wt-<run id>``
    base: str  # the commit it was detached at
    cwd: str  # where the worker starts: the same subdirectory inside the worktree


@dataclass(frozen=True)
class RunWorktreeReport:
    """What :func:`remove_run_worktree` found and did."""

    path: str
    removed: bool
    kept_because: str  # "" when removed, or when only reporting
    commits: tuple[str, ...]  # short shas of ``base..HEAD``, newest first, at most 20
    commit_count: int  # all of ``base..HEAD``; -1 when git could not say


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


def coerce_branch_prefix(value: object) -> str:
    """``agent.member_branch_prefix`` ending in ``/``; :data:`BRANCH_PREFIX` when unset or unusable.

    Every ``/``-separated part (one trailing ``/`` allowed) must be one :func:`_name`
    keeps as it is, so the branch is always a legal git name. Anything else is the
    default, with a warning.
    """
    if not isinstance(value, str) or not value:
        return BRANCH_PREFIX
    parts = value.removesuffix("/").split(sep="/")
    if all(_name(part) == part for part in parts):
        return "/".join(parts) + "/"
    logger.warning(
        "agent.member_branch_prefix %r is not a legal branch prefix; using %r", value, BRANCH_PREFIX
    )
    return BRANCH_PREFIX


def branch_for(slug: str, repo: str, prefix: str = BRANCH_PREFIX) -> str:
    """The branch a crewmate of *slug* works on in *repo*: *prefix*, then its slug and the repo."""
    return f"{prefix}{_name(slug)}/{_name(os.path.basename(repo))}"


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
    repository as one of its worktrees. A run worktree (:func:`make_run_worktree`)
    maps back the same way: its top must sit beside the repository, be named
    ``<repo>-wt-`` plus a run id's 16 lowercase hex characters, and be registered
    by that repository. A repository cloned inside the worktree is its own top,
    so it answers itself. A path with neither a ``worktrees`` folder nor a
    ``-wt-`` component in it runs no git. Never raises; blocks, so an async
    caller runs it off the loop.
    """
    if not cwd or not _absolute_here(cwd):
        return cwd
    try:
        real = os.path.realpath(os.path.expanduser(cwd))
    except (OSError, ValueError):
        return cwd
    parts = PurePath(real).parts
    crewmate = WORKTREES_DIR in parts[:-2]
    run = any(RUN_WORKTREE_INFIX in part for part in parts[1:])
    if not crewmate and not run:
        return cwd
    try:
        return (crewmate and _source_of(real)) or (run and _run_source_of(real)) or cwd
    except Exception:  # noqa: BLE001 - no sandbox, no git: the path matches as itself
        logger.debug("Bot worktree source of %s unknown", cwd, exc_info=True)
        return cwd


def _run_folder_name(repo: str, run_id: str) -> str:
    """The folder a run worktree of *repo* for *run_id* takes, beside *repo*."""
    return f"{_name(os.path.basename(repo))}{RUN_WORKTREE_INFIX}{run_id}"


def _plain_real(path: str) -> str:
    """*path*'s realpath without Windows' extended-length prefix: the form ``validate_cwd`` compares."""
    from kiro_crew.platform_compat import strip_extended_length_prefix

    return strip_extended_length_prefix(os.path.realpath(path))


def _is_run_worktree_of(repo: str, top: str) -> bool:
    """Whether the folder *top* is named and placed as a run worktree of *repo* (no git)."""
    prefix = f"{_name(os.path.basename(repo))}{RUN_WORKTREE_INFIX}"
    name = os.path.basename(top)
    return (
        name.startswith(prefix)
        and _RUN_ID_RE.fullmatch(name[len(prefix) :]) is not None
        and os.path.dirname(_plain_real(top)) == os.path.dirname(_plain_real(repo))
    )


def _run_source_of(real: str) -> str | None:
    """The checkout path *real* mirrors when it is in a run worktree, else ``None``."""
    wt = _wt()
    top = wt._git_toplevel(_nearest_dir(real))
    if not top or not _contains(top, real):
        return None
    repo = repo_root(top)
    if repo is None or repo == top or not _is_run_worktree_of(repo, top):
        return None
    trees = wt._worktree_branches(repo)
    if not trees or wt._norm_path(top) not in trees:
        return None
    rel = os.path.relpath(real, top)
    return repo if rel == "." else os.path.join(repo, rel)


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


def _works_in_place(repo: str) -> bool:
    """Whether the edition keeps *repo* in its own checkout (``AgentRuntime.works_in_place``)."""
    from kiro_crew.platform.context import current_context, safe_context_call

    return safe_context_call(
        lambda: current_context().agent_runtime.works_in_place(repo) is True,
        fallback=False,
        log_message="agent_runtime.works_in_place failed; the Bot gets a worktree",
    )


def ensure(
    slug: str, workspace_dir: str, path: str, prefix: str = BRANCH_PREFIX
) -> CrewmateWorktree | None:
    """The crewmate's own worktree for the repository *path* is in, made if missing.

    ``None`` when *path* is in no git repository, in one that contains the
    crewmate's own workspace (a home folder under git is not a repository it was
    handed), or in one the edition works in place (its build needs the checkout
    where it is). Raises :class:`WorktreeRefused` when the gateway will not make it (a
    checkout filter, an unlistable worktree table, something else at the place).
    One git already registers at the place is used as it is, whatever branch the
    worker has checked out there: that is the worker's own work. Only a worktree
    made here takes *prefix* (:func:`branch_for`).
    """
    wt = _wt()
    repo = repo_root(path)
    if repo is None or _contains(repo, os.path.realpath(workspace_dir)) or _works_in_place(repo):
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
            registered = branch_for(slug, repo, prefix)
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
    logger.info("Bot worktree %s on %s from %s", dest, branch, repo)


def _claim_folder(
    anchor: str, parts: tuple[str, ...], dest: str, *, what: str, create: bool
) -> tuple[int, int]:
    """Make the empty folder *dest* in *anchor*/*parts* without following a link; its ``(dev, ino)``.

    Each part is opened (with *create*, made first) with a no-follow descriptor from
    *anchor* down (``pinned_fs.open_pinned_descendant_dir``) and *dest* is made
    relative to the last one. Raises ``PinnedPathRefusal`` for a part it cannot open
    that way and ``FileExistsError`` when *dest* is taken.
    """
    from kiro_crew.pinned_fs import open_pinned_descendant_dir

    name = os.path.basename(dest)
    with open_pinned_descendant_dir(anchor, parts, what=what, create=create) as dir_fd:
        if dir_fd is None:  # Windows: the walk lstat-refused every link
            os.mkdir(dest)
            made = os.lstat(dest)
        else:
            os.mkdir(name, dir_fd=dir_fd)
            made = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    return made.st_dev, made.st_ino


def _make_dest(workspace_dir: str, slug: str, dest: str) -> tuple[int, int]:
    """Make the empty folder *dest* without following a link; its ``(dev, ino)``.

    ``worktrees/<slug>`` sits in the agent-writable workspace and this runs in the
    gateway: each part is made and opened with a no-follow descriptor from the
    workspace down (:func:`_claim_folder`). A link raises :class:`WorktreeRefused`.
    """
    from kiro_crew.pinned_fs import PinnedPathRefusal

    workspace = os.path.realpath(workspace_dir)
    os.makedirs(workspace, exist_ok=True)  # configured, not the agent's
    try:
        return _claim_folder(
            workspace,
            (WORKTREES_DIR, _name(slug)),
            dest,
            what="Bot worktree folder",
            create=True,
        )
    except FileExistsError:
        raise WorktreeRefused(f"{dest} already exists") from None
    except PinnedPathRefusal as exc:
        raise WorktreeRefused(f"{dest} is not a plain folder: {exc}") from None


def _unwind_add(
    wt, repo: str, dest: str, branch: str, made: tuple[int, int], *, claimed: bool, base_sha: str
) -> None:
    """Undo a failed ``worktree add``: the folder this lookup made, then git's records.

    The folder goes by descriptor, and only when it is still the one made (its
    ``(dev, ino)``), so a parent swapped for a link in between leads to nothing
    being removed; never by a by-name ``rmtree``. Then ``_cleanup_partial`` with
    ``created=False`` prunes the registration and drops the branch this lookup cut.

    A platform that cannot pin a tree (``supports_pinned_tree_walk`` is false, so
    Windows) makes ``remove_tree_pinned`` refuse before touching anything. Left
    there, the empty folder this lookup made stays and every later ``ensure``
    refuses it as "not a worktree", so the worker silently runs in the shared
    checkout. On that platform fall back to one guarded ``os.rmdir`` of that same
    folder: only while ``os.lstat(dest)`` is still a directory (not a link or
    junction) whose ``(dev, ino)`` equals the one made, and ``os.rmdir`` removes
    only an empty directory, so a re-resolved path can at worst drop an empty
    folder, never a tree. Anything else keeps the warning.
    """
    from kiro_crew import pinned_fs

    def _approve(root_fd: int, _tree: object) -> str | None:
        opened = os.fstat(root_fd)
        return None if (opened.st_dev, opened.st_ino) == made else "not the folder made"

    try:
        pinned_fs.remove_tree_pinned(
            os.path.realpath(dest),
            what="half-made Bot worktree",
            approve=_approve,
            refusal=OSError,
        )
    except OSError:
        if pinned_fs.supports_pinned_tree_walk() or not _rmdir_if_made(dest, made):
            logger.warning("Bot worktree %s left in place after a failed add", dest, exc_info=True)
    wt._cleanup_partial(repo, dest, branch, claimed=claimed, created=False, base_sha=base_sha)


def _rmdir_if_made(dest: str, made: tuple[int, int]) -> bool:
    """Remove *dest* by name, only while it is still the empty folder this lookup made.

    For a platform that cannot pin a tree walk, where ``remove_tree_pinned`` refuses.
    ``os.lstat`` must report a directory (no link or junction is followed) whose
    ``(dev, ino)`` equals *made*, and ``os.rmdir`` removes only an empty directory, so a
    folder with content, a foreign folder, or a swapped link is never torn down. Returns
    whether the folder was removed; a non-empty or vanished folder returns False and the
    caller keeps its warning.
    """
    try:
        here = os.lstat(dest)
        if not stat.S_ISDIR(here.st_mode) or (here.st_dev, here.st_ino) != made:
            return False
        os.rmdir(dest)
        return True
    except OSError:
        return False


def _allowed_anchor(parent: str, allowed_roots: list[str]) -> tuple[str, tuple[str, ...]] | None:
    """The longest allowed root holding *parent*, and the parts from it down to *parent*.

    ``validate_cwd``'s own test (``sandbox_plan.path_within`` on realpaths with the
    extended-length prefix stripped), so the folder made here passes it later. The parts
    are cut from those same strings, never case-folded ones.
    """
    from kiro_crew.sandbox_plan import path_within

    plain_parent = _plain_real(parent)
    best: tuple[str, tuple[str, ...]] | None = None
    best_len = -1
    for root in allowed_roots:
        try:
            real_root = os.path.realpath(os.path.expanduser(root))
            plain_root = _plain_real(real_root)
            if not path_within(plain_parent, plain_root) or len(plain_root) <= best_len:
                continue
            rel = os.path.relpath(plain_parent, plain_root)
        except (OSError, ValueError, TypeError):
            continue
        parts = () if rel == os.curdir else tuple(rel.split(os.sep))
        if any(part in ("", os.curdir, os.pardir) for part in parts):
            continue
        best, best_len = (real_root, parts), len(plain_root)
    return best


def make_run_worktree(cwd: str, run_id: str, allowed_roots: list[str]) -> RunWorktree:
    """The worktree isolated run *run_id* works in, made from the checkout *cwd* is in.

    Detached at that checkout's HEAD (uncommitted changes there are not carried
    over), at ``<parent of the repository>/<repo>-wt-<run id>``, and only when that
    folder is under one of *allowed_roots*: the longest root holding the parent is
    where the no-follow walk down to the new folder starts. Made again for the same
    run id, a worktree the repository registers at that exact place is reused.
    Raises :class:`WorktreeRefused` with a plain reason: no repository (or a bare
    one), a repository the edition works in place, a checkout filter, no parent
    folder, no allowed root, the place taken by something else, no HEAD, a folder
    on the way that cannot be opened, or a failed ``worktree add`` (which leaves no
    folder, registration or ref behind). Blocks; callers run it off the loop.
    """
    from kiro_crew.pinned_fs import PinnedPathRefusal

    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise WorktreeRefused(f"isolate: {run_id!r} is not a run id")
    wt = _wt()
    repo = repo_root(cwd)
    if repo is None:
        raise WorktreeRefused(f"isolate: {cwd} is not in a git repository with a checkout")
    if _works_in_place(repo):
        raise WorktreeRefused(
            f"isolate: this edition works {repo} in place, so it gets no worktree"
        )
    offending = wt._checkout_filter(repo)
    if offending:
        raise WorktreeRefused(f"isolate: {repo} configures a checkout filter ({offending})")
    parent = os.path.dirname(repo)
    if not parent or parent == repo:
        raise WorktreeRefused(f"isolate: {repo} has no parent folder to put a worktree beside it")
    anchor = _allowed_anchor(parent, allowed_roots)
    if anchor is None:
        raise WorktreeRefused(
            f"isolate: the folder beside {repo} is not under subagent_cwd_allowed_roots"
        )
    dest = os.path.join(parent, _run_folder_name(repo, run_id))
    with _lock(dest):
        trees = wt._worktree_branches(repo)
        if trees is None:
            raise WorktreeRefused("isolate: git could not list this repository's worktrees")
        registered = wt._norm_path(dest) in trees
        if registered and os.path.isdir(dest) and not os.path.islink(dest):
            base = wt._resolve_commit(dest, "HEAD")
            if not base:
                raise WorktreeRefused(f"isolate: cannot read the HEAD of {dest}")
            return RunWorktree(
                source_cwd=cwd, path=dest, base=base, cwd=_mapped_cwd(wt, repo, dest, cwd)
            )
        if os.path.lexists(dest):
            raise WorktreeRefused(f"isolate: {dest} already exists and is not this run's worktree")
        if registered:
            # Its folder was deleted by hand: drop the stale registration.
            wt._run_git(["worktree", "prune"], repo)
        checkout = _nearest_dir(os.path.realpath(os.path.expanduser(cwd)))
        base = wt._resolve_commit(checkout, "HEAD")
        if not base:
            raise WorktreeRefused(f"isolate: the checkout {cwd} is in has no commit at HEAD")
        try:
            made = _claim_folder(
                anchor[0], anchor[1], dest, what="run worktree folder", create=False
            )
        except FileExistsError:
            raise WorktreeRefused(f"isolate: {dest} already exists") from None
        except PinnedPathRefusal:
            raise WorktreeRefused(f"isolate: cannot open {parent} to make the worktree") from None
        except OSError as exc:
            raise WorktreeRefused(f"isolate: cannot make {dest}: {exc.strerror or exc}") from None
        try:
            proc = wt._run_git(["worktree", "add", "--detach", dest, base], repo)
        except (subprocess.TimeoutExpired, OSError):
            _unwind_add(wt, repo, dest, "", made, claimed=False, base_sha="")
            raise
        if proc.returncode != 0 or not os.path.exists(os.path.join(dest, ".git")):
            _unwind_add(wt, repo, dest, "", made, claimed=False, base_sha="")
            reason = wt._git_error(proc) if proc.returncode else "worktree add failed"
            raise WorktreeRefused(f"isolate: {reason}")
    logger.info("Run worktree %s at %s from %s", dest, base[:12], repo)
    return RunWorktree(source_cwd=cwd, path=dest, base=base, cwd=_mapped_cwd(wt, repo, dest, cwd))


def _commits_since(wt, path: str, base: str) -> tuple[tuple[str, ...], int]:
    """The short shas of ``base..HEAD`` in *path* (newest first, bounded) and their count."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base or ""):
        return (), -1
    counted = wt._run_git(["rev-list", "--count", f"{base}..HEAD"], path)
    try:
        count = int(counted.stdout.strip()) if counted.returncode == 0 else -1
    except ValueError:
        count = -1
    if count <= 0:
        return (), count
    listed = wt._run_git(
        ["rev-list", f"--max-count={_RUN_COMMITS_SHOWN}", "--abbrev-commit", f"{base}..HEAD"], path
    )
    shas = tuple(line.strip() for line in listed.stdout.splitlines() if line.strip())
    return (shas if listed.returncode == 0 else ()), count


def remove_run_worktree(
    path: str, base: str, run_id: str, *, remove: bool = True
) -> RunWorktreeReport:
    """Remove run *run_id*'s worktree at *path* when it is clean; what it found.

    *path* and *base* come from the gateway's own record. The folder is first
    re-derived from *run_id* and the repository git says *path* belongs to, and
    that repository must register a worktree at exactly that place; anything else
    is left alone. Clean means its HEAD is still *base*, ``git status`` shows
    nothing (untracked files included, whatever the config), it holds no
    ignored files and no file flagged assume-unchanged or skip-worktree (which
    both git checks skip) is still in the folder; only then ``git worktree
    remove`` runs, never with ``--force``.
    *remove* false only reports the commits ``base..HEAD``.
    Never raises; blocks, so callers run it off the loop.
    """
    wt = _wt()

    def kept(why: str, commits: tuple[str, ...] = (), count: int = -1) -> RunWorktreeReport:
        if remove and why:
            logger.warning("Run worktree %s kept: %s", path, why)
        return RunWorktreeReport(path, False, why, commits, count)

    try:
        if not path or not os.path.isdir(path) or os.path.islink(path):
            return RunWorktreeReport(path, False, "the folder is not there", (), -1)
        repo = repo_root(path)
        real = os.path.realpath(path)
        if repo is None or repo == real:
            return kept("it is not a worktree of a repository")
        expected = os.path.join(os.path.dirname(repo), _run_folder_name(repo, run_id))
        if wt._norm_path(expected) != wt._norm_path(path) or (
            wt._norm_path(real) != wt._norm_path(expected)
        ):
            return kept("it is not where this run's worktree goes")
        with _lock(expected):
            trees = wt._worktree_branches(repo)
            if not trees or wt._norm_path(expected) not in trees:
                return kept("the repository does not register it")
            commits, count = _commits_since(wt, expected, base)
            if not remove:
                return kept("", commits, count)
            why = ""
            if wt._resolve_commit(expected, "HEAD") != base:
                why = "it has commits" if count > 0 else "its HEAD moved"
            elif wt._checkout_filter(expected):
                why = "the repository configures a checkout filter"
            else:
                # --untracked-files=all: a status.showUntrackedFiles=no config must never
                # hide new files that ``git worktree remove`` would delete with the folder.
                status = wt._run_git(
                    [
                        "--no-optional-locks",
                        "status",
                        "--porcelain",
                        "--untracked-files=all",
                        "--ignore-submodules=all",
                    ],
                    expected,
                )
                if status.returncode != 0:
                    why = "git could not read its status"
                elif status.stdout.strip():
                    why = "it has uncommitted changes"
                elif ignored := _ignored_files(wt, expected):
                    why = f"it holds ignored files ({ignored})"
                elif flagged := _flagged_files(wt, expected):
                    why = f"it holds files flagged assume-unchanged or skip-worktree ({flagged})"
            if why:
                return kept(why, commits, count)
            proc = wt._run_git(["worktree", "remove", expected], repo)
    except Exception:  # noqa: BLE001 - no sandbox, no git: keep it
        logger.warning("Run worktree %s could not be checked; kept", path, exc_info=True)
        return RunWorktreeReport(path, False, "it could not be checked", (), -1)
    if proc.returncode != 0:
        return kept(wt._git_error(proc), commits, count)
    logger.info("Run worktree %s removed", expected)
    return RunWorktreeReport(path, True, "", commits, count)


def _ignored_files(wt, path: str) -> str:
    """The first ignored paths in the worktree at *path*, ``""`` when none; ``"unknown"`` if git cannot say."""
    proc = wt._run_git(
        ["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"], path
    )
    if proc.returncode != 0:
        return "unknown"
    found = [p for p in proc.stdout.split("\0") if p]
    return ", ".join(found[:5]) + (f" and {len(found) - 5} more" if len(found) > 5 else "")


def _flagged_files(wt, path: str) -> str:
    """The first flagged tracked files in the worktree at *path*, returned as :func:`_ignored_files`.

    Flagged means assume-unchanged (``ls-files -v`` tags it in lowercase) or
    skip-worktree (``S``), which ``git status`` and ``git worktree remove`` both
    skip. Only a flagged file still in the folder counts: an absent one (a sparse
    checkout leaves skipped files out) holds nothing a removal could lose. The
    presence check uses the exact bytes git listed, whatever the locale, and a
    name shows U+FFFD for a byte that is not UTF-8.
    """
    proc = wt._run_git(["ls-files", "-z", "-v"], path, stdout_decoder=utf8_path_stdout)
    if proc.returncode != 0:
        return "unknown"
    found = [
        entry[2:]
        for entry in proc.stdout.split("\0")
        if (entry[:1] == "S" or entry[:1].islower())
        and os.path.lexists(
            os.path.join(os.fsencode(path), entry[2:].encode("utf-8", "surrogateescape"))
        )
    ]
    shown = ", ".join(found[:5]).encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return shown + (f" and {len(found) - 5} more" if len(found) > 5 else "")


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
                logger.warning("Bot worktree %s kept: not a registered worktree", dest)
                kept.append(dest)
                continue
            why = ""
            if wt._checkout_filter(dest):
                why = "the repository configures a checkout filter"
            elif ignored := _ignored_files(wt, dest):
                why = f"ignored files ({ignored})"
            if why:
                logger.warning("Bot worktree %s kept: %s", dest, why)
                kept.append(dest)
                continue
            proc = wt._run_git(["worktree", "remove", dest], repo)
        except Exception:  # noqa: BLE001 - no sandbox, no git: keep it
            logger.warning("Bot worktree %s could not be removed", dest, exc_info=True)
            kept.append(dest)
            continue
        if proc.returncode == 0:
            removed.append(dest)
        else:
            logger.warning("Bot worktree %s kept: %s", dest, wt._git_error(proc))
            kept.append(dest)
    with contextlib.suppress(OSError):
        os.rmdir(root)  # only once empty; rmdir never follows a link
    return removed, kept


def remove_for_member(name: str, config) -> tuple[list[str], list[str]] | None:
    """Remove the worktrees of crewmate *name* (its record read from *config*). Best-effort.

    Called when the crewmate is deleted; never raises, so it cannot block that.
    Returns ``(removed, kept)`` as :func:`remove_crewmate_worktrees` does, or
    ``None`` when the worktrees could not be looked at at all.
    """
    from kiro_crew.config import loader
    from kiro_crew.members import member_slug

    record = getattr(config, "agents", {}).get(name)
    if record is None:
        return [], []
    try:
        workspace = str(loader.workspace_dir_for(getattr(record, "workspace", "") or None))
        removed, kept = remove_crewmate_worktrees(workspace, member_slug(name, config))
    except Exception:  # noqa: BLE001 - never blocks the crewmate's removal
        logger.warning("worktrees of %s were not removed", name, exc_info=True)
        return None
    if kept:
        logger.warning("kept %d worktree(s) of %s: %s", len(kept), name, kept)
    return removed, kept
