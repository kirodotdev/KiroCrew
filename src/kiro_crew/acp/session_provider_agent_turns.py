"""Turns the agent starts on its own, for a chat served by ``AcpSessionProvider``.

The session handle sees such a turn in its queue (``session_handle_agent_turns``);
these members give the chat the same three calls a per-chat client answers
(``providers.acp_turns``): register the watch, ask whether a turn is pending, and
read it. The watch is kept on the provider so it follows a handle that replaces
the session's (a fresh conversation, an MCP re-send).

Each function here whose first parameter is ``self`` is an ``AcpSessionProvider``
method: the class binds it by name, so it reads and writes the provider's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller.

Instance fields written: ``_agent_turn_callback``.

Instance fields read: ``_agent_turn_callback``, ``_handle``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.acp.session_provider import AcpSessionProvider


def watch_agent_turns(self: AcpSessionProvider, callback: Callable[[], None] | None) -> bool:
    """See AcpSessionHandle.watch_agent_turns; kept across a handle swap."""
    self._agent_turn_callback = callback
    return self._handle.watch_agent_turns(callback)


def _rewatch_agent_turns(self: AcpSessionProvider) -> None:
    """Put the chat's agent-turn watch on the handle that just replaced the old one."""
    if self._agent_turn_callback is not None:
        self._handle.watch_agent_turns(self._agent_turn_callback)


def agent_turn_pending(self: AcpSessionProvider) -> bool:
    """See AcpSessionHandle.agent_turn_pending."""
    return self._handle.agent_turn_pending()


async def stream_unsolicited(
    self: AcpSessionProvider, timeout: float | None = None
) -> AsyncGenerator[Any, None]:
    """The turn the agent started on its own, under this chat's shared-turn claim."""
    claim = self._claim_shared_turn()
    try:
        async with aclosing(self._handle.stream_unsolicited(timeout)) as events:
            async for event in events:
                yield event
    finally:
        await self._end_shared_turn(claim)
