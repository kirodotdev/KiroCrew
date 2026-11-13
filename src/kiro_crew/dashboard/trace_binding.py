"""Bind a dashboard turn's trace root: its turn id, its causes and its crew attributes.

``_run_chat`` calls :func:`bind_turn` once per turn. While tracing is on, every turn,
including a nested ``/prompts get`` turn sharing its task, reads its crew attributes
when the slot is an Ultra Mode or Bot conductor or one of their workers
(``tracing.crew``).

Only a top-level turn takes the sub-agent completions handed over by the queue drain
(``tracing.subagents.delivered``), and only while tracing is on: one joins that run's
trace, several give links. The work-item read touches disk, so it runs on a worker
thread before anything is bound.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

from kiro_crew.tracing import crew as trace_crew
from kiro_crew.tracing import provider as trace_provider
from kiro_crew.tracing import subagents as trace_subagents
from kiro_crew.tracing import turns as trace_turns

logger = logging.getLogger(__name__)


def _stamped_row(state: Any, slot_key: str) -> dict[str, Any]:
    """*slot_key*'s row with the Crew Mode and mate stamps, by the slot list's rule. No I/O."""
    from kiro_crew import crew_mode
    from kiro_crew.dashboard.session_control_parts.crew_filing import _frame_parents

    try:
        rows = crew_mode.live_stamp_rows(
            state._slots, _frame_parents(state), settled=crew_mode.lineage_is_settled()
        )
        return dict(rows.get(slot_key) or {})
    except Exception:
        logger.debug("trace crew stamps unreadable", exc_info=True)
        return {}


async def _crew_attrs(state: Any, slot_key: str, hash_key: bytes | None) -> dict[str, Any]:
    row = _stamped_row(state, slot_key)
    root, _dispatcher, _kind = trace_crew.root_of(slot_key, row)
    if not root:
        return {}
    item = await asyncio.to_thread(trace_crew.bound_item, slot_key) if root != slot_key else None
    return trace_crew.attrs(hash_key, slot_key, row, item)


async def bind_turn(state: Any, slot: Any, turn_id: str, *, top: bool) -> contextvars.Token | None:
    """Bind *turn_id* as the root of the ACP turns this task streams next. Never raises.

    While tracing is on, read crew attributes at every depth; only a *top* turn
    consumes completion causes and joins or links to their runs.
    Release with ``tracing.turns.release_turn_root``. Nothing is bound until every
    read is done, so a cancelled read leaves nothing to release.
    """
    join, links, attrs = None, (), None
    trace = trace_provider.peek()
    if trace is not None:
        # Taken first, so a failed attribute read below cannot leave them for a
        # later turn of this task.
        causes = trace_subagents.take_causes(turn_id) if top else ()
        try:
            if top:
                join, links = trace_subagents.completion_binding(causes)
            attrs = await _crew_attrs(state, str(getattr(slot, "key", "") or ""), trace.hash_key)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("trace root binding failed", exc_info=True)
    return trace_turns.bind_turn_root(turn_id, join=join, links=links, attrs=attrs)
