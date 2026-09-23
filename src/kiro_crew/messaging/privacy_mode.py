"""Channel-neutral ``!temporary`` / ``!incognito`` session privacy modifiers.

Two modes, and what each one actually forbids:

Temporary (blank-slate)
    No memory reads, no memory writes, no persistence. The session starts with
    zero context and discards everything on close.
Incognito
    Memory reads are allowed but writes are blocked; the ephemeral conversation
    log is discarded on close.

Both are tracked in bounded LRUs keyed by **session key**, never by a platform
thread id. That is what lets one copy of the machinery serve a Slack thread ts,
a Telegram DM route and a Telegram forum Topic without any of them colliding —
and it is why :func:`is_restricted` answers correctly for a
``telegram:{agent}:direct:{user}`` key, which a ``startswith("slack:")`` test
never could. The LRUs are process-local; :func:`hydrate` rebuilds them from the
durable ``SessionMap`` flag so a mode survives a gateway restart.

What a second channel must supply
---------------------------------
Everything platform-shaped is a parameter, because this module may not import
``kiro_crew.slack`` or ``kiro_crew.dashboard`` — not even function-locally (the
one-way dependency invariant in ``docs/system-specs/modules/messaging.md``):

* ``source`` — the channel name, e.g. ``"slack"``. It is the audit label and
  nothing else: the SEL operation is ``f"{source}.{mode}_mode"`` and the event's
  ``source`` field is ``source`` verbatim.
* ``sessions`` — the ``SessionManager``. Supplying it makes the mode DURABLE, in
  two records for two readers: the per-conversation flag in the one
  :class:`~kiro_crew.session_map.SessionMap` instance it owns (so the durable
  flag cannot be clobbered by a second instance's save), which is what
  :func:`hydrate` restores the trackers from on every inbound message and which
  keeps the entry alive through ``SessionMap.prune``; and ``memory_mode`` in the
  session's own transcript header, the field every memory reader already
  honours (``is_incognito_transcript``), so a transcript read never depends on
  the map being loaded. Omit it and the mode is in-memory only, which is also
  what a test double passing no ``sessions`` gets. The header write goes through
  the default ``ConversationLog``: every production log reads the one configured
  sessions directory, so it reaches the same file the channel writes.
* ``notify`` — an awaitable that delivers one confirmation message on the
  channel. The text is :data:`NOTICE_TEMPORARY` / :data:`NOTICE_INCOGNITO`, held
  here so two channels cannot describe the same mode differently.
* ``on_applied`` — optional, awaited once per NEWLY applied mode for the
  bookkeeping a mode change implies on that channel. Slack registers the thread
  (``set_slack_link``) so follow-up messages pass its in-active-thread gate; a
  channel that routes off the conversation id has nothing to do here.

Two entry points, because the two shapes are genuinely different:

* :func:`strip_and_apply` — one text in, ``(stripped_text, only_modifier)`` out.
  This is what a channel whose inbound message is a single string calls.
* :func:`strip_token` + :func:`apply_mode` — the primitives. Slack drives these
  directly because it carries TWO texts (the LLM-facing message and the
  mention-stripped command text) and only the command text decides whether the
  message was nothing BUT a modifier.

Applying a mode is idempotent: a repeat ``!incognito`` in an already-incognito
session neither re-audits nor re-notifies, so a user cannot spam the channel by
repeating the token.

One path from a request to a published mode
--------------------------------------------
Every mode a caller asks for -- a modifier on a message (:func:`apply_mode`), a
reservation ahead of a steer (:func:`reserve`) -- is published by exactly one
function, :func:`_commit_mode`, whose docstring is the ONE statement of the
order (its numbered steps; nothing here or at the callers restates them, so
there is one text to drift): the durable records first, awaited to disk, and
the publication only after them. A write that fails publishes nothing and refuses the turn
(``persist_failed``): the user is told the message was NOT processed, never
that a mode is on which the next boot would not find. While the row is landing
the session is already HELD as restricted by every predicate here
(:func:`is_restricted` and its siblings read the pending registry beside the
trackers) -- fail-closed, not published: nothing is announced, and the hold
vanishes with the write if it fails. The mirror, :func:`_release_mode`, takes a
reservation back the same way: durable clear awaited first, then the header
restored to the strictest claim still standing and the mark dropped; a clear
that fails RETAINS the mode. ``test_messaging_privacy_mode`` pins the shape
structurally: ``set_flag`` and ``aflush`` are called nowhere but :func:`_land`,
and :func:`mark` nowhere but the primitive, :func:`hydrate` (which restores
from the durable map and skips a row still landing) and the two in-memory
wrappers a caller with no durable store uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Cap on each tracker. Bounded so a long-running bot serving many conversations
#: cannot grow these without limit; eviction is least-recently-marked.
PRIVACY_LRU_MAX = 10_000

#: The two mode names. These are also the ``SessionMap`` flag names, so the
#: durable spelling and the in-memory spelling cannot drift.
MODE_TEMPORARY = "temporary"
MODE_INCOGNITO = "incognito"

#: Confirmation text, one copy per mode. A channel renders it with its own
#: markup dialect; the words are shared so the two channels cannot promise
#: different guarantees for the same mode.
NOTICE_TEMPORARY = "🔒 Temporary mode ON — this thread won't read or save memory."
NOTICE_INCOGNITO = "🕶️ Incognito mode ON — this thread can read memory but won't save anything."

#: The refusal text, one per reason, for a modifier the gateway could not honour.
#: Both say the same three things in the same order: the mode was NOT applied,
#: the message was NOT processed (nothing ran, nothing was saved), and what the
#: user can do. Refusing is the fail-closed answer: running the message with the
#: mode silently dropped would be the leak the modifier exists to prevent.
NOTICE_REFUSED_LIMIT = (
    "🔒 Private-conversation limit reached ({cap} conversations are already kept "
    "private on this gateway): this one could not be made {mode}, so your message "
    "was NOT processed — nothing ran and nothing was saved. Send it again without "
    "the modifier to continue with memory on."
)
NOTICE_REFUSED_KEY = (
    "🔒 This conversation's identifier is too long for a private-conversation "
    "record, so it could not be made {mode} and your message was NOT processed — "
    "nothing ran and nothing was saved. Send it again without the modifier to "
    "continue with memory on."
)
NOTICE_REFUSED_PERSIST = (
    "🔒 The private-conversation record for this conversation could not be written, "
    "so it could not be made {mode} and your message was NOT processed — nothing "
    "ran and nothing was saved. Try again in a moment, or send it without the "
    "modifier to continue with memory on."
)
#: The header cannot take the stamp at all: this conversation's transcript
#: predates private-conversation records (its first line is a message, not a
#: metadata record, and the writer skips such a file by design), so a retry will
#: fail the same way. The remedy is named, not "try again": start a new thread.
NOTICE_REFUSED_HEADER_LEGACY = (
    "🔒 This conversation's transcript predates private-conversation records, so "
    "it cannot be made {mode} and your message was NOT processed — nothing ran and "
    "nothing was saved. Start a new thread and send it there with the modifier."
)
#: Appended to the mode confirmation when the steer that carried the modifier
#: RAISED after its bytes were written to the backend (the flush is what fails),
#: so nobody knows whether the message is in the turn. The mode stands -- taking
#: it back would strip the protection from a message the backend may already be
#: recording -- and the user is told that the message itself is unconfirmed.
NOTICE_UNCONFIRMED_SUFFIX = (
    "Your message may not have reached the running turn — if no reply comes, " "send it again."
)
#: The refusal reasons, spelled as ``SessionMap.set_flag`` reports them -- plus
#: ``persist_failed`` (a durable record that could not be written or read back,
#: every commit's refusal). A steer that raised is not a refusal: the mode
#: stands and the confirmation carries :data:`NOTICE_UNCONFIRMED_SUFFIX`.
REFUSAL_LIMIT = "limit"
REFUSAL_KEY_TOO_LONG = "key_too_long"
REFUSAL_PERSIST_FAILED = "persist_failed"
#: The header write that can never land: a legacy message-first transcript.
REFUSAL_HEADER_LEGACY = "header_legacy"


class PrivacyModeRefused(RuntimeError):
    """``apply_mode`` could not put the session into *mode*; the turn must not run.

    Raised only AFTER the refusal was audited (one SEL ``denied`` record) and the
    user told (``notify``), so a caller that merely lets it propagate has still
    refused fail-closed: no mark, no row, no turn, and the user knows why. The
    channel callers catch it to return quietly instead of logging an error.
    """

    def __init__(self, mode: str, session_key: str, reason: str) -> None:
        super().__init__(f"{mode} refused for {session_key[:80]!r}: {reason}")
        self.mode = mode
        self.session_key = session_key
        self.reason = reason


class HeaderNotRecorded(RuntimeError):
    """A transcript header write returned, and the header does not record what it owed.

    The writer's answer is not the record. ``ConversationLog.update_metadata_if``
    answers ``False`` both for a header that already carries the mode (the guard
    declined, no write owed) and for a first line it could not read (damaged: no
    write, reported as nothing), and answers ``True`` for a first line that is
    valid JSON but not the metadata record, which it skips WITHOUT writing. So
    both header legs of this module -- the stamp in :func:`_commit_mode`, the
    restore in :func:`_release_mode` -- read the header back after the write and
    raise this when it does not carry what the write owed: at least *mode* after
    a stamp; anything but the released mode after a restore whose target was
    another value. Treated exactly as a write that raised: the commit refuses,
    the release retains, nothing is published.
    """

    def __init__(self, session_key: str, mode: str, found: object, *, written: bool) -> None:
        super().__init__(
            f"the transcript header of {session_key[:80]!r} does not record {mode!r} "
            f"after the write (the writer answered {written}; the header holds {found!r})"
        )
        self.session_key = session_key
        self.mode = mode
        self.found = found
        self.written = written


#: Standalone-token matchers. ``(?<!\S)`` / ``(?!\S)`` keep ``!incognito`` inside
#: a longer word (or a path) from matching, so only a token a user typed on its
#: own is a modifier.
TEMPORARY_TOKEN_RE = re.compile(r"(?<!\S)!temporary(?!\S)", re.IGNORECASE)
INCOGNITO_TOKEN_RE = re.compile(r"(?<!\S)!incognito(?!\S)", re.IGNORECASE)

#: Ordered ``(mode, pattern)`` pairs. Order is load-bearing: a caller that stops
#: at the first mode leaving nothing behind must check temporary first, matching
#: the shipped Slack ordering.
_MODES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (MODE_TEMPORARY, TEMPORARY_TOKEN_RE),
    (MODE_INCOGNITO, INCOGNITO_TOKEN_RE),
)

_PATTERNS: dict[str, "re.Pattern[str]"] = dict(_MODES)

#: Modes strictest-first, which is what :func:`strictest` ranks on. Declared
#: separately from :data:`_MODES` even though the two currently agree: that order
#: is about which token to STRIP first, and a third mode could need one position
#: for parsing and another for strength. Temporary leads because it forbids a
#: superset -- no reads, no writes, no persistence -- of what incognito forbids.
#: ``test_messaging_privacy_mode`` pins that every mode appears here, so a new one
#: has to state its rank rather than inherit a silent last place.
_STRICTNESS: tuple[str, ...] = (MODE_TEMPORARY, MODE_INCOGNITO)

#: session_key -> None. Values carry nothing; ``OrderedDict`` is here for the
#: LRU eviction order, not for a payload.
_temporary: "OrderedDict[str, None]" = OrderedDict()
_incognito: "OrderedDict[str, None]" = OrderedDict()


@dataclass
class _Pending:
    """The shared state of one (mode, key) group: a commit in flight, or a reservation held.

    Registered by the FIRST caller before its first await (:func:`_enter`), so
    a second caller for the same (mode, key) always finds it -- there is no
    window in which two callers each believe they own the only application and
    the loser's release erases the winner's committed mode. *settled* is set
    once the first caller's application has finished, one way or the other: a
    joiner waits on it, then rides the application (*applied*, *header_before*
    are the group's) or, if it *failed*, registers a group of its own.
    *holders* counts the reservations pending on the group; a plain application
    holds nothing. *committed*: a message landed under the mode -- a
    reservation's step, or a plain application's turn -- so the mode is the
    conversation's for good, whatever the other holders' steps do. *notify* and
    *notice_pending*: a reservation's confirmation is NOT sent when the mode is
    applied -- the step it protects may still fail -- but by :func:`commit`,
    once through the producer the reservation was given; a released reservation
    announces nothing it took back.

    While a group is in the registry its (mode, key) is HELD: the predicates
    answer restricted for it before anything is published, so a message that
    arrives while the row is landing runs restricted rather than persistent.
    The same hold covers a RELEASE in flight: :func:`release` leaves the group
    registered, ``releasing``, until the durable clear has landed and the mark
    is dropped, so a modifier arriving meanwhile finds the key held -- not
    "already applied" -- waits for the release to settle, and then lands a row
    of its own instead of running under a mark the release is about to drop.
    """

    settled: asyncio.Event
    holders: int = 0
    applied: bool = False
    header_before: object = None
    committed: bool = False
    failed: bool = False
    releasing: bool = False
    notify: "NoticeSender | None" = None
    notice_pending: bool = False


#: (mode, session_key) -> the group while a commit is in flight or a reservation
#: is held. Refcounted because two modifiers on the same thread can both reserve
#: before either step lands: the second finds the group already registered and
#: rides it, so the first holder's failed step must not take the mode away from
#: under the second's message. Only the last release of an uncommitted group
#: that applied the mode loosens anything.
_pending: dict[tuple[str, str], _Pending] = {}


def is_provisional(mode: str, session_key: str) -> bool:
    """Whether *mode* stands on *session_key* only PROVISIONALLY -- it may yet be taken back.

    True while a (mode, key) group is registered and no holder has committed:
    a reservation whose irreversible step has not happened, a commit whose
    write is still in flight, or a release in flight. Such a restriction is
    enforced (the predicates hold the key restricted) but must not be
    REMEMBERED: the consolidator's refusal memo consults this before memoizing
    a refused session, because a released reservation would otherwise leave a
    persistent session skipped by every automatic sweep for the life of the
    process. A dict lookup, any thread.
    """
    group = _pending.get((mode, session_key))
    return group is not None and not group.committed


def _held(mode: str, session_key: str) -> bool:
    """Whether *mode* is in flight or reserved for *session_key* -- held, not yet published.

    True from the moment a caller registers the group until it is retired: the
    row is landing, or landed and a reservation is pending on it. A dict lookup,
    so the predicates below can read it on their hot path and from any thread,
    beside the tracker lookup they already make.
    """
    return (mode, session_key) in _pending


def _landing(mode: str, session_key: str) -> bool:
    """Whether a commit of *mode* for *session_key* is in flight -- its row not yet on disk."""
    group = _pending.get((mode, session_key))
    return group is not None and not group.settled.is_set()


@dataclass
class _KeyLock:
    """One session's durable-write lock and the count of callers holding or waiting on it."""

    lock: asyncio.Lock
    users: int = 0


#: session_key -> the lock every durable privacy write of that session runs
#: under. The (mode, key) groups above serialize callers of ONE mode; two
#: modes on one key are two groups, and their durable sequences interleaved
#: (``_restore_target`` reading the other mode as a standing claim while that
#: mode's own release is between its header write and its retirement). Made on
#: first use, dropped when the last user leaves: a lock kept for a session
#: whose reservations are gone would bind an event loop that may be gone too.
_key_locks: dict[str, _KeyLock] = {}


@contextlib.asynccontextmanager
async def _serialized(session_key: str) -> AsyncIterator[None]:
    """Hold *session_key*'s durable-write lock for the block.

    :func:`_commit_mode` runs its durable steps under it and
    :func:`_release_mode` its whole sequence, so on one session the durable
    records are written by one sequence at a time -- a release computes its
    restore target from claims no other release is in the middle of taking
    back, and a commit's stamp is never a no-op against a header a release is
    about to rewrite. Sessions do not serialize each other. Re-entrant use is
    not supported (neither primitive calls the other), and the block awaits
    only the two records' I/O, never a channel.
    """
    entry = _key_locks.get(session_key)
    if entry is None:
        entry = _KeyLock(asyncio.Lock())
        _key_locks[session_key] = entry
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users <= 0 and _key_locks.get(session_key) is entry:
            _key_locks.pop(session_key, None)


#: An awaitable that posts one line on the channel.
NoticeSender = Callable[[str], Awaitable[None]]
#: An awaitable run once per newly applied mode, given the mode name.
ModeHook = Callable[[str], Awaitable[None]]


def _tracker(mode: str) -> "OrderedDict[str, None]":
    """Return the LRU backing *mode*.

    Raises ``ValueError`` on an unknown mode rather than defaulting to one of
    them: a typo that silently marked the wrong mode would fail toward the
    permissive answer (incognito still reads memory; temporary does not).
    """
    if mode == MODE_TEMPORARY:
        return _temporary
    if mode == MODE_INCOGNITO:
        return _incognito
    raise ValueError(f"unknown privacy mode: {mode!r}")


def notice(mode: str) -> str:
    """Confirmation text for *mode*.

    Public because a channel needs it for the IDEMPOTENT case too:
    :func:`apply_mode` deliberately says nothing when the session is already
    marked, and a command that answers with silence reads as having failed.
    """
    return NOTICE_TEMPORARY if mode == MODE_TEMPORARY else NOTICE_INCOGNITO


def refusal_notice(mode: str, reason: str) -> str:
    """The text a channel posts when *mode* was refused for *reason*."""
    if reason == REFUSAL_KEY_TOO_LONG:
        return NOTICE_REFUSED_KEY.format(mode=mode)
    if reason == REFUSAL_PERSIST_FAILED:
        return NOTICE_REFUSED_PERSIST.format(mode=mode)
    if reason == REFUSAL_HEADER_LEGACY:
        return NOTICE_REFUSED_HEADER_LEGACY.format(mode=mode)
    from kiro_crew.session_map import PRIVACY_ROW_CAP

    return NOTICE_REFUSED_LIMIT.format(mode=mode, cap=PRIVACY_ROW_CAP)


#: Retained spelling for the module's own call sites.
_notice = notice


def mark(mode: str, session_key: str) -> None:
    """Record *session_key* in *mode*'s bounded LRU -- the in-process PUBLICATION.

    Called from three places only, and the structural test pins the list:
    :func:`_commit_mode`, after the durable row is on disk; :func:`hydrate`,
    which restores a row the map already holds durably; and the two in-memory
    wrappers below, for a caller with no durable store at all. Nothing else
    may publish a mode -- a mark ahead of the row is exactly what a restart
    loses.
    """
    tracker = _tracker(mode)
    tracker[session_key] = None
    tracker.move_to_end(session_key)
    if len(tracker) > PRIVACY_LRU_MAX:
        tracker.popitem(last=False)


def mark_temporary(session_key: str) -> None:
    """Record *session_key* as temporary (blank-slate) in this process only."""
    mark(MODE_TEMPORARY, session_key)


def mark_incognito(session_key: str) -> None:
    """Record *session_key* as incognito in this process only."""
    mark(MODE_INCOGNITO, session_key)


def is_temporary(session_key: str) -> bool:
    """Whether *session_key* is in temporary mode (blocks memory READS too).

    True for a published mode and for one HELD -- a row still landing, or a
    reservation pending on it -- so a message that arrives while the modifier's
    write is in flight runs restricted, not persistent. The hold is not a
    publication: nothing is announced, and it is gone if the write fails.
    """
    return session_key in _temporary or _held(MODE_TEMPORARY, session_key)


def is_incognito(session_key: str) -> bool:
    """Whether *session_key* is in incognito mode (reads allowed, writes not); held counts."""
    return session_key in _incognito or _held(MODE_INCOGNITO, session_key)


def is_restricted(session_key: str) -> bool:
    """Whether *session_key* must skip memory WRITES and persistence.

    The single predicate every enforcement site consults. Namespace-agnostic by
    construction — the key is only ever a dict lookup, so a Telegram, Discord or
    Slack key all answer the same way. A held mode counts (see
    :func:`is_temporary`).
    """
    return is_temporary(session_key) or is_incognito(session_key)


def reset() -> None:
    """Drop every tracked session and pending reservation. For tests and gateway teardown only."""
    _temporary.clear()
    _incognito.clear()
    _pending.clear()
    _key_locks.clear()


def conv_state_map(sessions: object) -> Any:
    """Return the ``SessionManager``'s canonical ``SessionMap``, or ``None``.

    The durable ``temporary``/``incognito`` flags are persisted through the SAME
    ``SessionMap`` instance the ``SessionManager`` owns, so writes stay
    consistent and no second instance can clobber them on save.

    The ``isinstance`` check is load-bearing, not defensive politeness: a bare
    ``getattr`` is satisfied by any attribute, and an auto-attribute stub (a
    ``MagicMock``) yields a stand-in whose ``get_flag`` returns a **truthy mock**
    for every flag. Readers would then mark every session both temporary and
    incognito — failing closed, but wrongly, and silently. Requiring the real
    class is what actually delivers the "test doubles fall back to in-memory
    only" contract.

    The import is deferred to call time on purpose: ``session_map`` imports
    ``messaging.link``, so a module-level import here would add a second edge
    into a package this one already sits inside.
    """
    from kiro_crew.session_map import SessionMap

    sm = getattr(sessions, "_session_map", None)
    return sm if isinstance(sm, SessionMap) else None


def hydrate(sessions: object, session_key: str) -> None:
    """Restore persisted privacy flags for *session_key* into the LRUs.

    Called once per session on the inbound path so a conversation marked
    temporary or incognito stays so across a gateway restart, when the
    process-local LRUs start empty. Idempotent and allocation-free for an
    unflagged key, so a caller may run it on every decision point.

    Restores only a row the map holds DURABLY. A flag whose commit is still in
    flight (:func:`_landing`) is in the map's memory ahead of its disk write;
    marking it here would publish a mode the write may yet fail to land, so it
    is skipped -- the predicates already hold that key as restricted while the
    row lands, and :func:`_commit_mode` marks it the moment the row is on disk.
    """
    sm = conv_state_map(sessions)
    if sm is None:
        return
    for mode, _pattern in _MODES:
        if sm.get_flag(session_key, mode) and not _landing(mode, session_key):
            mark(mode, session_key)


def recorded_mode(sessions: object, session_key: str) -> str | None:
    """The strictest mode the trackers, a held group OR the durable map record for *session_key*.

    The read-only, any-thread form of :func:`hydrate` followed by
    :func:`is_temporary` / :func:`is_incognito`: the same records, the same
    strictest-first answer, dict lookups only -- and nothing is marked. The
    trackers are mutated on the event loop (``mark`` is a three-step LRU
    update); a reader on a worker thread must not join in, so this reads the
    map flag beside the tracker instead of copying it in. A mode held in flight
    counts, as it does for the predicates. ``None`` when no record names a
    mode. The consolidator's write gate asks this before every store mutation,
    from whatever thread performs it.
    """
    sm = conv_state_map(sessions)
    for mode in _STRICTNESS:
        if (
            session_key in _tracker(mode)
            or _held(mode, session_key)
            or (sm is not None and sm.get_flag(session_key, mode))
        ):
            return mode
    return None


async def _land(sm: Any, session_key: str, mode: str, value: bool) -> None:
    """Write *mode*'s durable flag to *value* and AWAIT its landing on disk.

    The durable half of both primitives, and the ONLY place this module calls
    ``SessionMap.set_flag`` or ``aflush`` (the structural test pins it): every
    row a mode owes is written here and every write is awaited here, so no
    caller can publish a row that is still owed to the map's debounced save.
    ``set_flag`` puts the value in the map's memory -- raising
    :class:`~kiro_crew.session_map.PrivacyRowRefused` for a NEW privacy row the
    map declines (its cap, or an over-long key), with nothing written -- and
    ``aflush`` lands it. A landing that fails leaves the map holding what it
    held before: the value the row had is written back, so a rollback can
    neither clear a row that was already there nor set one that was not, and
    the failure propagates for the caller to act on (a commit refuses, a
    release retains). A cancellation mid-write is restored the same way and
    re-raised as is.
    """
    had = sm.get_flag(session_key, mode)
    sm.set_flag(session_key, mode, value)
    try:
        await sm.aflush()
    except BaseException:
        if had != value:
            try:
                sm.set_flag(session_key, mode, had)
            except Exception:
                logger.warning(
                    "could not restore the %s flag of %s after a failed write", mode, session_key
                )
        raise


async def refuse(
    mode: str,
    session_key: str,
    reason: str,
    *,
    source: str,
    caller: str = "system",
    resources: str = "",
    notify: NoticeSender | None = None,
) -> None:
    """Record and announce that *mode* was refused for *session_key* -- nothing else.

    The two side effects every refusal owes, in this order: the SEL record
    (``<source>.<mode>_mode`` / ``denied`` / ``private_session_refused:<reason>``
    plus the target, the denied twin of the record :func:`apply_mode` writes when
    it applies) and the user-facing notice. :func:`apply_mode` calls this when
    the map refuses the row.
    """
    sel().log_api_access(
        caller=caller,
        operation=f"{source}.{mode}_mode",
        outcome="denied",
        source=source,
        resources=f"private_session_refused:{reason}:{resources or session_key}",
    )
    if notify is not None:
        await notify(refusal_notice(mode, reason))


async def _persist_transcript_mode(session_key: str, mode: str) -> None:
    """Record *mode* as the transcript header's ``memory_mode``; raises when it cannot.

    The SECOND durable record of a mode, and a required one: the session map
    flag is what the channel's own gate hydrates from and what keeps the map
    entry alive through ``SessionMap.prune``; the transcript header is the field
    every memory reader refuses on (``is_incognito_transcript``, the
    consolidator's header source, the dashboard's persisted probe, the
    consolidate route's own header probe) AND the one record the out-of-process
    gates consult -- ``execution_context.capture_session_execution`` reads the
    header alone and treats an absent ``memory_mode`` as ``persistent``, and MCP
    ``register_hook``, the task runner and workflow memory decide on that. So a
    header write that fails cannot be "best effort": a message run under a mode
    the header does not carry would let a hook register durable state under
    Incognito. :func:`_commit_mode` therefore writes it INSIDE the durable step,
    after the row and before the mark, and a failure here rolls the row back and
    refuses the message. Two records rather than one because they answer two
    readers: a transcript read must not depend on the map being loaded, and the
    channel gate must not pay a transcript read per inbound message.

    Upserted through ``ConversationLog.update_metadata_if``: a transcript that
    does not exist yet gets a metadata-only line, and ``ConversationLog.append``
    keeps an existing header, so the first row any later writer appends lands
    under a header that already carries the mode. Tighten-only: the guard
    admits the write only when *mode* is at least as strict as the header's
    current mode, so ``!incognito`` typed after ``!temporary`` cannot re-enable
    memory reads, and a header already carrying *mode* is rewritten unchanged
    (no write at all -- not a failure).

    The writer's answer is consumed and then NOT trusted on its own: the header
    is read back, and the stamp counts only when the header records at least
    *mode* (:class:`HeaderNotRecorded` otherwise, refused by the caller like a
    write that raised). ``update_metadata_if`` has two silent no-write returns
    that read as success -- a damaged first line it reports unreadable
    (``False``, the same answer as "already carries the mode"), and a first line
    that is valid JSON but not the metadata record (a legacy message-first
    transcript), which ``_update_metadata_locked`` skips and still reports
    written -- and either would have marked, audited and announced a mode the
    header-only gates could not see.

    The default ``ConversationLog`` reaches the same file the channel writes:
    every production log reads the one configured sessions directory. Off the
    loop, because ``update_metadata_if`` takes the transcript's cross-process
    flock. The startup sweep (``SessionMap.stamp_privacy_headers``) re-stamps
    every flagged row's header from its row, which repairs a header a cancelled
    task left behind; it is a repair pass, not what makes this write optional.
    """
    # Call-time import because the cycle is real: ``kiro_crew.history`` imports
    # ``history_consolidation`` at module scope (facade re-exports), and that
    # module imports THIS one at module scope for the session-map source of a
    # consolidation target's mode -- so a module-scope import here would load
    # ``history`` while ``history`` is still loading. Deferred, the edge is paid
    # once, at the first mark.
    from kiro_crew.history import ConversationLog, transcript_privacy_mode

    log = ConversationLog()

    def _tighten_only(metadata: dict) -> bool:
        # Normalized first: a raw ``Temporary`` would compare as unknown, and an
        # incognito stamp would then overwrite the stricter mode. No write at
        # all when the header already records the mode.
        return needs_tightening(transcript_privacy_mode(metadata.get("memory_mode")), mode)

    written = await asyncio.to_thread(
        log.update_metadata_if, session_key, {"memory_mode": mode}, _tighten_only
    )
    # Read back rather than believed: ``False`` is also "already at least this
    # strict", ``True`` is also "skipped a first line that is not a header". The
    # cache cannot serve the pre-write view -- its identity is the file's inode
    # and ctime, which the writer's tmp-plus-rename changes.
    found = (await asyncio.to_thread(log.get_metadata, session_key)).get("memory_mode")
    if needs_tightening(transcript_privacy_mode(found), mode):
        raise HeaderNotRecorded(session_key, mode, found, written=written)


def strip_token(text: str, mode: str) -> tuple[str, bool]:
    """Remove *mode*'s standalone token from *text*.

    Returns ``(cleaned_text, found)``. When the token is absent *text* is handed
    back untouched — no whitespace collapse — so a caller can tell "nothing to do
    here" from "stripped down to nothing".
    """
    pattern = _PATTERNS.get(mode)
    if pattern is None:
        raise ValueError(f"unknown privacy mode: {mode!r}")
    new, n = pattern.subn("", text)
    if not n:
        return text, False
    return " ".join(new.split()), True


def strip_tokens(text: str) -> tuple[str, tuple[str, ...]]:
    """Strip every modifier token from *text*.

    Returns ``(cleaned_text, modes_found)`` with *modes_found* in
    :data:`_MODES` order. Applies nothing; this is the pure half, for a caller
    that needs the cleaned text without the side effects (a log line, a preview).
    """
    found: list[str] = []
    for mode, _pattern in _MODES:
        text, had = strip_token(text, mode)
        if had:
            found.append(mode)
    return text, tuple(found)


def strictest(modes: "list[str] | tuple[str, ...]") -> str:
    """The strictest of *modes*, or ``""`` for none.

    For a caller that has to collapse several requests into ONE decision: a queue
    drain answers a burst of messages as a single turn under a single key, so it can
    honour exactly one mode. Taking the strictest is the only choice that cannot
    silently downgrade what a user asked for -- honouring the first would let a later
    request in the same burst be dropped, and honouring the last would drop an
    earlier one.

    Unknown names are ignored rather than raising: this runs on the delivery path,
    where refusing a turn over an unrecognized mode would cost the user their message
    to protect them from a mode that does not exist.
    """
    present = set(modes)
    return next((mode for mode in _STRICTNESS if mode in present), "")


def needs_tightening(current: str, mode: str) -> bool:
    """Whether a record holding *current* must be rewritten to hold *mode*.

    True only when *mode* is STRICTER than *current*; an absent or unknown
    current (``""``) counts as weakest. A record already at *mode* answers
    False -- so a tighten-only writer that asks this performs NO write for a
    header that already records the mode (the startup stamp would otherwise
    rewrite every flagged row's header on every boot) -- and a stricter record
    answers False too, which is the tighten-only rule itself.
    """
    return current != mode and strictest([current, mode]) == mode


async def apply_mode(
    mode: str,
    session_key: str,
    *,
    source: str,
    caller: str = "system",
    resources: str = "",
    sessions: object | None = None,
    notify: NoticeSender | None = None,
    on_applied: ModeHook | None = None,
) -> bool:
    """Put *session_key* into *mode*, durably, and tell the user once.

    Returns whether the mode was NEWLY applied. Idempotent: an already-marked
    session returns ``False`` without re-auditing or re-notifying, so repeating
    the token costs the channel nothing.

    The application itself is :func:`_commit_mode` -- the one path from a
    request to a published mode, whose docstring is the single statement of the
    order of its durable and published steps (nothing is restated here). A row
    the map refuses (``PRIVACY_ROW_CAP`` reached, an over-long key) or cannot
    write or land (``persist_failed``) raises
    :class:`PrivacyModeRefused` after the refusal is audited (one SEL
    ``denied`` record) and the user told through *notify*: nothing is marked,
    written or announced, and the caller must not run the turn -- the message
    is refused, never run with the mode dropped. A caller with no durable store
    (no *sessions*, or a test double) is published at once, in memory only.

    Two requests for the same (mode, key) cannot race each other: the first
    registers the group before its first await (:func:`_enter`), the second
    joins it and waits, so a concurrent modifier never re-commits, and while
    the row lands the key is already held as restricted by every predicate.
    A message that lands under the mode -- this one -- commits the group, so
    a reservation riding it cannot take the mode back when its own step fails.
    """
    if session_key in _tracker(mode):
        group = _pending.get((mode, session_key))
        if group is None:
            return False
        if not group.releasing:
            # A reservation is pending on a mode this message runs under: the
            # mode is the conversation's for good, whatever that step does --
            # and the confirmation the reservation deferred is this message's.
            group.committed = True
            await _announce_deferred(group, mode, notify)
            return False
        # The mark is standing only until the release in flight drops it: not
        # "already applied". Fall through -- ``_enter`` waits for the release to
        # settle and this request then lands a row of its own.
    group, first = await _enter(mode, session_key, hold=False)
    if not first:
        # The commit this request would have made just landed (or is landing)
        # for the same (mode, key); the message runs under it, and a confirmation
        # a reservation deferred is this message's to send.
        group.committed = True
        await _announce_deferred(group, mode, notify)
        return False
    try:
        applied = await _commit_mode(
            mode,
            session_key,
            sessions=sessions,
            source=source,
            caller=caller,
            resources=resources,
            notify=notify,
            on_applied=on_applied,
        )
    except BaseException:
        _settle(group, mode, session_key, failed=True)
        raise
    group.applied = applied
    group.committed = True
    _settle(group, mode, session_key)
    return applied


async def _announce_deferred(
    group: _Pending, mode: str, notify: NoticeSender | None, *, unconfirmed: bool = False
) -> None:
    """Send the confirmation a reservation on *group* deferred, once, to whoever
    commits the group first: the committer's own producer when it has one (the
    message that runs is theirs), else the reservation's. ``unconfirmed`` adds
    the line that says the message itself may not have run."""
    if not group.notice_pending:
        return
    sender = notify if notify is not None else group.notify
    group.notice_pending = False
    if sender is not None:
        text = _notice(mode)
        if unconfirmed:
            text = f"{text} {NOTICE_UNCONFIRMED_SUFFIX}"
        await sender(text)


@dataclass(frozen=True)
class Reservation:
    """A mode applied ahead of an irreversible step, so it can be taken back if the step fails.

    Returned by :func:`reserve`; ended by exactly one :func:`commit` (the step
    happened) or :func:`release` (it did not). What a release may take back is
    decided by the group the holders of one (mode, key) share
    (:class:`_Pending`), not by the holder releasing.
    """

    mode: str
    session_key: str


async def _enter(mode: str, session_key: str, *, hold: bool) -> tuple[_Pending, bool]:
    """Register with the (mode, key) group, or join the one already in flight.

    Returns ``(group, first)``. *first*: this caller registered the group --
    before any await, so a second caller for the same (mode, key) always finds
    it -- and owes the application and its settlement (:func:`_settle`). A
    joiner waits for the group to settle and rides its application; if that
    application failed and its owner is gone, the joiner comes back round to
    register a group of its own. A group whose RELEASE is in flight settles as
    failed too, so a joiner that waited on it lands the mode afresh. *hold*
    counts the caller as a reservation holder; a plain application holds
    nothing -- its message runs under the mode, so it commits the group instead.
    """
    key = (mode, session_key)
    while True:
        group = _pending.get(key)
        if group is None:
            group = _Pending(settled=asyncio.Event(), holders=1 if hold else 0)
            _pending[key] = group
            return group, True
        if hold:
            group.holders += 1
        await group.settled.wait()
        if not group.failed:
            return group, False
        if hold:
            group.holders -= 1


def _settle(group: _Pending, mode: str, session_key: str, *, failed: bool = False) -> None:
    """Settle *group*'s application: wake its joiners, retire the entry when no holder remains.

    A failed application retires the entry whatever the holder count -- the
    joiners hold the object, read ``failed`` off it and register anew -- so a
    refused (mode, key) is held for exactly as long as its write was in flight.
    """
    key = (mode, session_key)
    if failed:
        group.failed = True
    if failed or group.holders <= 0:
        if _pending.get(key) is group:
            _pending.pop(key, None)
    group.settled.set()


async def _commit_mode(
    mode: str,
    session_key: str,
    *,
    sessions: object | None,
    source: str,
    caller: str,
    resources: str,
    notify: NoticeSender | None,
    on_applied: ModeHook | None,
    announce: bool = True,
) -> bool:
    """THE one path from a mode request to a published mode. Durable first, published last.

    In this order and no other:

    1. The durable row: :func:`_land` writes the map flag and AWAITS its landing
       on disk. A row the map refuses (its cap, an over-long key) or cannot
       write or land (``persist_failed``) ends here -- :func:`refuse` writes one
       SEL ``denied`` record and tells the user the message was NOT processed,
       and :class:`PrivacyModeRefused` is raised. Nothing below has happened:
       no mark, no header, no notice, so nothing claims a mode the next boot
       would not find. (The best-effort form this replaces kept the mark and
       reported the mode on when its flush failed; a restart then ran the
       thread persistent under a mode-on notice.)
    2. The durable header: :func:`_persist_transcript_mode`, tighten-only, READ
       BACK after the write (the writer's answer is not the record: its two
       silent no-write returns are :class:`HeaderNotRecorded`). The record the
       out-of-process gates read (``capture_session_execution`` and the MCP
       ``register_hook``, task-runner and workflow-memory paths on it, which
       take an absent header as ``persistent``). A header write that fails, or
       returns with the header not recording the mode, is the same refusal as a
       row that cannot land: the row is rolled back (:func:`_land` to its prior
       value, best-effort -- a row left behind is over-restrictive and the
       startup sweep stamps its header at the next boot) and the message is NOT
       processed. Before the mark, so no message can run under a mode one of
       its two records does not carry; the key is HELD as restricted for the
       span of both writes.
    3. The mark -- :func:`mark`, the in-process publication every enforcement
       site reads. Both records it announces are on disk.
    4. The SEL ``allowed`` record. (A task cancelled during step 2 leaves the
       row on disk with neither mark nor record: the next inbound message
       hydrates the mark from the row, and the sweep restores the header.)
    5. The caller's hook, then the notice -- unless the caller defers it
       (``announce=False``: :func:`reserve`, whose confirmation is
       :func:`commit`'s to send once the step it protects has happened; a
       refusal's notice is never deferred).

    Always ``True``: idempotency is the caller's check, made before the group
    is entered, so a repeat request never reaches the write. A caller with no
    durable store (``sessions`` is ``None``, or not a real ``SessionMap``) has
    no row to land; it still gets the header when ``sessions`` is given, and is
    published at once otherwise -- the in-memory-only contract. Steps 1 and 2
    run under the session's durable-write lock (:func:`_serialized`), shared
    with :func:`_release_mode`: on one session, one durable sequence at a time.
    """
    sm = conv_state_map(sessions)
    reason: str | None = None
    cause: BaseException | None = None
    # The durable steps run under the session's lock (:func:`_serialized`):
    # a release of another mode on this key computes what it restores from
    # the claims standing, and this stamp must not land between that
    # computation and its write.
    async with _serialized(session_key):
        # What the row said before this commit, so a header that cannot be stamped
        # rolls the row back to THAT -- never to "off": after a restart a re-sent
        # modifier commits over a row that is already on disk (the tracker is
        # empty), and clearing it would erase the thread's one durable record.
        prior = bool(sm.get_flag(session_key, mode)) if sm is not None else False
        if sm is not None:
            # Call-time import for the same reason as ``conv_state_map``'s.
            from kiro_crew.session_map import PrivacyRowRefused

            try:
                await _land(sm, session_key, mode, True)
            except PrivacyRowRefused as refused:
                reason, cause = refused.reason, refused
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason, cause = REFUSAL_PERSIST_FAILED, exc
        if reason is None and sessions is not None:
            try:
                await _persist_transcript_mode(session_key, mode)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A header the writer SKIPPED (it answered True and wrote nothing:
                # the transcript's first line is a message, not a metadata record --
                # the legacy shape) will never take the stamp on a retry; the only
                # way out is a new thread, and the notice says so. Everything else
                # (a damaged first line, a write that raised) is the transient
                # persist failure whose notice says try again.
                legacy = isinstance(exc, HeaderNotRecorded) and exc.written and not exc.found
                reason, cause = (REFUSAL_HEADER_LEGACY if legacy else REFUSAL_PERSIST_FAILED), exc
                logger.warning(
                    "could not record %s mode in the transcript header of %s; refusing the message",
                    mode,
                    session_key,
                    exc_info=True,
                )
                if sm is not None:
                    # The row landed; put it back as it was so this commit claims
                    # nothing the header does not carry (a row that pre-existed it
                    # stays: over-restrictive, stamped at the next boot's sweep).
                    try:
                        await _land(sm, session_key, mode, prior)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning(
                            "the %s row of %s could not be taken back after the header write "
                            "failed; the startup sweep stamps its header at the next boot",
                            mode,
                            session_key,
                        )
    if reason is not None:
        await refuse(
            mode,
            session_key,
            reason,
            source=source,
            caller=caller,
            resources=resources,
            notify=notify,
        )
        raise PrivacyModeRefused(mode, session_key, reason) from cause
    mark(mode, session_key)
    sel().log_api_access(
        caller=caller,
        operation=f"{source}.{mode}_mode",
        outcome="allowed",
        source=source,
        resources=resources or session_key,
    )
    if on_applied is not None:
        await on_applied(mode)
    if notify is not None and announce:
        await notify(_notice(mode))
    return True


def _restore_target(
    sessions: object | None, session_key: str, mode: str, header_before: object
) -> str:
    """The header value a release of *mode* restores: the strictest claim still standing.

    Two kinds of claim: the mode the header recorded BEFORE the reservation
    stamped it (normalized -- a mixed-case ``Temporary`` ranks as
    ``temporary``, an unknown or absent value as nothing), and every OTHER
    mode still recorded for the session -- in a tracker, in the durable map,
    or held in flight -- by a reservation of that mode that committed or is
    still pending (its own commit stamps the header when it lands; a header
    left stricter meanwhile is the safe direction). ``persistent`` only when
    nothing claims the conversation. Restoring the released mode's own
    ``header_before`` alone was the leak: a ``temporary`` reservation released
    after an ``incognito`` one committed wrote ``persistent`` over a header
    every header-only reader then took at its word.
    """
    from kiro_crew.history import transcript_privacy_mode

    sm = conv_state_map(sessions)
    claims = [transcript_privacy_mode(header_before)]
    for other in _STRICTNESS:
        if other == mode:
            continue
        if (
            session_key in _tracker(other)
            or _held(other, session_key)
            or (sm is not None and sm.get_flag(session_key, other))
        ):
            claims.append(other)
    return strictest(claims) or "persistent"


def _reroot_claims(session_key: str, mode: str, header_before: object) -> None:
    """A release of *mode* took its stamp out of the header: re-root the claims that rested on it.

    Every other group still pending on *session_key* whose ``header_before``
    records *mode* read the header while *mode* stood -- what it read was
    *mode*'s stamp, not a claim of its own. Now that *mode* is released, the
    header that group must restore when IT releases is what the header held
    before *mode*'s stamp -- this release's *header_before* -- so that is what
    it records from here on. Restoring the recorded *mode* instead (the earlier
    shape) re-stamped a mode with no row, no mark and no group left on the
    session: ``!temporary`` then ``!incognito`` on one thread, both steers
    declined and released in that order, left the header saying ``temporary``
    for good. Runs under the session's lock, after the header restore, so no
    other release reads a claim this one is rewriting.
    """
    from kiro_crew.history import transcript_privacy_mode

    for (other, key), group in _pending.items():
        if key != session_key or other == mode:
            continue
        if transcript_privacy_mode(group.header_before) == mode:
            group.header_before = header_before


async def _release_mode(
    mode: str,
    session_key: str,
    *,
    group: _Pending,
    sessions: object | None,
    source: str,
    caller: str,
    resources: str,
) -> bool:
    """The mirror of :func:`_commit_mode`: take a reservation's mode back, the restart's record LAST.

    The commit lands its records strict-first -- the row, the header, then the
    mark. The release loosens them in the OPPOSITE order, so that a sequence
    that stops anywhere leaves the record the next boot trusts still saying the
    mode: :func:`hydrate` restores the mark from the map ROW alone, and the
    startup sweep (``SessionMap.stamp_privacy_headers``) re-stamps headers FROM
    rows, never rows from headers -- so the row is the last record loosened.

    1. The durable header, restored to :func:`_restore_target` -- the strictest
       of what it recorded before this reservation and every other mode still
       standing on the session, never a bare ``persistent`` while another mode
       holds the conversation. Written only if the header still records the
       released mode, and then READ BACK: a restore that raises, or that leaves
       the header still recording the released mode when its target was another
       value (the writer's silent no-write answers, see
       :class:`HeaderNotRecorded`), RETAINS the mode with NOTHING loosened yet
       -- the row and the mark stand as they were, one ``retained`` record
       reports the failure in place of ``released``, and ``False`` is returned.
       (Cleared first, the row was put back after a failed restore; a put-back
       that failed too left the row clear under a header and a mark that still
       said the mode, and the next boot hydrated nothing from it -- the channel
       gate read the session as persistent while every header-only reader still
       read it private.) A header left STRICTER than the release's target is
       not a failure -- another mode's commit landed meanwhile, and stricter is
       the safe direction.
    2. The durable clear: :func:`_land` clears the map flag and AWAITS the
       landing (the row stops counting against the cap; a row that carries
       another privacy flag keeps that one). A clear that fails RETAINS the
       mode: the flag is back in the map's memory (the map's next write
       retries), the mark never left, and the header loosened a step ago is
       re-stamped with the mode (:func:`_persist_transcript_mode`, tighten-only)
       so the two records agree again at once -- best effort in the SAFE
       direction, since where that re-stamp fails too the row still says the
       mode: this process stays restricted, the next boot hydrates the mark from
       the row, and the startup sweep re-stamps the header from it.
    3. The mark, dropped -- the one place this module unpublishes -- and the
       SEL ``released`` record. Only once both records are loosened on disk.
       Then the claims of the other groups pending on the session are re-rooted
       (:func:`_reroot_claims`): a group that read this mode off the header
       when it reserved must restore what stood BEFORE this mode, not this mode.

    The whole sequence runs under the session's durable-write lock
    (:func:`_serialized`), shared with :func:`_commit_mode`: two modes on one
    key are two groups, and two releases interleaved each read the other's mode
    as a standing claim -- both wrote the other mode into the header, both then
    retired, and the header stayed private over a session with no row, mark or
    group left on it. Serialized, the first release sees the second's claim and
    restores to it; the second sees nothing standing and restores what the
    header held before either.

    A cancellation during the durable steps does NOT abandon the sequence: the
    header write is a thread the cancellation cannot stop and the clear's flush
    may already be on its way, so both steps run as one task SHIELDED from this
    one, their outcome is awaited, and the sequence completes -- released or
    retained -- before the cancellation propagates. Only a SECOND cancellation
    while that outcome is awaited propagates at once, leaving the outcome to
    the shielded task: whatever it lands, the row is loosened last, so a boot
    that reads a half-finished release reads the mode.
    """
    async with _serialized(session_key):
        # Read off the group only now, under the lock: a release of the OTHER
        # mode that ran while this one waited re-rooted this claim
        # (:func:`_reroot_claims`), and a value copied before the wait would be
        # the stale one -- the released mode itself.
        header_before = group.header_before
        sm = conv_state_map(sessions)

        async def _loosen() -> bool:
            """Both durable records, header then row; ``True`` once both are loosened.

            Every failure is logged here and answered ``False``; nothing but a
            cancellation propagates."""
            if sessions is not None:
                from kiro_crew.history import ConversationLog, transcript_privacy_mode

                restored = _restore_target(sessions, session_key, mode, header_before)

                def _still_ours(metadata: dict) -> bool:
                    return transcript_privacy_mode(metadata.get("memory_mode")) == mode

                log = ConversationLog()
                try:
                    written = await asyncio.to_thread(
                        log.update_metadata_if,
                        session_key,
                        {"memory_mode": restored},
                        _still_ours,
                        require_existing=True,
                    )
                    # Read back, as the stamp is: after the restore the header
                    # must not claim the released mode, unless that is exactly
                    # what it should still say (it recorded the mode before this
                    # reservation, or another claim of it stands). A header that
                    # says nothing -- deleted, damaged -- claims no mode, so the
                    # release has nothing left to loosen there.
                    found = (await asyncio.to_thread(log.get_metadata, session_key)).get(
                        "memory_mode"
                    )
                    if restored != mode and transcript_privacy_mode(found) == mode:
                        raise HeaderNotRecorded(session_key, restored, found, written=written)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "the transcript header of %s could not be restored on release of %s; "
                        "the mode is retained, its row and mark as they were",
                        session_key,
                        mode,
                        exc_info=True,
                    )
                    return False
            if sm is not None:
                try:
                    await _land(sm, session_key, mode, False)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "%s mode of %s could not be released durably; the mode is retained",
                        mode,
                        session_key,
                        exc_info=True,
                    )
                    if sessions is not None:
                        # The header was loosened a step ago: put the mode back on
                        # it so the two records agree at once. Best effort in the
                        # safe direction -- where this fails too, the row (the
                        # record a restart hydrates from) still says the mode and
                        # the startup sweep re-stamps the header from it.
                        try:
                            await _persist_transcript_mode(session_key, mode)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.warning(
                                "the %s header of %s could not be re-stamped after the clear "
                                "failed; the row and the mark still say the mode, and the next "
                                "boot's sweep re-stamps the header from the row",
                                mode,
                                session_key,
                            )
                    return False
            return True

        # The durable steps are their own task, SHIELDED from this one's
        # cancellation: a cancellation arriving here (a gateway shutdown
        # mid-release) is noted, the steps' outcome is awaited anyway, and the
        # sequence ends in one of its two states before the cancellation
        # propagates. Re-raising at the first CancelledError (an earlier shape)
        # left the records split.
        interrupted: asyncio.CancelledError | None = None
        loosening = asyncio.ensure_future(_loosen())
        try:
            await asyncio.shield(loosening)
        except asyncio.CancelledError as exc:
            interrupted = exc
            try:
                await asyncio.shield(loosening)
            except asyncio.CancelledError:
                # Cancelled twice: the outcome stays unknown to this task. The
                # shielded steps run on; the group settles failed; the result is
                # consumed so it is not logged as an unretrieved exception.
                loosening.add_done_callback(lambda t: t.cancelled() or t.exception())
                raise
            except Exception:
                pass
        except Exception:
            pass
        loosened = loosening.exception() is None and loosening.result() is True
        if not loosened:
            sel().log_api_access(
                caller=caller,
                operation=f"{source}.{mode}_mode",
                outcome="retained",
                source=source,
                resources=f"release_failed:{REFUSAL_PERSIST_FAILED}:{resources or session_key}",
            )
            if interrupted is not None:
                raise interrupted
            return False
        _tracker(mode).pop(session_key, None)
        sel().log_api_access(
            caller=caller,
            operation=f"{source}.{mode}_mode",
            outcome="released",
            source=source,
            resources=resources or session_key,
        )
        _reroot_claims(session_key, mode, header_before)
        if interrupted is not None:
            raise interrupted
        return True


