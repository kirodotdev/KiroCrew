"""Worker creation, manager changes and tree-parent changes as spans (tracing RFC §5.5).

- **Creation.** A ``session_create`` or ``session_fork`` the dashboard serves (and so
  ``work_dispatch``, which creates through the same route) becomes one
  ``kirocrew.session.create`` / ``kirocrew.session.fork`` span in the calling turn. Its
  ids are kept under the new slot, and the new session's first traced turn takes them
  once as a link (:func:`first_turn_link`), so the worker's work points back at the
  turn that started it.
- **Manager changes.** A handoff or close hand-back becomes one
  ``kirocrew.session.manager_change`` span: in the calling turn when one is streaming,
  else in a one-span trace of its own (a hand-back runs inside a close, with no turn).
- **Tree-parent changes.** An adopt or release becomes one
  ``kirocrew.session.parent_change`` span with parent hashes, placed the same way.
  These operations change the tree edge, not the session's manager.

Attributes are the operation (a closed vocabulary) and keyed hashes of the session
keys; never a title or any other text. In memory only, bounded: a restart loses the
pending first-turn links. shortcut: a create made outside a traced turn (a person's
own call) records nothing; give it a one-span trace if those need to show.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

from kiro_crew.tracing import provider, runs, turns

logger = logging.getLogger(__name__)

OP_CREATE = "create"
OP_FORK = "fork"
OP_HANDOFF = "handoff"
OP_ADOPT = "adopt"
OP_RELEASE = "release"
OP_HAND_BACK = "hand_back"
#: Every ``kirocrew.session.op`` value: a closed vocabulary, never caller text.
CREATE_OPS = frozenset({OP_CREATE, OP_FORK})
MANAGER_OPS = frozenset({OP_HANDOFF, OP_HAND_BACK})
PARENT_OPS = frozenset({OP_ADOPT, OP_RELEASE})
SPAN_MANAGER_CHANGE = "kirocrew.session.manager_change"
SPAN_PARENT_CHANGE = "kirocrew.session.parent_change"

_MAX = 4096
_lock = threading.Lock()
#: New slot key -> ``(trace_id, span_id)`` of the span that created it, taken once.
_first: "OrderedDict[str, tuple[str, str]]" = OrderedDict()


def _next_ordinal(turn: Any) -> int:
    """1 for a turn attempt's first manager or parent change, 2 for its second, etc.

    Kept on the turn's own trace object, so the count lives exactly as long as the
    attempt does: nothing evicts it while the turn runs, and a replayed turn (a new
    object) counts from 1 again.
    """
    with _lock:
        ordinal = int(getattr(turn, "_manager_moves", 0)) + 1
        turn._manager_moves = ordinal
    return ordinal


def _session_key(slot: str) -> str:
    """A slot key in the session-key form its turns carry (the dashboard prefix put back)."""
    return slot if ":" in slot else f"dashboard:{slot}"


def _caller_turn(sessions: Any, caller_session_key: str) -> Any:
    """The turn streaming for *caller_session_key* right now, if traced."""
    if not caller_session_key:
        return None
    from kiro_crew.crew_log.resolve import unit_for_session_key

    return turns.open_turn(unit_for_session_key(sessions, caller_session_key))


def _hashes(state: provider.TraceState, **keys: str) -> dict[str, str]:
    from kiro_crew.tracing import correlation

    if state.hash_key is None:
        return {}
    return {
        f"kirocrew.session.{name}": correlation.keyed_hash(state.hash_key, _session_key(key))
        for name, key in keys.items()
        if key
    }


def created(sessions: Any, caller_session_key: str, target: str, op: str) -> None:
    """Record that the caller's turn opened session *target*. Never raises."""
    state = provider.peek()
    if state is None or not target or op not in CREATE_OPS:
        return
    try:
        from kiro_crew.tracing import correlation, ids

        turn = _caller_turn(sessions, caller_session_key)
        if turn is None:
            return
        span_id = ids.span_id(state.namespace, f"session.{op}", turn.turn_id, turn.attempt, target)
        provider.start_span(
            state,
            f"kirocrew.session.{op}",
            trace_id=turn.trace_id,
            span_id=span_id,
            parent=turn.turn_span,
            attributes={
                **turn.common,
                **correlation.clean(
                    {"kirocrew.session.op": op, **_hashes(state, target_hash=target)}
                ),
            },
        ).end()
        with _lock:
            _first[target] = (turn.trace_id, span_id)
            _first.move_to_end(target)
            while len(_first) > _MAX:
                _first.popitem(last=False)
    except Exception:
        logger.debug("session create trace failed", exc_info=True)


def first_turn_link(slot_key: str) -> tuple[Any, ...]:
    """The link from *slot_key*'s creating span, for its first traced turn; once."""
    if not slot_key or provider.peek() is None:
        return ()
    with _lock:
        found = _first.pop(slot_key, None)
    return (runs.link(*found),) if found else ()


def manager_changed(
    sessions: Any,
    caller_session_key: str,
    op: str,
    target: str,
    previous: str = "",
    manager: str = "",
) -> None:
    """Record a manager move, or a tree-parent move for adopt/release. Never raises.

    *previous* and *manager* name the old and new parent for tree operations.
    """
    state = provider.peek()
    if state is None or not target or (op not in MANAGER_OPS and op not in PARENT_OPS):
        return
    try:
        from kiro_crew.tracing import correlation, ids

        parent_change = op in PARENT_OPS
        span_name = SPAN_PARENT_CHANGE if parent_change else SPAN_MANAGER_CHANGE
        kind = span_name.removeprefix("kirocrew.")
        relation = "parent" if parent_change else "manager"
        attrs = correlation.clean(
            {
                "kirocrew.session.op": op,
                **_hashes(
                    state,
                    **{
                        "target_hash": target,
                        f"previous_{relation}_hash": previous,
                        f"{relation}_hash": manager,
                    },
                ),
            }
        )
        turn = _caller_turn(sessions, caller_session_key)
        if turn is not None:
            # The move's ordinal in its turn: one turn may move a session the same
            # way twice, and the same turn replayed numbers its moves the same way.
            ordinal = _next_ordinal(turn)
            parts: tuple[Any, ...] = (
                kind,
                turn.turn_id,
                turn.attempt,
                ordinal,
                op,
                target,
                previous,
                manager,
            )
            trace_id, parent, common = turn.trace_id, turn.turn_span, turn.common
        else:
            # No turn to sit in: a trace of its own in the target's session. A close
            # hands a session up once, so the move itself is the identity. shortcut: a
            # manager reopened from History that closes again with the same move gets
            # the same id, so a backend keeps one of the two spans; add a per-close
            # stamp from the close record if both must show.
            parts = (kind, op, target, previous, manager)
            trace_id, parent = ids.trace_id(state.namespace, *parts), None
            key = _session_key(target)
            common = correlation.session_attrs(state.hash_key, key, key)
        provider.start_span(
            state,
            span_name,
            trace_id=trace_id,
            span_id=ids.span_id(state.namespace, *parts),
            parent=parent,
            attributes={**common, **attrs},
        ).end()
    except Exception:
        logger.debug("session relationship change trace failed", exc_info=True)


def reset_for_testing() -> None:
    """Forget every pending first-turn link. Test isolation only."""
    with _lock:
        _first.clear()
