"""The channel's view tabs at Slack's cap: reuse ours, remove a stale one of ours, never theirs.

A code channel holds at most five views, at most one of them a diff
(`agents.conversations.setView`_): a ``setView`` for a NEW ``view_key`` past the
cap fails with ``too_many_views``. A ``setView`` for a key that exists is an
upsert and never counts against the cap, so a tab is reused simply by writing
the same key again.

What this module adds:

* **Which views are ours.** Every tab this gateway opens is recorded on the
  channel's surface record (:attr:`~.surface_store.SurfaceRecord.views`): its
  ``view_key``, ``view_id``, type and when it was last written (and, for a pull
  request tab, the PR's URL). ``listViews`` carries no owner, so the record is the
  ONLY thing that may mark a view ours; a view Slack lists that the record does
  not hold is never removed. (Slack also refuses removing another agent's view
  with ``not_view_owner``; that is a backstop, not the rule.)
* **The budget.** Five tabs, in three tiers:

  1. the channel's own tabs, never removed to make room: the Plan canvas
     (``plan``), the Code tab's diff (kept by the workspace, never recorded here)
     and the Workers tab (``workers``, :mod:`.tabs`);
  2. the native pull-request tabs of the channel's newest open PRs
     (``pr-<number>``, at most :data:`MAX_PR_TABS`, :mod:`.tabs`);
  3. the agent's own html / block_kit tabs (``code_channel_set_view``).

  On ``too_many_views`` (:func:`make_room`) the live list is read and ONE view of
  ours is removed, then the write is retried once. A tier-3 view is removed first
  (least recently written), for any write. A pull-request tab is removed (the
  oldest PR first) only to fit a tier-1 tab: a PR tab never makes room for
  another PR tab -- :mod:`.tabs` removes the unwanted ones first, so the rest
  are wanted -- nor for an agent's view, which ranks below it. With nothing
  of ours to remove, the caller gets ``too_many_views`` back and says so.
* **Rehydrating.** The same read (:func:`reconcile`) drops record entries missing
  from Slack's list (a view someone removed), so a later write reopens the tab
  instead of trusting a dead id.

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
#: The Plan canvas tab's key.
PLAN_KEY = "plan"
#: The Workers tab's key (a ``block_kit`` view, :mod:`.tabs`).
WORKERS_KEY = "workers"
#: The channel's own tabs: never removed to make room (the diff is never recorded).
PROTECTED_KEYS = frozenset({PLAN_KEY, WORKERS_KEY})
#: The Code tab's diff, as ``make_room``'s *for_key* (it is a channel tab too).
DIFF_KEY = "diff"
#: Pull-request tab keys: ``pr-<number>``.
PR_KEY_PREFIX = "pr-"
PR_VIEW_TYPE = "pull_request"
#: Pull-request tabs a channel keeps: five, less Plan, Code and Workers. With
#: both held, an agent's own tabs may be displaced entirely (they give way first).
MAX_PR_TABS = 2
#: Most view entries remembered per channel (Slack holds five; a margin for churn).
MAX_RECORDED = 16


def pr_key(number: int) -> str:
    return f"{PR_KEY_PREFIX}{int(number)}"


def _is_pr_key(key: str) -> bool:
    """``pr-`` followed by ASCII digits only, the shape :func:`pr_key` writes."""
    number = key[len(PR_KEY_PREFIX) :]
    return key.startswith(PR_KEY_PREFIX) and number.isascii() and number.isdigit()


def is_pr_tab(view: dict[str, Any]) -> bool:
    """A recorded pull-request tab: its key AND its recorded type say so.

    The type is checked too, so a view an agent wrote under a ``pr-`` key before
    those keys were reserved is never taken for one.
    """
    return view.get("type") == PR_VIEW_TYPE and _is_pr_key(str(view.get("key") or ""))


def pr_number(view: dict[str, Any]) -> int:
    return int(str(view.get("key"))[len(PR_KEY_PREFIX) :]) if is_pr_tab(view) else 0


def reserved_key(key: str) -> bool:
    """Keys the agent's own views may not take: the channel's tabs and the PR tabs."""
    return key in PROTECTED_KEYS or key == DIFF_KEY or _is_pr_key(key)


def _entry(view: dict[str, Any]) -> dict[str, str]:
    entry = {
        "key": str(view.get("key") or ""),
        "view_id": str(view.get("view_id") or ""),
        "type": str(view.get("type") or ""),
        "at": str(view.get("at") or "0"),
    }
    if view.get("url"):
        entry["url"] = str(view["url"])
    return entry


def recorded(record: Any, key: str) -> dict[str, str] | None:
    """The view of ours recorded under *key* on a surface record, or ``None``."""
    for view in getattr(record, "views", None) or ():
        if view.get("key") == key and view.get("view_id"):
            return dict(view)
    return None


#: One per channel: every read-modify-write of the recorded views holds it, so a
#: tool write and a refresh recording views together never lose one.
_record_locks: dict[str, asyncio.Lock] = {}


def _record_lock(channel_id: str) -> asyncio.Lock:
    lock = _record_locks.get(channel_id)
    if lock is None:
        if len(_record_locks) > 4096:
            for key in [k for k, v in _record_locks.items() if not v.locked()]:
                _record_locks.pop(key, None)
        lock = _record_locks[channel_id] = asyncio.Lock()
    return lock


def _protected_first(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    """*entries* cut to :data:`MAX_RECORDED`, the agent's oldest dropped first."""
    if len(entries) <= MAX_RECORDED:
        return entries
    spare = [v for v in entries if v.get("key") not in PROTECTED_KEYS and not is_pr_tab(v)]
    drop = {id(v) for v in spare[: len(entries) - MAX_RECORDED]}
    return [v for v in entries if id(v) not in drop][-MAX_RECORDED:]


