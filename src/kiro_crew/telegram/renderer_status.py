"""The status a Telegram turn shows around its answer.

The typing indicator and its refresh loop, the stall marks the footer carries when
nothing has moved for a while, the transient tool footer, the compaction note, the
reasoning post's budget and the turn footer (duration and context gauge). Each function
here whose first parameter is ``self`` is bound as the ``TelegramRenderer`` attribute of
the same name in :mod:`kiro_crew.telegram.renderer`, which keeps the renderer's state,
so ``self.<name>(...)`` calls and instance and class patches reach it as they reach a
method defined in the class body. They read ``_awaiting_approval``, ``_chat_id``,
``_client``, ``_closed``, ``_ctx_client``, ``_last_progress``, ``_last_tool``,
``_show_thinking``, ``_shown_stall``, ``_thinking``, ``_thinking_chars``,
``_thread_id``, ``_turn_started`` and ``_typing_task``, and write
``_awaiting_approval``, ``_closed``, ``_ctx_client``, ``_last_progress``,
``_last_tool``, ``_thinking_chars``, ``_tool`` and ``_typing_task``. New status
indicators go here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.telegram.renderer import TelegramRenderer

logger = logging.getLogger("kiro_crew.telegram.renderer")


# Telegram has no native token streaming: "streaming" meant editing one message
# on every chunk, and each edit is a full HTTP round-trip + a whole-bubble
# re-render, which reads as a stutter (WeCom streams frames over a persistent
# WebSocket, so it stays smooth). Instead we do "block streaming": hold a live
# "typing…" indicator while the answer forms, then post the finished answer as
# one clean block. This kills the edit-jank entirely and only touches this
# renderer -- the shared messaging event stream (and Slack/WeCom) is untouched.
#
# Telegram's "typing" chat action lasts ~5s, so refresh it just under that
# for the duration of a turn (shown while we accumulate before posting).
_TYPING_REFRESH_S = 4.0


# ── Stall marks on the live bubble ──
# Telegram has no message-reaction budget to spend on a phase indicator: a bot
# holds ONE reaction per message (setting is a replace, not an add), its emoji
# allow-list has no globe/wrench/brand mark, a chat's own available_reactions can
# narrow it further at any time, and the rate limit is per CHAT — so a reaction
# per phase competes with the streaming edits this channel already spends. The
# typing indicator plus the transient "🔧 {tool}…" footer already say "working";
# what they cannot say is "working, but nothing has moved in a while". These two
# marks ride the footer the renderer was going to edit anyway, so they cost no
# extra API class. Same thresholds as the Slack controller's soft/hard stalls.
_SOFT_STALL_S = 15.0
_HARD_STALL_S = 45.0
_SOFT_STALL_MARK = "🥱 still working…"
_HARD_STALL_MARK = "😨 this is taking a while…"


#: Context-usage gauge thresholds for the turn footer, and Slack's own: a shared
#: reading of "how close is this conversation to needing /compact".
_CTX_GAUGE = ((70, "🔴"), (50, "🟠"), (30, "🟡"), (0, "🟢"))

#: The footer is shown only when it carries something ACTIONABLE. Slack posts its
#: equivalent on every turn, but Slack has a `context` block — a small grey line
#: — while Telegram's nearest affordance is a quote bar under the answer, and
#: "Finished in 1s · 🟢 ctx 4%" as a permanent fixture under every reply is noise
#: that trains the reader to skip the place the real warning will appear. So:
#: a duration worth noticing, or a context reading worth acting on.
_FOOTER_MIN_SECS = 10.0
_FOOTER_MIN_CTX_PCT = 50

#: The reasoning post's fixed wrapper (``<blockquote expandable>💭 …`` plus its
#: closer). Subtracted from the render budget so the truncation can never cut
#: inside the tags it has to leave balanced.
_THINKING_SCAFFOLD = "<blockquote expandable>💭 </blockquote>"


# -- lifecycle ----------------------------------------------------------
async def on_turn_start(self: TelegramRenderer) -> None:
    # Typing indicator only — no draft preview. Idempotent (dispatch + driver
    # both call this).
    if self._typing_task is not None or self._closed:
        return
    self._note_progress()
    self._typing_task = asyncio.create_task(self._typing_loop())


async def _typing_loop(self: TelegramRenderer) -> None:
    """Keep the 'typing…' chat action alive (it expires after ~5s) for the
    duration of the turn, and surface a stall mark when nothing has moved.

    The stall check rides this existing tick rather than a task of its own:
    the loop already wakes every ``_TYPING_REFRESH_S``, which is finer than
    the soft threshold, and the mark it publishes goes on a frame the
    renderer edits anyway. Cancelled by ``_stop_typing``.
    """
    try:
        while not self._closed:
            try:
                await self._client.send_typing(self._chat_id, message_thread_id=self._thread_id)
            except Exception:
                logger.debug("Telegram: typing refresh failed", exc_info=True)
            if not self._closed and self._stall_mark() != self._shown_stall:
                # force=True so the mark lands now: the throttle exists to
                # coalesce a token stream, and by definition there is none.
                try:
                    await self._stream_live(force=True)
                except Exception:
                    logger.debug("Telegram: stall mark refresh failed", exc_info=True)
            await asyncio.sleep(_TYPING_REFRESH_S)
    except asyncio.CancelledError:
        pass


def _stop_typing(self: TelegramRenderer) -> None:
    self._closed = True
    task, self._typing_task = self._typing_task, None
    if task is not None and not task.done():
        task.cancel()


def _note_progress(self: TelegramRenderer) -> None:
    """Reset the stall clock, and end any approval wait. Any provider event is
    progress — and an event arriving after an approval prompt is exactly how
    this renderer learns the decision was made."""
    self._last_progress = time.monotonic()
    self._awaiting_approval = False


def _stall_mark(self: TelegramRenderer) -> str:
    """The stall mark the live frame should carry right now, or ``""``.

    Read from the clock rather than latched, so the mark clears itself the
    moment output resumes instead of persisting to the end of the turn.

    Suppressed while a tool approval is pending: the turn is blocked on the
    USER, not stalled, and the approval window is 300s against a 45s hard
    mark — so without this every approval that waits a minute reports the
    agent as hung. Slack's controller pauses its own watchdog for the same
    reason (``pause_stall_watchdog`` around its approval wait).
    """
    if self._awaiting_approval:
        return ""
    idle = time.monotonic() - self._last_progress
    if idle >= _HARD_STALL_S:
        return _HARD_STALL_MARK
    if idle >= _SOFT_STALL_S:
        return _SOFT_STALL_MARK
    return ""


def attach_context_client(self: TelegramRenderer, client: Any) -> None:
    """Supply the provider's ACP client, whose ``context_usage_pct`` the turn
    footer reads. Set after the session is acquired, because the provider does
    not exist before then; absent, the footer reports duration only."""
    self._ctx_client = client


async def on_thinking(self: TelegramRenderer, text: str) -> None:
    """Accumulate the model's reasoning; posted once at ``on_done``.

    Counts as progress: a turn emitting reasoning is working, and a stall mark
    that appeared over it would report the opposite.

    Deliberately NOT streamed. Reasoning arrives as many small chunks and
    Telegram's only streaming primitive is an edit of a message, so streaming
    it would either disturb the answer bubble it must stay out of, or spend an
    edit per chunk on a second bubble — against a per-CHAT rate budget the
    answer is already spending. Off by default (``telegram.show_thinking``),
    and dropped entirely when off so nothing accumulates unread.
    """
    self._note_progress()
    if not self._show_thinking or not text:
        return
    # Bounded: only one message of reasoning is ever posted, and a long
    # agentic turn can emit megabytes of it. Keeping the whole stream would
    # hold it all resident to then discard all but the first few thousand
    # characters. Stop appending once the budget is covered.
    if self._thinking_chars >= self._thinking_budget():
        return
    self._thinking.append(text)
    self._thinking_chars += len(text)


def _turn_footer(self: TelegramRenderer) -> str:
    """``Finished in 12s · 🟠 ctx 54%``, or ``""`` when neither is worth saying.

    Two facts a user cannot get any other way on this channel: how long the
    turn took, and how close the conversation is to needing ``/compact``. Both
    are only interesting past a threshold (see ``_FOOTER_MIN_SECS`` /
    ``_FOOTER_MIN_CTX_PCT``), and a footer that appears under every reply is
    one the reader learns to skip — including on the turn where the context
    warning finally matters.
    """
    elapsed = max(0.0, time.monotonic() - self._turn_started)
    pct: int | None = None
    reader = getattr(self._ctx_client, "context_usage_pct", None)
    if callable(reader):
        try:
            pct = round(reader())
        except Exception:
            logger.debug("Telegram: context usage read failed", exc_info=True)
    if elapsed < _FOOTER_MIN_SECS and (pct is None or pct < _FOOTER_MIN_CTX_PCT):
        return ""
    if elapsed < 60:
        duration = f"{int(elapsed)}s"
    else:
        mins, secs = divmod(int(elapsed), 60)
        duration = f"{mins}m {secs}s"
    footer = f"Finished in {duration}"
    if pct is None:
        return footer
    icon = next(mark for floor, mark in _CTX_GAUGE if pct >= floor)
    return f"{footer} · {icon} ctx {pct}%"


def _thinking_budget(self: TelegramRenderer) -> int:
    """Source characters worth accumulating for the one reasoning message."""
    return max(0, self._rendered_limit() - len(_THINKING_SCAFFOLD))


async def on_tool_call(
    self: TelegramRenderer,
    tool_call_id: str,
    title: str,
    tool_kind: str = "",
    tool_purpose: str = "",
) -> None:
    self._note_progress()
    # Surface mid-turn tool activity as a transient "🔧 {tool}…" footer on
    # the live bubble (force=True so it shows immediately, not throttled).
    # We deliberately do NOT seal a message here: models interleave tool
    # calls mid-sentence, so sealing at a tool boundary chops a sentence
    # into broken bare messages on a channel with no tool cards (proven on
    # Telegram). The footer lives only on live frames — on_text_chunk clears
    # it and seals/finals never carry it.
    self._last_tool = title or tool_kind or "tool"
    self._tool = self._last_tool
    await self._stream_live(force=True)


async def on_compaction(self: TelegramRenderer, context_usage_pct: float) -> None:
    self._note_progress()
    try:
        await self._client.send_message(
            self._chat_id,
            "🗜️ Compacting context…",
            message_thread_id=self._thread_id,
        )
    except Exception:
        logger.debug("Telegram: compaction notice send failed", exc_info=True)
