"""Run a channel turn through Shared Crew fair admission (``shared_crew.turn_gate``).

With admission off :func:`run_admitted` is ``ChannelTurns._run``. A refused
inbound turn tells its sender the Crew is at capacity; a refused wake answers
BUSY, so it retries its durable claim. Callers reach it through
``ChannelTurns._run_admitted`` in :mod:`kiro_crew.messaging.dispatch`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.messaging.dispatch_contract import Asker, RendererSource, TurnOutcome, Verdict
from kiro_crew.messaging.renderer import Renderer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.messaging.dispatch import ChannelTurns

logger = logging.getLogger("kiro_crew.messaging.dispatch")


async def run_admitted(
    turns: "ChannelTurns",
    asker: Asker,
    text: str,
    source: RendererSource,
    **kwargs: Any,
) -> TurnOutcome:
    """Run one turn of *turns* for *asker* behind the fair-admission gate."""
    from kiro_crew.shared_crew.turn_gate import run_channel_turn

    monitor = kwargs.get("monitor")

    async def _refused(reason: Any) -> TurnOutcome:
        if monitor is not None:
            return turns._outcome(Verdict.BUSY, monitor)
        await note_capacity_refusal(turns.channel, source, kwargs.get("is_muted"), reason)
        return turns._outcome(Verdict.BUSY, None)

    return await run_channel_turn(
        session_key=asker.session_key,
        text=text,
        automation=monitor is not None,
        run=lambda: turns._run(asker, text, source, **kwargs),
        refused=_refused,
    )


async def note_capacity_refusal(
    channel: str,
    source: RendererSource,
    is_muted: Callable[[], bool] | None,
    reason: Any,
) -> None:
    """Tell the sender the Crew is at capacity, unless the conversation is muted."""
    if is_muted is not None and is_muted():
        return
    from kiro_crew.shared_crew.turn_gate import capacity_note

    try:
        renderer: Any = source if isinstance(source, Renderer) else await source(False)
        await renderer.on_text_chunk(capacity_note(reason))
        await renderer.on_done()
    except Exception:  # noqa: BLE001 -- the note is best effort
        logger.debug("%s capacity note failed", channel, exc_info=True)
