"""``GET /api/path-complete``: one directory level of a known project for the composer's ``./`` completion."""

from __future__ import annotations

import asyncio
import ntpath
import os
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _PATH_COMPLETE_MAX_ENTRIES,
        _PATH_COMPLETE_MAX_SCAN,
        _PATH_TOKEN_SEPARATORS,
        DashboardState,
        _match_known_project_for,
        _PathProbeBusy,
        _probe_busy_response,
        _run_path_probe,
        _sel,
        _slot_project_snapshot,
        is_sensitive_resolved_path,
        is_unc_shape,
        pinned_fs,
        platform_compat,
    )


def _completion_segments(root: str, rel: str) -> list[str] | None:
    r"""Lexically resolve the typed prefix to segments under *root*.

    ``None`` means it does not name anything under the root: an absolute,
    drive-absolute or UNC-shaped prefix, or a ``..`` run that ends up outside it.
    This is the WHOLE containment decision and it touches no filesystem, which is
    what lets everything below refuse to resolve a caller-supplied string at all.

    The walk starts from the root's OWN segments rather than from empty, so a
    ``..`` run is judged on where it ends rather than refused for existing: going
    up and back down into the same project (``../<project-name>/src/``) is what a
    shell does and stays inside, while a run that ends anywhere else does not.
    Comparison is by segment, so no component is resolved to decide it.

    Both separators end a segment, on every platform -- see
    ``_PATH_TOKEN_SEPARATORS`` for why a Windows-style token must be split here
    rather than left for the OS to re-interpret after the check. A Windows
    component is also refused when trailing dots or spaces would be STRIPPED from
    it, for the same reason in miniature: ``".. "`` is not ``".."`` to this
    function but is to Win32, so accepting it would let the check and the OS
    disagree about one string. That rule applies to ORDINARY names only -- ``.``
    and ``..`` are parent references handled first, and ``".."`` would itself be
    stripped to nothing. Padded names are unopenable on Windows anyway, so the
    refusal costs nothing real; on POSIX they are ordinary filenames and are kept.
    """
    if is_unc_shape(rel) or os.path.isabs(rel) or ntpath.splitdrive(rel)[0]:
        return None
    base = [part for part in _PATH_TOKEN_SEPARATORS.split(root) if part]
    walked = list(base)
    for raw in _PATH_TOKEN_SEPARATORS.split(rel):
        if raw in ("", "."):
            continue
        if raw == "..":
            if not walked:
                return None
            walked.pop()
            continue
        # Ordinary names only: `.` and `..` are handled above, and `".."` would
        # itself be stripped to nothing by the rstrip below.
        if platform_compat.IS_WINDOWS and raw.rstrip(". ") != raw:
            return None
        walked.append(raw)
    if walked[: len(base)] != base:
        return None
    return walked[len(base) :]


def _open_completion_dir(root: str, segments: list[str]) -> int:
    r"""Open ``root/<segments>`` without ever following a link. Worker-thread only.

    Nothing here resolves a path, and that is the point. ``os.path.realpath`` on
    Windows opens the final path, so resolving a caller-influenced path whose link
    target is ``\\host\share`` IS an outbound SMB authentication -- and a screen
    that runs before the resolve only narrows the window in which a same-UID
    writer can swap a link into it. Refusing to follow a link at all removes the
    window instead of narrowing it: whatever is planted, the open fails.

    POSIX opens each component RELATIVE to the descriptor for the one above it,
    which is atomic. Windows has no ``dir_fd``, so components are re-opened by
    path; the property there is carried by ``pin_directory`` refusing a reparse
    point AT each name, so a junction swapped in mid-walk is rejected rather than
    traversed.

    Raises ``FileNotFoundError`` when nothing is there and another ``OSError``
    (``ELOOP``/``ENOTDIR``, or Windows ``NotADirectoryError``) when the name is a
    link or not a directory. Release with ``os.close``.
    """
    fd = platform_compat.pin_directory(root)
    walked = root
    try:
        for segment in segments:
            walked = os.path.join(walked, segment)
            if platform_compat.IS_POSIX:
                nxt = os.open(
                    segment,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fd,
                )
            else:
                nxt = platform_compat.pin_directory(walked)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _scan_completion_dir(dir_fd: int, target: str, root: str, prefix: str) -> list[dict]:
    """Rows for one already-pinned directory. Worker-thread only.

    *target* is the descriptor's own real path (see ``fd_real_path`` in
    :func:`_complete_path_listing`), never the spelling the caller typed, so the
    per-entry fence below judges canonical names.

    Read through *dir_fd* on POSIX so every name resolves against the directory
    that was actually inspected rather than against its path a second time.
    Windows has no ``dir_fd`` support in ``scandir``; there the pin itself is what
    holds the directory in place (its handle omits ``FILE_SHARE_DELETE``, so
    neither it nor any directory above it can be renamed while it lives).
    """
    # A shell hides dot entries until the user types the dot; so does this.
    want_hidden = prefix.startswith(".")
    lowered = prefix.lower()
    rows: list[dict] = []
    scanned = 0
    with os.scandir(dir_fd if platform_compat.IS_POSIX else target) as entries:
        for entry in entries:
            if scanned >= _PATH_COMPLETE_MAX_SCAN:
                break
            scanned += 1
            if entry.name.startswith(".") and not want_hidden:
                continue
            if lowered and not entry.name.lower().startswith(lowered):
                continue
            # Built from the pinned directory's path rather than read off the
            # entry, because a descriptor-based scan reports each entry's path
            # as its bare name.
            full = os.path.join(target, entry.name)
            try:
                # A link is never OFFERED, because it can never be entered: the
                # walk above refuses to follow one, so completing into it would
                # fail on the next keystroke. That one rule replaces every
                # question about where a link points -- out of the project, at a
                # `\\host\share`, or through a chain into either -- and it
                # answers them without resolving anything, which is what the
                # resolution was needed for.
                if entry.is_symlink() or (
                    platform_compat.IS_WINDOWS and platform_compat.is_link_or_junction(full)
                ):
                    continue
                # With no link anywhere in the walked path or at this name, `full`
                # IS the canonical path, so the sensitivity fence needs no
                # resolution to be exact -- and must not do one (see
                # ``_complete_path_listing``).
                if is_sensitive_resolved_path(full):
                    continue
                # No-follow metadata, atomically: the entry is known not to be a
                # link, and a following ``stat`` would be one more chance for a
                # swap to be resolved instead of refused. ``lstat`` on a
                # non-link answers exactly what ``stat`` would.
                is_dir = entry.is_dir(follow_symlinks=False)
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            rows.append(
                {
                    "path": full,
                    "name": entry.name,
                    "kind": "dir" if is_dir else "file",
                    "size": 0 if is_dir else st.st_size,
                    "mtime": int(st.st_mtime),
                }
            )

    # Alphabetical, directories first: the next thing a user completing a path
    # types is usually another separator.
    rows.sort(key=lambda r: (r["kind"] != "dir", r["name"].lower()))
    return rows[:_PATH_COMPLETE_MAX_ENTRIES]


