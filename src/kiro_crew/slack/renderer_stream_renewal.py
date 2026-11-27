"""Stream renewal for ``SlackRenderer``: a fresh stream before Slack's 10-minute limit.

Composed into ``SlackRenderer``, which owns the stream state it reads. The native
handler's twin is ``_AnswerStream.renew_if_due``; the limit and the age check are
``kiro_crew.slack.stream_window``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from kiro_crew.slack.stream_window import (
    CONTINUES_BELOW,
    message_landed_since,
    stream_clock,
    stream_due_for_renewal,
)

logger = logging.getLogger(__name__)


class StreamRenewal:
    """Renews one renderer's live stream before Slack ends it."""

    slack: Any
    channel: str
    thread_ts: str | None
    _finalized: bool
    _renewal_closed: bool = False
    _text_since_step: bool = False
    _use_slack_stream: bool
    _stream_ts: str | None
    _stream_started_at: float
    _stream_messages_noted: int | None
    _renewal: asyncio.Task | None
    _active_task_id: str
    _task_state: dict[str, tuple[str, str]]
    _task_with_details: set[str]
    _narration_pending: tuple[str, str, str | None] | None
    _rotate_stream: Callable[[], Awaitable[str | None]]

    async def _cancel_renewal(self) -> None:
        """Stop renewal permanently before teardown starts awaiting other cleanup."""
        self._renewal_closed = True
        renewal = self._renewal
        if renewal is not None:
            renewal.cancel()
            # ``wait`` never raises the renewal's own outcome, so only this task's
            # cancellation can leave here.
            await asyncio.wait([renewal])

    async def _renew_stream_if_due(self, *, step: bool = False) -> None:
        """Move to a fresh stream before Slack's 10-minute limit closes this one.

        Slack would close it itself and mark the running step's card as an error
        on a turn that is still going (``stream_window``). The running step is
        closed on the old message with a "continues below" title and opened
        again on the new one, so the reader sees it go on rather than fail.

        A caller drawing a running step (``step``) also moves once a person's
        message has landed in the thread below this stream, so the steps go on
        under that message. Text never moves for one: an answer already being
        written stays in one piece above it.

        One renewal at a time: a caller that comes while one is under way waits
        for it, so its card or text lands on the new message and a second
        rotation does not leave an empty message behind. Shielded, because the
        30 s card refresh that usually starts it is cancelled when its tool ends,
        and the next caller would otherwise stop and close the old message a
        second time. Best-effort: MUST NOT raise, as ``_rotate_stream``.
        """
        renewal = self._renewal
        if renewal is None or renewal.done():
            if (
                self._finalized
                or self._renewal_closed
                or not self._use_slack_stream
                or not self._stream_ts
                or not (
                    stream_due_for_renewal(self._stream_started_at, stream_clock())
                    or (
                        step
                        # A rescue can release reply text before the tool timer stops.
                        and not self._text_since_step
                        and message_landed_since(
                            self.channel, self.thread_ts, self._stream_messages_noted
                        )
                    )
                )
            ):
                return
            renewal = self._renewal = asyncio.ensure_future(self._renew_stream())
        await asyncio.shield(renewal)

    async def _renew_stream(self) -> None:
        """Close the running step on the old message, rotate, open it on the new one."""
        assert self._stream_ts is not None
        old_ts = self._stream_ts
        task_id = self._active_task_id
        # Only a recorded step has passed _append_task's display-safety floor.
        task_state = self._task_state.get(task_id)
        if task_state is not None and task_state[1] != "in_progress":
            # A closing card is already in flight; its narration stays on the old message.
            task_id = ""
        title = task_state[0] if task_state is not None else ""
        if task_id and task_state is not None:
            try:
                await self.slack.append_task(
                    self.channel, old_ts, task_id, title + CONTINUES_BELOW, "complete"
                )
            except Exception:
                logger.warning("Slack append_task failed closing a renewed stream", exc_info=True)
        logger.info("Renewing the Slack stream: its age, or a newer message below it")
        if await self._rotate_stream() and task_id:
            assert self._stream_ts is not None
            # Its held narration now finishes it there (``_finish_pending_step``).
            pending = self._narration_pending
            if pending is not None and pending[0] == task_id:
                self._narration_pending = (task_id, pending[1], self._stream_ts)
            if task_state is None:
                # A newly assigned step was never drawn; its caller draws it safely.
                return
            # The step is drawn afresh on the new message, so its details start clean there.
            self._task_with_details.discard(task_id)
            try:
                await self.slack.append_task(
                    self.channel, self._stream_ts, task_id, title, "in_progress"
                )
            except Exception:
                logger.warning("Slack append_task failed on a renewed stream", exc_info=True)
