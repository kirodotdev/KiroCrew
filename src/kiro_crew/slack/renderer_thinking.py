"""The Thinking reply of ``SlackRenderer``: the model's reasoning, above the answer.

With ``slack.show_thinking`` on, the reasoning is posted as its own thread reply,
and it must sit ABOVE the answer it belongs to. Slack orders a thread by ts, so
the reply has to exist before the answer's first message does. That message is
opened by the first tool call, by a flat reply's first text, or at the end of the
turn, and reasoning can still arrive after it (between tool calls, or after the
first answer chunk). So the slot is reserved before the answer's message is
opened (``_reserve_thinking``), in a thread with a placeholder when no reasoning
came yet, and filled in place at the end of the turn (``_settle_thinking``).
Nothing of the reasoning is ever written after the answer: reasoning that does
not fit in the messages already reserved is cut there, and reasoning with no
reserved slot above an opened answer is not shown.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from kiro_crew.slack.agent_sessions.fence import turn_muted
from kiro_crew.slack.handler import _THINKING_PLACEHOLDER

logger = logging.getLogger(__name__)

#: Ends the last reserved Thinking message when the reasoning outgrew the slot.
THINKING_CUT_NOTICE = "\n\n_The rest of the thinking did not fit here._"


class ThinkingReply:
    """Keeps one turn's Thinking reply above its answer.

    It only orders and sends. The text it sends is ``_thinking_text()``, which
    ``SlackRenderer`` defines: the reasoning past the renderer's display floor.
    """

    slack: Any
    channel: str
    thread_ts: str | None
    _show_thinking: bool
    _split_for_slack: Callable[..., Awaitable[list[str]]]
    _thinking_text: Callable[[], str]

    def _init_thinking(self) -> None:
        #: ``[ts, text]`` of each message showing the reasoning, oldest first,
        #: all above the answer; ``text`` is what Slack last accepted for it.
        self._thinking_msgs: list[list[str]] = []
        #: The reasoning the messages were last written from.
        self._thinking_shown = ""
        #: A post was tried. Once per turn: a refused post is not retried per chunk.
        self._thinking_tried = False
        #: The answer's first message exists, so nothing new may be posted above it.
        self._answer_opened = False
        self._thinking_settled = False

    async def _post_thinking(self, parts: list[str]) -> None:
        self._thinking_tried = True
        for part in parts:
            # Best-effort: MUST NOT raise. Reasoning is a decorative side
            # channel; the answer is delivered separately.
            try:
                ts = await self.slack.post_message(self.channel, part, self.thread_ts)
            except Exception:
                logger.warning("Failed to post thinking chunk", exc_info=True)
                continue
            if ts:
                self._thinking_msgs.append([ts, part])

    async def _reserve_thinking(self, *, opening_answer: bool) -> None:
        """Post the Thinking reply now, while it can still go above the answer.

        *opening_answer*: the caller is about to open the answer's first
        message. In a thread a placeholder then holds the slot for reasoning
        that has not come yet; a flat reply gets none, because each of its
        posts is a channel message that notifies. From then on nothing new is
        posted for the reasoning.
        """
        if not self._show_thinking or self._thinking_settled or self._answer_opened:
            return
        if opening_answer:
            self._answer_opened = True
        if self._thinking_tried:
            return
        shown = self._thinking_text()
        if shown:
            await self._post_thinking(await self._split_for_slack(f"*Thinking*\n{shown}"))
            self._thinking_shown = shown
        elif opening_answer and self.thread_ts:
            await self._post_thinking([_THINKING_PLACEHOLDER])

    async def _settle_thinking(self, *, post_new: bool = True) -> str:
        """Fill the reserved messages with the whole reasoning, once per turn.

        Updates them in place, deletes a placeholder no reasoning came for, and
        posts only while no answer message exists yet (*post_new* False: a
        dying turn's teardown only settles what is already up). Returns the
        Thinking text the reader is left with ("" when none is shown).
        """
        if not self._show_thinking or self._thinking_settled:
            return ""
        may_post = post_new and not self._answer_opened
        if may_post:
            await self._reserve_thinking(opening_answer=False)
        shown = self._thinking_text()
        if self._thinking_msgs and (shown != self._thinking_shown or not shown):
            # Changed since posted, or a placeholder no reasoning came for.
            await self._rewrite_thinking(shown, may_post=may_post)
        # Set last: a cancellation inside the rewrite leaves it for close().
        self._thinking_settled = True
        if shown and not self._thinking_msgs:
            logger.debug("Thinking reply not shown: no slot was reserved above the answer")
        return "".join(text for _, text in self._thinking_msgs if text != _THINKING_PLACEHOLDER)

    async def _rewrite_thinking(self, shown: str, *, may_post: bool) -> None:
        """Show *shown* in the reserved messages, in place; delete the ones it leaves empty.

        *may_post*: no answer message exists yet, so parts past the reserved
        messages are posted below them; otherwise the last one is cut.
        """
        reserved = len(self._thinking_msgs)
        parts: list[str] = []
        if shown:
            text = f"*Thinking*\n{shown}"
            parts = await self._split_for_slack(text)
            if len(parts) > reserved and not may_post:
                parts = await self._split_for_slack(text, reserve=len(THINKING_CUT_NOTICE))
                parts = parts[:reserved]
                parts[-1] += THINKING_CUT_NOTICE
        kept: list[list[str]] = []
        for i, msg in enumerate(self._thinking_msgs):
            # A native Stop drops this turn's edits (the client's fence), so an
            # edit would leave a placeholder up for good: it is deleted instead,
            # and a reply already showing reasoning stays as it is.
            muted = turn_muted(self.channel, ts=msg[0])
            if muted and msg[1] != _THINKING_PLACEHOLDER:
                kept.append(msg)
                continue
            try:
                if i < len(parts) and not muted:
                    await self.slack.update_message(self.channel, msg[0], parts[i])
                    msg[1] = parts[i]
                    kept.append(msg)
                else:
                    await self.slack.delete_message(self.channel, msg[0])
            except Exception:
                logger.warning("Failed to settle the thinking reply", exc_info=True)
                kept.append(msg)
        self._thinking_msgs = kept
        self._thinking_shown = shown
        if len(parts) > reserved:
            await self._post_thinking(parts[reserved:])