async def reserve(
    mode: str,
    session_key: str,
    *,
    source: str,
    caller: str = "system",
    resources: str = "",
    sessions: object | None = None,
    notify: NoticeSender | None = None,
    on_applied: ModeHook | None = None,
) -> Reservation:
    """Apply *mode* ahead of a step that cannot be taken back.

    :func:`_commit_mode` -- the order of its steps stated once, in its own
    docstring -- or :class:`PrivacyModeRefused`, plus the bookkeeping a later
    :func:`release` needs: the group is HELD (a holder counted) and the header
    the transcript carried before is remembered. The Telegram steer path
    reserves before it steers: the message it is about to fold into a running
    turn must already be protected by the row that turn's transcript write and
    the channel gate read, and a row that cannot be taken or made durable must
    refuse the message before it is in the turn. Every reservation must end in
    exactly one :func:`commit` or :func:`release`.

    The CONFIRMATION is not sent here. The step the reservation protects may
    still decline or fail, and a user told "mode ON" for a message that then
    ran elsewhere or not at all was told something false: the notice is kept
    with the group and sent by :func:`commit`, through *notify*, once the step
    has happened. A refusal's notice is sent at once, as ever.

    The first caller for a (mode, key) registers the group BEFORE its first
    await and runs the application; a concurrent second caller joins the
    registered group and waits for that application to settle instead of
    running its own, so the two cannot race each other's bookkeeping. If the
    first application fails, its group is retired and the joiner registers one
    of its own. A session already in the mode is held without a second
    application -- and left exactly as it was by a later release.
    """
    group, first = await _enter(mode, session_key, hold=True)
    if not first:
        return Reservation(mode, session_key)
    try:
        applied = False
        header_before: object = None
        if session_key not in _tracker(mode):
            if sessions is not None:
                from kiro_crew.history import ConversationLog

                try:
                    header_before = (
                        await asyncio.to_thread(ConversationLog().get_metadata, session_key)
                    ).get("memory_mode")
                except Exception:
                    logger.debug(
                        "could not read %s's header before reserving %s", session_key, mode
                    )
            applied = await _commit_mode(
                mode,
                session_key,
                sessions=sessions,
                source=source,
                caller=caller,
                resources=resources,
                notify=notify,
                on_applied=on_applied,
                announce=False,
            )
    except BaseException:
        group.holders -= 1
        _settle(group, mode, session_key, failed=True)
        raise
    group.applied = applied
    group.header_before = header_before
    if applied:
        group.notify = notify
        group.notice_pending = notify is not None
    _settle(group, mode, session_key)
    return Reservation(mode, session_key)


