"""The channel's own tabs: native pull-request tabs and the interactive Workers tab.

Both are published by the channel's own refresh -- the workspace service's, on
the same snapshot as the board, the context bar and the Code tab -- and recorded
as ours in :mod:`.views`, whose budget decides who gives way at Slack's five tabs
(Plan, Code and Workers never; then these PR tabs; the agent's own tabs first).

**Pull-request tabs** (:func:`sync_pr_tabs`). ``setView`` ``type=pull_request``
with the PR's URL, upserted under ``pr-<number>`` and named ``PR #<number>``, for
the channel's newest open PRs -- its own and its workers', the PRs the board and
the context bar already show (:func:`wanted_prs`) -- at most
:data:`~.views.MAX_PR_TABS`. A tab is removed (by the view id recorded when this
gateway opened it; nothing else is ever removed) only on positive evidence: its
PR was seen merged or closed, or two newer open PRs were seen. A pass that could
not read a PR (the board or the forge unavailable) removes nothing for it.
Nothing is written while the record already holds the wanted set.

**The Workers tab** (:func:`publish_workers`). A ``block_kit`` view under
``workers``: the board's own rows (:func:`..board.render.workers_view_blocks`),
each with Open in dashboard and, while it works, Stop (:mod:`.worker_stop`).
Written only when what it shows changed -- elapsed times aside, which are
refreshed every :data:`ELAPSED_REFRESH_SECS` -- and at most once per
:data:`WORKERS_MIN_GAP_SECS` (``setView`` has special rate limits). A failed
write backs off (5 s, x3, capped at 5 min, or Slack's ``Retry-After``); the
deferred or backed-off write is re-armed through the board's own refresh. A
board with no rows left leaves one empty-state tab, not stale Stop buttons.

**Falling through.** A refusal is logged once per channel, tab and answer and
never breaks the channel: the board message, the context bar and the diff carry
on without the tab, and the links to it fall back to the forge. A view type
Slack does not recognise (``invalid_view_type``) is not asked for again in this
process for that channel, nor a URL it will not take (``invalid_pr_url``) for
that PR; a full channel (``too_many_views`` with nothing of ours to remove) is
asked again after :data:`FULL_RETRY_SECS`; anything else backs off.

Every write checks the archive, Stop and ``channels`` fences again immediately
before it is sent (the retry after making room included).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.slack.code_channels import surface, views
from kiro_crew.slack.code_channels.models import CodeChannelApiError, CodeChannelError
from kiro_crew.slack.code_channels.surface_store import get_async

logger = logging.getLogger(__name__)

WORKERS_NAME = "Workers"
#: Shortest gap between two Workers-tab writes of one channel.
WORKERS_MIN_GAP_SECS = 15.0
#: How often a Workers tab whose only change is elapsed time is rewritten.
ELAPSED_REFRESH_SECS = 300.0
#: How long a channel Slack said is full is left before its tab is asked for again.
FULL_RETRY_SECS = 600.0
#: How often a channel's recorded tabs are checked against Slack's own list
#: (someone may have removed one, which would leave its links pointing nowhere).
RELIST_SECS = 900.0
#: A refusal answered with "fenced" by our own pre-send check (not Slack's).
FENCED = views.FENCED
#: Refusals that are a fence, not a failure: nothing is held or retried for them
#: (``views.STOPPED``: Slack's Stop landed while room was made).
_FENCED_CODES = frozenset(
    {
        FENCED,
        views.STOPPED,
        views.CHANNEL_OFF,
        # surface.write_refusal's own codes (the package's write gate).
        "channels_policy",
        "archived",
        "out_of_scope",
        "error",
    }
)
#: Backoff for a failed write: first, then x3 per failure, capped.
_RETRY_FIRST_SECS = 5.0
_RETRY_CAP_SECS = 300.0
#: 5 s x 3**4 = 405 s, already past the cap: no larger power is ever needed.
_RETRY_MAX_EXPONENT = 4
#: The most a Slack ``Retry-After`` may hold a tab back (a bad header never strands it).
_RETRY_AFTER_CAP_SECS = 3600.0
_MAX_TRACKED = 4096
_FOREVER = float("inf")

#: The tabs' clock (monotonic seconds); tests hand it a fake.
_clock: Callable[[], float] = time.monotonic

#: ``(channel, hold key) -> (answer, until)``: do not ask before *until*
#: (``inf`` for a refusal no retry cures). Hold keys are a tab key, ``remove:<key>``
#: or ``type:<view type>``.
_held_until: dict[tuple[str, str], tuple[str, float]] = {}
#: ``(channel, hold key) -> failures in a row`` for the backoff.
_failures: dict[tuple[str, str], int] = {}
#: ``(channel, tab key, answer)`` already logged.
_logged: set[tuple[str, str, str]] = set()
#: Per channel: the digest of the Workers tab last written (elapsed times left
#: out) and when, and a retry the board's re-arm should schedule.
_sent: dict[str, str] = {}
_last_write: dict[str, float] = {}
_retry_at: dict[str, float] = {}
#: Per channel: when its recorded tabs were last checked against listViews.
_listed_at: dict[str, float] = {}


@dataclass(frozen=True)
class PrRef:
    number: int
    url: str
    state: str


def _candidates(own: PrRef | None, rows: Any) -> list[PrRef]:
    out = [own] if own is not None else []
    out += [
        PrRef(int(r.pr_number), str(r.pr_url or ""), str(r.pr_state or ""))
        for r in (rows or ())
        if getattr(r, "pr_number", 0)
    ]
    return [p for p in out if p.number > 0]


def wanted_prs(own: PrRef | None, rows: Any) -> list[PrRef]:
    """The PRs that get a tab: open (or draft), forge-confirmed, newest first, at most two.

    *own* is the channel's own PR (the workspace record's); *rows* the board's
    rows, PR-filled by the workspace refresh. A worker PR the forge did not
    confirm carries no URL and gets no tab.
    """
    from kiro_crew.slack.code_channels.board.render import _safe_url

    seen: dict[int, PrRef] = {}
    for pr in _candidates(own, rows):
        if pr.state in {"open", "draft"} and _safe_url(pr.url):
            seen.setdefault(pr.number, pr)
    return sorted(seen.values(), key=lambda p: p.number, reverse=True)[: views.MAX_PR_TABS]


def settled_prs(own: PrRef | None, rows: Any) -> set[int]:
    """The PR numbers this pass positively saw merged or closed."""
    return {p.number for p in _candidates(own, rows) if p.state in {"merged", "closed"}}


def _note(channel: str, key: str, code: str, *, until: float | None = None) -> None:
    """Hold *key* until *until* (when given) and log the refusal once."""
    if until is not None:
        if len(_held_until) > _MAX_TRACKED:
            _held_until.clear()
        _held_until[(channel, key)] = (code, until)
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


def _held(channel: str, key: str) -> float:
    """Seconds until *key* may be asked again (``0``: now)."""
    hold = _held_until.get((channel, key))
    if hold is None:
        return 0.0
    left = hold[1] - _clock()
    if left > 0:
        return left
    _held_until.pop((channel, key), None)
    return 0.0


def _backoff(channel: str, key: str, code: str, retry_after: float = 0.0) -> float:
    """Count a failure of *key* and hold it for the backoff; returns the delay."""
    if len(_failures) > _MAX_TRACKED:
        _failures.clear()
    count = _failures.get((channel, key), 0) + 1
    _failures[(channel, key)] = min(count, _RETRY_MAX_EXPONENT + 1)  # never grows unbounded
    # The exponent is clamped BEFORE the power is taken: the cap is reached long
    # before it, and an unbounded power overflows a float after ~650 failures.
    backoff = _RETRY_FIRST_SECS * 3 ** min(count - 1, _RETRY_MAX_EXPONENT)
    # A Retry-After that is no finite, non-negative number counts as none.
    asked = retry_after if math.isfinite(retry_after) and retry_after > 0 else 0.0
    delay = max(min(backoff, _RETRY_CAP_SECS), min(asked, _RETRY_AFTER_CAP_SECS))
    _note(channel, key, code, until=_clock() + delay)
    return delay


def _succeeded(channel: str, key: str) -> None:
    _failures.pop((channel, key), None)
    _held_until.pop((channel, key), None)


def _refusal(channel: str, key: str, view_type: str, exc: CodeChannelApiError) -> float:
    """Hold *key* (or the whole view type) after Slack's refusal; returns the delay."""
    if exc.code == "invalid_view_type":
        _note(channel, f"type:{view_type}", exc.code, until=_FOREVER)
        return 0.0
    if exc.code in {"invalid_pr_url", "not_view_owner"}:
        _note(channel, key, exc.code, until=_FOREVER)
        return 0.0
    if exc.code == views.TOO_MANY_VIEWS:
        _note(channel, key, exc.code, until=_clock() + FULL_RETRY_SECS)
        return 0.0
    return _backoff(channel, key, exc.code)


