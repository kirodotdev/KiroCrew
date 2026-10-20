"""The channel's view tabs at Slack's cap: reuse ours, remove a stale one of ours, never theirs.

A code channel holds at most five views, at most one of them a diff
(`agents.conversations.setView`_): a ``setView`` for a NEW ``view_key`` past the
cap fails with ``too_many_views``. A ``setView`` for a key that exists is an
upsert and never counts against the cap, so a tab is reused simply by writing
the same key again.

What this module adds:

* **Which views are ours.** Every tab this gateway opens is recorded on the
  channel's surface record (:attr:`~.surface_store.SurfaceRecord.views`): its
  ``view_key``, ``view_id``, type and when it was last written. ``listViews``
  carries no owner, so the record is the ONLY thing that may mark a view ours;
  a view Slack lists that the record does not hold is never removed. (Slack also
  refuses removing another agent's view with ``not_view_owner``; that is a
  backstop, not the rule.)
* **Making room.** On ``too_many_views`` (:func:`make_room`), the live list is
  read and the least recently written view whose RECORDED id is listed is
  removed -- one of the agent's own html / block_kit tabs, never the Plan tab
  and never the Code tab's diff, which are the channel's own -- then the write
  is retried once. With nothing of ours to remove, the caller gets
  ``too_many_views`` back and says so.
* **Rehydrating.** The same read drops record entries Slack no longer lists (a
  view someone removed), so a later write reopens the tab instead of trusting a
  dead id.

.. _agents.conversations.setView: https://docs.slack.dev/reference/methods/agents.conversations.setView
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.slack.code_channels.models import CodeChannelApiError
from kiro_crew.slack.code_channels.surface_store import get_async, update_async

logger = logging.getLogger(__name__)

TOO_MANY_VIEWS = "too_many_views"
#: The views the channel keeps (the Plan tab); never removed to make room.
PROTECTED_KEYS = frozenset({"plan"})
#: Most view entries remembered per channel (Slack holds five; a margin for churn).
MAX_RECORDED = 16


def _entry(view: dict[str, Any]) -> dict[str, str]:
    return {
        "key": str(view.get("key") or ""),
        "view_id": str(view.get("view_id") or ""),
        "type": str(view.get("type") or ""),
        "at": str(view.get("at") or "0"),
    }


async def record_view(channel_id: str, *, key: str, view_id: str, type: str) -> None:
    """Remember that this gateway wrote view *key* (made or updated) just now."""
    record = await get_async(channel_id)
    kept = [dict(v) for v in (record.views if record else ()) if v.get("key") != key]
    kept.append(_entry({"key": key, "view_id": view_id, "type": type, "at": f"{time.time():.3f}"}))
    await update_async(channel_id, views=kept[-MAX_RECORDED:])


async def forget_view(channel_id: str, key: str) -> None:
    record = await get_async(channel_id)
    if record is None:
        return
    kept = [dict(v) for v in record.views if v.get("key") != key]
    if len(kept) != len(record.views):
        await update_async(channel_id, views=kept)


async def _reconcile(client: Any, channel_id: str) -> list[dict[str, str]]:
    """Our record pruned to the views Slack still lists, matched by view id.

    A recorded entry is ours only while its OWN recorded ``view_id`` is listed: a
    listed view that merely shares a key (another agent's) never makes it ours.
    """
    listed = await client.list_views(channel_id)
    live_ids = {str(v.get("view_id") or "") for v in listed if v.get("view_id")}
    record = await get_async(channel_id)
    recorded = [dict(v) for v in (record.views if record else ())]
    ours = [v for v in recorded if v.get("view_id") and v["view_id"] in live_ids]
    changes: dict[str, Any] = {}
    if len(ours) != len(recorded):
        changes["views"] = ours
    if record is not None and record.plan_view_id and record.plan_view_id not in live_ids:
        # The Plan tab is gone (removed in Slack): the next publish re-attaches it.
        changes["plan_view_id"] = ""
    if changes:
        await update_async(channel_id, **changes)
    return ours


async def make_room(client: Any, channel_id: str, *, for_key: str) -> str:
    """Remove the stalest view of ours to fit *for_key*; returns its key ("" = none).

    Removes only by the view id this gateway recorded when it opened the view.
    One per channel at a time, so a tool write and the diff publish meeting the
    cap together each find what the other left. Raises nothing: a read or
    removal Slack refuses leaves the channel as it was and answers "".
    """
    lock = _locks.setdefault(channel_id, asyncio.Lock())
    async with lock:
        return await _make_room(client, channel_id, for_key=for_key)


_locks: dict[str, asyncio.Lock] = {}


async def _make_room(client: Any, channel_id: str, *, for_key: str) -> str:
    try:
        ours = await _reconcile(client, channel_id)
    except CodeChannelApiError as exc:
        logger.info("code channel %s: views not listed: %s", channel_id, exc.code)
        return ""
    candidates = [
        v
        for v in ours
        # The diff is never recorded here (the workspace keeps its id), so it is
        # never a candidate.
        if v.get("key") not in PROTECTED_KEYS and v.get("key") != for_key
    ]
    candidates.sort(key=lambda v: float(v.get("at") or 0))
    for view in candidates:
        try:
            await client.remove_view(channel_id, view["view_id"])
        except CodeChannelApiError as exc:
            if exc.code == "view_not_found":
                await forget_view(channel_id, view["key"])
                return view["key"]  # already gone: the room is there
            logger.info(
                "code channel %s: view %s not removed: %s", channel_id, view["key"], exc.code
            )
            continue
        await forget_view(channel_id, view["key"])
        return view["key"]
    return ""


async def write_view(
    client: Any,
    channel_id: str,
    *,
    key: str,
    type: str,
    write: Callable[[], Awaitable[str]],
) -> tuple[str, str]:
    """Run the ``setView`` *write* for view *key*, making room once at the cap.

    Returns ``(view_id, removed_key)``; *removed_key* is the view of ours that was
    removed to fit this one ("" when none had to be). A ``too_many_views`` with
    nothing of ours to remove is raised to the caller. The view is recorded as
    ours once Slack accepted it.
    """
    removed = ""
    try:
        view_id = await write()
    except CodeChannelApiError as exc:
        if exc.code != TOO_MANY_VIEWS:
            raise
        removed = await make_room(client, channel_id, for_key=key)
        if not removed:
            raise
        # The refusal wrote nothing, so this is the request's first landing,
        # not a retried write.
        view_id = await write()
    await record_view(channel_id, key=key, view_id=view_id, type=type)
    return view_id, removed