async def commit(reservation: Reservation, *, unconfirmed: bool = False) -> None:
    """The irreversible step happened: the mode is the conversation's for good.

    Every holder of the same (mode, key) is committed with it -- a later release
    by a holder whose own step failed loosens nothing, because a message did
    land under the mode. The confirmation the reservation deferred is sent
    here, once, through the producer :func:`reserve` was given -- after the
    bookkeeping, so a notice that cannot be delivered changes nothing else.

    ``unconfirmed``: the step RAISED and its outcome is unknown -- a steer's
    bytes are written to the backend before the awaited flush that raises, so
    the message may already be in the turn and on its way into a transcript.
    Fail-closed: the mode stands exactly as a landed step leaves it (row, mark,
    header), and the confirmation carries :data:`NOTICE_UNCONFIRMED_SUFFIX` so
    the user knows the message itself may not have run. Releasing here would
    strip the protection from a message the backend may be recording.
    """
    key = (reservation.mode, reservation.session_key)
    group = _pending.get(key)
    if group is None:
        return
    group.committed = True
    group.holders -= 1
    if group.holders <= 0:
        _pending.pop(key, None)
    await _announce_deferred(group, reservation.mode, None, unconfirmed=unconfirmed)


async def release(
    reservation: Reservation,
    *,
    sessions: object | None,
    source: str,
    caller: str = "system",
    resources: str = "",
) -> bool:
    """The irreversible step did NOT happen: take back what the reservation applied.

    Loosening a privacy mode is otherwise never done, so the conditions are
    narrow and each one is checked: the mode is taken back only when this is
    the LAST pending holder, no holder committed (a reservation's step, or a
    plain application's message, landed under the mode), and the group newly
    applied the mode -- then :func:`_release_mode` does the taking back,
    durable first and published last. Returns whether the mode was released. A
    session that was already in the mode is left exactly as it was. The group
    stays registered, ``releasing``, until the clear has landed: the key is held
    for that span, and a request arriving in it waits and re-lands the mode
    rather than running under the mark this release drops.
    """
    key = (reservation.mode, reservation.session_key)
    group = _pending.get(key)
    if group is None:
        return False
    group.holders -= 1
    if group.holders > 0:
        return False
    if group.committed or not group.applied:
        _pending.pop(key, None)
        return False
    # The clear is awaited with the group STILL registered and unsettled: the
    # key stays held, so a same-mode request arriving meanwhile is neither run
    # under the standing mark (``apply_mode``'s shortcut skips a releasing
    # group) nor allowed to ride this group -- it waits, and when the release
    # settles as failed it registers a group of its own and lands the mode
    # afresh. Popping the group first (the earlier shape) let that request read
    # "already applied" off the mark, run, and lose the mode when this clear
    # landed under it.
    group.releasing = True
    group.settled.clear()
    try:
        return await _release_mode(
            reservation.mode,
            reservation.session_key,
            group=group,
            sessions=sessions,
            source=source,
            caller=caller,
            resources=resources,
        )
    finally:
        _settle(group, reservation.mode, reservation.session_key, failed=True)


