"""Each crewmate works in a git worktree of its own.

Two crewmates handed the same repository must not share a working tree or a
checked-out branch: one would commit the other's edits, or land a commit on the
branch the other has checked out. So when a crewmate's worker works on a git
repository, the gateway gives that crewmate its own worktree of it:

* one per crewmate per repository, BESIDE the repository:
  ``<parent>/<repo>.worktrees/<crewmate-slug>/`` for the repository
  ``<parent>/<repo>`` (:func:`path_for`), reused by every worker it opens. It is a
  worktree of that repository, so it sits next to it rather than inside the
  crewmate's workspace. When the repository's parent is not writable (or the
  repository is a filesystem root) a fallback under the crewmate's workspace is
  used instead (``<workspace>/worktrees/<crewmate-slug>/<repo>-<hash>``,
  :func:`legacy_path_for`), and the reason is logged once;
* on a branch of its own, ``crew/<crewmate-slug>/<repo>``, cut from the
  repository's default branch the first time and kept after that. A topic
  branch a worker cuts there (``crew/<crewmate-slug>/<topic>``) keeps the
  worktree the crewmate's;
* registered in an index in the crewmate's workspace
  (``<workspace>/worktrees/index.json``, ``{worktree path: {repo, branch,
  member}}``). Two crewmates may share one workspace, so every lookup, reuse and
  removal reads only the entries whose ``member`` is the crewmate's slug. Removal
  finds every worktree without scanning the disk and the "already in one of its
  worktrees" check is a membership test. The turn's ``[PROJECT]`` line reads the
  worktree's own ``.git`` file (:func:`describe`), with no git call.

A worktree the index already lists for the repository, on the crewmate's
branch, is used where it is (:func:`ensure`), so a fallback worktree keeps
serving after the repository's parent becomes writable.

The worker's ``slot.project`` (its cwd and project scope) is pointed at that
worktree at the start of its turn (:func:`kiro_crew.dashboard.crewmate_worktree_scope`),
so the shared checkout is never a worker's cwd. When the crewmate is removed its
worktrees go with it (:func:`remove_crewmate_worktrees`); a worktree with
uncommitted changes is kept and reported rather than deleted, the branch always
stays (its commits are the crewmate's work), and the original checkout is never
touched.

Every git call goes through the follow-up card's sandboxed runner
(:mod:`kiro_crew.dashboard.handlers.worktree`), with its repo-code guards: no
hooks, no fsmonitor, and a repository declaring a checkout filter is refused.
All functions here block; callers run them off the event loop.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: The directory under a crewmate's workspace that holds its index (and, as the
#: fallback, worktrees a repository's parent had no room for).
WORKTREES_DIR = "worktrees"
#: The per-crewmate index: ``{worktree path: {repo, branch}}``.
INDEX_FILE = "index.json"
#: The suffix of the folder beside a repository that holds its crewmate worktrees.
SIBLING_SUFFIX = ".worktrees"
#: Every crewmate branch starts with this, then the crewmate's slug.
BRANCH_PREFIX = "crew/"

# An absolute or home-relative path inside free text: a worker's brief or a goal.
_PATH_RE = re.compile(r"(?<![\w.~/-])((?:~|/)[^\s'\"`<>|;,()\[\]{}]+)")
# A Windows drive path (``D:\projects\tasklog`` or ``D:/projects``), unquoted.
_WIN_PATH_RE = re.compile(r"(?<![\w.~/\\-])([A-Za-z]:[\\/][^\s'\"`<>|;,()\[\]{}]*)")
# Any of those paths inside matching quotes, read whole so a space does not cut it.
_QUOTED_PATH_RE = re.compile(r"([\"'`])((?:~|/|[A-Za-z]:[\\/])[^\"'`\n]{0,400}?)\1")
_PATH_TRAILING = ".:!?*"
_MAX_PATHS = 20
_NAME_STRIP_RE = re.compile(r"[^A-Za-z0-9._-]+")


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


#: Longest name segment kept as it is; a longer one is cut and keeps a digest of the whole.
_NAME_MAX = 40


def _name(text: str) -> str:
    """*text* as one safe path and branch segment, distinct for distinct names.

    A name already safe and short is kept as it is. One the cleaning changed
    (``app+tools`` and ``app-tools`` both clean to ``app-tools``) or cut keeps an
    8-character digest of the whole, so two names never share a segment.

    The segment is also a legal git ref component (``git check-ref-format``): no
    ``..``, no leading or trailing ``.``, and no ``.lock`` ending, so
    ``app.lock`` and ``app..tools`` get a branch git can create.
    """
    clean = _ref_safe(_NAME_STRIP_RE.sub("-", text))
    if clean == text and len(clean) <= _NAME_MAX:
        return clean
    digest = hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return f"{_ref_safe(clean[: _NAME_MAX - 9])}-{digest}"


def _ref_safe(text: str) -> str:
    """*text* (already in ``[A-Za-z0-9._-]``) as a legal git ref component."""
    clean = _DOTS_RE.sub("-", text).strip("-.")
    while clean.lower().endswith(".lock"):
        clean = clean[: -len(".lock")].rstrip("-.")
    return clean or "repo"


#: Two or more dots in a row, which a git ref may not hold.
_DOTS_RE = re.compile(r"\.{2,}")


def worktrees_root(workspace_dir: str) -> str:
    """Where *workspace_dir*'s crewmate keeps its index (and any fallback worktrees)."""
    return os.path.join(os.path.realpath(workspace_dir), WORKTREES_DIR)


