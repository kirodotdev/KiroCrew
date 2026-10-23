"""MCP elicitation requests on the single-session ``AcpClient``.

The ask, the answer and the cancel of an elicitation an MCP server raised during
one of this client's tool calls. The twin of
:mod:`kiro_crew.acp.session_handle_elicitation`: same decisions, same wire shape
(:mod:`kiro_crew.acp.elicitation`), reached through this client's own
``_send_response`` instead of the shared runtime's.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method:
the class binds it by name, so it reads and writes the client's own state on the
instance, and a patch of the method on the class or on an instance reaches every
caller.

Every decision this client makes itself and every answer relayed through it leaves
a SEL record (:func:`_audit_elicitation_decision`), as the handle's twin does.

Instance fields written: ``_pending_elicitations``.

Instance fields read: ``_session_id``, ``_session_key``, ``accepts_elicitation``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew import sel as sel_module
from kiro_crew.acp._dispatch import _loggable_request_id
from kiro_crew.acp.client_timeouts import _SEL_AUDIT_TIMEOUT_SECONDS
from kiro_crew.acp.elicitation import (
    ELICITATION_ACCEPT,
    ELICITATION_ACTIONS,
    ELICITATION_CANCEL,
    ELICITATION_DECLINE,
    ElicitationParseError,
    ElicitationRequest,
    elicitation_response,
    parse_elicitation_request,
    validate_form_content,
)
from kiro_crew.acp.types import EVENT_ELICITATION_REQUEST, AcpEvent, JsonRpcMessage
from kiro_crew.executors import subprocess_executor

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's logger: this code logs under the client's name like its owners.
logger = logging.getLogger("kiro_crew.acp.client")

#: The SEL operation name every elicitation decision is recorded under (the same
#: one the session handle and the dashboard use).
ELICITATION_AUDIT_TOOL = "mcp_elicitation"


async def _audit_elicitation_decision(
    self: AcpClient,
    request_id: str | int | None,
    action: str,
    reason: str,
    *,
    tool_call_id: str = "",
    mode: str = "",
) -> None:
    """SEL-audit an elicitation decision made or relayed by this client.

    Off-loop, with the accessor inside the hop (an unwarmed ``sel()``
    initialises on the calling thread), and bounded like
    ``_audit_cancelled_approvals``, so a stuck SEL backend costs the caller at
    most one audit timeout. Best-effort: a failure to write the record must not
    change the decision it records. ``reason`` names who decided (``consumer``
    for a person's answer relayed through :func:`answer_elicitation`, else this
    client's own ground).
    """
    metadata = {
        "reason": reason,
        "tool_call_id": tool_call_id,
        "mode": mode,
        "backend": self.backend,
    }
    rid = request_id if isinstance(request_id, (str, int)) else ""
    session_key = self._session_key or ""

    def _audit() -> None:
        # Resolved through the MODULE at call time so a test's substitute on
        # ``kiro_crew.sel`` is the emitter that sees it.
        sel_module.sel().log_tool_invocation(
            session_key=session_key,
            source="acp",
            tool_name=ELICITATION_AUDIT_TOOL,
            outcome=action,
            request_id=rid,
            metadata=metadata,
        )

    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(subprocess_executor(), _audit),
            timeout=_SEL_AUDIT_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("elicitation: audit of a decision failed", exc_info=True)


async def _handle_elicitation_request(self: AcpClient, msg: JsonRpcMessage) -> AcpEvent | None:
    """Turn an elicitation request into the event a consumer answers, or decline it.

    See ``AcpSessionHandle._handle_elicitation_request`` for the three outcomes;
    they are the same here. The text-only prompt APIs (``send_message``,
    ``_read_prompt_response``) have no consumer to yield to and always take the
    decline, which is why they call :func:`_decline_elicitation` directly.
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
            await self._audit_elicitation_decision(
                msg.id, ELICITATION_DECLINE, "unrenderable_request"
            )
            await self._send_response(msg.id, elicitation_response(ELICITATION_DECLINE))
        return None
    if not self.accepts_elicitation:
        logger.info(
            "elicitation: declining -- this client's consumer cannot put the ask to a "
            "person [mode=%s tool_call=%s session=%s]",
            request.mode,
            _loggable_request_id(request.tool_call_id),
            self._session_id,
        )
        await self._audit_elicitation_decision(
            request.request_id,
            ELICITATION_DECLINE,
            "no_consumer_can_ask",
            tool_call_id=request.tool_call_id,
            mode=request.mode,
        )
        await self._send_response(request.request_id, elicitation_response(ELICITATION_DECLINE))
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


async def _decline_elicitation(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Answer ``decline`` on a path with no person: the text-only prompt APIs."""
    if msg.id is None:
        return
    logger.info(
        "elicitation: declining on a text-only prompt path [method=%s session=%s]",
        msg.method,
        self._session_id,
    )
    await self._audit_elicitation_decision(msg.id, ELICITATION_DECLINE, "text_only_prompt_api")
    await self._send_response(msg.id, elicitation_response(ELICITATION_DECLINE))


async def answer_elicitation(
    self: AcpClient,
    request_id: str | int,
    action: str,
    content: dict[str, Any] | None = None,
) -> bool:
    """Send the person's answer to a pending elicitation.

    Same contract as ``AcpSessionHandle.answer_elicitation``: form content is
    validated first and a bad value leaves the request pending; False when no
    such request is pending.
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
    if action == ELICITATION_ACCEPT and request.is_form:
        payload = elicitation_response(action, validate_form_content(request, content))
    else:
        payload = elicitation_response(action)
    self._pending_elicitations.pop(request_id, None)
    await self._audit_elicitation_decision(
        request_id, action, "consumer", tool_call_id=request.tool_call_id, mode=request.mode
    )
    await self._send_response(request_id, payload)
    return True


async def cancel_pending_elicitations(self: AcpClient) -> int:
    """Answer ``cancel`` to every elicitation still waiting on a person."""
    pending = list(self._pending_elicitations.items())
    self._pending_elicitations.clear()
    for request_id, request in pending:
        await self._audit_elicitation_decision(
            request_id,
            ELICITATION_CANCEL,
            "turn_boundary",
            tool_call_id=request.tool_call_id,
            mode=request.mode,
        )
        try:
            await self._send_response(request_id, elicitation_response(ELICITATION_CANCEL))
        except Exception as exc:  # the process may already be gone
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
    "ElicitationRequest",
    "_audit_elicitation_decision",
    "_decline_elicitation",
    "_handle_elicitation_request",
    "answer_elicitation",
    "cancel_pending_elicitations",
]