def _complete_path_listing(project: str, rel: str, prefix: str) -> tuple[str, str, list[dict]]:
    """List one directory level for path completion. Worker-thread only.

    Every filesystem touch for the request lives here (the project dir's own
    canonicalization, the component walk, ``scandir`` and the per-entry ``stat``),
    same shape as ``_resolve_project_git``: a project on a stalled mount must not
    block the loop on any of them.

    Returns ``(status, root, rows)`` with status ``"ok"``, ``"sensitive"``,
    ``"outside"`` (the token resolved out of the project), ``"refused"`` (a link
    or a non-directory sat at the target when it was opened, or the opened
    descriptor could not be identified) or ``"missing"`` (nothing is there). The
    last two are one answer to the caller and two different audit facts, which is
    why they are separate statuses.

    Nothing caller-supplied is ever RESOLVED. Containment is decided lexically by
    :func:`_completion_segments` -- an absolute, drive-absolute or UNC-shaped
    ``rel``, or a ``..`` run that pops above the root, names nothing under it --
    and the directory is then reached by :func:`_open_completion_dir`, which opens
    one component at a time and refuses to follow a link at any of them. So the
    target is under the root by construction rather than by a check, and a link a
    same-UID writer plants mid-walk is refused rather than followed. That is why
    ``realpath`` appears nowhere below: on Windows it opens the final path, so
    resolving a caller-influenced path whose link target is a share is itself an
    outbound SMB authentication, and any screen placed before it can only narrow
    the window rather than close it.
    """
    # The project dir is server-held (it came from the known-project allow-list),
    # so canonicalizing IT is not a caller-influenced resolution -- and it is what
    # makes the walk below start from a link-free base.
    root = os.path.realpath(os.path.expanduser(project))
    # The NON-resolving fence, here and for every entry below. ``is_sensitive_path``
    # canonicalises what it is handed (``_candidate_forms`` -> ``realpath``), which
    # on Windows follows a junction aimed at a share -- so the fence itself would be
    # the outbound SMB authentication the rest of this function exists to avoid, and
    # it would run BEFORE the no-follow open that is supposed to have removed the
    # window. ``is_sensitive_resolved_path`` matches the candidate lexically and
    # resolves only its own anchors (``$HOME``, the override roots), which are
    # server-held. Its contract wants a canonical input and gets one: ``root`` is
    # realpath'd, every component below it is proven not to be a link by the walk,
    # and a link entry is never offered -- so these paths have no link left to
    # follow, which is what "canonical" means here.
    if is_sensitive_resolved_path(root):
        return "sensitive", root, []
    segments = _completion_segments(root, rel)
    if segments is None:
        return "outside", root, []

    # The walk raises for a link, a reparse point or a non-directory at any
    # component, and separately for a component that is simply not there. Both
    # complete nothing, but only the first says something happened that the audit
    # trail should carry.
    try:
        dir_fd = _open_completion_dir(root, segments)
    except FileNotFoundError:
        return "missing", root, []
    except OSError:
        return "refused", root, []
    try:
        # What the kernel says the OPEN descriptor really is. A path string is not
        # a single name on Windows: an 8.3 alias (``SSH~1``) is a second name the
        # filesystem keeps for the same directory, so no lexical fence can see that
        # ``./SSH~1/`` IS ``.ssh`` -- and adding another string rule would only
        # rename the problem. ``fd_real_path`` is the documented containment witness
        # for exactly this shape (the descriptor is already held, so the name has no
        # component left to swap), and it fails CLOSED: a host that cannot answer
        # leaves nothing to validate, so the request is refused rather than served
        # on the caller's spelling.
        witness = pinned_fs.fd_real_path(dir_fd)
        if witness is None:
            return "refused", root, []
        # Containment again, on the canonical name this time: the lexical pass
        # judged the string the caller typed, and only this judges the directory it
        # turned out to name.
        if not (witness == root or witness.startswith(root + os.sep)):
            return "outside", root, []
        if is_sensitive_resolved_path(witness):
            return "sensitive", witness, []
        # Entries are built from the WITNESS, so the per-entry fence sees canonical
        # names too -- a row under an aliased directory would otherwise carry the
        # alias straight past it.
        rows = _scan_completion_dir(dir_fd, witness, root, prefix)
    except OSError:
        return "missing", root, []
    finally:
        os.close(dir_fd)
    return "ok", root, rows