def branch_for(slug: str, repo: str) -> str:
    """The branch a crewmate of *slug* works on in *repo*."""
    return f"{BRANCH_PREFIX}{_name(slug)}/{_name(os.path.basename(repo))}"


def sibling_root(repo: str) -> str:
    """The folder beside *repo* that holds its crewmate worktrees: ``<repo>.worktrees``."""
    real = os.path.realpath(repo)
    return os.path.join(os.path.dirname(real), _name(os.path.basename(real)) + SIBLING_SUFFIX)


def path_for(repo: str, slug: str) -> str:
    """The worktree a crewmate of *slug* gets for *repo*: ``<repo>.worktrees/<slug>``."""
    return os.path.join(sibling_root(repo), _name(slug))


def legacy_path_for(workspace_dir: str, repo: str, slug: str) -> str:
    """The fallback place, under the crewmate's workspace, in a folder of its own slug.

    Two crewmates may share one workspace, so the slug keeps their fallback
    worktrees of one repository apart.
    """
    digest = hashlib.sha256(os.path.realpath(repo).encode("utf-8", "surrogateescape"))
    return os.path.join(
        worktrees_root(workspace_dir),
        _name(slug),
        f"{_name(os.path.basename(repo))}-{digest.hexdigest()[:8]}",
    )


#: Repositories whose fallback was already logged, so the reason is said once.
_FALLBACK_LOGGED: set[str] = set()


def _beside_repo_refusal(repo: str) -> str:
    """Why a worktree cannot sit beside *repo*, or ``""`` when it can."""
    real = os.path.realpath(repo)
    parent = os.path.dirname(real)
    if not parent or parent == real:
        return "the repository is at a filesystem root"
    root = sibling_root(real)
    if os.path.lexists(root):
        if os.path.islink(root) or not os.path.isdir(root):
            return f"{root} exists and is not a folder"
        if not os.access(root, os.W_OK | os.X_OK):
            return f"{root} is not writable"
        return ""
    if not os.access(parent, os.W_OK | os.X_OK):
        return f"{parent} is not writable"
    return ""


def destination(workspace_dir: str, repo: str, slug: str) -> str:
    """Where this crewmate's worktree of *repo* goes: beside it, else the fallback."""
    reason = _beside_repo_refusal(repo)
    if not reason:
        return path_for(repo, slug)
    key = os.path.realpath(repo)
    if key not in _FALLBACK_LOGGED:
        _FALLBACK_LOGGED.add(key)
        logger.info("crewmate worktrees of %s go under the crewmate's workspace: %s", key, reason)
    return legacy_path_for(workspace_dir, repo, slug)


def _has_git_marker(path: str) -> bool:
    """Whether *path* or an ancestor holds ``.git``: the cheap check before any spawn."""
    current = Path(path)
    for candidate in (current, *current.parents):
        if os.path.exists(candidate / ".git"):  # never raises (an unsearchable folder)
            return True
    return False


def _nearest_dir(real: str) -> str:
    """*real* when it is a directory, else its nearest existing ancestor directory.

    A brief may name a file or folder that does not exist yet ("create
    ``/repos/b/new_package/module.py``"); the repository is the one it would sit in.
    """
    current = real
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            return current
        current = parent
    return current


def repo_root(path: str) -> str | None:
    """The repository checkout *path* belongs to, else ``None``.

    For a path inside a linked worktree (another crewmate's, say) this is the
    repository's own checkout, read from git's common directory, so every
    crewmate maps one repository to one root. A bare repository answers ``None``.
    """
    wt = _wt()
    real = os.path.realpath(os.path.expanduser(path))
    start = _nearest_dir(real)
    if not os.path.isdir(start) or not _has_git_marker(start):
        return None
    proc = wt._run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], start)
    common = proc.stdout.strip() if proc.returncode == 0 else ""
    if not common or os.path.basename(os.path.normpath(common)) != ".git":
        return None
    return os.path.realpath(os.path.dirname(os.path.normpath(common)))


#: One lock per index file: two worker turns of one crewmate may run ``ensure`` at
#: once (on two repositories), and each rewrites the whole index.
_INDEX_LOCKS: dict[str, threading.Lock] = {}
_INDEX_LOCKS_GUARD = threading.Lock()


