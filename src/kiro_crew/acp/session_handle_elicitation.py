"""MCP elicitation requests on the multiplexed session handle.

The ask, the answer and the cancel of an elicitation an MCP server raised during
one of this session's tool calls (:mod:`kiro_crew.acp.elicitation` owns the wire
shape on both methods).

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle``
method: the class binds it by name, so it reads and writes the session's own state
on the instance, and a patch of the method on the class or on an instance reaches
every caller.

Every decision this handle makes itself (a decline, a cancel) and every answer a
consumer sends through it leaves a SEL record (:func:`_audit_elicitation_decision`),
as every permission decision does: an accept lets a server's gated operation go
ahead, and a refusal is what an operator looks for when a tool "just failed".

Instance fields written: ``_audit_tasks``, ``_pending_elicitations``.

Instance fields read: ``_runtime``, ``_session_id``, ``accepts_elicitation``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.client import _loggable_request_id
from kiro_crew.acp.elicitation import (
    ELICITATION_ACCEPT,
    ELICITATION_ACTIONS,
    ELICITATION_CANCEL,
    ELICITATION_DECLINE,
    ElicitationContentError,
    ElicitationParseError,
    ElicitationRequest,
    elicitation_response,
    parse_elicitation_request,
    validate_form_content,
)
from kiro_crew.acp.types import EVENT_ELICITATION_REQUEST, AcpEvent, JsonRpcMessage

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The session handle's logger, like the other split modules.
logger = logging.getLogger("kiro_crew.acp.session_handle")

#: The SEL operation name every elicitation decision is recorded under, on both
#: drivers and in the dashboard, so one query finds them all.
ELICITATION_AUDIT_TOOL = "mcp_elicitation"


def _audit_elicitation_decision(
    self: AcpSessionHandle,
    request_id: str | int | None,
    action: str,
    reason: str,
    *,
    tool_call_id: str = "",
    mode: str = "",
) -> None:
    """SEL-audit an elicitation decision made or relayed by this handle.

    Off-loop and before the wire write, like :func:`_audit_handle_reject`: the
    audit must land whether or not the answer reaches the engine. ``reason``
    names who decided (``consumer`` for a person's answer relayed through
    :func:`answer_elicitation`, else the handle's own ground).
    """
    rid = request_id if isinstance(request_id, (str, int)) else ""
    session_id = self._session_id

    def _audit() -> None:
        try:
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key=f"acp:{session_id}",
                agent="kirocrew",
                source="acp_session_handle",
                tool_name=ELICITATION_AUDIT_TOOL,
                outcome=action,
                request_id=rid,
                metadata={
                    "reason": reason,
                    "tool_call_id": tool_call_id,
                    "mode": mode,
                },
            )
        except Exception:
            logger.exception("SEL audit for elicitation decision failed")

    task = asyncio.ensure_future(asyncio.to_thread(_audit))
    self._audit_tasks.add(task)
    task.add_done_callback(self._audit_tasks.discard)


async def _handle_elicitation_request(
    self: AcpSessionHandle, msg: JsonRpcMessage
) -> AcpEvent | None:
    """Turn an elicitation request into the event a consumer answers, or decline it.

    Three outcomes, none of which leaves the request unanswered:

    * The request cannot be rendered (:class:`ElicitationParseError`) -- answered
      ``decline`` here. Not a JSON-RPC error: the KAS engine reports an error to
      the MCP server as ``cancel``, and the server should hear that the person
      was not asked rather than that something broke.
    * This session's consumer never said it can put an ask to a person
      (``accepts_elicitation`` false: a cron, a subagent, a channel bridge) --
      answered ``decline`` here, so a consumer that predates elicitation cannot
      leave the tool call hanging on an event kind it does not read.
    * Otherwise the request is recorded as pending and the event returned for
      the dispatch loop to yield; :func:`answer_elicitation` sends the answer.

    Trust grants are not consulted and never will be: this is the SERVER's
    human-in-the-loop requirement, not Crew's tool gate.
    """
    try:
        request = parse_elicitation_request(msg)
    except ElicitationParseError as exc:
        logger.warning(
            "elicitation: declining a request this client cannot render (%s) "
            "[method=%s session=%s]",
            exc,
            msg.method,
            self._session_id,
        )
        if msg.id is not None:
            self._audit_elicitation_decision(msg.id, ELICITATION_DECLINE, "unrenderable_request")
            await self._runtime.send_response(msg.id, elicitation_response(ELICITATION_DECLINE))
        return None
    if not self.accepts_elicitation:
        logger.info(
            "elicitation: declining -- this session's consumer cannot put the ask to a "
            "person [mode=%s tool_call=%s session=%s]",
            request.mode,
            _loggable_request_id(request.tool_call_id),
            self._session_id,
        )
        self._audit_elicitation_decision(
            request.request_id,
            ELICITATION_DECLINE,
            "no_consumer_can_ask",
            tool_call_id=request.tool_call_id,
            mode=request.mode,
        )
        await self._runtime.send_response(
            request.request_id, elicitation_response(ELICITATION_DECLINE)
        )
        return None
    self._pending_elicitations[request.request_id] = request
    logger.info(
        "elicitation: asking the person [mode=%s tool_call=%s req=%s session=%s]",
        request.mode,
        _loggable_request_id(request.tool_call_id),
        _loggable_request_id(request.request_id),
        self._session_id,
    )
    return AcpEvent(
        kind=EVENT_ELICITATION_REQUEST,
        request_id=request.request_id,
        tool_call_id=request.tool_call_id,
        title=request.message,
        elicitation=request.as_dict(),
    )


async def answer_elicitation(
    self: AcpSessionHandle,
    request_id: str | int,
    action: str,
    content: dict[str, Any] | None = None,
) -> bool:
    """Send the person's answer to a pending elicitation.

    ``content`` is read only for an accepted form, and is checked against the
    requested schema first: a value that does not fit raises
    :class:`ElicitationContentError` and the request STAYS pending, so the card
    can show the problem and the person can correct it. Returns False when no
    such request is pending (already answered, or never this session's).
    """
    if action not in ELICITATION_ACTIONS:
        raise ValueError(f"unknown elicitation action {action!r}")
    request = self._pending_elicitations.get(request_id)
    if request is None:
        logger.warning(
            "answer_elicitation: no pending elicitation req=%s [session=%s]",
            _loggable_request_id(request_id),
            self._session_id,
        )
        return False
    payload = _answer_payload(request, action, content)
    # Popped only once the payload is built: a content error above leaves the
    # request pending for a corrected answer.
    self._pending_elicitations.pop(request_id, None)
    self._audit_elicitation_decision(
        request_id, action, "consumer", tool_call_id=request.tool_call_id, mode=request.mode
    )
    # Answered: the turn stops waiting on a human here (see approve_tool).
    self._end_human_wait()
    await self._runtime.send_response(request_id, payload)
    return True


def _answer_payload(
    request: ElicitationRequest, action: str, content: dict[str, Any] | None
) -> dict[str, Any]:
    """The response for *action*, with form content validated when it rides along."""
    if action == ELICITATION_ACCEPT and request.is_form:
        return elicitation_response(action, validate_form_content(request, content))
    return elicitation_response(action)


async def cancel_pending_elicitations(self: AcpSessionHandle) -> int:
    """Answer ``cancel`` to every elicitation still waiting on a person.

    Run when the turn that raised them is over (per-turn reset) and at teardown.
    A pending ask outliving its turn would hold the tool call open on the engine
    for an answer nothing can give once its card is gone. Returns how many were
    cancelled.
    """
    pending = list(self._pending_elicitations.items())
    self._pending_elicitations.clear()
    for request_id, request in pending:
        self._audit_elicitation_decision(
            request_id,
            ELICITATION_CANCEL,
            "turn_boundary",
            tool_call_id=request.tool_call_id,
            mode=request.mode,
        )
        try:
            await self._runtime.send_response(request_id, elicitation_response(ELICITATION_CANCEL))
        except Exception as exc:  # the runtime may already be gone at teardown
            logger.debug(
                "cancel_pending_elicitations: req=%s not answered (%s)",
                _loggable_request_id(request_id),
                exc,
            )
    if pending:
        logger.info(
            "elicitation: cancelled %d unanswered request(s) [session=%s]",
            len(pending),
            self._session_id,
        )
    return len(pending)


__all__ = [
    "ELICITATION_AUDIT_TOOL",
    "ElicitationContentError",
    "_audit_elicitation_decision",
    "_handle_elicitation_request",
    "answer_elicitation",
    "cancel_pending_elicitations",
]
