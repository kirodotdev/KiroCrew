"""Bind a dashboard turn's trace root: its turn id, its causes and its crew attributes.

``_run_chat`` calls :func:`bind_turn` once per turn. While tracing is on, every turn,
including a nested ``/prompts get`` turn sharing its task, reads its crew attributes
when the slot is an Ultra Mode or Bot conductor or one of their workers
(``tracing.crew``), and joins its board's round, or its item's anchor for a worker
(``tracing.work.binding``), unless a run it delivers joins it first.

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
from kiro_crew.tracing import work as trace_work

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


async def _crew_attrs(
    state: Any, slot_key: str, hash_key: bytes | None
) -> tuple[dict[str, Any], Any, str]:
    """The turn's crew attributes, and the round or item anchor it joins (``tracing.work``)."""
    row = _stamped_row(state, slot_key)
    root, _dispatcher, kind = trace_crew.root_of(slot_key, row)
    if not root:
        return {}, None, ""
    item = await asyncio.to_thread(trace_crew.bound_item, slot_key) if root != slot_key else None
    join, rollup_key = await asyncio.to_thread(trace_work.binding, slot_key, item, kind)
    return trace_crew.attrs(hash_key, slot_key, row, item), join, rollup_key


def root_kind(state: Any, slot_key: str) -> str:
    """``ultra`` or ``mate`` when *slot_key* is on a crew tree, else ``""``. No I/O."""
    return trace_crew.root_of(slot_key, _stamped_row(state, slot_key))[2]


async def bind_turn(state: Any, slot: Any, turn_id: str, *, top: bool) -> contextvars.Token | None:
    """Bind *turn_id* as the root of the ACP turns this task streams next. Never raises.

    While tracing is on, read crew attributes at every depth; only a *top* turn
    consumes completion causes and joins or links to their runs.
    Release with ``tracing.turns.release_turn_root``. Nothing is bound until every
    read is done, so a cancelled read leaves nothing to release.
    """
    join, links, attrs, rollup_key = None, (), None, ""
    trace = trace_provider.peek()
    if trace is not None:
        # Taken first, so a failed attribute read below cannot leave them for a
        # later turn of this task.
        causes = trace_subagents.take_causes(turn_id) if top else ()
        try:
            if top:
                join, links = trace_subagents.completion_binding(causes)
            attrs, board_join, board_rollup = await _crew_attrs(
                state, str(getattr(slot, "key", "") or ""), trace.hash_key
            )
            if join is None and board_join is not None:
                # A turn a run caused joins that run; any other turn on a board, its round.
                join, rollup_key = board_join, board_rollup
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("trace root binding failed", exc_info=True)
    return trace_turns.bind_turn_root(
        turn_id, join=join, links=links, attrs=attrs, rollup_key=rollup_key
    )


def slot_session_key(state: Any, slot_key: str) -> str:
    """The registry session key dashboard slot *slot_key* runs on, for lifecycle spans.

    ``tracing.sessions`` keys a session by the key the session registry holds
    (``dashboard:<slot>``, or a linked slot's channel key), while lineage stamps and
    the session tree name bare slot keys. No I/O; ``""`` for an empty key.
    shortcut: a slot missing from this dashboard falls back to its dashboard key,
    which misses a closed channel-linked parent; resolve from the session map if that
    matters.
    """
    if not slot_key:
        return ""
    from kiro_crew.dashboard.chat_utils_slots import _history_key_for, effective_session_key

    try:
        slot = state.get_slot(slot_key)
    except Exception:
        slot = None
    return effective_session_key(slot) if slot is not None else _history_key_for(slot_key)


def note_slot_lineage(state: Any, slot: Any, session_key: str, *, creator: str) -> None:
    """Note the parent *slot*'s next session open names, before an allocation claims it.

    Called by both dashboard allocation sites, the first turn's claim and the eager
    spawn's prefetch, so whichever registers the session first already has it.
    *creator* is the slot key ``_crew_log_lineage`` gives (a worker's creator, only
    when this process minted the lineage); then a fork's source, then the dashboard
    slot a channel session is linked to. No I/O, and nothing at all while tracing is
    off.
    """
    if trace_provider.peek() is None:
        return
    from kiro_crew.dashboard.chat_utils_slots import _history_key_for
    from kiro_crew.tracing import sessions as trace_sessions

    try:
        trace_sessions.note_lineage(
            session_key,
            creator=slot_session_key(state, creator),
            forked_from=str(getattr(slot, "forked_from", "") or ""),
            slot_key=_history_key_for(str(slot.key)),
        )
    except Exception:
        logger.debug("trace lineage note failed", exc_info=True)


def registry_key(sessions: Any, session_key: str) -> str:
    """*session_key* as the session registry folds it, the key lifecycle spans carry.

    A switch records its span under this key, so an alias spelling (a bare channel
    timestamp for ``slack:<ts>``) still lands in the session's own trace. Never raises.
    """
    fold = getattr(sessions, "_fold_key", None)
    try:
        return str(fold(session_key)) if callable(fold) else session_key
    except Exception:  # tracing must never cost the switch
        return session_key