async def record_view(channel_id: str, *, key: str, view_id: str, type: str, url: str = "") -> None:
    """Remember that this gateway wrote view *key* (made or updated) just now."""
    entry = {"key": key, "view_id": view_id, "type": type, "at": f"{time.time():.6f}", "url": url}
    async with _record_lock(channel_id):
        record = await get_async(channel_id)
        kept = [dict(v) for v in (record.views if record else ()) if v.get("key") != key]
        kept.append(_entry(entry))
        await update_async(channel_id, views=_protected_first(kept))


async def forget_view(channel_id: str, key: str) -> None:
    async with _record_lock(channel_id):
        record = await get_async(channel_id)
        if record is None:
            return
        kept = [dict(v) for v in record.views if v.get("key") != key]
        if len(kept) != len(record.views):
            await update_async(channel_id, views=kept)


async def reconcile(client: Any, channel_id: str) -> list[dict[str, str]]:
    """Our record pruned to the views Slack still lists, matched by view id.

    A recorded entry is ours only while its OWN recorded ``view_id`` is listed: a
    listed view that merely shares a key (another agent's) never makes it ours.
    Raises :class:`CodeChannelApiError` when the list cannot be read.
    """
    # Recorded times are kept to the microsecond: a hair of tolerance for rounding.
    asked_at = time.time() - 1e-6
    listed = await client.list_views(channel_id)
    live_ids = {str(v.get("view_id") or "") for v in listed if v.get("view_id")}

    def _listed(view: dict[str, str]) -> bool:
        # A view recorded after the list was asked for may simply be newer than
        # it: kept, never pruned as "gone".
        if view.get("view_id") in live_ids:
            return True
        try:
            return float(view.get("at") or 0) >= asked_at
        except ValueError:
            return False

    async with _record_lock(channel_id):
        record = await get_async(channel_id)
        recorded_views = [dict(v) for v in (record.views if record else ())]
        ours = [v for v in recorded_views if v.get("view_id") and _listed(v)]
        changes: dict[str, Any] = {}
        if len(ours) != len(recorded_views):
            changes["views"] = ours
        if record is not None and record.plan_view_id and record.plan_view_id not in live_ids:
            # The Plan tab is gone (removed in Slack): the next publish re-attaches it.
            changes["plan_view_id"] = ""
        if changes:
            await update_async(channel_id, **changes)
    return [v for v in ours if v.get("view_id") in live_ids]


