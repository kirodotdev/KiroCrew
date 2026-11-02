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
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.slack.code_channels.models import CodeChannelApiError
from kiro_crew.slack.code_channels.surface_store import get_async, update_async

logger = logging.getLogger(__name__)

#: The one wall clock a recorded view's stamp and :func:`reconcile`'s "asked
#: at" are both read from, so tests can hand both the same fake clock.
_wall: Callable[[], float] = time.time

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


#: What a view write raises when the caller's own fence refused it at the cap
#: (no removal was made; the caller's pre-send check, not Slack, said no).
FENCED = "fenced"


def _stopped(channel_id: str) -> bool:
    from kiro_crew.slack.code_channels import surface

    return surface.channel_stopped(channel_id)


TOO_MANY_VIEWS = "too_many_views"
#: The Plan canvas tab's key; its id is also kept as ``plan_view_id``.
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
#: Whose pull request a PR tab shows, recorded when it is written: the channel's
#: own (its workspace record) or a worker's (the conductor's ledger). A tab keeps
#: its origin after the channel's own PR moves on, so its removal is fenced by
#: what it SHOWS, not by what the channel's current PR happens to be.
ORIGIN_OWN = "own"
ORIGIN_WORKER = "worker"
ORIGINS = frozenset({ORIGIN_OWN, ORIGIN_WORKER})


def from_ledger(view: dict[str, Any]) -> bool:
    """Whether a recorded PR tab shows ledger content (a worker's PR). A tab with
    no recorded origin is taken as one (fails closed)."""
    return view.get("origin") != ORIGIN_OWN


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
    if view.get("origin") in ORIGINS:
        entry["origin"] = str(view["origin"])
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
    # Never pruned: one per code channel (a bounded set), and a lock dropped
    # between its release and its next waiter's wake-up would let two writers in.
    return _record_locks.setdefault(channel_id, asyncio.Lock())


def _stamp(view: dict[str, Any]) -> float:
    """When *view* was last written; 0 (oldest) for a stamp that is no finite number."""
    try:
        at = float(view.get("at") or 0)
    except ValueError:
        return 0.0
    return at if math.isfinite(at) else 0.0