def _index_lock(root: str) -> threading.Lock:
    with _INDEX_LOCKS_GUARD:
        return _INDEX_LOCKS.setdefault(os.path.realpath(root), threading.Lock())


#: Largest index read; a bigger one is not this crewmate's, and reads as none.
_INDEX_MAX = 1 << 20


def _read_index(root: str) -> dict[str, dict[str, str]]:
    """The crewmate's worktree index, ``{}`` when it is absent, refused or malformed.

    The file sits in the agent-writable workspace and this read runs in the
    gateway, outside any sandbox, so it goes through ``hooks.safe_read_prefix``:
    a link to a credential file (or any sensitive path) is refused unread.
    """
    from kiro_crew.hooks import safe_read_prefix

    if os.path.islink(root):
        # The folder was swapped for a link: what it points at is not this index.
        return {}
    raw = safe_read_prefix(str(Path(root, INDEX_FILE)), _INDEX_MAX + 1)
    if raw is None or len(raw) > _INDEX_MAX:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v
        for k, v in data.items()
        if isinstance(k, str)
        and isinstance(v, dict)
        and isinstance(v.get("repo"), str)
        and isinstance(v.get("branch"), str)
    }


def _write_index(root: str, index: dict[str, dict[str, str]]) -> None:
    """Publish the index under *root* (``<workspace>/worktrees``) without following a link.

    The workspace is agent-writable and this write runs in the gateway, outside any
    sandbox: a ``worktrees`` swapped for a link (to the governance ``profiles/``, say)
    would otherwise be written through. The folder is made and opened with no-follow
    descriptors from the workspace down (``pinned_fs.open_pinned_descendant_dir``)
    and the file is renamed into place relative to that descriptor; a link, or a
    destination on a sensitive path, raises :class:`WorktreeRefused` and nothing is
    written.
    """
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.pinned_fs import PinnedPathRefusal, open_pinned_descendant_dir
    from kiro_crew.security.paths import is_sensitive_path

    target = Path(root, INDEX_FILE)
    if is_sensitive_path(str(target)):
        raise WorktreeRefused(f"{target} is on a sensitive path")
    payload = json.dumps(index, indent=2, sort_keys=True)
    workspace, name = os.path.split(os.path.normpath(root))
    # The workspace is the configured folder (``worktrees_root`` resolved it); only
    # ``worktrees`` below it is the agent's to swap, so only that is walked pinned.
    os.makedirs(workspace, exist_ok=True)
    try:
        with open_pinned_descendant_dir(
            workspace, (name,), what="crewmate worktree index folder", create=True
        ) as dir_fd:
            # Windows cannot pin a directory: the walk lstat-refused every link, and
            # the write goes by name there, as the pinned helpers document.
            atomic_write(target, payload, parent_dir_fd=dir_fd)
    except PinnedPathRefusal as exc:
        raise WorktreeRefused(f"{root} is not a plain folder of the workspace: {exc}") from None


def _read_small(path: Path) -> str:
    """The first 4 KiB of a git metadata file, ``""`` when refused or unreadable.

    Read through ``hooks.safe_read_prefix``: the ``.git`` marker, ``commondir``
    and ``HEAD`` are paths a worker can point anywhere (a ``.git`` symlinked to a
    credential file), and this read runs in the gateway, outside any sandbox.
    """
    from kiro_crew.hooks import safe_read_prefix

    data = safe_read_prefix(str(path), 4096)
    if data is None:
        return ""
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


def _linked_worktree(top: Path) -> tuple[str, str] | None:
    """``(repo, branch)`` read from *top*'s ``.git`` file when it is a linked worktree."""
    marker = top / ".git"
    if not os.path.isfile(marker):
        return None
    text = _read_small(marker)
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:") :].strip())
    if not gitdir.is_absolute():
        gitdir = top / gitdir
    common_rel = _read_small(gitdir / "commondir")
    if not common_rel:
        return None
    common = Path(common_rel) if os.path.isabs(common_rel) else gitdir / common_rel
    common = Path(os.path.realpath(common))
    if common.name != ".git":
        return None
    head = _read_small(gitdir / "HEAD")
    if "\n" in head or "\r" in head or not head.startswith("ref: refs/heads/"):
        return None
    branch = head[len("ref: refs/heads/") :]
    repo = str(common.parent)
    if not valid_branch(branch) or not rail_safe(repo):
        # Both reach the trusted ``[PROJECT]`` line; a forged HEAD names nothing.
        return None
    return repo, branch


#: The characters a branch read from a worktree's HEAD may hold.
_BRANCH_CHARS_RE = re.compile(r"[A-Za-z0-9._/-]+")