def _candidates(ours: list[dict[str, str]], for_key: str) -> list[dict[str, str]]:
    """The views of ours *for_key* may displace, in the order they go (see the module)."""
    agent = [
        v
        for v in ours
        if v.get("key") not in PROTECTED_KEYS and not is_pr_tab(v) and v.get("key") != for_key
    ]
    agent.sort(key=lambda v: float(v.get("at") or 0))
    if for_key not in PROTECTED_KEYS and for_key != DIFF_KEY:
        return agent
    prs = [v for v in ours if is_pr_tab(v) and v.get("key") != for_key]
    prs.sort(key=pr_number)
    return agent + prs


Fence = Callable[[], Awaitable[bool]]
"""Asked right before each removal; False leaves the view where it is."""


async def _fenced(fence: Fence | None) -> bool:
    return fence is None or bool(await fence())


async def make_room(
    client: Any, channel_id: str, *, for_key: str, fence: Fence | None = None
) -> str:
    """Remove one view of ours to fit *for_key*; returns its key ("" = none).

    Removes only by the view id this gateway recorded when it opened the view,
    and only while *fence* (when given) still admits a write to the channel.
    One per channel at a time, so a tool write and the diff publish meeting the
    cap together each find what the other left. Raises nothing: a read or
    removal Slack refuses leaves the channel as it was and answers "".
    """
    if len(_locks) > 4096:
        for key in [k for k, v in _locks.items() if not v.locked()]:
            _locks.pop(key, None)
    lock = _locks.setdefault(channel_id, asyncio.Lock())
    async with lock:
        return await _make_room(client, channel_id, for_key=for_key, fence=fence)


_locks: dict[str, asyncio.Lock] = {}


async def _make_room(
    client: Any, channel_id: str, *, for_key: str, fence: Fence | None = None
) -> str:
    try:
        ours = await reconcile(client, channel_id)
    except CodeChannelApiError as exc:
        logger.info("code channel %s: views not listed: %s", channel_id, exc.code)
        return ""
    for view in _candidates(ours, for_key):
        if not await _fenced(fence):
            return ""
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


async def remove_ours(
    client: Any, channel_id: str, key: str, *, fence: Fence | None = None
) -> bool:
    """Remove the view of ours recorded under *key*, by its recorded id. True when gone.

    A view Slack does not have (``view_not_found``) counts as gone too. Any other refusal,
    or a *fence* that no longer admits a write, keeps the record, so the next pass tries
    again.
    """
    record = await get_async(channel_id)
    view = recorded(record, key)
    if view is None:
        return True
    if not await _fenced(fence):
        return False
    try:
        await client.remove_view(channel_id, view["view_id"])
    except CodeChannelApiError as exc:
        if exc.code != "view_not_found":
            logger.info("code channel %s: view %s not removed: %s", channel_id, key, exc.code)
            return False
    await forget_view(channel_id, key)
    return True


async def write_view(
    client: Any,
    channel_id: str,
    *,
    key: str,
    type: str,
    write: Callable[[], Awaitable[str]],
    url: str = "",
    fence: Fence | None = None,
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
        removed = await make_room(client, channel_id, for_key=key, fence=fence)
        if not removed:
            raise
        # The refusal wrote nothing, so this is the request's first landing,
        # not a retried write.
        view_id = await write()
    await record_view(channel_id, key=key, view_id=view_id, type=type, url=url)
    return view_id, removed
