"""The session verbs only Kiro Agent (KAS) answers, forwarded to the session handle.

An observed turn (one the engine started on a chat's session by itself), the rewind
of an unanswered prompt, and a person's ``/context``, ``/knowledge`` and ``/code``.

Each function here whose first parameter is ``self`` is an ``AcpSessionProvider``
method: the class binds it by name, so it reads and writes the provider's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller.

Instance fields read: ``_handle``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.runtime import AcpRuntimeDead, AcpRuntimeError

if TYPE_CHECKING:
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.base import LLMEvent

#: The provider's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_provider")


async def stream_observe(
    self: AcpSessionProvider, timeout: float | None = None
) -> AsyncIterator[LLMEvent]:
    """Yield the turn the engine started on this session by itself.

    :meth:`AcpSessionHandle.observe_turn` behind the same claim and the same
    exception translation as :meth:`AcpSessionProvider.stream`, so the chat turn
    that reads it handles approvals, failures and a dead runtime as it does for a
    prompt. Nothing is sent, so the context-delivery receipt is neither consulted
    nor advanced.
    """
    try:
        self.reclaim()
    except Exception:
        logger.debug("stream_observe: stub re-claim failed", exc_info=True)
    claim = self._claim_shared_turn()
    try:
        async with aclosing(self._handle.observe_turn(timeout)) as events:
            async for event in events:
                yield event
    except AcpRuntimeDead as exc:
        raise self._translate_dead(exc) from exc
    except AcpRuntimeError as exc:
        raise AcpError(str(exc)) from exc
    finally:
        await self._end_shared_turn(claim)


async def discard_unanswered_prompt(self: AcpSessionProvider) -> bool:
    """Forward to the session handle: rewind KAS history to before the last prompt."""
    discard = getattr(self._handle, "discard_unanswered_prompt", None)
    if discard is None:
        return False
    return await self._guarded(discard()) is True


async def kas_command_call(
    self: AcpSessionProvider, command: str, params: dict[str, Any]
) -> dict[str, Any] | None:
    """Forward to the session handle: one of KAS's ``/context``, ``/knowledge``
    or ``/code`` verbs for this session, or None when it cannot be sent."""
    call = getattr(self._handle, "kas_command_call", None)
    if call is None:
        return None
    return await self._guarded(call(command, params))