def valid_branch(name: str) -> bool:
    """Whether *name* is a branch name ``git check-ref-format --branch`` takes.

    Stricter than git (a plain character set): it reaches the trusted
    ``[PROJECT]`` line, so no control character, ``[``, space or newline.
    """
    if not name or len(name) > 255 or not _BRANCH_CHARS_RE.fullmatch(name):
        return False
    if ".." in name or "//" in name or name.endswith(("/", ".")) or name.startswith("/"):
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock") for part in name.split("/")
    )


def rail_text(text: str) -> str:
    """*text* from git or the repository as it may stand inside a ``[PROJECT]`` note.

    One line, no control character, brackets turned to parentheses, at most 200
    characters: a ref name, a config key or git's error is the repository's to
    choose, and the note is trusted context.
    """
    out = "".join(" " if ord(c) < 0x20 or c == "\x7f" else c for c in str(text))
    out = out.replace("[", "(").replace("]", ")")
    return " ".join(out.split())[:200]


def rail_safe(text: str) -> bool:
    """Whether *text* (a path or branch) can go into the trusted ``[PROJECT]`` line.

    No control character, and none of the brackets a forged context block opens with.
    """
    return bool(text) and not any(ord(c) < 0x20 or c in "\x7f[]" for c in text)


def describe(project: str) -> tuple[str, str, str] | None:
    """``(worktree, repo, branch)`` when *project* is inside a crewmate worktree.

    Reads the worktree's own ``.git`` file and the ``HEAD`` it points at (no git
    call and no path segment to rely on), so the per-turn ``[PROJECT]`` line can
    name it wherever the worktree sits. A crewmate worktree is a linked worktree
    on a ``crew/`` branch. ``None`` for any other directory.
    """
    if not project:
        return None
    real = Path(os.path.realpath(os.path.expanduser(project)))
    for top in (real, *real.parents):
        if not os.path.exists(top / ".git"):
            continue
        found = _linked_worktree(top)
        if found is None:
            return None
        repo, branch = found
        if not branch.startswith(BRANCH_PREFIX):
            return None
        return str(top), repo, branch
    return None


def _own_entries(index: dict[str, dict[str, str]], slug: str) -> dict[str, dict[str, str]]:
    """The index entries crewmate *slug* made: two crewmates may share one workspace."""
    return {k: v for k, v in index.items() if v.get("member") == slug}


def owns(workspace_dir: str, path: str, slug: str) -> bool:
    """Whether *path* is inside one of the worktrees crewmate *slug*'s index entries list."""
    real = os.path.realpath(os.path.expanduser(path))
    for dest in _own_entries(_read_index(worktrees_root(workspace_dir)), slug):
        if real == dest or real.startswith(dest.rstrip(os.sep) + os.sep):
            return True
    return False


def _own_branch(slug: str, branch: str | None) -> bool:
    """Whether *branch* is one of crewmate *slug*'s: its own, or a topic under its prefix."""
    return bool(branch) and str(branch).startswith(f"{BRANCH_PREFIX}{_name(slug)}/")


def _registered_branch(wt, repo: str, dest: str) -> str | None:
    trees = wt._worktree_branches(repo)
    if trees is None:
        raise WorktreeRefused("git could not list this repository's worktrees")
    return trees.get(wt._norm_path(dest))


def ensure(slug: str, workspace_dir: str, path: str) -> CrewmateWorktree | None:
    """The crewmate's own worktree for the repository *path* is in, made if missing.

    ``None`` when *path* is not inside a git repository. Raises
    :class:`WorktreeRefused` when the repository is one the gateway will not
    check out for it (a checkout filter, an unlistable worktree table, a
    destination something else owns). The worktree is reused across workers: an
    existing one on the crewmate's branch is returned as it is, whatever is in
    it, because it holds this crewmate's own work.
    """
    wt = _wt()
    repo = repo_root(path)
    if repo is None:
        return None
    if _contains(repo, os.path.realpath(workspace_dir)):
        # The crewmate's own workspace sits inside this repository (a home
        # directory under git, say): that is not a repository it was handed.
        return None
    branch = branch_for(slug, repo)
    root = worktrees_root(workspace_dir)
    with _index_lock(root):
        return _ensure_locked(wt, slug, workspace_dir, root, repo, branch, path)


def _contains(parent: str, child: str) -> bool:
    """Whether *child* is *parent* or inside it; paths on different drives are not."""
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:
        return False


def _listed(wt, repo: str, slug: str, index: dict[str, dict[str, str]]) -> str:
    """The worktree crewmate *slug*'s index lists for *repo* and git still has, or ``""``.

    Whatever of the crewmate's branches it has checked out: a worker that cut a
    topic branch there (``crew/<slug>/<topic>``) leaves it the crewmate's.
    """
    for listed, entry in sorted(_own_entries(index, slug).items()):
        if entry.get("repo") != repo or not os.path.isdir(listed):
            continue
        if _own_branch(slug, _registered_branch(wt, repo, listed)):
            return listed
    return ""