def _protected_first(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    """*entries* cut to :data:`MAX_RECORDED`, the agent's oldest dropped first."""
    if len(entries) <= MAX_RECORDED:
        return entries
    spare = [v for v in entries if v.get("key") not in PROTECTED_KEYS and not is_pr_tab(v)]
    drop = {id(v) for v in spare[: len(entries) - MAX_RECORDED]}
    return [v for v in entries if id(v) not in drop][-MAX_RECORDED:]


async def record_view(
    channel_id: str, *, key: str, view_id: str, type: str, url: str = "", origin: str = ""
) -> None:
    """Remember that this gateway wrote view *key* (made or updated) just now.

    *origin* (a pull-request tab's): whose PR it shows, :data:`ORIGIN_OWN` (the
    channel's own, from its workspace record) or :data:`ORIGIN_WORKER`.
    """
    entry = {
        "key": key,
        "view_id": view_id,
        "type": type,
        "at": f"{_wall():.6f}",
        "url": url,
        "origin": origin,
    }
    async with _record_lock(channel_id):
        record = await get_async(channel_id)
        kept = [dict(v) for v in (record.views if record else ()) if v.get("key") != key]
        kept.append(_entry(entry))
        changes: dict[str, Any] = {"views": _protected_first(kept)}
        if key == PLAN_KEY:
            # In the same write, so a reconcile never sees the entry without the id.
            changes["plan_view_id"] = view_id
        await update_async(channel_id, **changes)


async def set_origin(channel_id: str, key: str, origin: str) -> None:
    """Re-label the recorded view *key*'s :data:`ORIGINS` (no Slack call)."""
    if origin not in ORIGINS:
        return
    async with _record_lock(channel_id):
        record = await get_async(channel_id)
        if record is None:
            return
        kept = [dict(v) for v in record.views]
        hit = [v for v in kept if v.get("key") == key]
        if not hit or all(v.get("origin") == origin for v in hit):
            return
        for v in hit:
            v["origin"] = origin
        await update_async(channel_id, views=kept)


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

    The listing is read before the record's lock is taken, so an entry recorded
    after the listing began (a write that landed meanwhile) is kept as it is: the
    listing is no evidence it is gone. Only listed entries are returned, the ones
    this listing shows taking a slot. Raises :class:`CodeChannelApiError` when the
    list cannot be read.
    """
    # Recorded times are kept to the microsecond: a hair of tolerance for rounding.
    asked_at = _wall() - 1e-6
    listed = await client.list_views(channel_id)
    live_ids = {str(v.get("view_id") or "") for v in listed if v.get("view_id")}
    async with _record_lock(channel_id):
        record = await get_async(channel_id)
        recorded_views = [dict(v) for v in (record.views if record else ())]
        fresh_ids = {
            v["view_id"]
            for v in recorded_views
            if v.get("view_id") and v["view_id"] not in live_ids and _stamp(v) >= asked_at
        }
        kept = [
            v
            for v in recorded_views
            if v.get("view_id") and (v["view_id"] in live_ids or v["view_id"] in fresh_ids)
        ]
        changes: dict[str, Any] = {}
        if len(kept) != len(recorded_views):
            changes["views"] = kept
        plan_id = record.plan_view_id if record is not None else ""
        if plan_id and plan_id not in live_ids and plan_id not in fresh_ids:
            # The Plan tab is gone (removed in Slack): the next publish re-attaches it.
            changes["plan_view_id"] = ""
        if changes:
            await update_async(channel_id, **changes)
    return [v for v in kept if v["view_id"] in live_ids]


def _candidates(ours: list[dict[str, str]], for_key: str) -> list[dict[str, str]]:
    """The views of ours *for_key* may displace, in the order they go (see the module).

    The Code tab (:data:`DIFF_KEY`) is not in the view record, so a recorded
    view under that key is another view (an agent's) and may give way to it.
    """
    itself = "" if for_key == DIFF_KEY else for_key
    agent = [
        v
        for v in ours
        if v.get("key") not in PROTECTED_KEYS and not is_pr_tab(v) and v.get("key") != itself
    ]
    agent.sort(key=_stamp)
    if for_key not in PROTECTED_KEYS and for_key != DIFF_KEY:
        return agent
    prs = [v for v in ours if is_pr_tab(v) and v.get("key") != for_key]
    prs.sort(key=pr_number)
    return agent + prs


async def make_room(
    client: Any,
    channel_id: str,
    *,
    for_key: str,
    admits: Admits | None = None,
) -> str:
    """Remove one view of ours to fit *for_key*; returns its key ("" = none).

    Removes only by the view id this gateway recorded when it opened the view,
    and only while the write gate (*admits*, else the package's own) still
    admits a write to the channel: the listing before it can outlast the channel
    being set Off or Review, archived or stopped.
    One per channel at a time, so a tool write and the diff publish meeting the
    cap together each find what the other left. Raises nothing: a read or
    removal Slack refuses leaves the channel as it was and answers "".
    """
    async with _lock(channel_id):
        return await _make_room(client, channel_id, for_key=for_key, admits=admits)


_locks: dict[str, asyncio.Lock] = {}


def _lock(channel_id: str) -> asyncio.Lock:
    """The channel's view lock: every view upsert by a tool, and every eviction.

    Never pruned, like :func:`_record_lock`: a lock dropped between its release
    and its next waiter's wake-up would let two writers into one channel.
    """
    return _locks.setdefault(channel_id, asyncio.Lock())


async def _make_room(
    client: Any,
    channel_id: str,
    *,
    for_key: str,
    admits: Admits | None = None,
) -> str:
    try:
        ours = await reconcile(client, channel_id)
    except CodeChannelApiError as exc:
        logger.info("code channel %s: views not listed: %s", channel_id, exc.code)
        return ""
    for view in _candidates(ours, for_key):
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
            if exc.maybe_applied:
                # The removal may have landed with its answer lost: never remove
                # another tab on a guess. Slack's own list says which it was.
                return await _settle_lost_removal(client, channel_id, view)
            continue
        await forget_view(channel_id, view["key"])
        return view["key"]
    return ""


async def _settle_lost_removal(client: Any, channel_id: str, view: dict[str, str]) -> str:
    """A removal whose answer was lost: its key when Slack no longer lists it, else "".

    Nothing else is removed either way: the room exists only if that one tab
    went, and an unreadable list leaves the channel as it is.
    """
    try:
        listed = await client.list_views(channel_id)
    except CodeChannelApiError as exc:
        logger.info("code channel %s: views not listed: %s", channel_id, exc.code)
        return ""
    if any(str(v.get("view_id") or "") == view["view_id"] for v in listed):
        return ""  # still there: no room was made, and no other tab is taken
    await forget_view(channel_id, view["key"])
    return view["key"]


async def remove_ours(
    client: Any,
    channel_id: str,
    key: str,
    *,
    admits: Admits | None = None,
    refused: Callable[[CodeChannelApiError], None] | None = None,
) -> bool:
    """Remove the view of ours recorded under *key*, by its recorded id. True when gone.

    A view Slack does not have (``view_not_found``) counts as gone too. Any other refusal,
    or a write gate (*admits*, else the package's own) that no longer admits a write,
    keeps the record, so the next pass tries again. Slack's refusal is handed to
    *refused* (when given), so the caller's back-off can read its ``retry_after``.
    """
    record = await get_async(channel_id)
    view = recorded(record, key)
    if view is None:
        return True
    if await _refusal(channel_id, admits):
        return False
    try:
        await client.remove_view(channel_id, view["view_id"])
    except CodeChannelApiError as exc:
        if exc.code != "view_not_found":
            logger.info("code channel %s: view %s not removed: %s", channel_id, key, exc.code)
            if refused is not None:
                refused(exc)
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
    admits: Admits | None = None,
    url: str = "",
    origin: str = "",
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
        return await _write_view(
            client,
            channel_id,
            key=key,
            type=type,
            write=write,
            admits=admits,
            url=url,
            origin=origin,
        )


async def _write_view(
    client: Any,
    channel_id: str,
    *,
    key: str,
    type: str,
    write: Callable[[], Awaitable[str]],
    admits: Admits | None,
    url: str = "",
    origin: str = "",
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
    await record_view(channel_id, key=key, view_id=view_id, type=type, url=url, origin=origin)
    return view_id, removed
