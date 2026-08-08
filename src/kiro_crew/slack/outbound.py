"""The lifecycle of a posted Slack OPTIONS control.

Rendering text for Slack is NOT this module's job. ``slack.format`` owns that —
``render_for_slack`` for bodies and ``build_options_blocks`` for the control,
which redacts every choice through ``redact_for_display`` so a key split by ANSI,
emphasis, backticks or link markup is caught in the form Slack actually shows.
This module deliberately holds no second copy of that pipeline: an earlier
version did, and the two drifted apart until the same credential-exposure bug
had to be fixed twice, three review rounds apart.

What is left here is the part ``slack.format`` has no opinion about: a posted
control stays answerable until something spends it. ``PostedOptions`` carries
enough to find the control again, and ``expire_options`` renders it spent once
the conversation has moved past the question it asked.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kiro_crew.slack.format import build_options_selected_blocks, replace_options_blocks
from kiro_crew.slack.retry import is_retryable_slack_error

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)

#: Notification/fallback text for the message carrying an OPTIONS control.
OPTIONS_FALLBACK_TEXT = "Options"


@dataclass(frozen=True)
class PostedOptions:
    """A posted OPTIONS control, addressed well enough to expire it later.

    ``blocks`` is the block list exactly as posted. Keeping it means expiry can
    run the same block surgery the Send button uses, editing only the OPTIONS
    block and leaving any surrounding blocks (a timing footer, a
    Link-to-Dashboard button) intact — without re-fetching the message.
    """

    channel: str
    ts: str
    choices: tuple[str, ...]
    blocks: tuple[dict, ...]
    text: str = OPTIONS_FALLBACK_TEXT


_MAX_EDIT_LOCKS = 512
_EDIT_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_ANSWERED: dict[tuple[str, str], bool] = {}
# Threads with an OPTIONS answer currently routing: thread_ts -> the dispatch
# tasks carrying it. See :func:`track_answer_routing`.
_ANSWER_ROUTING: dict[str, set[asyncio.Task[Any]]] = {}
# Interested parties per edit-lock key: holders plus coroutines waiting to hold.
_LOCK_USERS: dict[tuple[str, str], int] = {}


def claim_options_answer(channel: str, ts: str) -> bool:
    """Claim the right to answer the OPTIONS control posted as *ts*. Once only.

    Returns True for the FIRST caller and False for every later one, so a second
    Send click on the same message is a no-op instead of a second turn. Two rapid
    clicks produce two handler tasks; without this the first would render the
    selection and dispatch it, and the second would dispatch the same answer
    again, giving the conversation a duplicate (or, once the first turn has moved
    on, a superseded) turn.

    MUST be called while holding :func:`options_edit_lock` for the same message —
    the check and the claim are only atomic under that lock.

    Deliberately NOT "is a record still tracked". ``remember_slack_options`` is a
    documented no-op when the session has no dashboard slot, which is the normal
    state for a plain Slack conversation, so keying click validity on record
    presence would reject every legitimate click in those threads. "Has this
    control already been answered" is a different question and is answerable here
    without a slot.

    Bounded, but NEVER at the cost of a control that can still be clicked. The
    value records whether the control's buttons are provably GONE from Slack --
    the in-place edit landed, the original was deleted, or the expiry's
    strike-through succeeded. Only such settled entries are evictable.

    An earlier version evicted the oldest entry outright, justified as "a dropped
    entry can only re-admit a click on a control old enough to have aged out of a
    512-message window, which the turn-start expiry has long since struck
    through". That is wrong, and wrong in the same way this PR's other bounds
    were: eviction here is by insertion order across the WHOLE workspace, not by
    age within one conversation, so traffic in busy channels can evict the claim
    on a control still sitting unanswered in a quiet thread. When the render or
    the strike-through failed those buttons are genuinely still on screen, and
    this entry is the only thing standing between a second click and a duplicate
    -- or superseded -- turn. Unsettled entries are inherently few (one per
    control mid-edit or whose edit failed), so refusing to evict them keeps the
    bound meaningful while making it safe.
    """
    key = (channel, ts)
    if key in _ANSWERED:
        return False
    _ANSWERED[key] = False
    _evict_settled_answers()
    return True


def _evict_settled_answers() -> None:
    """Trim the claim map, dropping ONLY controls whose buttons are gone.

    Stops at the first pass that finds nothing evictable, so a map full of live
    claims grows past the cap rather than re-admitting a click. That is the right
    trade: the cap exists to bound memory, not to bound correctness.
    """
    if len(_ANSWERED) <= _MAX_EDIT_LOCKS:
        return
    for key, settled in list(_ANSWERED.items()):
        if len(_ANSWERED) <= _MAX_EDIT_LOCKS:
            return
        if settled:
            _ANSWERED.pop(key, None)


def settle_options_answer(channel: str, ts: str) -> None:
    """Mark this control's buttons as provably gone, making the claim evictable.

    Called once the original message is known to be off screen -- rewritten in
    place, or replaced and successfully deleted. Until then the claim is pinned:
    a click can still arrive and must still lose.
    """
    if (channel, ts) in _ANSWERED:
        _ANSWERED[(channel, ts)] = True


def track_answer_routing(thread_ts: str, task: asyncio.Task[Any]) -> None:
    """Remember that an OPTIONS answer for *thread_ts* is on its way to a session.

    A click that succeeds does two things: it FORGETS the record (the control is
    spent) and it dispatches the answer as a task. Between those, the record --
    which is what the unlink handler consults to decide whether the thread is
    still needed -- is already gone, while the answer has not yet resolved which
    session it belongs to. An unlink landing in that window pops the thread from
    the reverse index and the answer starts a BRAND-NEW Slack session carrying the
    user's selection: the same corruption the unlink ordering was built to
    prevent, reached from the other side.

    Registered by task, not by a counter, so nothing can leak: a task that
    finishes -- or raises, or is cancelled -- stops counting as in flight by
    itself, and a stuck counter that made unlink refuse forever would be a worse
    bug than the one being fixed.
    """
    bucket = _ANSWER_ROUTING.setdefault(thread_ts, set())
    bucket.add(task)

    def _done(finished: asyncio.Task[Any]) -> None:
        live = _ANSWER_ROUTING.get(thread_ts)
        if live is None:
            return
        live.discard(finished)
        if not live:
            _ANSWER_ROUTING.pop(thread_ts, None)

    task.add_done_callback(_done)


def answer_routing_in_flight(thread_ts: str) -> bool:
    """Is an OPTIONS answer for *thread_ts* still finding its session?

    Prunes finished tasks on the way past, so a missed callback cannot pin a
    thread as busy forever.
    """
    if not thread_ts:
        return False
    live = _ANSWER_ROUTING.get(thread_ts)
    if not live:
        return False
    for task in list(live):
        if task.done():
            live.discard(task)
    if not live:
        _ANSWER_ROUTING.pop(thread_ts, None)
        return False
    return True


def release_options_answer(channel: str, ts: str) -> None:
    """Give the claim back, because nothing was actually answered.

    A claim means "this control's answer has been dealt with". If the submit that
    took it then fails to render the selection at all -- the in-place edit fails
    AND the replacement post fails -- nothing happened, the buttons are still on
    screen, and holding the claim would refuse every retry forever: a control
    permanently visible and permanently unanswerable.

    Only for the no-op case. Once the message shows the selection the claim must
    STAY, even if a later step stumbles, because the answer is on screen and the
    turn is on its way.
    """
    _ANSWERED.pop((channel, ts), None)


def mark_options_terminal(channel: str, ts: str) -> None:
    """Record that this control can never be answered again.

    Called by the expiry once it has struck the choices through, while it still
    holds the message's edit lock. Without this, a Send click queued behind a
    successful expiry would find the claim unheld, take it, and dispatch an answer
    to the question the expiry just retired -- the stale click this whole lifecycle
    exists to prevent, arriving through the one door the click-vs-click claim does
    not cover.

    Same store as :func:`claim_options_answer`, so the loser is refused by exactly
    the check a duplicate click hits.

    Recorded as SETTLED: the caller only reaches here once ``expire_options``
    confirmed the strike-through edit landed, so the buttons are gone and this
    entry may be evicted under memory pressure.
    """
    _ANSWERED[(channel, ts)] = True
    _evict_settled_answers()


def options_edit_lock(channel: str, ts: str) -> "_OptionsEditLock":
    """The one lock guarding edits to a single OPTIONS message.

    Two writers race for the same Slack message: this module's expiry, and the
    Send handler rewriting it with the user's selection. Without a shared lock the
    expiry's edit can land AFTER the selection's and erase the answer the user
    just gave. Slack offers no compare-and-set on an edit, so ordering has to be
    imposed here — and it can be, because both writers run in this one gateway
    process. (Two gateways driving the same workspace would defeat it; that is not
    a supported topology.)

    Holding this lock is not sufficient on its own: the expiry must ALSO re-read,
    inside the lock, whether its record is still tracked — a Send that won the
    race forgets the record, and that is what tells the expiry to skip its edit.

    Created lazily and never awaited between lookup and insert, so no registry
    lock is needed on a single-threaded event loop. Bounded: once the registry
    exceeds ``_MAX_EDIT_LOCKS``, uncontended entries are dropped, since a lock
    nobody holds carries no state worth keeping.
    """
    key = (channel, ts)
    lock = _EDIT_LOCKS.get(key)
    if lock is None:
        lock = _EDIT_LOCKS[key] = asyncio.Lock()
    _prune_edit_locks(key)
    return _OptionsEditLock(key, lock)


def _prune_edit_locks(keep: tuple[str, str]) -> None:
    """Trim the registry, dropping ONLY entries nobody is using.

    ``not lock.locked()`` is not that test. A lock that was just released still
    has its next waiter scheduled but not yet resumed, and it reads as unlocked in
    that window: evicting it there hands the following caller a BRAND-NEW lock for
    the same message while the waiter proceeds on the old one, so two coroutines
    edit one Slack message at once and every guarantee built on this lock is off.

    So interest is counted explicitly, registered before the acquire and dropped
    after the release, and an entry with any interest is never evicted. As in the
    answer-claim map, the cap bounds memory, not correctness: a registry full of
    live locks grows past it rather than splitting synchronization.
    """
    if len(_EDIT_LOCKS) <= _MAX_EDIT_LOCKS:
        return
    for stale_key, stale in list(_EDIT_LOCKS.items()):
        if len(_EDIT_LOCKS) <= _MAX_EDIT_LOCKS:
            return
        if stale_key != keep and not stale.locked() and not _LOCK_USERS.get(stale_key):
            del _EDIT_LOCKS[stale_key]


class _OptionsEditLock:
    """Async context manager that pins its registry entry while anyone wants it.

    Holds a direct reference to the lock, so even a pathological eviction could
    not swap it for a different object mid-flight.
    """

    __slots__ = ("_key", "_lock")

    def __init__(self, key: tuple[str, str], lock: asyncio.Lock) -> None:
        self._key = key
        self._lock = lock

    async def __aenter__(self) -> "_OptionsEditLock":
        # Registered BEFORE the await: a coroutine waiting on the lock has to be
        # visible to the pruner, and after `acquire()` suspends it is too late.
        _LOCK_USERS[self._key] = _LOCK_USERS.get(self._key, 0) + 1
        try:
            await self._lock.acquire()
        except BaseException:
            # Cancelled while waiting -- give the interest back or the entry is
            # pinned forever, which is the leak the answer-claim map avoids too.
            self._drop_interest()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        self._lock.release()
        self._drop_interest()
        return False

    def _drop_interest(self) -> None:
        remaining = _LOCK_USERS.get(self._key, 1) - 1
        if remaining > 0:
            _LOCK_USERS[self._key] = remaining
        else:
            _LOCK_USERS.pop(self._key, None)


async def expire_options(slack: SlackClientOps, posted: PostedOptions) -> bool:
    """Render a previously-posted OPTIONS control as spent.

    Strikes every choice through, so a control the conversation has moved past
    reads as unanswerable rather than inviting a click that would answer a
    superseded question. Only the OPTIONS block is replaced; surrounding blocks
    survive.

    Returns True when the record is SETTLED and the caller should stop tracking
    it — either the edit landed, or it failed in a way that will fail identically
    forever (a deleted message, a channel we are not in, a malformed payload).
    Returns False only for a transient failure, so the caller can keep the record
    and try again on a later turn instead of leaving a live control on screen
    with nothing tracking it.

    Still never raises: a thread that keeps a stale control is bad, but it is not
    worth disrupting the turn that triggered the cleanup.
    """
    try:
        spent = build_options_selected_blocks(list(posted.choices), [])
        blocks = replace_options_blocks(list(posted.blocks), spent)
        await slack.update_message(posted.channel, posted.ts, text=posted.text, blocks=blocks)
        return True
    except Exception as exc:
        if is_retryable_slack_error(exc):
            logger.debug(
                "Slack OPTIONS control expiry failed transiently; keeping the "
                "record so a later turn retries it",
                exc_info=True,
            )
            return False
        logger.debug("Failed to expire Slack OPTIONS control", exc_info=True)
        return True