async def strip_and_apply(
    text: str,
    session_key: str,
    *,
    source: str,
    caller: str = "system",
    resources: str = "",
    sessions: object | None = None,
    notify: NoticeSender | None = None,
    on_applied: ModeHook | None = None,
) -> tuple[str, bool]:
    """Strip the privacy modifiers from *text* and apply each one found.

    Returns ``(stripped_text, only_modifier)``:

    * *stripped_text* — *text* with every modifier token removed and whitespace
      collapsed. The token MUST NOT reach the model: it is an instruction to the
      gateway, and a prompt containing it invites the model to answer it.
    * *only_modifier* — ``True`` when the message was nothing but modifier(s).
      The caller MUST then return without starting a turn; there is no question
      to answer, and running one would spend a turn on the word ``!incognito``.

    Modes are applied in :data:`_MODES` order, stopping as soon as nothing is
    left to say — so ``!temporary`` alone applies temporary and returns, exactly
    as the shipped Slack path does. A :class:`PrivacyModeRefused` from
    :func:`apply_mode` propagates: the user has been told, and the caller must
    not run the turn.
    """
    for mode, _pattern in _MODES:
        stripped, had = strip_token(text, mode)
        if not had:
            continue
        await apply_mode(
            mode,
            session_key,
            source=source,
            caller=caller,
            resources=resources,
            sessions=sessions,
            notify=notify,
            on_applied=on_applied,
        )
        text = stripped
        if not text:
            return text, True
    return text, False
