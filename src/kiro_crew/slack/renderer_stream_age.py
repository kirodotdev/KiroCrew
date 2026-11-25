"""Handing a Slack stream on before Slack ends it, for ``SlackRenderer``.

Slack ends a streaming message at ten minutes of age, whatever is appended to it:
the first append past that is refused with ``message_not_in_streaming_state``, the
message is titled "Something went wrong" and its open step becomes an error. So the
renderer rotates a stream once it is old, and parks the step still running on the
message it leaves. Composed into ``SlackRenderer`` (``kiro_crew.slack.renderer``),
which owns the stream, its clock and ``_rotate_stream``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

logger = logging.getLogger(__name__)

#: What a message says about a step still running when its stream hands over to the next.
_STEP_CONTINUES = "Still running; it continues in the next message."
_STREAM_ROTATED_TITLE = "Continued in the next message"
#: Slack ends a streaming message at ten minutes of age, whatever is appended to it:
#: the first append past that is refused with ``message_not_in_streaming_state``, the
#: message turns "Something went wrong" and its open step becomes an error. A stream
#: is handed on to a new one before then, with a minute to spare for the 30 s refresh.
_STREAM_MAX_AGE_SECS = 540.0


class StreamHandover:
    """Rotate an aged stream, and settle the step open on the one it leaves."""

    slack: Any
    channel: str
    _use_slack_stream: bool
    _stream_ts: str | None
    _stream_born: float
    _stream_clock: Callable[[], float]
    _rotate_lock: asyncio.Lock
    _active_task_id: str
    _active_task_title: str

    if TYPE_CHECKING:  # defined on SlackRenderer

        async def _rotate_stream(self) -> str | None: ...

        def _tool_elapsed_str(self) -> str: ...

        async def _append_task(
            self,
            task_id: str,
            title: str,
            status: str,
            details: str = "",
            *,
            rotate_if_aged: bool = True,
        ) -> bool: ...

    async def _rotate_if_aged(self) -> None:
        """Hand the stream on to a new one before Slack ends it at ten minutes.

        Called ahead of every append, so a long tool phase, whose 30 s step refresh
        is the only thing touching the stream, rotates as well as a long answer.
        Best-effort: a failed rotation is the same outcome as one forced by a refused
        append (the caller falls back to ``chat.update``) and never raises.
        """
        if not (self._use_slack_stream and self._stream_ts):
            return
        if self._stream_clock() - self._stream_born < _STREAM_MAX_AGE_SECS:
            return
        async with self._rotate_lock:
            # Another caller may have rotated while this one waited for the lock.
            if self._stream_clock() - self._stream_born < _STREAM_MAX_AGE_SECS:
                return
            logger.info(
                "Slack stream is %.0fs old; rotating before Slack ends it",
                self._stream_clock() - self._stream_born,
            )
            await self._rotate_stream()

    async def _park_running_task(self) -> tuple[str, str] | None:
        """Settle the step still open on the stream about to stop.

        Slack turns every step that is not complete when a stream stops into an
        ``error`` and then titles the whole message "Something went wrong", over a
        turn that is running fine. So the open step is closed as done on the old
        message, with a detail saying it continues, and the message is retitled to
        say where the turn went. Returns the step's id and its title so the caller
        can open it again on the new stream, or ``None`` when no step is open.
        Best-effort: both writes are decoration and MUST NOT raise.
        """
        task_id = self._active_task_id
        if not task_id or not self._use_slack_stream or not self._stream_ts:
            return None
        elapsed = self._tool_elapsed_str()
        title = f"{self._active_task_title}  {elapsed}" if elapsed else self._active_task_title
        await self._append_task(
            task_id, title, "complete", details=_STEP_CONTINUES, rotate_if_aged=False
        )
        try:
            await self.slack.set_plan_title(self.channel, self._stream_ts, _STREAM_ROTATED_TITLE)
        except Exception:
            logger.warning("Slack set_plan_title failed — leaving the plan title", exc_info=True)
        return task_id, title
