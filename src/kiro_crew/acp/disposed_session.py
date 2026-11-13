"""A session whose backend the harness disposed inside a process that stays up.

A member of :data:`~kiro_crew.agent_sdk.backends.ACP_BACKENDS_RELOAD_DISPOSED_SESSION`
can stop serving one session while its process keeps running, and from then on it
answers every ``session/prompt`` on that id with the same error. ``AcpClient``'s prompt
loop asks this module about each prompt error and each approval request:

* :func:`settle_disposed_error` marks the client stalled (``_stdin_stalled``), so its
  next ``ensure_ready`` replaces the process the way it replaces any stalled child,
  and keeps the session id as ``_resume_session_id`` so the replacement
  ``session/load``s it. It ends a cancelled turn as ``cancelled`` and fails any other
  turn once with a plain message.
* :func:`answer_late_permission` answers ``cancelled`` to an approval request that
  arrives after Crew sent ``session/cancel``: the harness holds its turn open on that
  answer, so the cancel cannot finish while a person is still being asked.

Only the exact texts a harness writes when it disposes a backend count. Any other error
on the same code is an ordinary turn failure (a model error, a refused tool), and
reading it as a disposed backend would replace a healthy process.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import OUTCOME_CANCELLED, STOP_REASON_CANCELLED, JsonRpcMessage
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_STRANDS,
    ACP_BACKENDS_RELOAD_DISPOSED_SESSION,
)

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

__all__ = [
    "DISPOSED_SESSION_DETAILS",
    "DISPOSED_SESSION_TEXT",
    "answer_late_permission",
    "is_disposed_session_error",
    "settle_disposed_error",
]

logger = logging.getLogger(__name__)

_JSONRPC_INTERNAL_ERROR = -32603

#: Per member, the ``data.details`` texts that mean the session's backend is gone. An
#: entry ending in ``(`` is matched as the leading part, the rest whole. Strands CLI
#: 0.2.3 (``tui/project/python.js``): the cancel that did not stop within 3s, the close
#: of a disposed backend, a worker that exited, a worker that wrote an unreadable line.
#: Each one sets the backend's sticky failure, which every later prompt on the id
#: answers again.
DISPOSED_SESSION_DETAILS: dict[str, tuple[str, ...]] = {
    ACP_BACKEND_STRANDS: (
        "The Python agent did not stop after cancellation. Reload it to continue.",
        "Python agent closed.",
        "Python agent exited (",
        "The Python agent returned an invalid message.",
    ),
}

#: What a turn that was not cancelled fails with. The chat is reloaded before the next
#: message goes out, so sending it again is all the person has to do.
DISPOSED_SESSION_TEXT = (
    "The agent stopped working on this chat before it answered. Send your message "
    "again: Kiro Crew restarts the agent and reopens this chat first."
)


def is_disposed_session_error(backend: str, error: object) -> bool:
    """Whether *error*, a JSON-RPC error object from *backend*, says the session's backend is gone.

    The callers in ``_prompt_loop`` test the set membership first, so a harness
    outside it never reaches this module; the check here is the same answer again.
    """
    if backend not in ACP_BACKENDS_RELOAD_DISPOSED_SESSION or not isinstance(error, dict):
        return False
    if error.get("code") != _JSONRPC_INTERNAL_ERROR:
        return False
    data = error.get("data")
    details = data.get("details") if isinstance(data, dict) else None
    if not isinstance(details, str):
        return False
    for known in DISPOSED_SESSION_DETAILS.get(backend, ()):
        if details == known or (known.endswith("(") and details.startswith(known)):
            return True
    return False


def settle_disposed_error(client: AcpClient, msg: JsonRpcMessage) -> JsonRpcMessage | None:
    """Answer a prompt error that says the session's backend is gone, or ``None``.

    Marks *client* stalled, so its next ``ensure_ready`` kills this process and loads
    the same session id in a fresh one: the stalled-child replacement already does
    that, so nothing on the start path changes for this. A turn Crew cancelled gets back a ``cancelled``
    completion to end on, which is what the person asked for. Any other turn raises
    :exc:`AcpError` once: nothing it was doing can be trusted to have finished.
    """
    if not is_disposed_session_error(client.backend, msg.error):
        return None
    session_id = client._session_id
    client._stdin_stalled = True
    if session_id:
        client._resume_session_id = session_id
    logger.info(
        "%s disposed the backend of session %s; reloading it before the next prompt",
        client.backend,
        session_id,
    )
    if client._cancelled:
        return JsonRpcMessage(id=msg.id, result={"stopReason": STOP_REASON_CANCELLED})
    raise AcpError(DISPOSED_SESSION_TEXT, transient=False, user_worded=True)


async def answer_late_permission(client: AcpClient, msg: JsonRpcMessage) -> bool:
    """Answer ``cancelled`` to an approval request read after Crew cancelled the turn.

    ``cancel_session`` answers the requests open when the cancel went out; this is the
    one that arrived after it. The denial is audited like theirs. ``True`` when the
    request was answered here and must not reach a person.
    """
    if not client._cancelled or msg.id is None:
        return False
    await client._audit_cancelled_approvals([msg.id], cause="asked_after_cancel")
    try:
        await client._send_response(msg.id, {"outcome": {"outcome": OUTCOME_CANCELLED}})
    except Exception:
        logger.debug("answering an approval asked after the cancel failed", exc_info=True)
    return True