def _ensure_locked(
    wt, slug: str, workspace_dir: str, root: str, repo: str, branch: str, path: str
) -> CrewmateWorktree:
    index = _read_index(root)
    dest = _listed(wt, repo, slug, index) or destination(workspace_dir, repo, slug)
    real = os.path.realpath(os.path.expanduser(path))
    start = _nearest_dir(real)
    # The same subdirectory, measured from the work tree *path* sits in (the
    # repository's checkout, or a worktree of it).
    top = wt._git_toplevel(start) or repo
    rel = os.path.relpath(start, top)
    sub = "" if rel == "." or rel.startswith("..") else rel
    registered = _registered_branch(wt, repo, dest) if os.path.exists(dest) else None
    if registered is None:
        if os.path.exists(dest):
            raise WorktreeRefused(f"{dest} exists and is not a worktree of {repo}")
        _create(wt, repo, dest, branch, anchor=_anchor_for(workspace_dir, repo, dest))
        registered = branch
    elif not _own_branch(slug, registered):
        raise WorktreeRefused(f"{dest} is registered to {registered or 'a detached HEAD'}")
    entry = {"repo": repo, "branch": branch, "member": slug}
    if index.get(dest) != entry:
        index[dest] = entry
        _write_index(root, index)
    # The subfolder comes from a path the brief named and becomes the project the
    # trusted ``[PROJECT]`` line prints, so one that could forge a marker is not used.
    if sub and not (rail_safe(sub) and os.path.isdir(os.path.join(dest, sub))):
        sub = ""
    cwd = os.path.join(dest, sub) if sub else dest
    if sub and not _contains(os.path.realpath(dest), os.path.realpath(cwd)):
        # A link inside the worktree (``docs -> ../../repo``) would lead the
        # worker's cwd back into the shared checkout: the worktree root instead.
        cwd = dest
    return CrewmateWorktree(repo=repo, path=dest, branch=str(registered), cwd=cwd)


def _anchor_for(workspace_dir: str, repo: str, dest: str) -> str:
    """The folder *dest* is made below: the crewmate's workspace, or the repository's parent."""
    workspace = os.path.realpath(workspace_dir)
    if dest.startswith(worktrees_root(workspace_dir) + os.sep):
        return workspace
    return os.path.dirname(os.path.realpath(repo))


def _make_dest(dest: str, anchor: str) -> tuple[int, int]:
    """Make the empty folder *dest* below *anchor* without following a link; its ``(dev, ino)``.

    Its parents (``<repo>.worktrees``, or ``worktrees/<slug>`` in the workspace)
    are agent-writable, and this runs in the gateway: each is made and opened with
    a no-follow descriptor from *anchor* down (``pinned_fs.open_pinned_descendant_dir``)
    and *dest* is made relative to the last one. A link, or *dest* on a sensitive
    path, raises :class:`WorktreeRefused`; an existing *dest* raises
    :class:`FileExistsError`. The identity is read through the same descriptor,
    so a failure later can remove THIS folder and nothing a swap put in its place
    (:func:`_drop_made_dest`).
    """
    from kiro_crew.pinned_fs import PinnedPathRefusal, open_pinned_descendant_dir
    from kiro_crew.security.paths import is_sensitive_path

    rel = os.path.relpath(os.path.dirname(dest), anchor)
    parts = () if rel == "." else tuple(rel.split(os.sep))
    if any(part in ("", "..") for part in parts) or is_sensitive_path(dest):
        raise WorktreeRefused(f"{dest} is not a place for a crewmate worktree")
    # The anchor is configured, not the agent's (the workspace, or the parent the
    # repository already sits in), so only it may be made by name.
    os.makedirs(anchor, exist_ok=True)
    try:
        with open_pinned_descendant_dir(
            anchor, parts, what="crewmate worktree folder", create=True
        ) as dir_fd:
            if dir_fd is None:
                os.mkdir(dest)  # Windows: the walk lstat-refused every link
                made = os.lstat(dest)
            else:
                name = os.path.basename(dest)
                os.mkdir(name, dir_fd=dir_fd)
                made = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except PinnedPathRefusal as exc:
        raise WorktreeRefused(f"{dest} is not a plain folder: {exc}") from None
    return made.st_dev, made.st_ino