async def _write_refusal(binding: Any, *, ledger: bool = True) -> str:
    """Why a tab write or removal may not go out now (``""``: it may).

    The package's write gate (:func:`..surface.write_refusal`: the channels policy
    first, then archive and Slack's Stop read after it, with no await between)
    plus, for what the conductor's ledger supplies (*ledger*: the Workers tab and
    the workers' PR tabs), the board's scope, so a channel that turned shared or
    whose slot was relinked gets none of those written or removed from here. The
    channel's OWN PR tab (from its workspace record, not the ledger) is not ledger
    content: it needs no worker hand-off (``ledger=False``) and is held to the
    channel's activation (Off or Review) instead, which the scope also asks.
    """
    from kiro_crew.slack.code_channels.board import source

    channel = str(getattr(binding, "channel_id", "") or "")
    if not channel:
        return FENCED
    if ledger:
        return await surface.write_refusal(
            channel,
            binding,
            scope=lambda: source.scope_admits(surface.dashboard_state(), binding),
        )
    return await surface.write_refusal(channel, binding, activation=True)


async def _may_write(binding: Any, *, ledger: bool = True) -> bool:
    return not await _write_refusal(binding, ledger=ledger)


def _fence(binding: Any, *, ledger: bool = True) -> Callable[[], Awaitable[str]]:
    """The same gate, for :mod:`.views` to ask right before each write, each removal
    and the resend (``""``: admitted, else the refusal code)."""

    async def _ask() -> str:
        return await _write_refusal(binding, ledger=ledger)

    return _ask


