"""The status a Discord turn shows around its answer.

The typing indicator and its refresh loop; the shared phase ladder of reactions on the
user's own message, with this channel's emoji vocabulary and the
``_MessageReactionSink`` whose ``PUT`` and ``DELETE`` both go through the client's
request ladder; the reasoning gate; the transient tool footer; and the one-line turn
footer with its context-usage chip. Each function here whose first parameter is ``self``
is bound as the ``DiscordRenderer`` attribute of the same name in
:mod:`kiro_crew.discord.renderer`, which keeps the renderer's state, so
``self.<name>(...)`` calls and instance and class patches reach it as they reach a
method defined in the class body. They read ``_channel_id``, ``_client``, ``_closed``,
``_context_source``, ``_ladder``, ``_ladder_paused``, ``_last_tool``, ``_now``,
``_react_message_id``, ``_reactions_enabled``, ``_show_thinking``, ``_t0``,
``_thinking``, ``_typing_task`` and ``capabilities``, and write ``_closed``,
``_ladder``, ``_ladder_paused``, ``_last_tool``, ``_thinking``, ``_tool`` and
``_typing_task``. New status indicators go here.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import TYPE_CHECKING

from kiro_crew.discord.client import DISCORD_MAX_TEXT
from kiro_crew.messaging.status_reactions import (
    PHASE_QUEUED,
    PHASE_THINKING,
    PhaseReactionLadder,
    StallEmojis,
    format_turn_status,
    phase_for_tool_title,
)

if TYPE_CHECKING:
    from kiro_crew.discord.client import DiscordClient
    from kiro_crew.discord.renderer import DiscordRenderer

logger = logging.getLogger("kiro_crew.discord.renderer")


# Discord's typing indicator lasts ~10s per trigger; refresh just under that
# for the duration of a turn.
_TYPING_REFRESH_S = 8.0


#: Phase → reaction for the shared status ladder. Unicode only: Discord's
#: reaction route takes the emoji itself as a path segment, so a Slack-style
#: shortcode would be added literally and rejected. The meanings mirror the
#: Slack ladder so someone reading both channels reads one story, and each mark
#: is a single code point, which keeps percent-encoding out of the picture.
DISCORD_PHASE_EMOJIS: dict[str, str | None] = {
    "queued": "👀",
    "thinking": "🤔",
    "coding": "💻",
    "browsing": "🌐",
    "tool": "🔧",
    "done": "🦞",
    "error": "😱",
}
#: Additive marks for a turn that has gone quiet (soft first, then hard).
DISCORD_STALL_EMOJIS = StallEmojis(soft="🥱", hard="😨")


def _own_reaction_route(channel_id: str, message_id: str, emoji: str) -> str:
    """REST route for the bot's OWN reaction on a message.

    ``PUT`` adds it and ``DELETE`` removes it, so one route carries both halves
    of the status ladder's swap. The emoji is percent-encoded with nothing safe:
    it is a path segment here, not a query value.
    """
    encoded = urllib.parse.quote(emoji, safe="")
    return f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"


class _MessageReactionSink:
    """The status ladder's emoji sink for one Discord message.

    Routes through the client's public request ladder so both verbs share its
    rate-limit accounting and retry budget. Reaction edits are rate-limited per
    channel, which is what the ladder's debounce exists to respect; a rejected
    edit reports itself and is dropped, since a reaction is never the turn.
    """

    def __init__(self, client: "DiscordClient", channel_id: str, message_id: str) -> None:
        self._client = client
        self._channel_id = channel_id
        self._message_id = message_id

    async def _edit(self, method: str, emoji: str) -> None:
        route = _own_reaction_route(self._channel_id, self._message_id, emoji)
        result = await self._client.api_json(method, route, None)
        if not result:
            logger.debug("discord: reaction %s %s failed (%s)", method, emoji, result.detail)

    async def add(self, emoji: str) -> None:
        await self._edit("PUT", emoji)

    async def remove(self, emoji: str) -> None:
        await self._edit("DELETE", emoji)


# -- lifecycle ----------------------------------------------------------
async def on_turn_start(self: DiscordRenderer) -> None:
    # Typing indicator plus the "queued" reaction — no placeholder bubble.
    # Idempotent (dispatch + driver both call this).
    if self._typing_task is not None or self._closed:
        return
    self._ensure_ladder()  # set_phase("queued")
    self._typing_task = asyncio.create_task(self._typing_loop())


# -- status ladder ------------------------------------------------------
def _ensure_ladder(self: DiscordRenderer) -> PhaseReactionLadder | None:
    """Lazily arm the shared phase ladder at ``queued``.

    Returns ``None`` when reactions are off for this channel, when the
    transport cannot react, or when there is no message to react on: the
    ladder marks the USER's message, and there is nothing to decorate
    without one.
    """
    if self._ladder is not None:
        return self._ladder
    if not self._reactions_enabled or not self._react_message_id:
        return None
    if not self.capabilities.reactions or self._closed:
        # A late event after teardown must not arm a fresh ladder, whose
        # timers would then outlive the turn.
        return None
    self._ladder = PhaseReactionLadder(
        _MessageReactionSink(self._client, self._channel_id, self._react_message_id),
        emojis=DISCORD_PHASE_EMOJIS,
        stall=DISCORD_STALL_EMOJIS,
    )
    self._ladder.set_phase(PHASE_QUEUED)
    return self._ladder


def _set_phase(self: DiscordRenderer, phase: str) -> None:
    ladder = self._ensure_ladder()
    if ladder is not None:
        ladder.set_phase(phase)
        self._note_progress()


def _note_progress(self: DiscordRenderer) -> None:
    """Tell the ladder the turn is moving, un-pausing it after an approval."""
    ladder = self._ladder
    if ladder is None:
        return
    if self._ladder_paused:
        self._ladder_paused = False
        ladder.resume_stall_watchdog()
    else:
        ladder.on_progress()


async def _typing_loop(self: DiscordRenderer) -> None:
    """Keep the 'typing…' indicator alive (~10s per trigger) for the
    duration of the turn. Cancelled by ``_stop_typing``."""
    try:
        while not self._closed:
            try:
                await self._client.send_typing(self._channel_id)
            except Exception:
                logger.debug("Discord: typing refresh failed", exc_info=True)
            await asyncio.sleep(_TYPING_REFRESH_S)
    except asyncio.CancelledError:
        pass


def _stop_typing(self: DiscordRenderer) -> None:
    self._closed = True
    task, self._typing_task = self._typing_task, None
    if task is not None and not task.done():
        task.cancel()


async def on_thinking(self: DiscordRenderer, text: str) -> None:
    # The ladder moves on reasoning regardless: the reaction reports what the
    # agent is doing, which is independent of whether the words are shown.
    self._set_phase(PHASE_THINKING)
    if not self._show_thinking:
        # Reasoning stays private unless the operator opts in (parity with
        # Telegram), so it is not even accumulated.
        return None
    self._thinking += text or ""
    return None


async def on_tool_call(
    self: DiscordRenderer,
    tool_call_id: str,
    title: str,
    tool_kind: str = "",
    tool_purpose: str = "",
) -> None:
    # Surface mid-turn tool activity as a transient "🔧 {tool}…" footer on
    # the live bubble (force=True so it shows immediately). We deliberately
    # do NOT seal a message here — see the Telegram renderer's rationale.
    self._last_tool = title or tool_kind or "tool"
    self._tool = self._last_tool
    self._set_phase(phase_for_tool_title(self._last_tool, tool_kind))
    await self._stream_live(force=True)


def _context_pct(self: DiscordRenderer) -> float | None:
    """This session's context-window usage, or ``None`` when unknown.

    Unknown is not 0: an unbound or failing provider must not render as a
    reassuring "plenty of room" chip.
    """
    provider = self._context_source
    if provider is None:
        return None
    try:
        return float(provider.context_usage_pct())
    except Exception:
        logger.debug("discord: context usage unavailable", exc_info=True)
        return None


def _with_turn_footer(self: DiscordRenderer, text: str) -> str:
    """Append the one-line turn footer as Discord subtext.

    Dropped rather than truncated when the segment leaves no room: a clipped
    answer costs the user more than a missing timing line. ``_limit()``
    already holds back headroom below the platform cap for exactly this.
    """
    footer = f"-# {format_turn_status(max(0.0, self._now() - self._t0), self._context_pct())}"
    if len(text) + len(footer) + 2 > DISCORD_MAX_TEXT:
        return text
    return f"{text}\n\n{footer}"
