"""Steps inside a turn that a Crew call site reports (tracing RFC §5.3).

The turn's own stream gives the derived model-call steps and in-turn compaction
(``turns.TurnTrace.observe``). The rest happen in Crew, so the code that already
handles each one calls in here:

- **Approvals.** :func:`approval_opened` where a prompt is put to someone and
  :func:`approval_closed` where its answer lands: one ``kirocrew.approval`` span
  under the asking turn, with ``kirocrew.approval.wait_ms``,
  ``kirocrew.approval.decided_by`` (``person``, ``policy`` when the host decided
  without an answer, ``timeout``) and ``kirocrew.approval.outcome``. A spawn
  approval sits under the parent's spawn tool span instead. A tool the harness
  auto-approves never reaches Crew, so it has no approval span.
- **Steers.** :func:`steered`: a zero-length ``kirocrew.steer`` in the turn it went
  into, carrying ``kirocrew.steer.mode`` and never the text.
Best-effort: a failure here is logged and the turn goes on. Off, each call
returns at the switch read.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from kiro_crew.tracing import provider, runs, turns

logger = logging.getLogger(__name__)

SPAN_APPROVAL = "kirocrew.approval"
SPAN_STEER = "kirocrew.steer"

DECIDED_PERSON = "person"
DECIDED_POLICY = "policy"
DECIDED_TIMEOUT = "timeout"
STEER_INTERRUPT = "interrupt"
STEER_FOLLOW_UP = "follow_up"
_STEER_MODES = frozenset({STEER_INTERRUPT, STEER_FOLLOW_UP})

#: Spelled out rather than imported from ``constants`` (pinned by ``test_tracing_steps``).
_CAUSE_TIMEOUT = "approval_timeout"


@dataclass
class Approval:
    """One open approval span: release it with :func:`approval_closed`."""

    span: Any
    started: float


def decided_by(*, host: bool = False, cause: str = "") -> str:
    """Who decided: the window ran out, the host decided alone, or a person answered."""
    if cause == _CAUSE_TIMEOUT:
        return DECIDED_TIMEOUT
    return DECIDED_POLICY if host or cause else DECIDED_PERSON


@dataclass(frozen=True)
class Asked:
    """When a prompt became answerable: (monotonic, epoch ns), from :func:`asked_now`."""

    monotonic: float
    epoch_ns: int


def asked_now() -> Asked:
    """The moment a prompt can first be answered, for an :func:`approval_opened` made later.

    For a call site that publishes the prompt and then awaits something
    cancellable before it can open and close the span as a pair.
    """
    return Asked(time.monotonic(), time.time_ns())


def approval_opened(
    approval_id: str,
    *,
    session_key: str = "",
    tool_call_id: str = "",
    subagent_id: str = "",
    spawn: bool = False,
    since: Asked | None = None,
) -> Approval | None:
    """Open the span of a prompt put to someone now, or at *since*. Never raises.

    It hangs under the turn the root bound in this task streams, else the one
    open for *session_key*; under sub-agent run *subagent_id*'s open turn; or,
    for its *spawn* approval, under the spawn tool span of the parent turn that
    asked. ``None`` when tracing is off or that turn is not traced.
    """
    state = provider.peek()
    if state is None or not approval_id:
        return None
    try:
        from kiro_crew.tracing import correlation, ids

        if spawn:
            from kiro_crew.tracing import subagents

            origin = subagents._origin_for(subagent_id)
            if origin is None:
                return None
            trace_id, scope = origin.trace_id, subagent_id
            parent = runs.parent_of(origin.trace_id, origin.spawn_span_id)
            common: dict[str, Any] = {
                "kirocrew.subagent_id": subagent_id,
                **correlation.session_attrs(
                    state.hash_key, origin.parent_session_key, origin.root_session_key
                ),
            }
        else:
            turn = (
                turns.open_turn_of_subagent(subagent_id)
                if subagent_id
                else turns.current_turn(session_key)
            )
            if turn is None:
                return None
            # The turn's tracer, which a settings write cannot swap under it.
            state, trace_id, scope = turn._state, turn.trace_id, turn.acp_session_id
            parent, common = turn.turn_span, turn.common
        asked = since or asked_now()
        span = provider.start_span(
            state,
            SPAN_APPROVAL,
            trace_id=trace_id,
            span_id=ids.span_id(
                state.namespace, ids.KIND_APPROVAL, scope, tool_call_id or approval_id
            ),
            parent=parent,
            attributes={**common, **correlation.clean({"kirocrew.tool_call_id": tool_call_id})},
            start_time=asked.epoch_ns,
        )
        return Approval(span, asked.monotonic)
    except Exception:
        logger.debug("approval trace open failed", exc_info=True)
        return None


def approval_closed(approval: Approval | None, *, approved: bool, by: str) -> None:
    """End *approval*'s span with its wait, who decided (*by*) and the outcome."""
    if approval is None:
        return
    try:
        from kiro_crew.tracing import correlation

        approval.span.set_attributes(
            correlation.clean(
                {
                    "kirocrew.approval.wait_ms": int((time.monotonic() - approval.started) * 1000),
                    "kirocrew.approval.decided_by": by,
                    "kirocrew.approval.outcome": "approved" if approved else "rejected",
                }
            )
        )
        approval.span.end(end_time=time.time_ns())  # the same clock as its start
    except Exception:
        logger.debug("approval trace close failed", exc_info=True)


def steered(mode: str, *, source: object = None, subagent_id: str = "") -> None:
    """A steer of *mode* went into the turn open on *source*'s ACP session, or run *subagent_id*'s."""
    if mode not in _STEER_MODES or provider.peek() is None:
        return
    try:
        from kiro_crew.tracing import ids

        turn = (
            turns.open_turn_of_subagent(subagent_id) if subagent_id else turns.open_turn_for(source)
        )
        if turn is not None:
            span = turn.numbered(SPAN_STEER, ids.KIND_STEER, {"kirocrew.steer.mode": mode})[0]
            span.end(end_time=span.start_time)  # a point: when it went in
    except Exception:
        logger.debug("steer trace failed", exc_info=True)