def _drop_made_dest(dest: str, made: tuple[int, int]) -> None:
    """Remove the folder :func:`_make_dest` made at *dest*, and only that folder. Never raises.

    By descriptor throughout (``pinned_fs.remove_tree_pinned``), approved only when
    the folder it opens is the one made (*made*, its ``(dev, ino)``): its parents
    are agent-writable, so one swapped for a link after the make leads elsewhere,
    and whatever is there is refused, not removed. No by-name ``rmtree``. A platform
    that cannot pin (Windows) keeps the folder, logged; the next turn refuses it as
    "exists and is not a worktree" rather than reusing it.
    """
    from kiro_crew import pinned_fs

    def _approve(root_fd: int, _tree: object) -> str | None:
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != made:
            return "not the folder this lookup made"
        return None

    try:
        outcome = pinned_fs.remove_tree_pinned(
            os.path.realpath(dest),
            what="half-made crewmate worktree",
            approve=_approve,
            refusal=OSError,
        )
    except OSError:
        logger.warning("crewmate worktree %s left in place after a failed add", dest, exc_info=True)
        return
    if not outcome.removed:
        logger.warning("crewmate worktree %s only partly removed after a failed add", dest)


def _unwind_add(
    wt, repo: str, dest: str, branch: str, made: tuple[int, int], *, claimed: bool, base_sha: str
) -> None:
    """Undo a failed ``worktree add``: the folder this lookup made, then git's records.

    The folder goes by descriptor and identity (:func:`_drop_made_dest`), never by a
    by-name ``rmtree``: the follow-up card's ``_cleanup_partial`` with
    ``created=True`` falls back to one, which a swapped parent would lead into the
    crew data home. ``_cleanup_partial`` with ``created=False`` then only prunes the
    registration (sandboxed git) and drops the branch this lookup claimed.
    """
    _drop_made_dest(dest, made)
    wt._cleanup_partial(repo, dest, branch, claimed=claimed, created=False, base_sha=base_sha)


def _create(wt, repo: str, dest: str, branch: str, *, anchor: str) -> None:
    """Add *dest* on *branch*: cut the branch first unless it already exists."""
    offending = wt._checkout_filter(repo)
    if offending:
        raise WorktreeRefused(f"the repository configures a checkout filter ({offending})")
    trees = wt._worktree_branches(repo)
    if trees is None:
        raise WorktreeRefused("git could not list this repository's worktrees")
    holder = next((p for p, held in trees.items() if held == branch), None)
    if holder is not None and not os.path.exists(holder):
        # A worktree folder deleted by hand stays registered (git lists it as
        # prunable) and holds its branch: drop such registrations so the
        # worktree is built again. A locked one is kept, and still refuses.
        wt._run_git(["worktree", "prune"], repo)
        trees = wt._worktree_branches(repo) or {}
        holder = next((p for p, held in trees.items() if held == branch), None)
    if holder is not None:
        raise WorktreeRefused(f"{branch} is checked out at {holder}")
    claimed = False
    base_sha = ""
    if not wt._resolve_commit(repo, f"refs/heads/{branch}"):
        base_sha = wt._resolve_commit(repo, _default_branch(wt, repo, cut=True))
        if not base_sha:
            raise WorktreeRefused("the repository has no commit to branch from")
        if not wt._claim_branch(repo, branch, base_sha):
            raise WorktreeRefused(f"could not create {branch}")
        claimed = True
    try:
        made = _make_dest(dest, anchor)
    except FileExistsError:
        if claimed:
            wt._cleanup_partial(repo, dest, branch, claimed=True, created=False, base_sha=base_sha)
        raise WorktreeRefused(f"{dest} already exists") from None
    except (OSError, WorktreeRefused):
        if claimed:
            wt._cleanup_partial(repo, dest, branch, claimed=True, created=False, base_sha=base_sha)
        raise
    try:
        proc = wt._run_git(["worktree", "add", dest, branch], repo)
    except (subprocess.TimeoutExpired, OSError):
        # A timeout (or a lost sandbox) can still leave a registered worktree
        # and a claimed branch behind, which a later turn would reuse half made.
        _unwind_add(wt, repo, dest, branch, made, claimed=claimed, base_sha=base_sha)
        raise
    if proc.returncode != 0 or not os.path.exists(os.path.join(dest, ".git")):
        _unwind_add(wt, repo, dest, branch, made, claimed=claimed, base_sha=base_sha)
        raise WorktreeRefused(wt._git_error(proc) if proc.returncode else "worktree add failed")
    logger.info("crewmate worktree %s on %s from %s", dest, branch, repo)


#: The base-branch note for the next worker turn in a worktree, by worktree path.
#: Process-local: the turn that follows the count runs in this gateway.
_BASE_NOTES: dict[str, str] = {}


#: Local branches tried, in order, when the repository names no ``origin/HEAD``.
_LOCAL_BASES = ("main", "master")  # wokeignore:rule=master