async def api_path_complete(request: web.Request) -> web.Response:
    """GET /api/path-complete?path=…&dir=…&q=… — one directory level of a project.

    ``path`` is matched against the gateway's own known project directories and
    the matched SERVER-HELD value is what gets resolved, so this route cannot
    enumerate arbitrary host directories. ``dir`` is the caller's relative
    directory prefix (``./``, ``../src/``) and ``q`` the partial entry name
    being typed. A ``dir`` that resolves outside the project root is answered
    with the ordinary empty result set -- not an error, and not a distinguishing
    field: the composer shows "no matches" while the user is still typing the
    token, and the refusal is recorded in the SEL audit rather than handed to a
    caller that has nothing to do with it.

    Rows carry the same shape as ``/api/file-search`` so the picker renders both
    unchanged, and like that endpoint they are NOT redacted -- the name the
    picker inserts has to be the real one for the path to resolve.
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
            operation="path_complete",
            outcome="denied",
            resources=raw,
            error="not a known project directory",
        )
        return web.json_response(
            {"error": "Unknown project directory", "code": "unknown_project_dir"},
            status=403,
        )

    rel = request.query.get("dir", "").strip()
    prefix = request.query.get("q", "").strip()

    # A NUL cannot occur in a path on any supported platform, and the resolver
    # would raise ValueError rather than OSError for one -- a 500 on caller
    # input. It is the same answer as any other unresolvable token: nothing to
    # complete.
    if "\0" in rel or "\0" in prefix:
        return web.json_response({"results": [], "root": ""})

    # The resolved directory is caller-INFLUENCED (``dir`` is joined onto the
    # allow-listed root), so the listing takes a probe slot exactly as the
    # search walk does rather than a shared default-executor worker.
    try:
        status, root, rows = await _run_path_probe(
            _complete_path_listing, project, rel, prefix, transfer=True
        )
    except _PathProbeBusy:
        return _probe_busy_response(resource=project, operation="path_complete", caller=caller)

    if status == "sensitive":
        _sel().log_api_access(
            caller=caller,
            operation="path_complete",
            outcome="denied",
            resources=root,
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied", "code": "access_denied"}, status=403)
    if status == "outside":
        _sel().log_api_access(
            caller=caller,
            operation="path_complete",
            outcome="denied",
            resources=f"{root} dir={rel}",
            error="outside project root",
        )
        # The one fact the picker cannot work out for itself: an out-of-project
        # token and an empty directory are both zero rows, and only this side knows
        # which. A boolean rather than a named scope, because there is one thing to
        # say and no second value to leave room for; it exists BECAUSE it has a
        # consumer -- the composer's empty-state copy -- and the alternative was the
        # client re-deriving a containment verdict this endpoint already reached.
        return web.json_response({"results": [], "root": "", "outside": True})
    if status == "refused":
        _sel().log_api_access(
            caller=caller,
            operation="path_complete",
            outcome="denied",
            resources=f"{root} dir={rel}",
            error="not a real directory at the completion target",
        )
        return web.json_response({"results": [], "root": root})
    if status == "missing":
        _sel().log_api_access(
            caller=caller,
            operation="path_complete",
            outcome="allowed",
            resources=f"{root} dir={rel} q={prefix} results=0",
            error="no such directory",
        )
        return web.json_response({"results": [], "root": root})

    _sel().log_api_access(
        caller=caller,
        operation="path_complete",
        outcome="allowed",
        resources=f"{root} dir={rel} q={prefix} results={len(rows)}",
    )
    return web.json_response({"results": rows, "root": root})
