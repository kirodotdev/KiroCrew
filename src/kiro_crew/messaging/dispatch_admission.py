"""Whether an inbound message is let into the channel turn pipeline, and its answer back out.

Callback admission (:func:`admit_inbound_callback`, :func:`hold_inbound_callback`) counts a
handler in the restart census or durably refuses it; :func:`inbound_permitted` is the
per-message governance gate with its pure-cancel exemption; :func:`delivery_is_muted` is the
one mute predicate the channels on this pipeline (and Telegram's own loop) consult before
writing output back; Slack is gated by ``slack_mirror_is_paused`` instead. Callers import these
names from :mod:`kiro_crew.messaging.dispatch`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from kiro_crew.messaging.dispatch_contract import ChannelTurn
from kiro_crew.messaging.identity import channel_inbound_permitted
from kiro_crew.messaging.inbound_spool import (
    InboundRoute,
    spool_refused_turn,
    spool_refused_turn_sync,
)
from kiro_crew.messaging.link import channel_namespace_of, is_channel_session_key

logger = logging.getLogger("kiro_crew.messaging.dispatch")


async def admit_inbound_callback(
    sessions: Any,
    *,
    channel_type: str,
    route: InboundRoute | None,
    restricted: bool | Callable[[], Awaitable[bool]] = False,
) -> bool:
    """Reserve one handler task or durably refuse it before pre-turn effects.

    A callable restriction is resolved only after admission refuses the task.
    Resume-capable channels use that form because their effective session takes
    an awaited routing decision to determine. The upstream client handler task
    remains census-visible during that decision, while the happy path keeps the
    synchronous reserve-before-effects boundary and pays no routing lookup.
    """
    reserve = getattr(sessions, "reserve_inbound_callback", None)
    if not callable(reserve):
        # Compatibility for focused embedders/test doubles that are not a
        # SessionManager. Production channel dispatch always receives the real
        # facade, whose structural contract requires this method.
        return True
    reservation = reserve()
    if reservation is None:
        try:
            refusal_restricted = bool(await restricted()) if callable(restricted) else restricted
        except Exception:
            logger.warning(
                "%s: could not resolve refused callback privacy; suppressing durable spool",
                channel_type,
                exc_info=True,
            )
            refusal_restricted = True
        if not refusal_restricted:
            if getattr(sessions, "update_restart_fenced", False):
                spool_refused_turn_sync(channel_type=channel_type, route=route)
            else:
                await spool_refused_turn(channel_type=channel_type, route=route)
        return False
    task = asyncio.current_task()
    if task is None:
        reservation.release()
        raise RuntimeError("inbound callback has no owning asyncio task")
    task.add_done_callback(lambda _done: reservation.release())
    return True


class _InboundCallbackAdmission:
    """Explicit callback lease for dispatchers running on a long-lived task."""

    def __init__(
        self,
        sessions: Any,
        *,
        channel_type: str,
        route: InboundRoute | None,
        restricted: bool,
    ) -> None:
        self._sessions = sessions
        self._channel_type = channel_type
        self._route = route
        self._restricted = restricted
        self._reservation: Any = None

    async def __aenter__(self) -> bool:
        reserve = getattr(self._sessions, "reserve_inbound_callback", None)
        if not callable(reserve):
            return True
        self._reservation = reserve()
        if self._reservation is not None:
            return True
        if not self._restricted:
            if getattr(self._sessions, "update_restart_fenced", False):
                spool_refused_turn_sync(channel_type=self._channel_type, route=self._route)
            else:
                await spool_refused_turn(channel_type=self._channel_type, route=self._route)
        return False

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self._reservation is not None:
            self._reservation.release()


def hold_inbound_callback(
    sessions: Any,
    *,
    channel_type: str,
    route: InboundRoute | None,
    restricted: bool = False,
) -> _InboundCallbackAdmission:
    """Hold callback census ownership only for the surrounding async scope."""
    return _InboundCallbackAdmission(
        sessions,
        channel_type=channel_type,
        route=route,
        restricted=restricted,
    )


#: Every spelling a channel accepts for "abort the running turn". The union of
#: the per-channel command tables (``/stop`` and ``/cancel`` everywhere, plus
#: Discord's ``!`` bang forms and WeCom's ``停止``), owned here because the
#: governance exemption below is channel-neutral.
#:
#: This is a MIRROR of data that lives in the channel packages, so it cannot be
#: kept correct by intention alone — and it already drifted once: WeCom shipped
#: ``停止`` in its own table and on its ``/help`` card while this set knew only
#: the ASCII spellings, so a denied WeCom conversation had no reachable
#: off-switch in the language that channel exists for. Deriving the union here
#: would mean the shared layer importing all nine channel packages, which
#: inverts the dependency it exists to hold. So the tripwire lives in
#: ``test_messaging_dispatch.py``, and it DISCOVERS the channel tables rather
#: than listing them: the earlier version checked Discord and Telegram by hand
#: and was blind to the three channels that actually diverged.
_CANCEL_ALIASES = frozenset(("/stop", "/cancel", "!stop", "!cancel", "停止"))


def is_pure_cancel(text: str, *, has_attachments: bool = False) -> bool:
    """Whether *text* is nothing but a cancellation, carrying nothing with it.

    PURE is the load-bearing word, and it is why the match is exact rather than a
    prefix or a search:

    * **Whole message.** A cancel alias with anything else on the line is an
      ordinary message that happens to start with one, and every channel's own
      ``parse_command`` already matches only the whole message.
    * **No attachments.** A channel fetches media AFTER it authorizes the message,
      so exempting an attachment-bearing cancel would spend an authenticated
      download, a transcription, and a ``channel_history`` write on a conversation
      policy has denied -- the exact leak the gate exists to stop -- before any
      turn is refused. Such a message is gated like any other.

    Case- and whitespace-insensitive, matching the per-channel tables.
    """
    if has_attachments:
        return False
    return text.strip().lower() in _CANCEL_ALIASES


async def inbound_permitted(
    channel_type: str, *, text: str = "", has_attachments: bool = False
) -> bool:
    """Per-message governance gate.

    Rechecked on every message (not just at connect) so a host-profile deny
    added while the transport is live stops dispatch without a restart. The
    pipeline calls this itself, so a channel cannot forget it.

    A PURE cancellation is the one exemption, matching the native Slack route's
    ``!stop`` carve-out: a denied channel must still be able to halt a runaway
    session it already STARTED, and on a channel with no interactive buttons
    (``max_buttons=0``) the typed cancel is the only affordance there is, so
    gating it makes the off-switch unreachable exactly when it is needed. Nothing
    else is exempt -- a restart is not a cancellation.

    Callers pass *text* and *has_attachments* only where a cancel can arrive: the
    dispatcher's per-message entry, ahead of command parsing. The defaults leave
    the gate strict, which is what keeps the pipeline's backstop a backstop -- a
    cancel runs no turn, so it never reaches it.
    """
    if await channel_inbound_permitted(channel_type):
        return True
    if text and is_pure_cancel(text, has_attachments=has_attachments):
        # Logged, not silent: an operator reading the trail needs to see that a
        # governed-off channel was still allowed to stop its own session.
        logger.info("%s cancellation allowed through a channels governance deny", channel_type)
        return True
    logger.info("%s inbound dropped: denied by channels governance policy", channel_type)
    return False


def delivery_is_muted(sessions: Any, session_key: str, channel_type: str) -> bool:
    """True when output to *channel_type* must NOT be written back for this session.

    The primitive behind :func:`conversation_is_muted`, taking explicit arguments
    because :class:`ChannelTurns` reads it without a ``ChannelTurn`` and Telegram
    runs its OWN copy of the turn loop. Every channel that can be disconnected
    must consult this, or the dashboard control is a label with nothing behind it.

    ``origin`` is resolved rather than passed because a session can hold two
    non-Slack deliveries at once, and they mute independently: the conversation
    it was BORN in, and an explicit mirror it was told to post to. This turn came
    from the born-in conversation exactly when the session key IS a channel key
    in this turn's own namespace — a channel-born session's key is its
    conversation. Anything else arriving here is a mirror/resume binding, so it
    reads the mirror flag.

    Slack never reaches these pipelines (it drives its own gateway and is gated by
    ``slack_mirror_is_paused``), so no Slack special-case is needed here.

    Fails OPEN, matching the dashboard-side predicates: ``sessions`` is a bare
    ``MagicMock`` across much of the suite and would return a truthy child for an
    unstubbed accessor, which would silence every channel in the test suite. A
    muted conversation that stays noisy is a visible bug; a live conversation
    silently dead is a much worse one.
    """
    origin = is_channel_session_key(session_key) and (
        channel_namespace_of(session_key) == channel_type
    )
    try:
        return sessions.is_mirror_paused(session_key, origin=origin) is True
    except Exception:
        logger.debug(
            "%s: mirror pause lookup failed session=%s",
            channel_type,
            session_key,
            exc_info=True,
        )
        return False


def conversation_is_muted(sessions: Any, turn: ChannelTurn) -> bool:
    """:func:`delivery_is_muted` for a turn on the shared pipeline."""
    return delivery_is_muted(sessions, turn.session_key, turn.channel_type)
