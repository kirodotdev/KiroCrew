"""Bulk delete of sidebar folders that hold no live session.

``POST /api/chat/folders/cleanup`` is the folder twin of the session cleanup
(``/api/chat/slots/cleanup``): a dry run lists what would go, a second call
deletes it. It exists because agents file their worker sessions into folders
they create on the fly (``<goal>/<agent>``), so a busy sidebar collects dozens
of folders whose sessions have all closed. Deleting them one by one through the
row menu does not scale, and hiding them only moves the clutter out of sight.

A folder is removable when nothing that still runs would notice it going:

* no LIVE session is filed in it or anywhere below it, and no saved cron job
  files its tab into it or anywhere below it (the job keeps the folder's id,
  so recreating the folder by name would not bring the filing back),
* none of its rows carries a setting the person chose (project directory,
  default agent, steering directories, tags, color, icon), and
* it is not a channel folder: neither one stamped for a channel nor one whose
  name a channel's ``session_folder`` setting names. Inbound channel sessions
  are filed by looking that name up, and only a settings save creates the
  folder again, so deleting it would leave every later channel session
  unfiled.

Archived sessions do not keep a folder. Deleting the folder leaves them in
Older Sessions, unfiled, the same as the single-folder delete does: a
``folder_id`` that names no folder is ignored when the history is read.

Top-level folders are spared unless the request asks for them. They are the
ones a person usually made by hand, while the deep ones are what agents leave
behind.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG
from kiro_crew.dashboard.channel_folders import CHANNEL_CONFIG_SECTIONS
from kiro_crew.dashboard.chat_folders import (
    _CHAT_FOLDER_ICON_EPOCHS,
    _CHAT_FOLDER_PENDING_ICON_TASKS,
    _audit_origin,
    _effective_request_app,
    _folder_history_counts,
    _refuse_unattributable_caller,
)
from kiro_crew.dashboard.folder_rank import custom_sort_key
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Folder fields that mean the person configured the folder on purpose. A row
#: carrying any of them is kept even when it is empty: deleting it would throw
#: the setting away along with the row.
_KEEP_FIELDS = ("project_dir", "default_agent", "steering_dirs", "tags", "color", "icon")


def _name_key(name: object) -> str:
    return str(name or "").strip().lower()


def channel_folder_names() -> set[str]:
    """Folder names some channel's ``session_folder`` setting points at.

    A channel can adopt an existing folder by name without stamping it
    (``ensure_channel_folder``), so the ``channel`` field alone misses it.
    Matched case-insensitively, as the channel lookup does.

    Raises when config cannot be read: unlike the channel lookup, which may
    treat that as "off", a cleanup that cannot tell which folders a channel
    files into must delete nothing.

    Raises as well when the load came back degraded for a channel section or
    for the whole document: the loader then substitutes defaults, which would
    read as "no channel files anywhere".

    Blocking (config I/O): the route runs it off the loop, under the
    folder-store lock for the delete.
    """
    cfg = KiroCrewConfig.load()
    degraded = cfg.degraded_sections
    sections = set(CHANNEL_CONFIG_SECTIONS.values())
    if DEGRADED_WHOLE_CONFIG in degraded or degraded & sections:
        # The loader handed back defaults for what it could not parse, so a
        # channel's folder name would read as empty rather than as unknown.
        raise _ConfigUnreadable(f"degraded config sections: {sorted(degraded)}")
    names = {
        _name_key(getattr(getattr(cfg, section, None), "session_folder", ""))
        for section in sections
    }
    names.discard("")
    return names


def _keeps_itself(
    folder: dict[str, Any], kept_names: set[str] | frozenset[str] = frozenset()
) -> bool:
    """Whether *folder* must stay for its own sake, regardless of contents."""
    if folder.get("channel") or _name_key(folder.get("name")) in kept_names:
        return True
    return any(folder.get(field) for field in _KEEP_FIELDS)


def removable_folder_ids(
    folders: list[dict[str, Any]],
    occupied: Iterable[str],
    *,
    include_top_level: bool,
    kept_names: Iterable[str] = (),
) -> list[str]:
    """Ids of folders with no occupied folder or kept folder at or below them.

    *occupied* is the set of folder ids a live session is filed in.
    *kept_names* are folder names a channel files into (see
    :func:`channel_folder_names`); a folder with one of them keeps itself. A folder is
    removable when it is not occupied, does not keep itself, and every child is
    removable too, so a whole empty branch goes at once and a branch with one
    live session somewhere inside keeps every ancestor of that session.

    Returned in tree order (parents before children) so a preview reads like
    the sidebar. A cycle in ``parent_id`` (a damaged store) is treated as
    keeping every folder on it. So is an ``id`` that more than one row
    carries: a delete removes every row with that id, so one empty row would
    take a configured twin with it. Each such row's parent is kept too.
    """
    occupied_ids = {str(fid) for fid in occupied if fid}
    kept = {_name_key(n) for n in kept_names} - {""}
    seen_ids: set[str] = set()
    duplicated: set[str] = set()
    for folder in folders:
        fid = str(folder.get("id") or "")
        if fid:
            (duplicated if fid in seen_ids else seen_ids).add(fid)
    for folder in folders:
        if str(folder.get("id") or "") in duplicated:
            occupied_ids.add(str(folder.get("id")))
            occupied_ids.add(str(folder.get("parent_id") or ""))
    occupied_ids.discard("")
    by_id = {str(f.get("id") or ""): f for f in folders if f.get("id")}
    children: dict[str, list[str]] = {}
    for fid, folder in by_id.items():
        parent = str(folder.get("parent_id") or "")
        if parent in by_id:
            children.setdefault(parent, []).append(fid)

    verdict: dict[str, bool] = {}
    visiting: set[str] = set()

    def _removable(fid: str) -> bool:
        if fid in verdict:
            return verdict[fid]
        if fid in visiting:
            return False
        visiting.add(fid)
        folder = by_id[fid]
        ok = fid not in occupied_ids and not _keeps_itself(folder, kept)
        for child in children.get(fid, []):
            # Evaluate every child even after one fails, so each child gets its
            # own verdict: a kept sibling must not hide an empty one.
            ok = _removable(child) and ok
        visiting.discard(fid)
        verdict[fid] = ok
        return ok

    for fid in by_id:
        _removable(fid)

    ordered: list[str] = []

    def _walk(fid: str, depth: int, seen: set[str]) -> None:
        if fid in seen:
            return
        seen.add(fid)
        if verdict.get(fid) and (include_top_level or depth > 0):
            ordered.append(fid)
        for child in sorted(children.get(fid, []), key=lambda c: custom_sort_key(by_id[c])):
            _walk(child, depth + 1, seen)

    roots = [fid for fid, f in by_id.items() if str(f.get("parent_id") or "") not in by_id]
    seen: set[str] = set()
    # Siblings and roots walk in the sidebar's custom order (rank first, then
    # legacy ``order`` and name), so the preview lists folders as the sidebar
    # draws them. ``folders.json`` is loaded with only ``id`` checked, so a
    # hand-edited rank or order reaches this module verbatim; the key reads
    # both without raising, so one bad row cannot turn the cleanup into a 500.
    for root in sorted(roots, key=lambda r: custom_sort_key(by_id[r])):
        _walk(root, 0, seen)
    return ordered


def confirmed_subset(
    folders: list[dict[str, Any]], removable: Iterable[str], previewed: Iterable[str]
) -> list[str]:
    """The previewed folders that may still go, without orphaning anything.

    A folder is deleted only when the person saw it in the preview AND it is
    still removable now. A folder whose child would stay (a child made after
    the preview, so never shown) is dropped too, and that repeats up the
    chain, so a delete never leaves a folder pointing at a missing parent.
    Keeps the order of *removable*.
    """
    allowed = {str(fid) for fid in previewed if fid}
    picked = [fid for fid in removable if fid in allowed]
    keep = set(picked)
    children: dict[str, list[str]] = {}
    for folder in folders:
        fid = str(folder.get("id") or "")
        parent = str(folder.get("parent_id") or "")
        if fid and parent:
            children.setdefault(parent, []).append(fid)
    changed = True
    while changed:
        changed = False
        for fid in list(keep):
            if any(child not in keep for child in children.get(fid, [])):
                keep.discard(fid)
                changed = True
    return [fid for fid in picked if fid in keep]


def _live_folder_ids(state: DashboardState) -> set[str]:
    """Folder ids a live session is filed in right now."""
    return {
        str(getattr(slot, "folder_id", "") or "")
        for slot in getattr(state, "_slots", {}).values()
        if getattr(slot, "folder_id", "")
    }


def _refusal(request_app: str, fid_note: str) -> web.Response:
    sel().log_api_access(
        caller=request_app,
        operation="chat.folder_cleanup",
        outcome="denied",
        source="app_isolation",
        resources=fid_note,
        error="agents cannot delete folders",
    )
    return web.json_response(
        {
            "error": "only the person can delete folders",
            "code": "folder_delete_forbidden",
        },
        status=403,
    )


class _ConfigUnreadable(Exception):
    """Channel settings could not be read, or the load was degraded."""


class _CronUnreadable(Exception):
    """The cron store was busy or unreadable."""


def _unreadable(exc: Exception) -> web.Response:
    logger.warning("folder cleanup: refused, %s", type(exc).__name__, exc_info=exc)
    return _cron_unreadable() if isinstance(exc, _CronUnreadable) else _config_unreadable()


def _cron_unreadable() -> web.Response:
    return web.json_response(
        {
            "error": "scheduled jobs could not be read, so no folder was deleted",
            "code": "cron_unreadable",
        },
        status=503,
    )


def _config_unreadable() -> web.Response:
    return web.json_response(
        {
            "error": "channel settings could not be read, so no folder was deleted",
            "code": "config_unreadable",
        },
        status=503,
    )


async def api_chat_folders_cleanup(request: web.Request) -> web.Response:
    """POST /api/chat/folders/cleanup — delete folders that hold no live session.

    Body: ``{"dry_run": true, "include_top_level": false}``. Both default to
    false. A dry run answers ``{"ids": [...], "archived": {id: n}}`` and changes
    nothing. A real run must send back the ``ids`` the person saw in the
    preview, and deletes only those that are still removable; it answers
    ``{"deleted": [...]}``.

    Person only, like the single-folder delete: an app or crew member is
    refused, because the emptiness of a folder cannot be checked in the same
    lock as the session archive, and the person is the one who can see the
    preview before confirming.
    """
    state: DashboardState = request.app["state"]
    if (refusal := _refuse_unattributable_caller(state, request)) is not None:
        return refusal
    request_app = _effective_request_app(state, request)
    if request_app:
        return _refusal(request_app, "cleanup")
    member = str(request.get(MEMBER_CHAT_PRINCIPAL_KEY) or "")
    if member:
        return _refusal(member, "cleanup")
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None
    dry_run = body.get("dry_run") is True
    include_top_level = body.get("include_top_level") is True
    source, caller = _audit_origin(request)
    previewed = body.get("ids")
    if not dry_run and (
        not isinstance(previewed, list) or not all(isinstance(fid, str) for fid in previewed)
    ):
        return web.json_response(
            {"error": "ids must list the previewed folder ids", "code": "invalid_ids"},
            status=400,
        )

    async def _referents() -> tuple[set[str], set[str]]:
        """Folder ids saved jobs file into, and folder names channels file into.

        Both are strict, off-loop reads: an unreadable or busy store is an
        unknown set, not an empty one, so they raise and nothing is deleted.
        """
        try:
            jobs = await state.crons.chat_folder_ids_async()
        except Exception as exc:  # noqa: BLE001 - fail closed, delete nothing
            raise _CronUnreadable() from exc
        try:
            names = await asyncio.to_thread(channel_folder_names)
        except Exception as exc:  # noqa: BLE001 - fail closed, delete nothing
            raise _ConfigUnreadable() from exc
        return jobs, names

    if dry_run:
        # A preview only: the delete below re-reads everything under the lock.
        try:
            job_folders, kept_names = await _referents()
        except (_CronUnreadable, _ConfigUnreadable) as exc:
            return _unreadable(exc)
        # The archive scan is a synchronous walk of the session store; keep it
        # off the loop like the folder list read does.
        loop = asyncio.get_running_loop()
        history = await loop.run_in_executor(subprocess_executor(), _folder_history_counts, state)
        # Read committed folder state under the store lock: an unlocked read
        # could see a mutation whose write is about to be rolled back.
        ids = await state.read_folders(
            lambda folders: removable_folder_ids(
                folders,
                _live_folder_ids(state) | job_folders,
                include_top_level=include_top_level,
                kept_names=kept_names,
            )
        )
        sel().log_api_access(
            caller=caller,
            operation="chat.folder_cleanup_dry_run",
            outcome="allowed",
            source=source,
            resources=f"count={len(ids)}",
        )
        return web.json_response(
            {
                "ok": True,
                "dry_run": True,
                "ids": ids,
                "count": len(ids),
                "archived": {fid: history[fid] for fid in ids if history.get(fid)},
            }
        )

    referents: dict[str, set[str]] = {}

    async def _prepare() -> None:
        # Awaited UNDER the folder-store lock, so no folder writer lands between
        # these reads and the delete. A channel settings save commits its
        # ``session_folder`` before it takes that lock to adopt the folder, and
        # a job save that names a folder checks and persists it while holding
        # the lock (``_hold_folder_for_job``), so each either finished first --
        # and is seen here -- or runs after and finds the folder gone.
        referents["jobs"], referents["names"] = await _referents()

    def _prune(folders: list[dict[str, Any]]) -> tuple[bool, list[str]]:
        # Occupancy is read inside the store lock so a session filed between the
        # preview and this call keeps its folder (and every ancestor of it), and
        # only previewed folders go, so one emptied after the preview stays.
        removable = removable_folder_ids(
            folders,
            _live_folder_ids(state) | referents["jobs"],
            include_top_level=include_top_level,
            kept_names=referents["names"],
        )
        ids = confirmed_subset(folders, removable, previewed or [])
        if not ids:
            return False, []
        gone = set(ids)
        folders[:] = [f for f in folders if str(f.get("id") or "") not in gone]
        return True, ids

    try:
        removed = await state.mutate_folders(_prune, prepare=_prepare)
    except (_CronUnreadable, _ConfigUnreadable) as exc:
        return _unreadable(exc)
    for fid in removed:
        _CHAT_FOLDER_ICON_EPOCHS.pop(fid, None)
        pending = _CHAT_FOLDER_PENDING_ICON_TASKS.pop(fid, None)
        if pending is not None and not pending.done():
            pending.cancel()
    if removed:
        state.push_slots_update()
    sel().log_api_access(
        caller=caller,
        operation="chat.folder_cleanup",
        outcome="allowed",
        source=source,
        resources=f"count={len(removed)}",
    )
    logger.info("folder cleanup: deleted %d folder(s)", len(removed))
    return web.json_response({"ok": True, "deleted": removed, "count": len(removed)})
