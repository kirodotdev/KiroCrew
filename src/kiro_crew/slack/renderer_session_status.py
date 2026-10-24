"""Agent-session status around a tool-approval prompt, for ``SlackRenderer``.

While a turn waits on a person's approval the agent session reads ``suspended``;
the decision, or failing that the next output, puts it back to ``processing``.
Every call is a no-op with agent sessions off. Composed into ``SlackRenderer``
(``kiro_crew.slack.renderer``), which owns ``_suspended`` and ``_resume_tasks``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Protocol

from kiro_crew.slack.agent_sessions.status import (
    SESSION_STATUS_PROCESSING,
    SESSION_STATUS_SUSPENDED,
    set_session_status,
)


class _DecisionNotifier(Protocol):
    def on_decided(self, request_id: str | int, callback: Callable[[], None]) -> bool: ...


class ApprovalSessionStatus:
    """The ``suspended`` -> ``processing`` round trip of one renderer's turn."""

    slack: Any
    channel: str
    thread_ts: str | None
    _suspended: bool
    _resume_tasks: set[asyncio.Task[None]]

    async def _suspend_for_approval(
        self, request_id: str | int, decider: _DecisionNotifier | None
    ) -> None:
        # The turn now waits on a person: with agent sessions live the session reads
        # ``suspended`` until the decision lands (the approved tool then runs with
        # its "Working..." loader and Stop button back), and ``active`` at turn end.
        # No call at all with the feature off. Best-effort: the prompt is posted.
        if self.thread_ts:
            # An earlier approval's resume may still be on its way to Slack: let it
            # land first, or its late ``processing`` overwrites this ``suspended``.
            await self._settle_resumes()
            self._suspended = await set_session_status(
                self.slack, self.channel, self.thread_ts, SESSION_STATUS_SUSPENDED
            )
            if self._suspended and decider is not None:
                decider.on_decided(request_id, self._schedule_resume)

    def _schedule_resume(self) -> None:
        # A future's done-callback runs in the context it was added from -- this
        # turn's -- so the write fence still applies to the resume it starts.
        task = asyncio.ensure_future(self._resume_from_suspended())
        self._resume_tasks.add(task)
        task.add_done_callback(self._resume_tasks.discard)

    async def _settle_resumes(self) -> None:
        """Let a resume started by an approval decision land before the turn's last
        status, so a late ``processing`` can never follow the closing ``active``."""
        pending = [task for task in self._resume_tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _resume_from_suspended(self) -> None:
        """Put a session paused on an approval back to ``processing``; never raises."""
        if not self._suspended:
            return
        self._suspended = False
        await set_session_status(
            self.slack, self.channel, self.thread_ts or None, SESSION_STATUS_PROCESSING
        )