def _default_branch(wt, repo: str, *, cut: bool = False) -> str:
    """The repository's default branch: ``origin/HEAD``, else the first local one of ``_LOCAL_BASES``.

    Not the shared checkout's ``HEAD``: whoever has that checkout open (a person, or a
    worker that stepped out of its worktree) decides what it points at, and a crewmate
    branch measured against another crewmate's branch would be told the wrong thing.
    With neither, ``""``: no base is known, so :func:`note_behind_base` says
    nothing. Only the first cut of a branch (*cut*) falls back to ``HEAD``.

    Always a fully qualified ref (``refs/remotes/origin/HEAD``,
    ``refs/heads/main``): a tag also named ``main`` would win a short name's
    lookup, and the worker would start from, or be measured against, the old tag.
    :func:`_base_label` shortens it for the worker's note only.
    """
    if wt._resolve_commit(repo, "refs/remotes/origin/HEAD"):
        return "refs/remotes/origin/HEAD"
    for name in _LOCAL_BASES:
        ref = f"refs/heads/{name}"
        if wt._resolve_commit(repo, ref):
            return ref
    return "HEAD" if cut else ""


def _base_label(wt, repo: str, ref: str) -> str:
    """The base ref as a person names it: ``main`` rather than ``HEAD`` or ``origin/HEAD``."""
    # The full name, shortened here: ``--abbrev-ref`` says ``heads/main`` when a
    # tag shares the name, and ``origin/HEAD`` resolves to the branch it names.
    proc = wt._run_git(["rev-parse", "--symbolic-full-name", ref], repo)
    full = proc.stdout.strip() if proc.returncode == 0 else ""
    full = full or ref
    for prefix in ("refs/heads/", "refs/remotes/"):
        if full.startswith(prefix):
            return full[len(prefix) :]
    return full


def note_behind_base(entry: CrewmateWorktree) -> str:
    """The note for this turn when *entry*'s branch is behind the default branch, else ``""``.

    Read only: the gateway never rebases, merges or checks anything out in a
    crewmate's worktree. It counts the commits the repository's default branch
    (:func:`_default_branch`, a fully qualified ref) has that the branch lacks
    (``git rev-list --count``) and, when there are any, tells the worker once,
    in the turn's ``[PROJECT]`` line (:func:`take_base_note`), that the branch is
    that far behind and that it rebases itself before it pushes if it needs the
    base's changes. A repository that names no default branch, or git that cannot
    answer, says nothing. Blocking.
    """
    from kiro_crew import crewmate_prompts

    wt = _wt()
    ref = _default_branch(wt, entry.repo)
    if not ref:
        # No default branch the repository names: never the shared checkout's HEAD.
        return ""
    base = wt._resolve_commit(entry.repo, ref)
    if not base:
        return ""
    proc = wt._run_git(["rev-list", "--count", f"HEAD..{base}"], entry.path)
    try:
        count = int(proc.stdout.strip()) if proc.returncode == 0 else 0
    except ValueError:
        count = 0
    if count <= 0:
        return ""
    note = crewmate_prompts.fill(
        "worktree.base_behind",
        branch=rail_text(entry.branch),
        base=rail_text(_base_label(wt, entry.repo, ref)),
        count=count,
    )
    _BASE_NOTES[entry.path] = note
    return note


def take_base_note(path: str) -> str:
    """The base-branch note :func:`note_behind_base` left for worktree *path*, once."""
    return _BASE_NOTES.pop(path, "")


def paths_in(text: str) -> list[str]:
    """Absolute, ``~`` or Windows drive paths named in *text*, in order, at most a bounded few.

    A quoted path is read whole, spaces included (``"/projects/My Repo"``); an
    unquoted one ends at the first space.
    """
    text = text or ""
    found: list[tuple[int, str]] = []
    quoted: list[tuple[int, int]] = []
    for match in _QUOTED_PATH_RE.finditer(text):
        quoted.append(match.span())
        found.append((match.start(), match.group(2).strip()))
    # Windows paths first, so the ``/tools/x`` inside ``C:/tools/x`` is not a path of its own.
    for regex in (_WIN_PATH_RE, _PATH_RE):
        for match in regex.finditer(text):
            if any(a <= match.start() < b for a, b in quoted):
                continue
            quoted.append(match.span())
            found.append((match.start(), match.group(1).rstrip(_PATH_TRAILING)))
    out: list[str] = []
    for _start, candidate in sorted(found):
        if len(candidate) > 1 and candidate not in out:
            out.append(candidate)
        if len(out) >= _MAX_PATHS:
            break
    return out


def _drop_empty_sibling_root(dest: str) -> None:
    """Remove ``<repo>.worktrees`` once its last worktree is gone, never before."""
    parent = os.path.dirname(dest)
    if parent.endswith(SIBLING_SUFFIX):
        with contextlib.suppress(OSError):
            os.rmdir(parent)  # only succeeds when empty: another crewmate's stays


def remove_crewmate_worktrees(workspace_dir: str, slug: str) -> tuple[list[str], list[str]]:
    """Remove the worktrees crewmate *slug*'s index entries list; ``(removed, kept)``.

    ``git worktree remove`` without ``--force``: a worktree with uncommitted
    changes is kept (and listed in ``kept``) rather than deleted. Branches stay.
    The repository's own checkout is never touched, nor is any worktree the
    index does not list for *slug*: another crewmate's beside the same
    repository, or one listed by another crewmate sharing this workspace.
    """
    root = worktrees_root(workspace_dir)
    with _index_lock(root):
        return _remove_listed(_wt(), root, slug)


