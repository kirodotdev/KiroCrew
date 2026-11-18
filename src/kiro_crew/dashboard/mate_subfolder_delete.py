"""The person's delete of an empty folder inside a Bot's home folder.

A Bot files every folder it makes under its own home (``mate_folders``), and
nobody else may write there. The one exception is this: the person may delete
a folder INSIDE a Bot's home, never the home itself, when it is empty -- no
child folder, no live session, no archived session, and no saved cron job
filing its tab into it. A folder that is not empty stays the Bot's.

The live checks run inside the folder-store callback that removes the row,
the way the ``if_empty`` branch of ``api_chat_folder_delete`` does, so a
session filed or a child created between the check and the removal keeps the
folder. Archived sessions are counted first, off the loop, because that is a
disk scan; one filed AND closed inside that scan is left unfiled, the outcome
the person's ordinary delete and the empty-folder cleanup already accept.

The caller decides who may reach this: only the person (no app, no member
principal). A Bot's own placement is unchanged: a conductor that names a gone
folder files in its home, and a Bot that needs the folder creates it again.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.chat_folders_icons import (
    _CHAT_FOLDER_ICON_EPOCHS,
    _CHAT_FOLDER_PENDING_ICON_TASKS,
    _prune_icon_tasks_from_other_loops,
)
from kiro_crew.dashboard.chat_folders_records import _audit_origin, _folder_history_counts
from kiro_crew.dashboard.mate_folders import is_mate_home
from kiro_crew.dashboard.snapshot_commit import (
    VocabularyDeleteCancellations,
    slots_with_a_close_in_flight,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


def is_person_browser(principal: str, request: web.Request) -> bool:
    """Whether *request* is the person's own browser: no app or member
    principal (*principal* empty) and no internal secret. An internal request
    is an agent tool call, even one running in the person's own session. The
    single delete and the cleanup both decide a Bot sub-folder by this."""
    return not principal and _audit_origin(request)[0] == "dashboard"


class _CronUnreadable(Exception):
    """The cron store was busy or unreadable, so the folder's jobs are unknown."""


def _not_empty(reason: str) -> web.Response:
    return web.json_response(
        {
            "error": f"{reason}; only an empty folder inside a Bot's folder can be deleted",
            "code": "folder_not_empty",
        },
        status=409,
    )


async def delete_empty_bot_subfolder(
    state: DashboardState, request: web.Request, fid: str
) -> web.Response:
    """Delete *fid*, a folder inside a Bot's home, if it is empty; refuse otherwise."""
    loop = asyncio.get_running_loop()
    archived = await loop.run_in_executor(discovery_executor(), _folder_history_counts, state)
    if archived.get(fid, 0):
        return _not_empty("folder still holds archived sessions")
    job_folders: set[str] = set()

    async def _prepare() -> None:
        # Under the folder-store lock: a job save that names a folder checks and
        # persists it while holding the same lock, so it lands before or after.
        try:
            job_folders.update(await state.crons.chat_folder_ids_async())
        except Exception as exc:  # noqa: BLE001 - unknown jobs, delete nothing
            raise _CronUnreadable() from exc

    def _remove(folders: list[dict[str, Any]]) -> tuple[bool, str]:
        row = next((f for f in folders if f.get("id") == fid), None)
        if row is None:
            return False, "gone"
        if is_mate_home(row):
            return False, "home"
        if any(f.get("parent_id") == fid for f in folders):
            return False, "folder has subfolders"
        live = [*state._slots.values(), *(s for _, s in slots_with_a_close_in_flight(state))]
        if any(getattr(slot, "folder_id", "") == fid for slot in live):
            return False, "folder still holds live sessions"
        if fid in job_folders:
            return False, "a scheduled job files its chat into this folder"
        folders[:] = [f for f in folders if f.get("id") != fid]
        return True, ""

    # A cancellation during the write is captured, not propagated: the write can
    # still land, and a landed delete owes its slots push and audit before the
    # cancellation is re-raised (the ordinary delete's order).
    cancels = VocabularyDeleteCancellations()
    committed = False

    def _confirm_committed() -> None:
        nonlocal committed
        committed = True

    refused = ""
    with cancels.capturing_commit():
        try:
            refused = await state.mutate_folders(
                _remove, prepare=_prepare, on_committed=_confirm_committed
            )
        except _CronUnreadable:
            logger.warning("bot sub-folder delete: cron store unreadable", exc_info=True)
            return web.json_response(
                {
                    "error": "scheduled jobs could not be read, so the folder was not deleted",
                    "code": "cron_unreadable",
                },
                status=503,
            )
    if cancels.commit is not None and not committed:
        # Cancelled before the removal was proven to land: nothing to finalize.
        raise cancels.commit
    if refused == "gone":
        return web.json_response({"error": "not found", "code": "folder_not_found"}, status=404)
    if refused == "home":
        return web.json_response(
            {
                "error": "this folder belongs to a Bot and is managed for you",
                "code": "mate_home_locked",
            },
            status=403,
        )
    if refused:
        return _not_empty(refused)
    _CHAT_FOLDER_ICON_EPOCHS.pop(fid, None)
    _prune_icon_tasks_from_other_loops(loop)
    pending_icon = _CHAT_FOLDER_PENDING_ICON_TASKS.pop(fid, None)
    if pending_icon is not None and not pending_icon.done():
        pending_icon.cancel()
    state.push_slots_update()
    source, caller = _audit_origin(request)
    sel().log_api_access(
        caller=caller,
        operation="chat.folder_delete",
        outcome="allowed",
        source=source,
        resources=f"{fid} (empty, inside a Bot's folder)",
    )
    cancels.reraise_in_order()
    return web.json_response({"ok": True})