def _fenced_write(
    binding: Any, send: Callable[[], Any], *, ledger: bool = True
) -> Callable[[], Any]:
    """*send* behind the fences, checked again right before every attempt."""

    async def _write() -> Any:
        if not await _may_write(binding, ledger=ledger):
            raise CodeChannelApiError("agents.conversations.setView", FENCED)
        return await send()

    return _write


async def _relist(client: Any, channel: str, record: Any) -> Any:
    """Prune recorded tabs missing from Slack's list, at most once per :data:`RELIST_SECS`."""
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


async def sync_pr_tabs(
    binding: Any, wanted: list[PrRef], settled: set[int] | None = None, *, own: int = 0
) -> dict[int, str]:
    """Bring the channel's PR tabs toward *wanted*; returns ``{PR number: view id}`` held.

    *settled* is the PRs this pass saw merged or closed. A held tab is removed
    only when its PR is settled, or when *wanted* already fills every PR slot
    with newer PRs; one this pass simply did not see stays. Removes first (it
    frees room), then opens or re-points the wanted ones. Never raises.

    *own* is the channel's own PR number (its workspace record's). Its tab is the
    channel's, not the ledger's: it is kept current outside the board's scope too
    (worker hand-off off, a shared channel). A worker's PR tab is opened or
    re-pointed or removed only while the board's scope admits the channel. Out of
    scope only the own PR is wanted or known settled, the workers' tabs stay as
    they are, and the own PR waits for a free slot if they fill both.
    """
    from kiro_crew.slack.code_channels.board import source

    channel = str(getattr(binding, "channel_id", "") or "")
    settled = settled or set()
    own_key = views.pr_key(own) if own else ""

    def ledger(key: str) -> bool:
        return key != own_key

    try:
        client = surface.client()
        if client is None or not await _may_write(binding, ledger=False):
            return {}
        record = await _relist(client, channel, await get_async(channel))
        held = [v for v in (getattr(record, "views", None) or ()) if views.is_pr_tab(v)]
        scoped = source.scope_admits(surface.dashboard_state(), binding)
        if not scoped:
            # Out of the board's scope: only the channel's own PR is wanted or
            # known settled; nothing the ledger supplied is used.
            wanted = [p for p in wanted if own_key and views.pr_key(p.number) == own_key]
            settled = {n for n in settled if own_key and views.pr_key(n) == own_key}
        want = {views.pr_key(p.number): p for p in wanted}
        stale = [v for v in held if v["key"] not in want]
        unseen = [v for v in stale if views.pr_number(v) not in settled]
        # An unseen tab (its PR was not read this pass) goes only when the newest
        # PR slots are taken by newer PRs SEEN open; otherwise it keeps its slot,
        # even over an older wanted PR, which then waits for room.
        ranked = sorted(
            [(p.number, "want", k) for k, p in want.items()]
            + [(views.pr_number(v), "keep", v["key"]) for v in unseen],
            reverse=True,
        )[: views.MAX_PR_TABS]
        keep = [v for v in unseen if (views.pr_number(v), "keep", v["key"]) in ranked]
        want = {k: p for k, p in want.items() if (p.number, "want", k) in ranked}
        for view in stale:
            if view in keep or _held(channel, f"remove:{view['key']}"):
                continue
            # By what the tab SHOWS, recorded when it was written: a former own
            # PR's tab stays the channel's after its own PR moves on.
            worker = views.from_ledger(view)
            if not scoped and worker:
                continue  # a worker's tab: neither written nor removed out of scope
            # A worker's tab is asked the scope again right before its removal;
            # the channel's own takes the channel fences only.
            fence = _fence(binding, ledger=worker)
            if await views.remove_ours(client, channel, view["key"], admits=fence):
                _succeeded(channel, f"remove:{view['key']}")
            elif await fence():
                continue  # fenced, not failed: nothing to back off from
            else:
                _backoff(channel, f"remove:{view['key']}", "view_remove_failed")
        for key, pr in want.items():
            # Checked per PR: a refusal of the type on one PR stops the rest.
            if _held(channel, f"type:{views.PR_VIEW_TYPE}"):
                break
            record = await get_async(channel)
            current = views.recorded(record, key)
            if current is not None and current.get("url") == pr.url:
                # Up already. Its origin follows whose PR it now shows (a worker's
                # PR that became the channel's own, or the reverse), so its later
                # removal is fenced by that.
                origin = views.ORIGIN_WORKER if ledger(key) else views.ORIGIN_OWN
                # Re-labelled as the channel's own only while the board's scope
                # admits it (asked right before, no await between): out of scope a
                # worker's tab stays exactly as it is.
                if origin == views.ORIGIN_WORKER or source.scope_admits(
                    surface.dashboard_state(), binding
                ):
                    await views.set_origin(channel, key, origin)
                continue
            if _held(channel, key):
                continue
            recorded = getattr(record, "views", None) or ()
            if current is None and sum(views.is_pr_tab(v) for v in recorded) >= (views.MAX_PR_TABS):
                # A removal that failed still holds its slot: a new PR waits.
                continue
            await _open_pr_tab(client, binding, key, pr, ledger=ledger(key))
        record = await get_async(channel)
        return {
            views.pr_number(v): str(v["view_id"])
            for v in (getattr(record, "views", None) or ())
            if views.is_pr_tab(v) and v.get("view_id")
        }
    except Exception:
        logger.warning("code channel %s: pull request tabs not synced", channel, exc_info=True)
        return {}


