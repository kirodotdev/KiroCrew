"""An observed turn: one the engine started on a chat's session by itself.

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
from typing import TYPE_CHECKING

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
