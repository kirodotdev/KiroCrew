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
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.slack.code_channels.models import CodeChannelApiError
from kiro_crew.slack.code_channels.surface_store import get_async, update_async

logger = logging.getLogger(__name__)

#: What a view write refused by Slack's own Stop raises (no agents.* call
#: for the channel until its next turn).
STOPPED = "stopped"
#: What a view write raises when the channel stopped admitting the bot (Off or
#: Review, or out of the board's scope) while room was made.
CHANNEL_OFF = "channel_off"

#: The write gate, asked right before each write, each removal and the resend:
#: ``""`` when the channel still admits it, else the refusal code
#: (:func:`..surface.write_refusal`). None: the package's own gate is asked.
Admits = Callable[[], Awaitable[str]]


async def _refusal(channel_id: str, admits: Admits | None) -> str:
    """Why a write may not go out now (``""``: it may). Fails closed.

    The caller writes right after awaiting this, with no other await between.
    """
    try:
        if admits is None:
            from kiro_crew.slack.code_channels import surface

            return await surface.write_refusal(channel_id)
        return str(await admits() or "")
    except Exception:
        logger.debug("code channel %s: view write gate failed", channel_id, exc_info=True)
        return CHANNEL_OFF


def _stopped(channel_id: str) -> bool:
    from kiro_crew.slack.code_channels import surface

    return surface.channel_stopped(channel_id)


TOO_MANY_VIEWS = "too_many_views"
#: The Plan tab's view key; its id is also kept as ``plan_view_id``.
PLAN_KEY = "plan"
#: The views the channel keeps (the Plan tab); never removed to make room.
PROTECTED_KEYS = frozenset({PLAN_KEY})
#: Most view entries remembered per channel (Slack holds five; a margin for churn).
MAX_RECORDED = 16


def _entry(view: dict[str, Any]) -> dict[str, str]:
    return {
        "key": str(view.get("key") or ""),
        "view_id": str(view.get("view_id") or ""),
        "type": str(view.get("type") or ""),
        "at": str(view.get("at") or "0"),
    }


#: One per channel, held across every read-modify-write of the recorded views, so
#: a tab write and an eviction meeting each other never drop the other's change.
#: Separate from :data:`_locks`: :func:`make_room` holds that one while it calls
#: :func:`forget_view`, and an asyncio lock is not reentrant.
_record_locks: dict[str, asyncio.Lock] = {}


def _stamp(view: dict[str, Any]) -> float:
    """When *view* was last written; 0 (oldest) for a stamp that is no finite number."""
    try:
        at = float(view.get("at") or 0)
    except ValueError:
        return 0.0
    return at if math.isfinite(at) else 0.0


def _record_lock(channel_id: str) -> asyncio.Lock:
    return _record_locks.setdefault(channel_id, asyncio.Lock())


async def record_view(channel_id: str, *, key: str, view_id: str, type: str) -> None:
    """Remember that this gateway wrote view *key* (made or updated) just now."""
    async with _record_lock(channel_id):
        record = await get_async(channel_id)
        kept = [dict(v) for v in (record.views if record else ()) if v.get("key") != key]
        entry = {"key": key, "view_id": view_id, "type": type, "at": f"{time.time():.3f}"}
        kept.append(_entry(entry))
        changes: dict[str, Any] = {"views": kept[-MAX_RECORDED:]}
        if key == PLAN_KEY:
            # In the same write, so a reconcile never sees the entry without the id.
            changes["plan_view_id"] = view_id
        await update_async(channel_id, **changes)