async def _open_pr_tab(
    client: Any, binding: Any, key: str, pr: PrRef, *, ledger: bool = True
) -> None:
    channel = binding.channel_id

    def _send() -> Any:
        return client.set_view(
            channel,
            type=views.PR_VIEW_TYPE,
            view_key=key,
            name=f"PR #{pr.number}",
            pr_url=pr.url,
        )

    try:
        await views.write_view(
            client,
            channel,
            key=key,
            type=views.PR_VIEW_TYPE,
            write=_fenced_write(binding, _send, ledger=ledger),
            url=pr.url,
            admits=_fence(binding, ledger=ledger),
            origin=views.ORIGIN_WORKER if ledger else views.ORIGIN_OWN,
        )
    except CodeChannelApiError as exc:
        if exc.code not in _FENCED_CODES:
            _refusal(channel, key, views.PR_VIEW_TYPE, exc)
        return
    except CodeChannelError as exc:  # refused locally, before it was sent
        _note(channel, key, str(exc)[:80], until=_FOREVER)
        return
    _succeeded(channel, key)


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


#: What the tab says once its board has no workers left.
EMPTY_BLOCKS: tuple[dict[str, Any], ...] = (
    {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*No workers right now.* New ones appear here."},
    },
)


async def publish_workers(
    binding: Any,
    snapshot: Any,
    *,
    link_for: Callable[[str], str],
    code: bool = False,
    urgent: bool = False,
) -> str:
    """Bring the channel's Workers tab up to *snapshot*; returns what it did. Never raises.

    ``""`` nothing to do, ``written``, ``deferred`` (inside the gap; the retry is
    armed), ``backoff`` (after a failure; the retry is armed), ``held`` (a refusal
    not yet due again), ``failed``, or a fence word.
    """
    channel = str(getattr(binding, "channel_id", "") or "")
    try:
        if snapshot is None:
            return ""
        return await _publish_workers(
            binding, channel, snapshot, link_for=link_for, code=code, urgent=urgent
        )
    except Exception:
        logger.warning("code channel %s: Workers tab not published", channel, exc_info=True)
        return "failed"


