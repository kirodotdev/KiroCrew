"""The channel's own tabs: native pull-request tabs and the interactive Workers tab.

Both are published by the channel's own refresh -- the workspace service's, on
the same snapshot as the board, the context bar and the Code tab -- and recorded
as ours in :mod:`.views`, whose budget decides who gives way at Slack's five tabs
(Plan, Code and Workers never; then these PR tabs; the agent's own tabs first).

**Pull-request tabs** (:func:`sync_pr_tabs`). ``setView`` ``type=pull_request``
with the PR's URL, upserted under ``pr-<number>`` and named ``PR #<number>``, for
the channel's newest open PRs -- its own and its workers', the PRs the board and
the context bar already show (:func:`wanted_prs`) -- at most
:data:`~.views.MAX_PR_TABS`. A PR that merges, closes or falls out of the newest
two has its tab removed, by the view id recorded when this gateway opened it;
nothing else is ever removed. Nothing is written while the wanted set is what the
record already holds.

**The Workers tab** (:func:`publish_workers`). A ``block_kit`` view under
``workers``: the board's own rows (:func:`..board.render.workers_view_blocks`),
each with Open in dashboard and, while it works, Stop (:mod:`.worker_stop`).
Written only when what it shows changed, at most once per
:data:`WORKERS_MIN_GAP_SECS` (``setView`` has special rate limits); a deferred
or failed write is retried through the board's own re-arm.

**Falling through.** A refusal is logged once per channel, tab and answer and
never breaks the channel: the board message, the context bar and the diff carry
on without the tab, and the links to it fall back to the forge. A type Slack does
not recognise (``invalid_view_type``) or a URL it will not take
(``invalid_pr_url``) is not asked again in this process; a full channel
(``too_many_views`` with nothing of ours to remove) is asked again after
:data:`FULL_RETRY_SECS`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.slack.code_channels import surface, views
from kiro_crew.slack.code_channels.models import CodeChannelApiError, CodeChannelError
from kiro_crew.slack.code_channels.surface_store import get_async

logger = logging.getLogger(__name__)

WORKERS_NAME = "Workers"
#: Shortest gap between two Workers-tab writes of one channel.
WORKERS_MIN_GAP_SECS = 15.0
#: How long a channel Slack said is full is left before its tab is asked for again.
FULL_RETRY_SECS = 600.0
#: How often a channel's recorded tabs are checked against Slack's own list
#: (someone may have removed one, which would leave its links pointing nowhere).
RELIST_SECS = 900.0
#: Answers no retry in this process cures, for this channel and tab.
_FOR_GOOD = frozenset({"invalid_view_type", "invalid_pr_url", "not_view_owner"})
#: Backoff for a failed Workers-tab write: first, then x3 per failure, capped.
_RETRY_FIRST_SECS = 5.0
_RETRY_CAP_SECS = 300.0
_MAX_TRACKED = 4096

#: The tabs' clock (monotonic seconds); tests hand it a fake.
_clock: Callable[[], float] = time.monotonic

#: ``(channel, tab key) -> (answer, until)``: a refusal, and when to ask again
#: (``inf`` for one no retry cures).
_refused: dict[tuple[str, str], tuple[str, float]] = {}
#: ``(channel, tab key, answer)`` already logged.
_logged: set[tuple[str, str, str]] = set()
#: Per channel: the digest of the Workers tab last written, when, the failures
#: since, and a retry the board's re-arm should schedule.
_sent: dict[str, str] = {}
_last_write: dict[str, float] = {}
_failures: dict[str, int] = {}
_retry_at: dict[str, float] = {}
#: Per channel: when its recorded tabs were last checked against listViews.
_listed_at: dict[str, float] = {}


@dataclass(frozen=True)
class PrRef:
    number: int
    url: str
    state: str


def wanted_prs(own: PrRef | None, rows: Any) -> list[PrRef]:
    """The PRs that get a tab: open (or draft), forge-confirmed, newest first, at most two.

    *own* is the channel's own PR (the workspace record's); *rows* the board's
    rows, PR-filled by the workspace refresh. A worker PR the forge did not
    confirm carries no URL and gets no tab.
    """
    from kiro_crew.slack.code_channels.board.render import _safe_url

    seen: dict[int, PrRef] = {}
    candidates = [own] if own is not None else []
    candidates += [
        PrRef(int(r.pr_number), str(r.pr_url or ""), str(r.pr_state or ""))
        for r in (rows or ())
        if getattr(r, "pr_number", 0)
    ]
    for pr in candidates:
        if pr.number <= 0 or pr.state not in {"open", "draft"} or not _safe_url(pr.url):
            continue
        seen.setdefault(pr.number, pr)
    return sorted(seen.values(), key=lambda p: p.number, reverse=True)[: views.MAX_PR_TABS]


def _note(channel: str, key: str, code: str, *, until: float | None = None) -> None:
    """Remember a refusal (when *until* is given) and log it once."""
    if until is not None:
        if len(_refused) > _MAX_TRACKED:
            _refused.clear()
        _refused[(channel, key)] = (code, until)
    mark = (channel, key, code)
    if mark in _logged:
        return
    if len(_logged) > _MAX_TRACKED:
        _logged.clear()
    _logged.add(mark)
    logger.warning(
        "code channel %s: the %s tab was not published (%s); the channel carries on without it",
        channel,
        key,
        code,
    )


def _held(channel: str, key: str) -> bool:
    refusal = _refused.get((channel, key))
    if refusal is None:
        return False
    if _clock() < refusal[1]:
        return True
    _refused.pop((channel, key), None)
    return False


def _refusal(channel: str, key: str, exc: CodeChannelApiError) -> None:
    if exc.code in _FOR_GOOD:
        _note(channel, key, exc.code, until=float("inf"))
    elif exc.code == views.TOO_MANY_VIEWS:
        _note(channel, key, exc.code, until=_clock() + FULL_RETRY_SECS)
    else:
        _note(channel, key, exc.code)


async def _may_write(binding: Any) -> bool:
    """The fences every write to the channel honours (archive, Stop, the channels policy)."""
    channel = str(getattr(binding, "channel_id", "") or "")
    if not channel or await surface.is_archived(binding) or surface.channel_stopped(channel):
        return False
    return await surface.outbound_permitted()


async def _relist(client: Any, channel: str, record: Any) -> Any:
    """Prune recorded tabs Slack no longer lists, at most once per :data:`RELIST_SECS`."""
    ours = [v for v in (getattr(record, "views", None) or ()) if v.get("view_id")]
    if not ours:
        return record
    now = _clock()
    last = _listed_at.get(channel)
    if last is not None and now - last < RELIST_SECS:
        return record
    if len(_listed_at) > _MAX_TRACKED:
        _listed_at.clear()
    _listed_at[channel] = now
    try:
        await views.reconcile(client, channel)
    except CodeChannelApiError as exc:
        logger.info("code channel %s: views not listed: %s", channel, exc.code)
        return record
    return await get_async(channel)


async def sync_pr_tabs(binding: Any, wanted: list[PrRef]) -> dict[int, str]:
    """Bring the channel's PR tabs to *wanted*; returns ``{PR number: view id}`` now held.

    Removes first (it frees room), then opens or re-points the wanted ones. Never
    raises.
    """
    channel = str(getattr(binding, "channel_id", "") or "")
    try:
        client = surface.client()
        if client is None or not await _may_write(binding):
            return {}
        record = await _relist(client, channel, await get_async(channel))
        held = [v for v in (getattr(record, "views", None) or ()) if views.is_pr_tab(v)]
        want = {views.pr_key(p.number): p for p in wanted}
        for view in held:
            if view["key"] not in want:
                await views.remove_ours(client, channel, view["key"])
        for key, pr in want.items():
            current = views.recorded(await get_async(channel), key)
            if current is not None and current.get("url") == pr.url:
                continue
            if _held(channel, key):
                continue
            await _open_pr_tab(client, channel, key, pr)
        record = await get_async(channel)
        return {
            views.pr_number(v): str(v["view_id"])
            for v in (getattr(record, "views", None) or ())
            if views.is_pr_tab(v) and v.get("view_id")
        }
    except Exception:
        logger.warning("code channel %s: pull request tabs not synced", channel, exc_info=True)
        return {}


async def _open_pr_tab(client: Any, channel: str, key: str, pr: PrRef) -> None:
    def _write() -> Any:
        return client.set_view(
            channel,
            type=views.PR_VIEW_TYPE,
            view_key=key,
            name=f"PR #{pr.number}",
            pr_url=pr.url,
        )

    try:
        await views.write_view(
            client, channel, key=key, type=views.PR_VIEW_TYPE, write=_write, url=pr.url
        )
    except CodeChannelApiError as exc:
        _refusal(channel, key, exc)
    except CodeChannelError as exc:  # refused locally, before it was sent
        _note(channel, key, str(exc)[:80], until=float("inf"))


def tabs_for(record: Any, channel: str, *, code: bool) -> Any:
    """The :class:`..board.render.Tabs` a message in *channel* may link, from its record."""
    from kiro_crew.slack.code_channels.board.render import Tabs

    entries = list(getattr(record, "views", None) or ())
    workers = views.recorded(record, views.WORKERS_KEY) if record is not None else None
    prs = tuple(
        sorted(
            ((views.pr_number(v), str(v["view_id"])) for v in entries if views.is_pr_tab(v)),
            reverse=True,
        )
    )
    return Tabs(
        channel_id=channel,
        workers=str(workers["view_id"]) if workers else "",
        code=code,
        prs=prs,
    )


def _digest(blocks: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(blocks, sort_keys=True, default=str).encode()).hexdigest()


def _stamp() -> str:
    return time.strftime("%H:%M UTC", time.gmtime())


async def publish_workers(
    binding: Any, snapshot: Any, *, link_for: Callable[[str], str], code: bool = False
) -> str:
    """Bring the channel's Workers tab up to *snapshot*; returns what it did. Never raises.

    ``""`` nothing to do, ``written``, ``deferred`` (inside the gap; the retry is
    armed), ``held`` (a refusal not yet due again), ``failed``, or a fence word.
    """
    channel = str(getattr(binding, "channel_id", "") or "")
    try:
        if snapshot is None or not getattr(snapshot, "rows", ()):
            return ""
        return await _publish_workers(binding, channel, snapshot, link_for=link_for, code=code)
    except Exception:
        logger.warning("code channel %s: Workers tab not published", channel, exc_info=True)
        return "failed"


async def _publish_workers(
    binding: Any, channel: str, snapshot: Any, *, link_for: Callable[[str], str], code: bool
) -> str:
    from kiro_crew.slack.code_channels.board.render import workers_view_blocks

    key = views.WORKERS_KEY
    if _held(channel, key):
        return "held"
    client = surface.client()
    if client is None:
        return "no_client"
    if not await _may_write(binding):
        return "fenced"
    record = await get_async(channel)
    tabs = tabs_for(record, channel, code=code)
    digest = _digest(workers_view_blocks(snapshot, link_for, tabs=tabs, updated=""))
    if _sent.get(channel) == digest and views.recorded(record, key) is not None:
        return ""
    now = _clock()
    last = _last_write.get(channel)
    if last is not None and now - last < WORKERS_MIN_GAP_SECS:
        _retry_at[channel] = WORKERS_MIN_GAP_SECS - (now - last)
        return "deferred"
    blocks = workers_view_blocks(snapshot, link_for, tabs=tabs, updated=_stamp())

    def _write() -> Any:
        return client.set_view(
            channel, type="block_kit", view_key=key, blocks=blocks, name=WORKERS_NAME
        )

    try:
        await views.write_view(client, channel, key=key, type="block_kit", write=_write)
    except CodeChannelApiError as exc:
        if exc.code in _FOR_GOOD or exc.code == views.TOO_MANY_VIEWS:
            _refusal(channel, key, exc)
            return "held"
        _note(channel, key, exc.code)
        return _failed(channel)
    except CodeChannelError as exc:
        _note(channel, key, str(exc)[:80], until=float("inf"))
        return "held"
    if len(_sent) > _MAX_TRACKED:
        _sent.clear()
        _last_write.clear()
    _sent[channel] = digest
    _last_write[channel] = now
    _failures.pop(channel, None)
    return "written"


def _failed(channel: str) -> str:
    count = _failures.get(channel, 0) + 1
    if len(_failures) > _MAX_TRACKED:
        _failures.clear()
    _failures[channel] = count
    _retry_at[channel] = min(_RETRY_FIRST_SECS * 3 ** (count - 1), _RETRY_CAP_SECS)
    return "failed"


def take_retry(channel: str) -> float:
    """The delay a retry of *channel*'s Workers tab should wait, once (``0``: none)."""
    return _retry_at.pop(channel, 0.0)


def forget(channel: str) -> None:
    """Drop the per-channel state (the channel was archived or wrapped up)."""
    for table in (_sent, _last_write, _failures, _retry_at, _listed_at):
        table.pop(channel, None)
    for mark in [k for k in _refused if k[0] == channel]:
        _refused.pop(mark, None)


def reset_for_tests() -> None:
    for table in (_refused, _sent, _last_write, _failures, _retry_at, _listed_at):
        table.clear()
    _logged.clear()
