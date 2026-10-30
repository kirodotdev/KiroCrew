"""``GET /api/project/tree``: the workspace file listing for the Files tab."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import ntpath
import os
import posixpath
import stat as _stat_mod
from collections.abc import Callable, Iterator
from pathlib import PurePath
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _PROJECT_TREE_MAX_ENTRIES,
        _PROJECT_TREE_SCAN_LIMIT,
        _PROJECT_TREE_SCANDIR_TAKES_FD,
        _PROJECT_TREE_SKIP_DIRS,
        DashboardState,
        _match_known_project_for,
        _redact_project_path,
        _run_git_bounded,
        _sel,
        _slot_project_snapshot,
        is_sensitive_path,
        is_sensitive_resolved_path,
        path_contains_sensitive,
        pinned_fs,
        platform_compat,
        redact,
        redact_path_segments,
    )


def _project_tree_fence(dirpath: str) -> Callable[[str], bool]:
    """Whether the non-git tree walk drops an entry of *dirpath* as fenced.

    Dot-directories are walked (``.worktrees`` holds checkouts users navigate
    to). A dot-name, and every entry beneath a dot-directory, is checked
    against the sensitive-path fence on its real path, since the stores it
    fences (``.aws``, ``.config/gcloud``, ``.docker/config.json``, ...) live
    under dot-names. "Beneath a dot-directory" is read off the real path of
    *dirpath*, ancestors above the project root included, so a project rooted
    inside one (``~/.config``) is fenced too. Built once per folder the walk
    reads; runs on the walk's worker thread, so the pre-resolved gate answers
    inline.

    What the gate costs is paid per CALL: it resolves its own anchors --
    ``$HOME``, the override roots, the keystone leaves -- every time, then
    compares the candidate against every resolved target. A call per entry
    therefore pays both of those per entry, and under a dot-named root
    ``under_dot`` holds for every directory, so every entry in the project is a
    candidate. Most of them are settled by two questions about the DIRECTORY,
    asked once: whether it is itself inside a store, and whether a store lies
    beneath it. When neither holds, a name whose real path is this directory's
    own real path plus that name cannot spell a fenced path. That settles the
    publish-artifact clause with it: a directory holding a keystone leaf holds a
    sensitive target as well, so it is never one of these. When either holds,
    the directory's entries are asked about one at a time.

    Which entries that covers is read off the ``realpath`` the fence needs
    anyway, never off a separate link check. An entry whose real path is the
    expected join resolved to itself: no component of it was a link, so the
    directory's answer binds it. One whose real path came back different led
    somewhere else, and the gate answers on the path it actually led to. The
    resolve is therefore the only question asked of the filesystem, which is
    what makes a link planted mid-walk harmless rather than a window: there is
    no earlier verdict about the name for the resolve to contradict.

    The comparison is exact, and a mismatch it did not mean is safe by
    construction: it costs the entry its shortcut and sends it to the gate,
    which is the answer the walk gave every entry before. That is what makes it
    sound on a case-insensitive host, where ``realpath`` may hand back the
    on-disk spelling of a name -- the names come from the directory listing, so
    they already carry that spelling and match, and a host that spells one
    differently anyway loses a shortcut rather than a check.
    """
    real_dirpath = os.path.realpath(dirpath)
    under_dot = any(part.startswith(".") for part in PurePath(real_dirpath).parts)
    # Decided once for the whole directory: whether an entry that stays inside it
    # still has to be asked about on its own. Both calls compare lists against
    # the resolved targets and walk nothing.
    ask_per_entry = is_sensitive_resolved_path(real_dirpath) or path_contains_sensitive(
        real_dirpath, pre_resolved=True
    )

    def fenced(name: str) -> bool:
        if not (under_dot or name.startswith(".")):
            return False
        resolved = os.path.realpath(os.path.join(dirpath, name))
        if not ask_per_entry and resolved == os.path.join(real_dirpath, name):
            return False
        return is_sensitive_resolved_path(resolved)

    return fenced


def _project_tree_walk_order(directory: str) -> tuple[int, list[str]]:
    """Sort key for directory rows: by depth, then segment by segment.

    The walk's own order, which reads ``a/z`` before ``a-b/c`` because ``a``
    sorts before ``a-b``. Git paths are POSIX on every platform.
    """
    segments = directory.split(posixpath.sep)
    return len(segments), segments


def _project_tree_git_layout(listed: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Directory rows and files by direct parent for a git listing.

    A directory row exists only as an ancestor of a listed file. Rows are
    ordered shallowest first, as the walk discovers them, so the row cap in
    :func:`_project_tree_allot` is spent on the same folders on both branches.
    """
    files: dict[str, list[str]] = {}
    directories: set[str] = set()
    for path in listed:
        parent, _, name = path.rpartition("/")
        files.setdefault(parent, []).append(name)
        # An ancestor already seen has had its own ancestors added with it.
        while parent and parent not in directories:
            directories.add(parent)
            parent = posixpath.dirname(parent)
    return sorted(directories, key=_project_tree_walk_order), files