async def _publish_workers(
    binding: Any,
    channel: str,
    snapshot: Any,
    *,
    link_for: Callable[[str], str],
    code: bool,
    urgent: bool = False,
) -> str:
    from kiro_crew.slack.code_channels.board.render import workers_view_blocks

    key = views.WORKERS_KEY
    if _held(channel, "type:block_kit"):
        return "held"
    wait = _held(channel, key)
    if wait:
        _retry_at[channel] = wait
        return "backoff"
    client = surface.client()
    if client is None:
        return "no_client"
    if not await _may_write(binding):
        return "fenced"
    record = await get_async(channel)
    from kiro_crew.slack.code_channels.board import stops

    # The last await before rendering: a worker that moved to a newer turn
    # meanwhile is not shown as stopped.
    snapshot = stops.reconcile(snapshot)
    exists = views.recorded(record, key) is not None
    rows = tuple(getattr(snapshot, "rows", ()) or ())
    if not rows and not exists:
        return ""  # nothing to show, and no tab to empty
    tabs = tabs_for(record, channel, code=code)
    if rows:
        digest = _digest(
            workers_view_blocks(snapshot, link_for, tabs=tabs, updated="", elapsed=False)
        )
    else:
        digest = _digest(list(EMPTY_BLOCKS))
    now = _clock()
    last = _last_write.get(channel)
    ticking = bool(rows) and last is not None and now - last >= ELAPSED_REFRESH_SECS
    if _sent.get(channel) == digest and exists and not ticking:
        return ""
    if not urgent and last is not None and now - last < WORKERS_MIN_GAP_SECS:
        _retry_at[channel] = WORKERS_MIN_GAP_SECS - (now - last)
        return "deferred"
    blocks = (
        workers_view_blocks(snapshot, link_for, tabs=tabs, updated=_stamp())
        if rows
        else [dict(b) for b in EMPTY_BLOCKS]
    )

    def _send() -> Any:
        return client.set_view(
            channel, type="block_kit", view_key=key, blocks=blocks, name=WORKERS_NAME
        )

    try:
        _view, removed = await views.write_view(
            client,
            channel,
            key=key,
            type="block_kit",
            write=_fenced_write(binding, _send),
            admits=_fence(binding),
        )
    except CodeChannelApiError as exc:
        if exc.code in _FENCED_CODES:
            return "fenced"
        delay = _refusal(channel, key, "block_kit", exc)
        if delay:
            _retry_at[channel] = delay
            return "failed"
        return "held"
    except CodeChannelError as exc:
        _note(channel, key, str(exc)[:80], until=_FOREVER)
        return "held"
    _succeeded(channel, key)
    if len(_last_write) > _MAX_TRACKED:
        for table in (_sent, _last_write):
            for stale in [c for c in table if c != channel][: _MAX_TRACKED // 2]:
                table.pop(stale, None)
    _last_write[channel] = now
    if views.is_pr_tab({"key": removed, "type": views.PR_VIEW_TYPE}):
        # A PR tab removed to fit this one is linked from what was just written.
        # The next pass writes it again without that link, and one is armed for
        # when the edit gap allows it: a finished board gets no other trigger.
        _sent[channel] = ""
        _retry_at[channel] = max(_retry_at.get(channel, 0.0), WORKERS_MIN_GAP_SECS)
        return "written"
    _sent[channel] = digest
    return "written"


def take_retry(channel: str) -> float:
    """The delay a retry of *channel*'s Workers tab should wait, once (``0``: none)."""
    return _retry_at.pop(channel, 0.0)


def forget(channel: str) -> None:
    """Drop the per-channel state (the channel was archived or wrapped up)."""
    for table in (_sent, _last_write, _retry_at, _listed_at):
        table.pop(channel, None)
    for mark in [k for k in _held_until if k[0] == channel]:
        _held_until.pop(mark, None)
    for mark in [k for k in _failures if k[0] == channel]:
        _failures.pop(mark, None)


def reset_for_tests() -> None:
    for table in (_held_until, _failures, _sent, _last_write, _retry_at, _listed_at):
        table.clear()
    _logged.clear()