async def forget_view(channel_id: str, key: str) -> None:
    async with _record_lock(channel_id):
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

    The listing is read before the record's lock is taken, so an entry recorded
    after the listing began (a write that landed meanwhile) is kept as it is: the
    listing is no evidence it is gone. Only listed entries are returned, the ones
    this listing shows taking a slot.
    """
    # Recorded stamps have millisecond precision: the margin keeps one rounded
    # down past the listing's start from reading as older than it.
    listed_at = time.time() - 0.001
    listed = await client.list_views(channel_id)
    live_ids = {str(v.get("view_id") or "") for v in listed if v.get("view_id")}
    async with _record_lock(channel_id):
        record = await get_async(channel_id)
        recorded = [dict(v) for v in (record.views if record else ())]
        ours = [v for v in recorded if v.get("view_id") and v["view_id"] in live_ids]
        fresh_ids = {
            v["view_id"]
            for v in recorded
            if v.get("view_id") and v not in ours and _stamp(v) >= listed_at
        }
        kept = [v for v in recorded if v in ours or v.get("view_id") in fresh_ids]
        changes: dict[str, Any] = {}
        if len(kept) != len(recorded):
            changes["views"] = kept
        plan_id = record.plan_view_id if record is not None else ""
        if plan_id and plan_id not in live_ids and plan_id not in fresh_ids:
            # The Plan tab is gone (removed in Slack): the next publish re-attaches it.
            changes["plan_view_id"] = ""
        if changes:
            await update_async(channel_id, **changes)
    return ours


async def make_room(
    client: Any, channel_id: str, *, for_key: str, admits: Admits | None = None
) -> str:
    """Remove the stalest view of ours to fit *for_key*; returns its key ("" = none).

    Removes only by the view id this gateway recorded when it opened the view,
    and only while *admits* (when given) still admits a write to the channel:
    the listing before it can outlast the channel being set Off or Review.
    One per channel at a time, so a tool write and the diff publish meeting the
    cap together each find what the other left. Raises nothing: a read or
    removal Slack refuses leaves the channel as it was and answers "".
    """
    async with _lock(channel_id):
        return await _make_room(client, channel_id, for_key=for_key, admits=admits)


_locks: dict[str, asyncio.Lock] = {}


def _lock(channel_id: str) -> asyncio.Lock:
    """The channel's view lock: every view upsert by a tool, and every eviction."""
    return _locks.setdefault(channel_id, asyncio.Lock())


async def _make_room(
    client: Any, channel_id: str, *, for_key: str, admits: Admits | None = None
) -> str:
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
    candidates.sort(key=_stamp)
    for view in candidates:
        if await _refusal(channel_id, admits):
            return ""  # refused right before the removal: nothing is removed
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
    admits: Admits | None = None,
) -> tuple[str, str]:
    """Run the ``setView`` *write* for view *key*, making room once at the cap.

    Returns ``(view_id, removed_key)``; *removed_key* is the view of ours that was
    removed to fit this one ("" when none had to be). A ``too_many_views`` with
    nothing of ours to remove is raised to the caller. The view is recorded as
    ours once Slack accepted it. *admits* (when given) is asked again before a
    removal and before the resend; a refusal raises :data:`CHANNEL_OFF`.

    Holds the channel's view lock (the one :func:`make_room` takes) across the
    write, any room made and the record, so an eviction running for another
    write (the workspace's diff tab) never removes a view this one is updating:
    it either finishes first (this write then lands on the freed key) or waits
    until this one is recorded (and then sees it as the freshest).
    """
    async with _lock(channel_id):
        return await _write_view(client, channel_id, key=key, type=type, write=write, admits=admits)


async def _write_view(
    client: Any,
    channel_id: str,
    *,
    key: str,
    type: str,
    write: Callable[[], Awaitable[str]],
    admits: Admits | None,
) -> tuple[str, str]:
    removed = ""
    # Asked again once the lock is held: the wait for it can outlast an Off, a
    # policy deny or Slack's Stop that came after the tool was admitted.
    refused = await _refusal(channel_id, admits)
    if refused:
        raise CodeChannelApiError("agents.conversations.setView", refused)
    try:
        view_id = await write()
    except CodeChannelApiError as exc:
        if exc.code != TOO_MANY_VIEWS:
            raise
        # Under the channel's view lock already (asyncio locks are not reentrant).
        removed = await _make_room(client, channel_id, for_key=key, admits=admits)
        refused = await _refusal(channel_id, admits)
        if refused:
            # Refused while room was made (or before it): nothing more is sent.
            raise CodeChannelApiError("agents.conversations.setView", refused) from None
        if not removed:
            raise
        # The refusal wrote nothing, so this is the request's first landing,
        # not a retried write.
        view_id = await write()
    await record_view(channel_id, key=key, view_id=view_id, type=type)
    return view_id, removed