def _project_tree_add_folder_rows(rows: list[str], folders: set[str]) -> list[str]:
    """*rows* with *folders* and their ancestors added, in the same order.

    A nested repository is a folder with no listed file beneath it
    (:func:`_project_tree_nested_repos`), so :func:`_project_tree_git_layout`
    gives it no row of its own.
    """
    directories = set(rows)
    for folder in folders:
        while folder and folder not in directories:
            directories.add(folder)
            folder = posixpath.dirname(folder)
    return sorted(directories, key=_project_tree_walk_order)


def _project_tree_file_quotas(file_counts: dict[str, int], limit: int) -> dict[str, int]:
    """Split *limit* round-robin across directories that directly own files."""
    quotas = {directory: 0 for directory in file_counts}
    active = sorted(directory for directory, count in file_counts.items() if count > 0)
    remaining = min(max(limit, 0), sum(file_counts.values()))
    while active and remaining:
        next_active: list[str] = []
        for directory in active:
            if remaining == 0:
                break
            quotas[directory] += 1
            remaining -= 1
            if quotas[directory] < file_counts[directory]:
                next_active.append(directory)
        active = next_active
    return quotas


def _project_tree_allot(
    rows: list[str], files: dict[str, list[str]], incomplete: set[str], cap: int
) -> tuple[list[str], set[str], list[str], list[str]]:
    """Spend *cap* rows on directory *rows* and *files* together.

    *rows* lists every parent before its children; *files* maps a directory
    (``""`` is the root) to the names directly in it; *incomplete* names rows
    whose own listing is not complete.

    Folder rows are spent first, shallowest first, because a folder row is what
    lets the user navigate to the rest -- but never past half the cap while
    there are files to show, so a tree with more folders than the cap still
    lists files, the root's ``README.md`` first among them. Half is the floor
    for each side, not a split: what one side leaves unused goes to the other.
    The files share their rows round-robin by direct parent, the root first
    (:func:`_project_tree_file_quotas`), so no one folder takes every row; a
    file whose folder lost its row is not listed either.

    Returns the shown rows, the shown directory set (rows plus the root), the
    shown file paths, and the sorted shown directories whose listing is cut
    short -- files over their quota, a child row over the cap, or an
    *incomplete* listing. The listing is truncated exactly when that last list
    is non-empty.
    """
    total_files = sum(len(names) for names in files.values())
    row_count = min(len(rows), max(cap // 2, cap - total_files))
    shown = {"", *rows[:row_count]}
    file_counts = {
        directory: len(names) for directory, names in files.items() if directory in shown
    }
    quotas = _project_tree_file_quotas(file_counts, cap - row_count)
    # Rows the files could not use -- some counted files sit in folders past the
    # row budget -- go back to folder rows. Those folders' files are not
    # listed: sharing the spare rows with them would evict files that fit.
    spare = cap - row_count - sum(quotas.values())
    if spare > 0 and row_count < len(rows):
        row_count = min(len(rows), row_count + spare)
        shown = {"", *rows[:row_count]}
    shown_rows = rows[:row_count]
    # Every parent precedes its children, so a row over the budget has a shown
    # parent or a parent over the budget as well; only the shown one is marked.
    truncated = {
        parent for parent in (posixpath.dirname(row) for row in rows[row_count:]) if parent in shown
    }
    truncated.update(directory for directory in incomplete if directory in shown)
    truncated.update(
        directory
        for directory, names in files.items()
        if directory in shown and quotas.get(directory, 0) < len(names)
    )
    paths = [
        (f"{directory}/{name}" if directory else name)
        for directory in file_counts
        for name in files[directory][: quotas[directory]]
    ]
    return shown_rows, shown, paths, sorted(truncated)


class _ProjectTreeFolderMoved(Exception):
    """The folder at a queued row's path is not the one its parent listed."""


def _project_tree_is_link(st: os.stat_result) -> bool:
    """Whether an ``lstat`` result is a link to another name.

    A symlink, or a Windows name-surrogate reparse point (a junction, a
    symlink). A reparse directory without that bit -- a cloud-files
    placeholder, a dedup directory -- holds its own contents and is walked.
    """
    return _stat_mod.S_ISLNK(st.st_mode) or platform_compat.lstat_is_name_surrogate(st)


def _project_tree_scandir(
    native: str, identity: tuple[int, int] | None
) -> contextlib.AbstractContextManager[Iterator[os.DirEntry[str]]]:
    """``os.scandir`` of the folder a queued row names, as a context manager.

    The reading is :func:`_project_tree_scandir_entries`. It is wrapped here, at
    call time, rather than decorated: a decorated owner function is the
    decorator's wrapper, which ``_facade_compose.compose`` does not rebind, so its body
    would read this owner's globals instead of the handlers module's.
    """
    return contextlib.contextmanager(_project_tree_scandir_entries)(native, identity)


def _project_tree_scandir_entries(
    native: str, identity: tuple[int, int] | None
) -> Iterator[Iterator[os.DirEntry[str]]]:
    """``os.scandir`` of the folder a queued row names, and only that folder.

    A row is queued by name, and its read happens one breadth-first level later,
    so the name can be swapped in between -- renamed away, with a symlink to a
    directory outside the project put in its place. The read is therefore pinned
    to the folder's identity, the ``(st_dev, st_ino)`` its parent's listing
    recorded (*identity*; ``None`` for the root, whose path is resolved already).

    POSIX opens the name with ``O_DIRECTORY | O_NOFOLLOW``, so a link at the name
    fails, ``fstat``s the descriptor, and lists THAT descriptor: the folder read is
    the one compared, whatever the path names by then. An ancestor swapped for a
    link reaches a different inode and fails the comparison the same way. One
    descriptor is held per folder being read, never one per queued row.

    Windows has no descriptor ``scandir``: the name is ``lstat``ed and compared,
    and a link refused, before it is listed by path. A swap landing
    between that check and the ``scandir`` is not caught -- the residual window
    of a platform that lists only by name.

    Raises :class:`_ProjectTreeFolderMoved` when the folder is not the one
    recorded; any other ``OSError`` is the read failing.
    """
    if _PROJECT_TREE_SCANDIR_TAKES_FD and pinned_fs.supports_pinned_walk():
        try:
            fd = os.open(native, platform_compat.pinned_dir_flags() | getattr(os, "O_CLOEXEC", 0))
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise _ProjectTreeFolderMoved(native) from exc
            raise
        try:
            st = os.fstat(fd)
            if identity is not None and (st.st_dev, st.st_ino) != identity:
                raise _ProjectTreeFolderMoved(native)
            with os.scandir(fd) as entries:
                yield entries
        finally:
            os.close(fd)
        return
    if identity is not None:
        st = os.lstat(native)
        if _project_tree_is_link(st) or (st.st_dev, st.st_ino) != identity:
            raise _ProjectTreeFolderMoved(native)
    with os.scandir(native) as entries:
        yield entries


def _project_tree_identity(entry: os.DirEntry[str], native: str) -> os.stat_result:
    """The ``lstat`` of a listed entry, with a real ``st_dev`` / ``st_ino``.

    A descriptor listing answers from the open folder. A Windows ``DirEntry``
    reports zero for both, so that platform asks the path.
    """
    if _PROJECT_TREE_SCANDIR_TAKES_FD:
        return entry.stat(follow_symlinks=False)
    return os.lstat(native)


def _project_tree_walk(base: str, cap: int, scan_limit: int) -> dict:
    """The non-git listing of *base*: :func:`_project_tree_walk_pinned` from an unpinned root."""
    return _project_tree_walk_pinned(base, cap, scan_limit, None)


def _project_tree_walk_pinned(
    base: str, cap: int, scan_limit: int, root_identity: tuple[int, int] | None
) -> dict:
    """The non-git listing of *base*: one bounded breadth-first pass.

    *root_identity* is the ``(st_dev, st_ino)`` *base* must still have when it
    is read (``None``: *base* is the resolved project root itself). A folder
    listed on its own passes the identity :func:`_project_tree_pin` read, so a
    folder whose path an ancestor swap now points elsewhere is named
    unreadable rather than read.

    Reads at most *scan_limit* directory entries, each folder at most once, and
    returns at most *cap* rows -- directory rows and files together, spent by
    :func:`_project_tree_allot`. Breadth-first rather than depth-first, because
    the cap is spent in discovery order: depth-first, the first deep subtree
    would take every row and the top-level folders after it would vanish.

    The read budget is shared so that no one folder can starve its siblings of
    it: the folders of one depth and the depths below (one more claimant) are
    each guaranteed a floor, half the even split the depth starts with, and the
    folders are read in order, each up to everything the claimants after it do
    not need for their floors -- capped at *cap* entries, more than one folder
    could show -- and never less than an even split of what is left. So a tree
    of at most about half *scan_limit* entries is read whole when no folder of
    it holds more than *cap*, wherever its large folder sorts. A folder holding
    more than its share is cut there; the entries it did not read are unknown,
    so it is named as truncated rather than listed as if it were whole. Telling
    a full folder from a cut one costs one look-ahead entry, drawn from the
    same budget, so no more than *scan_limit* entries are ever read. A folder
    whose row falls past *cap* is never read, and neither is anything after
    it: nothing read there could be shown.

    Each queued folder is read only if it is still the folder its parent
    listed (:func:`_project_tree_scandir`); one swapped for another directory
    or a link in between is shown as a row and named as unreadable -- a row
    that claims nothing about its contents -- and nothing behind it is listed.

    *base* is the native path, and each folder carries its own native path
    alongside its project-relative row: an extended-length ``\\\\?\\`` root on
    Windows does not convert a ``/`` inside a joined path.
    """
    # Directory rows in discovery (breadth-first) order. A folder is a row as
    # soon as its parent's read names it, so a folder the walk then cannot
    # read, or never reaches, is still shown.
    rows: list[str] = []
    # Symlinks (and Windows junctions) to directories, listed as rows of their
    # own: ``ls`` shows them, but the walk never follows one (against link
    # cycles), so nothing beneath it is listed and the dashboard says so
    # beneath its row rather than calling the link -- or the folder holding
    # only links -- empty. The name filter applies to links and real folders
    # alike: what a folder shows must be predictable from the NAME alone, so a
    # ``node_modules`` that is a link to another disk is as hidden as its real
    # twin.
    linked: list[str] = []
    # Rows the walk could not read, whether ``scandir`` refused the folder, its
    # listing failed part-way, the folder at the name is not the one its
    # parent listed, or the ``lstat`` recording a listed child's identity
    # failed (permission denied is the usual cause; any failure counts, because
    # the parent listed the entry and a row that claims nothing about its
    # contents is the honest rendering). The root failing is named as ``.``: it
    # is no row, and the payload would otherwise be indistinguishable from an
    # empty workspace.
    unreadable: list[str] = []
    # Fully read folders the listing leaves CHILDLESS although they are not
    # empty on disk: every entry is one the filter drops by nature (a
    # ``_PROJECT_TREE_SKIP_DIRS`` directory or a fenced entry, see
    # :func:`_project_tree_fence`) and no entry is kept. ``_bg/`` holding only ``.kiro/`` is the reported case. The root is
    # judged by the same rule and named as ``.``. A folder the budget cut is
    # never judged: what it did not read is unknown, and it is truncated.
    hidden_only: list[str] = []
    # Read folders and the file names they hold, in read order.
    files: dict[str, list[str]] = {}
    # Shown rows whose listing is not complete: the budget cut their read or
    # ran out before the walk reached them, or a child's row fell past the cap.
    incomplete: set[str] = set()
    # (row, native path, identity) of every folder of the depth being read; the
    # identity is the ``(st_dev, st_ino)`` the parent's listing saw (``None`` for
    # the root). Only rows inside the cap are ever queued.
    level: list[tuple[str, str, tuple[int, int] | None]] = [("", base, root_identity)]
    budget = scan_limit
    while level:
        deeper: list[tuple[str, str, tuple[int, int] | None]] = []
        # What each claimant of this depth -- every folder of it, and the depths
        # below as one more -- is guaranteed whatever the folders before it
        # hold: half the even split the depth starts with.
        floor = -(-budget // (2 * (len(level) + 1)))
        for position, (directory, native, identity) in enumerate(level):
            if budget <= 0:
                incomplete.update(queued[0] for queued in level[position:])
                break
            claimants = len(level) - position + 1
            # A folder may read everything the claimants after it do not need
            # for their floor, up to *cap* entries -- more than it could ever
            # show -- so a small tree is read whole wherever its one large
            # folder sorts, and a huge folder sorted first still leaves every
            # sibling its floor. Never less than an even split of what is left
            # (ceiling division, so the first folders still get an entry each
            # when the budget is smaller than their number).
            share = max(-(-budget // claimants), min(cap, budget - (claimants - 1) * floor))
            # One entry past the share is pulled to tell a full folder from a cut
            # one, and it is drawn from the same budget: no read outlives it.
            allowance = min(share + 1, budget)
            names: list[str] = []
            kept: list[os.DirEntry[str]] = []
            fenced = _project_tree_fence(native)
            had_entries = False
            complete = True
            pulled = 0
            try:
                with _project_tree_scandir(native, identity) as entries:
                    listing = iter(entries)
                    while True:
                        if pulled == allowance:
                            complete = False
                            break
                        entry = next(listing, None)
                        if entry is None:
                            break
                        pulled += 1
                        if pulled > share:
                            complete = False
                            break
                        # ``os.walk``'s classification: a link to a directory is
                        # a directory, and an entry whose type cannot be read is
                        # a file.
                        try:
                            is_dir = entry.is_dir()
                        except OSError:
                            is_dir = False
                        had_entries = True
                        if not is_dir:
                            if not fenced(entry.name):
                                names.append(entry.name)
                            continue
                        if entry.name not in _PROJECT_TREE_SKIP_DIRS and not fenced(entry.name):
                            kept.append(entry)
                    files[directory] = sorted(names)
                    prefix = f"{directory}/" if directory else ""
                    for kept_entry in sorted(kept, key=lambda e: e.name):
                        # A row past the cap is never shown, never read and costs
                        # no ``lstat``; its parent is named as truncated instead.
                        if len(rows) >= cap:
                            incomplete.add(directory)
                            break
                        child = prefix + kept_entry.name
                        rows.append(child)
                        child_native = os.path.join(native, kept_entry.name)
                        try:
                            st = _project_tree_identity(kept_entry, child_native)
                        except OSError:
                            # Listed but not stat-able -- a parent readable but
                            # not searchable, or the entry gone since: a row
                            # nothing read, which is what unreadable means.
                            unreadable.append(child)
                            continue
                        if _project_tree_is_link(st):
                            linked.append(child)
                        else:
                            deeper.append((child, child_native, (st.st_dev, st.st_ino)))
            except (_ProjectTreeFolderMoved, OSError):
                # Refused, failed part-way, or not the folder its parent
                # listed (what is there now is not this row's content): nothing
                # of it is read, and the row claims nothing about its contents.
                budget -= pulled
                files.pop(directory, None)
                unreadable.append(directory or ".")
                continue
            budget -= pulled
            if not complete:
                incomplete.add(directory)
            elif had_entries and not names and not kept:
                hidden_only.append(directory or ".")
        # A budget spent part-way through a depth leaves ``deeper`` holding rows
        # nobody will read; the next pass names them incomplete and stops.
        level = deeper

    shown_rows, shown, paths, truncated = _project_tree_allot(rows, files, incomplete, cap)
    return {
        "root": base,
        "paths": paths,
        "directories": shown_rows,
        "repo": False,
        "truncated": bool(truncated),
        "truncatedDirectories": truncated,
        "hiddenOnlyDirectories": [d for d in hidden_only if d == "." or d in shown],
        "unreadableDirectories": [d for d in unreadable if d == "." or d in shown],
        "linkedDirectories": [d for d in linked if d in shown],
        "nestedRepoDirectories": [],
    }


def _project_tree_body(result: dict) -> str:
    """The redacted JSON body for an ``api_project_tree`` listing.

    Egress redaction, same rationale as ``api_project_git_status``: listed names
    are repo content and this body is rendered by the dashboard. ``root`` is an
    absolute path and takes the path-aware redactor; every list entry goes
    through ``redact_path_segments`` over the same context-aware ``redact()``.
    The whole-string ``redact()`` collapses each matched token to a fixed
    placeholder, so two genuinely-different paths whose only differing segment
    is credential-shaped would redact to one string; the helper suffixes every
    redacted segment with an opaque label keyed per gateway process, so both
    stay in the tree, it never emits less redaction than ``redact()`` itself,
    and the label is stable across responses within this process, so the git
    status listing labels the same path identically and the dashboard's join by
    path holds.

    Then each list is de-duplicated, preserving order and first occurrence, as
    the fallback for a collision the helper does not separate: the dashboard
    tree hands this list straight to @pierre/trees, whose appendPresortedPaths
    throws "Duplicate path" on adjacent identical entries. This does not affect
    ``truncated``: the cap is applied to the raw listing.
    """
    result["root"] = _redact_project_path(result["root"])
    for key in (
        "paths",
        "directories",
        "truncatedDirectories",
        "hiddenOnlyDirectories",
        "unreadableDirectories",
        "linkedDirectories",
        "nestedRepoDirectories",
    ):
        result[key] = list(dict.fromkeys(redact_path_segments(p, redact) for p in result[key]))
    # A folder listing echoes the folder it lists: a project-relative path like
    # every list entry, so it takes the same redactor and stays joinable to them.
    if "dir" in result:
        result["dir"] = redact_path_segments(result["dir"], redact)
    return json.dumps(result)


async def api_project_tree(request: web.Request) -> web.Response:
    """GET /api/project/tree?path=... - workspace file listing for a project dir.

    Returns project-relative POSIX file paths for rendering a workspace tree.
    Inside a git repository the listing is ``git ls-files --cached --others
    --exclude-standard`` scoped to the project dir (tracked + untracked,
    .gitignore honored); outside one, or when that listing fails, it is one
    bounded breadth-first walk (:func:`_project_tree_walk`) that caps files and
    directory rows together. Path must match a known project directory (same
    allow-list as api_project_git).

    ``dir`` (optional) lists one folder of that project instead of the whole
    tree -- a folder the whole listing named in ``truncatedDirectories`` --
    under the same row cap, with its paths still project-relative and the
    folder echoed as ``dir``. It is checked after, and in addition to, every
    check on ``path`` (:func:`_project_tree_subdir`): a folder that is not a
    plain, link-free path strictly inside the project root is refused.
    """
    state: DashboardState = request.app["state"]
    caller = request.get("user", "dashboard")
    raw = request.query.get("path", "").strip()
    if not raw:
        return web.json_response({"error": "path required", "code": "path_required"}, status=400)
    project = await asyncio.to_thread(_match_known_project_for, _slot_project_snapshot(state), raw)
    if project is None:
        _sel().log_api_access(
            caller=caller,
            operation="project_tree",
            outcome="denied",
            resources=raw,
            error="not a known project directory",
        )
        return web.json_response(
            {"error": "Unknown project directory", "code": "unknown_project_dir"}, status=403
        )

    base = await asyncio.to_thread(lambda: os.path.realpath(os.path.expanduser(project)))
    if await asyncio.to_thread(is_sensitive_path, base):
        _sel().log_api_access(
            caller=caller,
            operation="project_tree",
            outcome="denied",
            resources=base,
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied", "code": "access_denied"}, status=403)
    _sel().log_api_access(
        caller=caller, operation="project_tree", outcome="allowed", resources=base
    )
    if not await asyncio.to_thread(os.path.isdir, base):
        return web.json_response(
            {
                "root": _redact_project_path(base),
                "paths": [],
                "directories": [],
                "repo": False,
                "truncatedDirectories": [],
                "hiddenOnlyDirectories": [],
                "unreadableDirectories": [],
                "linkedDirectories": [],
                "nestedRepoDirectories": [],
            }
        )

    # ``?dir=``: one folder's own listing, for a folder the whole-project
    # listing named in ``truncatedDirectories``. Every check above has already
    # run on the project root exactly as before; this only narrows the listing
    # to a folder that must resolve, link-free, to a path inside that root.
    raw_dir = request.query.get("dir", "")
    listing_root = base
    prefix = ""
    identity: tuple[int, int] | None = None
    if raw_dir:
        listing_root, prefix, identity, refusal = await asyncio.to_thread(
            _project_tree_subdir, base, raw_dir
        )
        if refusal:
            _sel().log_api_access(
                caller=caller,
                operation="project_tree",
                outcome="denied",
                resources=f"{base} dir={raw_dir}",
                error=refusal,
            )
            status = 404 if refusal == "dir_not_found" else 403
            return web.json_response(
                {"error": "Folder cannot be listed", "code": refusal}, status=status
            )

    def _run() -> dict:
        if not prefix:
            return _project_tree_listing(base)
        result = _project_tree_listing(base, prefix, listing_root, identity)
        return _project_tree_rebase(result, base, prefix)

    # The listing, its redaction and its serialization all run in ONE worker
    # thread: each is linear in the size of the listing, and the dashboard
    # refetches this tree every 10 s while it is open, so any of them on the
    # event loop stalls every other request the gateway is serving.
    body = await asyncio.to_thread(lambda: _project_tree_body(_run()))
    return web.Response(text=body, content_type="application/json")


def _project_tree_subdir(base: str, raw: str) -> tuple[str, str, tuple[int, int] | None, str]:
    """Resolve ``?dir=`` *raw* to a folder inside the project root *base*.

    Returns ``(native_path, posix_prefix, identity, refusal)``; *refusal* is
    empty when the folder may be listed. *raw* is a project-relative POSIX
    path, exactly as the listing names a directory row. Refused
    (``dir_refused``) when it is absolute, carries an empty, ``.`` or ``..`` segment
    or a NUL, or on Windows a drive, a backslash or a colon; when its real
    path is not the plain join of *base* and its segments -- any link along
    it, which is also every way out of the root -- or is not strictly inside
    *base*; and when it is a sensitive path. A name that is not a directory is
    ``dir_not_found``.

    *identity* is the folder's ``(st_dev, st_ino)``, read by descending from
    *base* one segment at a time without following a link
    (:func:`_project_tree_pin`). The listing reads the folder only if it is
    still that one, so an ancestor swapped for a link after this check cannot
    point the listing outside the project.
    """
    refused: tuple[str, str, tuple[int, int] | None, str] = (base, "", None, "dir_refused")
    if "\0" in raw or raw.startswith("/"):
        return refused
    # A drive, a backslash and a colon mean something only to Windows; on POSIX
    # they are ordinary name characters (a folder named ``a:b`` is listable).
    if os.sep != "/" and (ntpath.splitdrive(raw)[0] or "\\" in raw or ":" in raw):
        return refused
    segments = raw.split(posixpath.sep)
    if any(segment in ("", ".", "..") for segment in segments):
        return refused
    target = os.path.join(base, *segments)
    real = os.path.realpath(target)
    if os.path.normcase(real) != os.path.normcase(os.path.normpath(target)):
        return refused
    try:
        inside = os.path.commonpath([real, base]) == base and real != base
    except ValueError:
        inside = False
    if not inside or is_sensitive_path(real):
        return refused
    if not os.path.isdir(real):
        return (base, "", None, "dir_not_found")
    identity = _project_tree_pin(base, segments)
    if identity is None:
        return refused
    return (real, posixpath.join(*segments), identity, "")


def _project_tree_pin(base: str, segments: list[str]) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of *base* / *segments*, reached without a link.

    Descends with :func:`pinned_fs.open_pinned_descendant_dir`, which refuses a
    link at every component: by descriptor where the platform can pin, by
    ``lstat`` per prefix where it cannot (Windows, with that helper's documented
    by-name residual). ``None`` when the folder cannot be reached that way.
    """
    try:
        with pinned_fs.open_pinned_descendant_dir(base, segments, what="project folder") as fd:
            st = os.lstat(os.path.join(base, *segments)) if fd is None else os.fstat(fd)
    except (pinned_fs.PinnedPathRefusal, OSError):
        return None
    return (st.st_dev, st.st_ino)


def _project_tree_rebase(result: dict, base: str, prefix: str) -> dict:
    """Re-anchor a listing of the folder *prefix* onto the project root *base*.

    Every list names paths relative to the folder that was listed; they are
    made project-relative, so a folder's listing merges into the whole-project
    one. The listed folder's own marker (``""`` or ``.``) becomes *prefix*.
    """

    def under(path: str) -> str:
        return f"{prefix}/{path}" if path not in ("", ".") else prefix

    for key in (
        "paths",
        "directories",
        "truncatedDirectories",
        "hiddenOnlyDirectories",
        "unreadableDirectories",
        "linkedDirectories",
        "nestedRepoDirectories",
    ):
        result[key] = [under(p) for p in result[key]]
    result["root"] = base
    result["dir"] = prefix
    return result


def _project_tree_has_gitmodules(base: str, probe_out: str) -> bool:
    """Whether the repository holding *base* can have a submodule at all.

    A submodule is a gitlink, and a gitlink is named in ``.gitmodules`` at the
    top of the repository. *probe_out* is the ``git rev-parse --git-dir
    --show-cdup`` output: its second line is the way up from *base* to that top
    (empty at the top itself, and in a bare repository). Looking for the file
    costs one ``lstat``; the index read that finds the gitlinks costs a
    sandboxed git run, which a repository without submodules never needs.
    """
    lines = probe_out.split("\n")
    cdup = lines[1].strip() if len(lines) > 1 else ""
    top = os.path.normpath(os.path.join(base, cdup)) if cdup else base
    return os.path.lexists(os.path.join(top, ".gitmodules"))


def _project_tree_nested_repos(
    base: str,
    subdir: str,
    listed: list[str],
    read_gitlinks: bool,
) -> tuple[list[str], set[str]]:
    """Split a git listing into plain files and the nested repositories in it.

    ``git ls-files`` names a nested repository as ONE entry, never its contents:
    a submodule is a gitlink (mode 160000), printed like a file, and an
    untracked nested clone is printed with a trailing slash. Both are folders
    on disk, so listing them as files gives a row that cannot be opened. The
    gitlinks come from a second, index-only read (``--stage`` carries the mode;
    the listing itself must not, it also names untracked files), made only when
    *read_gitlinks* says the repository has a ``.gitmodules``. A failed read
    leaves them as the files they were listed as, which is the previous
    behaviour. Returns the files and the nested repositories, both relative to
    *subdir* like *listed*.
    """
    nested = {path.rstrip("/") for path in listed if path.endswith("/")}
    if read_gitlinks:
        stage_rc, stage_out, _ = _run_git_bounded(
            [
                "git",
                "-c",
                "core.fsmonitor=",
                "ls-files",
                "-z",
                "--stage",
                *(["--", subdir] if subdir else []),
            ],
            cwd=base,
            env={**os.environ, "GIT_LITERAL_PATHSPECS": "1"} if subdir else os.environ.copy(),
            timeout=15,
        )
        if stage_rc == 0:
            cut = f"{subdir}/" if subdir else ""
            for record in stage_out.split("\0"):
                meta, _, path = record.partition("\t")
                if meta.startswith("160000 ") and path.startswith(cut):
                    nested.add(path[len(cut) :])
    return [p for p in listed if p.rstrip("/") not in nested], nested


def _project_tree_in_nested_repo(base: str, subdir: str) -> bool:
    """Whether the folder *subdir* of *base* is, or sits inside, a nested repository.

    A nested repository is any folder between *base* and *subdir* (the folder
    itself included) that has a ``.git`` of its own. The project's git cannot
    list one: for a submodule it names the gitlink and nothing beneath it, and
    for a path beneath one it refuses ("is in submodule"). Such a folder is
    therefore listed by the identity-checked walk instead.
    """
    segments = PurePath(subdir).parts
    return any(
        os.path.lexists(os.path.join(base, *segments[:depth], ".git"))
        for depth in range(1, len(segments) + 1)
    )


def _project_tree_listing(
    base: str,
    subdir: str = "",
    native: str = "",
    identity: tuple[int, int] | None = None,
) -> dict:
    """The tree listing of *base*, or of its folder *subdir*: git, else the walk.

    For *subdir* the paths come back relative to that folder. git runs from the
    project root *base* with *subdir* as a literal pathspec, never with the
    folder as its working directory: git does not descend through a link, so an
    ancestor swapped for one lists nothing, where a ``cwd`` would follow it. On
    a platform that cannot pin by descriptor (Windows, where git walks into a
    junction) a folder listing uses the walk alone, as does a folder that is, or
    sits inside, a nested repository: the project's git cannot list inside one.
    The walk reads the folder's *native* path pinned to *identity*
    (:func:`_project_tree_pin`), so a folder that is not the checked one by then
    is named unreadable and nothing behind it is read.
    """
    # git listing first: honors .gitignore, includes tracked-but-deleted
    # files (they render with a deleted status lane), and with cwd=base a
    # project dir that is a repo SUBDIRECTORY lists only its own subtree.
    # -z: NUL separation, so no C-quoting and exotic names survive intact.
    # git does not descend through a symlink, so a pathspec past a swapped
    # ancestor lists nothing on POSIX -- but on Windows it walks into a
    # junction. Where the walk cannot pin a folder by descriptor, a folder
    # listing therefore takes the pinned walk (identity-checked) instead of git.
    in_nested = bool(subdir) and _project_tree_in_nested_repo(base, subdir)
    git_safe = not subdir or (
        _PROJECT_TREE_SCANDIR_TAKES_FD and pinned_fs.supports_pinned_walk() and not in_nested
    )
    # `--show-cdup` also tells where the repository's top is, so the gitlink
    # read below can be skipped for a repository that has no `.gitmodules`.
    probe_rc, probe_out, _ = (
        _run_git_bounded(
            ["git", "rev-parse", "--git-dir", "--show-cdup"],
            cwd=base,
            env=os.environ.copy(),
            timeout=5,
        )
        if git_safe
        else (1, "", "")
    )
    if probe_rc == 0:
        ls_rc, ls_out, _ = _run_git_bounded(
            # `core.fsmonitor=` disables the filesystem-monitor hook: it names a
            # command git would SPAWN, and it is repository-writable, so an agent
            # that can write `.git/config` could otherwise have a tree listing
            # execute it. Empty rather than `false` to match the sibling git
            # invocations in the file API. The `rev-parse` probe above needs no
            # such guard — it reads no index and walks no working tree.
            [
                "git",
                "-c",
                "core.fsmonitor=",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                *(["--", subdir] if subdir else []),
            ],
            cwd=base,
            # A literal pathspec: a folder name is a name, never a glob.
            env={**os.environ, "GIT_LITERAL_PATHSPECS": "1"} if subdir else os.environ.copy(),
            timeout=15,
        )
        if ls_rc == 0:
            # Git emits tracked and untracked files in separate blocks and
            # documents no combined order. Sort once, then spend the row cap
            # the way the walk does (``_project_tree_allot``): folder rows
            # first but files keep at least half, the files round-robin
            # across direct parent directories, so a large subtree cannot
            # consume every file row.
            listed = sorted(p for p in ls_out.split("\0") if p)
            if subdir:
                # Relative to the folder, as the walk's paths are; git names
                # nothing outside the pathspec.
                cut = f"{subdir}/"
                listed = [p[len(cut) :] for p in listed if p.startswith(cut)]
            # The gitlink read runs after the listing and only for a repository
            # that has a `.gitmodules`: each git run costs a sandbox start, and
            # a repository without submodules has no gitlink to find.
            listed, nested = _project_tree_nested_repos(
                base, subdir, listed, _project_tree_has_gitmodules(base, probe_out)
            )
            rows, files = _project_tree_git_layout(listed)
            rows = _project_tree_add_folder_rows(rows, nested)
            shown_rows, shown, selected_paths, truncated_directories = _project_tree_allot(
                rows, files, set(), _PROJECT_TREE_MAX_ENTRIES
            )
            return {
                "root": base,
                "paths": selected_paths,
                "directories": shown_rows,
                "repo": True,
                "truncated": bool(truncated_directories),
                "truncatedDirectories": truncated_directories,
                # A directory row exists here only as the parent of a listed
                # file or as a nested repository, so an ignored-only folder is
                # absent rather than childless; the only childless directories
                # this branch can produce are a truncated one, reported above,
                # and a nested repository, reported below. The same holds
                # for a directory git cannot read: `--others` cannot scan it,
                # so it contributes no untracked file, and with no indexed
                # file beneath it it is absent, never childless -- while an
                # indexed path beneath it still comes from the index
                # (`--cached` reads no directory) and makes it an ordinary
                # populated row. A symlink to a directory is listed by git
                # as a FILE (the link itself is the tracked object), so it
                # is a file row here, never a childless directory.
                "hiddenOnlyDirectories": [],
                "unreadableDirectories": [],
                "linkedDirectories": [],
                "nestedRepoDirectories": sorted(d for d in nested if d in shown),
            }

    # Fallback: one bounded breadth-first walk (``_project_tree_walk``).
    if subdir:
        walked = _project_tree_walk_pinned(
            native, _PROJECT_TREE_MAX_ENTRIES, _PROJECT_TREE_SCAN_LIMIT, identity
        )
        if in_nested:
            # A nested repository's `.git` (a pointer file in a submodule) is
            # git's bookkeeping, not something to browse; the git listing never
            # names it either.
            walked["paths"] = [p for p in walked["paths"] if posixpath.basename(p) != ".git"]
        return walked
    return _project_tree_walk(base, _PROJECT_TREE_MAX_ENTRIES, _PROJECT_TREE_SCAN_LIMIT)
