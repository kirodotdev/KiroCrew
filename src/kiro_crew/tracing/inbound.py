"""Join the caller's trace: an inbound W3C ``traceparent`` parents the turn it starts.

A service that calls Kiro Crew over HTTP (``POST /v1/chat/completions``, the
agent webhook) can pass its own ``traceparent`` header. With
``telemetry.accept_traceparent`` on and tracing on, the route calls :func:`begin`
once it has authenticated the caller and is about to start the turn:

- the header is parsed with string methods (``ids.parse_traceparent``); anything
  that is not a valid version ``00`` value is ignored, and the request is never
  refused for it;
- a turn id is minted here, so the turn span's id (``H(turn, turn_id, 1)``) is
  known before the turn runs and the response can carry it as ``traceparent``;
- the turn takes that id (:func:`claim_turn_id`) and joins the caller's trace under
  the caller's span (:func:`join_for`, read where a turn root is bound).

The caller's sampled flag and ``tracestate`` are ignored: this only chooses where
a turn that the gateway's own tracing records is attached. With tracing off
nothing is bound and no header is returned. The value returned is built from this
install's ids and the validated trace id, never an echo of the request.
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from kiro_crew.tracing import ids, provider, runs

logger = logging.getLogger(__name__)

#: ``RunRef.kind`` of the caller's span: names the join, never emitted as a span.
KIND_CALLER = "caller"


@dataclass
class _Inbound:
    turn_id: str
    ref: runs.RunRef
    claimed: bool = False


_CURRENT: contextvars.ContextVar[_Inbound | None] = contextvars.ContextVar(
    "kirocrew_trace_inbound", default=None
)


def begin(headers: Any) -> str:
    """Bind the caller's trace for the turn this task starts; return its ``traceparent``.

    Returns ``""`` (and binds nothing) unless the setting is on, tracing is on and
    exactly one valid header was sent. Never raises.
    """
    try:
        state = provider.peek()
        if state is None or not provider.accepts_traceparent():
            return ""
        sent = headers.getall("traceparent", [])
        if not sent:
            return ""
        parsed = ids.parse_traceparent(sent[0]) if len(sent) == 1 else None
        if parsed is None:
            logger.debug("inbound traceparent ignored: not a valid version 00 value")
            return ""
        turn_id = uuid.uuid4().hex
        ref = runs.RunRef(KIND_CALLER, (turn_id,), parsed[0], parsed[1], started=time.time())
        _CURRENT.set(_Inbound(turn_id, ref))
        return ids.format_traceparent(
            parsed[0], ids.span_id(state.namespace, ids.KIND_TURN, turn_id, 1)
        )
    except Exception:
        logger.debug("inbound traceparent read failed", exc_info=True)
        return ""


def claim_turn_id() -> str:
    """The turn id :func:`begin` minted, once; ``""`` when none is bound or it was taken."""
    inbound = _CURRENT.get()
    if inbound is None or inbound.claimed:
        return ""
    inbound.claimed = True
    return inbound.turn_id


def hand_back(turn_id: str) -> None:
    """Let the turn nested under the one holding *turn_id* take the same id.

    ``/prompts get`` runs the expanded prompt as a second turn in the same task; the
    outer turn returns before it binds a trace root, so the model-producing turn is
    the one the response header names.
    """
    inbound = _CURRENT.get()
    if inbound is not None and inbound.turn_id == turn_id:
        inbound.claimed = False


def join_for(turn_id: str) -> runs.RunRef | None:
    """The caller's span as a join for the turn *turn_id*, when it is the one :func:`begin` named."""
    inbound = _CURRENT.get()
    return inbound.ref if inbound is not None and inbound.turn_id == turn_id else None
