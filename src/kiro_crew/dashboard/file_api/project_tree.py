"""``GET /api/project/tree``: the workspace file listing for the Files tab."""

from __future__ import annotations

import asyncio
import os
import posixpath
from pathlib import PurePath
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _PROJECT_TREE_MAX_ENTRIES,
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
        platform_compat,
        redact,
        redact_path_segments,
    )


def _project_tree_entries(
    dirpath: str, dirnames: list[str], filenames: list[str]
) -> tuple[list[str], list[str]]:
    """The ``(dirs, files)`` the non-git tree walk keeps under *dirpath*.

    Dot-directories are walked (``.worktrees`` holds checkouts users navigate
    to). A dot-name, and every entry beneath a dot-directory, is checked
    against the sensitive-path fence on its real path, since the stores it
    fences (``.aws``, ``.config/gcloud``, ``.docker/config.json``, ...) live
    under dot-names. "Beneath a dot-directory" is read off the real path of
    *dirpath*, ancestors above the project root included, so a project rooted
    inside one (``~/.config``) is fenced too. Runs on the walk's worker thread,
    so the pre-resolved gate answers inline.

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

    dirs = sorted(d for d in dirnames if d not in _PROJECT_TREE_SKIP_DIRS and not fenced(d))
    return dirs, [f for f in filenames if not fenced(f)]


def _project_tree_directories(paths: list[str]) -> list[str]:
    """Return every POSIX parent directory named by *paths*."""
    directories: set[str] = set()
    for path in paths:
        parent = posixpath.dirname(path)
        while parent:
            directories.add(parent)
            parent = posixpath.dirname(parent)
    return sorted(directories)


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


def _project_tree_sample_files(paths: list[str], limit: int) -> tuple[list[str], list[str]]:
    """Cap files fairly by direct parent and report parents that lost files."""
    file_counts: dict[str, int] = {}
    for path in paths:
        parent = posixpath.dirname(path)
        file_counts[parent] = file_counts.get(parent, 0) + 1
    quotas = _project_tree_file_quotas(file_counts, limit)
    selected_counts = {directory: 0 for directory in file_counts}
    selected: list[str] = []
    for path in paths:
        parent = posixpath.dirname(path)
        if selected_counts[parent] >= quotas[parent]:
            continue
        selected.append(path)
        selected_counts[parent] += 1
    truncated_directories = sorted(
        directory for directory, count in file_counts.items() if selected_counts[directory] < count
    )
    return selected, truncated_directories


async def api_project_tree(request: web.Request) -> web.Response:
    """GET /api/project/tree?path=... - workspace file listing for a project dir.

    Returns project-relative POSIX file paths for rendering a workspace tree.
    Inside a git repository the listing is ``git ls-files --cached --others
    --exclude-standard`` scoped to the project dir (tracked + untracked,
    .gitignore honored); outside one it walks the complete directory skeleton
    while capping returned files. Path must match a known project directory
    (same allow-list as api_project_git).
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
            }
        )

    def _run() -> dict:
        # git listing first: honors .gitignore, includes tracked-but-deleted
        # files (they render with a deleted status lane), and with cwd=base a
        # project dir that is a repo SUBDIRECTORY lists only its own subtree.
        # -z: NUL separation, so no C-quoting and exotic names survive intact.
        probe_rc, _probe_out, _ = _run_git_bounded(
            ["git", "rev-parse", "--git-dir"],
            cwd=base,
            env=os.environ.copy(),
            timeout=5,
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
                ],
                cwd=base,
                env=os.environ.copy(),
                timeout=15,
            )
            if ls_rc == 0:
                # Git emits tracked and untracked files in separate blocks and
                # documents no combined order. Sort once, then distribute the
                # file budget round-robin across direct parent directories so a
                # large subtree cannot consume every file row.
                listed = sorted(p for p in ls_out.split("\0") if p)
                selected_paths, truncated_directories = _project_tree_sample_files(
                    listed, _PROJECT_TREE_MAX_ENTRIES
                )
                return {
                    "root": base,
                    "paths": selected_paths,
                    "directories": _project_tree_directories(listed),
                    "repo": True,
                    "truncated": bool(truncated_directories),
                    "truncatedDirectories": truncated_directories,
                    # A directory row exists here only as the parent of a listed
                    # file, so an ignored-only folder is absent rather than
                    # childless; the only childless directory this branch can
                    # produce is a truncated one, reported above. The same holds
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
                }

        # Fallback: walk twice so the first pass can compute fair per-directory
        # quotas without retaining every filename in memory. The complete walk
        # is required to return the directory skeleton past the file cap.
        directories: list[str] = []
        # Directories the walk leaves CHILDLESS although they are not empty on
        # disk: every entry is one this filter drops (a skip-set directory or
        # a fenced entry) and no entry is kept -- a symlink to a directory
        # is NOT such an entry (it is a visible row of its own, see
        # ``linked_directories``). The dashboard renders a childless folder
        # with a state row beneath it, and the row must not call such a folder
        # empty -- `_bg/` holding only `.kiro/` is the reported case. Reported
        # separately from `directories` so the tree can tell the two apart; a
        # directory with a listed file or a kept subfolder is never in this list
        # even when it also holds hidden entries. The root itself, when its top
        # level holds only such entries, is named as ``.`` (it is no row).
        hidden_only_directories: list[str] = []
        # Symlinks to directories, listed as rows of their own (see the walk
        # below): the walk never follows a link, so nothing beneath one is
        # listed, and the dashboard says so beneath its row rather than calling
        # the link -- or the folder holding only links -- empty.
        linked_directories: list[str] = []
        # Directories the walk KEPT but could not read. ``os.walk`` reports a
        # failed ``scandir`` on a subdirectory through ``onerror`` and then
        # skips it WITHOUT yielding it (its default ``onerror=None`` swallows
        # the failure), so a kept, non-symlink child the process may not read
        # (permission denied is the usual cause) would otherwise leave no trace:
        # its parent has no row beneath it, is not hidden-only (the child is no
        # symlink), and the dashboard would call the parent empty -- a lie,
        # ``ls`` shows the child. Such a directory is therefore listed as a row
        # AND named here: the tree shows the folder, with nothing beneath it and
        # no status line (a failed read is an error, and the dashboard reports
        # an error only through its ``ErrorNotice`` above the tree, which names
        # every directory in this list; the folder's own row carries a lock
        # marker pointing at that notice), and its parent is not childless at
        # all. Any failure
        # counts, not only EACCES: the parent listed the entry, so a row that
        # makes no claim about its contents is the honest rendering whatever
        # stopped the read (a directory removed mid-walk is stale for exactly
        # one refresh either way). The file pass below needs no hook: an
        # unreadable directory has no files to list and is already a row. The
        # root itself failing is recorded as ``.`` (see ``_record_unreadable``).
        unreadable_directories: list[str] = []

        def _record_unreadable(error: OSError) -> None:
            failed = error.filename
            # ``scandir`` names the directory on every error it raises; the
            # guard keeps a bare OSError from aborting the whole listing.
            if not isinstance(failed, str):
                return
            rel_failed = os.path.relpath(failed, base)
            if rel_failed == ".":
                # The root itself could not be read: the walk yields nothing,
                # so the payload would be indistinguishable from a workspace
                # with no files in it and the dashboard would say so -- the
                # same "empty" claim this listing refuses to make one level
                # down. The root is no directory row (rows are relative to
                # it), so it is named only here, as ``.``; the dashboard shows
                # its not-readable state in place of the empty-workspace one.
                unreadable_directories.append(".")
                return
            directory = rel_failed.replace(os.sep, "/")
            directories.append(directory)
            unreadable_directories.append(directory)

        file_counts: dict[str, int] = {}
        for dirpath, dirnames, filenames in os.walk(base, onerror=_record_unreadable):
            had_entries = bool(dirnames or filenames)
            rel_dir = os.path.relpath(dirpath, base)
            directory = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            if directory:
                directories.append(directory)
            # A symlink to a directory is a visible, navigable entry -- ``ls``
            # shows it -- but the walk never descends it (``followlinks`` is
            # off, against link cycles) and never yields it, so it would be
            # neither a row nor a parent and its folder would read as childless.
            # It is listed as a directory row of its own and named in
            # ``linkedDirectories``: the row shows, nothing beneath it is
            # listed (the target is not walked), and the dashboard says so
            # beneath it instead of calling the link empty. The name filter
            # below applies to every entry alike, link or not: what a folder
            # shows must be predictable from the NAME alone, and a ``.cache``
            # or ``node_modules`` that is a link to another disk is as much a
            # hidden item as its real twin (Design lane on ``9f52681b54``) --
            # so links are told apart among the names the filter KEPT, and a
            # filtered link counts as a hidden entry like any filtered
            # directory. Hidden-only therefore means every entry the folder
            # holds is one the listing filters out by nature (skip-set
            # directories, fenced entries), real or linked: it applies when the
            # filter left no directory and no file. A kept link is a row, and a
            # folder holding only kept links is not hidden-only. The root is
            # judged by the same rule, OUTSIDE the ``if directory`` above: a
            # project directory whose top level holds only skipped or hidden
            # entries yields no file and no kept subdirectory, so the payload
            # would be the empty-workspace shape and the dashboard would call
            # the workspace empty -- the claim this listing refuses to make one
            # level down. The root is no directory row of its own, so it is
            # named as ``.``, exactly as an unreadable root is.
            dirnames[:], filenames = _project_tree_entries(dirpath, dirnames, filenames)
            # ``os.walk`` stops at a symlink but descends a Windows junction, so
            # both are listed as link rows and pruned here, never walked.
            links = [
                name
                for name in dirnames
                if platform_compat.is_link_or_junction(os.path.join(dirpath, name))
            ]
            for name in links:
                link = f"{directory}/{name}" if directory else name
                directories.append(link)
                linked_directories.append(link)
            if had_entries and not filenames and not dirnames:
                hidden_only_directories.append(directory or ".")
            dirnames[:] = [name for name in dirnames if name not in links]
            file_counts[directory] = len(filenames)

        quotas = _project_tree_file_quotas(file_counts, _PROJECT_TREE_MAX_ENTRIES)
        truncated_directories = sorted(
            directory for directory, count in file_counts.items() if quotas[directory] < count
        )
        paths: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = os.path.relpath(dirpath, base)
            directory = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            dirnames[:], filenames = _project_tree_entries(dirpath, dirnames, filenames)
            dirnames[:] = [
                name
                for name in dirnames
                if not platform_compat.is_link_or_junction(os.path.join(dirpath, name))
            ]
            prefix = "" if not directory else directory + "/"
            for name in sorted(filenames)[: quotas.get(directory, 0)]:
                paths.append(prefix + name)
        return {
            "root": base,
            "paths": paths,
            "directories": directories,
            "repo": False,
            "truncated": bool(truncated_directories),
            "truncatedDirectories": truncated_directories,
            "hiddenOnlyDirectories": hidden_only_directories,
            "unreadableDirectories": unreadable_directories,
            "linkedDirectories": linked_directories,
        }

    result = await asyncio.to_thread(_run)
    # Egress redaction, same rationale as api_project_git_status: listed names
    # are repo content and this body is rendered by the dashboard.
    result["root"] = _redact_project_path(result["root"])
    # Redact each path with redact_path_segments over the same context-aware
    # redact(): the whole-string redact() collapses each matched token to a
    # fixed placeholder, so two genuinely-different project-relative paths
    # whose only differing segment is credential-shaped redact to the same
    # string. The helper redacts each path segment-wise and suffixes every
    # redacted segment with an opaque label keyed per gateway process, so both
    # stay in the tree; it never emits less redaction than redact() itself, and
    # the label is stable across responses within this process, so the git
    # status listing labels the same path identically and the dashboard's join
    # by path holds.
    # Then de-duplicate, preserving order and first occurrence, as the fallback
    # for a collision the helper does not separate: the dashboard tree hands
    # this list straight to @pierre/trees, whose appendPresortedPaths throws
    # "Duplicate path" on adjacent identical entries. dict.fromkeys keeps first
    # occurrence. This does not affect "truncated": the cap is applied to the
    # raw listing above.
    for key in (
        "paths",
        "directories",
        "truncatedDirectories",
        "hiddenOnlyDirectories",
        "unreadableDirectories",
        "linkedDirectories",
    ):
        result[key] = list(dict.fromkeys(redact_path_segments(p, redact) for p in result[key]))
    return web.json_response(result)
