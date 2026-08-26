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
* ``sessions`` — the ``SessionManager``, used ONLY to reach the one
  :class:`~kiro_crew.session_map.SessionMap` instance it owns, so the durable
  flag cannot be clobbered by a second instance's save. Omit it and the mode is
  in-memory only, which is also what a test double gets.
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
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
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


#: Retained spelling for the module's own call sites.
_notice = notice


def mark(mode: str, session_key: str) -> None:
    """Record *session_key* in *mode*'s bounded LRU."""
    tracker = _tracker(mode)
    tracker[session_key] = None
    tracker.move_to_end(session_key)
    if len(tracker) > PRIVACY_LRU_MAX:
        tracker.popitem(last=False)


def mark_temporary(session_key: str) -> None:
    """Record *session_key* as temporary (blank-slate) in this process."""
    mark(MODE_TEMPORARY, session_key)


def mark_incognito(session_key: str) -> None:
    """Record *session_key* as incognito in this process."""
    mark(MODE_INCOGNITO, session_key)


def is_temporary(session_key: str) -> bool:
    """Whether *session_key* is in temporary mode (blocks memory READS too)."""
    return session_key in _temporary


def is_incognito(session_key: str) -> bool:
    """Whether *session_key* is in incognito mode (reads allowed, writes not)."""
    return session_key in _incognito


def is_restricted(session_key: str) -> bool:
    """Whether *session_key* must skip memory WRITES and persistence.

    The single predicate every enforcement site consults. Namespace-agnostic by
    construction — the key is only ever a dict lookup, so a Telegram, Discord or
    Slack key all answer the same way.
    """
    return session_key in _temporary or session_key in _incognito


def reset() -> None:
    """Drop every tracked session. For tests and gateway teardown only."""
    _temporary.clear()
    _incognito.clear()


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
    """
    sm = conv_state_map(sessions)
    if sm is None:
        return
    for mode, _pattern in _MODES:
        if sm.get_flag(session_key, mode):
            mark(mode, session_key)


def _persist(sessions: object, session_key: str, mode: str) -> None:
    """Write *mode*'s durable flag, best-effort.

    Best-effort because the in-memory mark has already happened: a session whose
    flag could not be written is still restricted for the life of this process,
    which is the safe direction. Failing the modifier outright would leave the
    user believing the mode is off when it is on.
    """
    sm = conv_state_map(sessions)
    if sm is None:
        return
    try:
        sm.set_flag(session_key, mode, True)
    except Exception:
        logger.warning(
            "could not persist %s mode for %s; it holds for this process only",
            mode,
            session_key,
            exc_info=True,
        )


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
    """Put *session_key* into *mode*, and tell the user once.

    Returns whether the mode was NEWLY applied. Idempotent: an already-marked
    session returns ``False`` without re-auditing or re-notifying, so repeating
    the token costs the channel nothing.

    Ordering is deliberate. The in-memory mark lands FIRST, before any await, so
    a concurrent inbound message on the same session cannot observe the session
    as unrestricted after the user asked for privacy. The durable write, the
    audit, the caller's hook and the notice follow.
    """
    if session_key in _tracker(mode):
        return False
    mark(mode, session_key)
    if sessions is not None:
        _persist(sessions, session_key, mode)
    sel().log_api_access(
        caller=caller,
        operation=f"{source}.{mode}_mode",
        outcome="allowed",
        source=source,
        resources=resources or session_key,
    )
    if on_applied is not None:
        await on_applied(mode)
    if notify is not None:
        await notify(_notice(mode))
    return True


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
    as the shipped Slack path does.
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