def _verified_own(wt, workspace: str, slug: str, dest: str, repo: str) -> bool:
    """Whether *dest* is the folder this gateway would make for crewmate *slug* in *repo*.

    Its path is :func:`path_for` or :func:`legacy_path_for` of that repository and
    slug, and, when it still exists, git registers it on one of the crewmate's own
    ``crew/<slug>/`` branches. Anything git cannot answer counts as unverified.
    """
    real = os.path.realpath(dest)
    if real not in {
        os.path.realpath(path_for(repo, slug)),
        os.path.realpath(legacy_path_for(workspace, repo, slug)),
    }:
        return False
    if not os.path.isdir(dest):
        return True  # gone already: only its stale registration is pruned
    try:
        return _own_branch(slug, _registered_branch(wt, repo, dest))
    except Exception:  # noqa: BLE001 - unverifiable: keep it
        logger.warning("crewmate worktree %s: registration unreadable", dest, exc_info=True)
        return False


def _remove_listed(wt, root: str, slug: str) -> tuple[list[str], list[str]]:
    index = _read_index(root)
    mine = _own_entries(index, slug)
    removed: list[str] = []
    kept: list[str] = []
    workspace = os.path.dirname(os.path.normpath(root))
    for dest, entry in sorted(mine.items()):
        repo = entry["repo"]
        if not _verified_own(wt, workspace, slug, dest, repo):
            # The index is agent-written: an entry naming any other folder, or one
            # git does not show on this crewmate's own branch, is kept untouched.
            logger.warning("crewmate worktree %s kept: not verified as %s's own", dest, slug)
            kept.append(dest)
            continue
        if not os.path.isdir(dest):
            removed.append(dest)
            with contextlib.suppress(Exception):
                wt._run_git(["worktree", "prune"], repo)
            _drop_empty_sibling_root(dest)
            continue
        try:
            offending = wt._checkout_filter(dest)
            if offending:
                # ``worktree remove`` checks the files for changes, which can run
                # a clean filter: keep it for the user, as a dirty one is kept.
                logger.warning("crewmate worktree %s kept: checkout filter %s", dest, offending)
                kept.append(dest)
                continue
            proc = wt._run_git(["worktree", "remove", dest], repo)
        except Exception:  # noqa: BLE001 - no sandbox, no git: keep it
            logger.warning("crewmate worktree %s could not be removed", dest, exc_info=True)
            kept.append(dest)
            continue
        if proc.returncode == 0:
            removed.append(dest)
            _drop_empty_sibling_root(dest)
        else:
            logger.warning("crewmate worktree %s kept: %s", dest, wt._git_error(proc))
            kept.append(dest)
    remaining = {k: v for k, v in index.items() if k not in mine or k in kept}
    if mine:
        if remaining:
            _write_index(root, remaining)
        _tidy_index_folder(root, slug, drop_index=not remaining)
    return removed, kept


def _tidy_index_folder(root: str, slug: str, *, drop_index: bool) -> None:
    """Remove the empty fallback folder, and the index when *drop_index*, without following a link.

    Relative to the same no-follow descriptor :func:`_write_index` writes through,
    so a ``worktrees`` swapped for a link (to the governance ``profiles/``, say) has
    nothing below it removed. Never raises.
    """
    from kiro_crew.pinned_fs import PinnedPathRefusal, open_pinned_descendant_dir

    workspace, name = os.path.split(os.path.normpath(root))
    try:
        with open_pinned_descendant_dir(
            workspace, (name,), what="crewmate worktree index folder"
        ) as dir_fd:
            if dir_fd is None:
                # Windows cannot pin: the walk lstat-refused a link, then by name.
                with contextlib.suppress(OSError):
                    os.rmdir(os.path.join(root, _name(slug)))
                if drop_index:
                    with contextlib.suppress(OSError):
                        os.unlink(os.path.join(root, INDEX_FILE))
            else:
                with contextlib.suppress(OSError):
                    os.rmdir(_name(slug), dir_fd=dir_fd)  # the fallback folder, once empty
                if drop_index:
                    with contextlib.suppress(OSError):
                        os.unlink(INDEX_FILE, dir_fd=dir_fd)
    except (PinnedPathRefusal, OSError):
        logger.warning("crewmate worktree index folder %s left as it is", root, exc_info=True)
        return
    if drop_index:
        with contextlib.suppress(OSError):
            os.rmdir(root)  # rmdir never follows a link, and only takes an empty folder


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
        logger.warning(
            "kept %d worktree(s) of %s with uncommitted changes: %s", len(kept), name, kept
        )
