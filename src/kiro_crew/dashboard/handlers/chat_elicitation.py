"""``POST /api/chat/slots/{slot}/elicitation`` -- a person answers an MCP elicitation.

The card the chat runner rendered (``chat_turn/elicitation_prompt.py``) is
answered here. The body names the request (``request_id``), the ``action``
(``accept`` / ``decline`` / ``cancel``) and, for an accepted form, the
``content`` -- which is validated against the fields the server asked for BEFORE
the pending future is resolved, so a value the schema refuses comes back as a
400 the card can show, and the ask stays open for a corrected answer.

Only the dashboard owner's own browser may answer. An elicitation is the MCP
server's statement that a PERSON must decide; an agent on the internal secret or
an app token answering it would be exactly the automation the server asked to
exclude, so both are refused outright rather than judged by a grant.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew.agent_sdk.elicitation import (
    ELICITATION_ACCEPT,
    ELICITATION_ACTIONS,
    ELICITATION_MODE_URL,
    PROPERTIES_MAX,
    STRING_VALUE_MAX_CHARS,
    ElicitationContentError,
    ElicitationRequest,
    validate_form_content,
)
from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.slot_ownership import slot_not_found
from kiro_crew.dashboard.state import DashboardState, row_mid

logger = logging.getLogger(__name__)

#: The row role the chat runner appends for an elicitation card.
ELICITATION_ROW_ROLE = "elicitation"

#: The answer body's ceiling, sized from what the parser admits rather than the
#: shared 64 KiB default: every field a form may carry, each at the longest
#: string the schema allows, in four-byte UTF-8, plus JSON quoting, the field
#: names and the two ids. A form the card drew must be answerable; a body past
#: this is not an answer to any form this client renders.
ELICITATION_BODY_MAX_BYTES = PROPERTIES_MAX * STRING_VALUE_MAX_CHARS * 4 + 64 * 1024


def _bad_request(code: str, error: str) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=400)


def _elicitation_row(
    slot: Any, request_id: str, request_mid: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The slot's elicitation row carrying *request_id* AND stamped *request_mid*,
    with its parsed meta: the row the card that posted was drawn from."""
    for message in reversed(slot.messages):
        if message.get("role") != ELICITATION_ROW_ROLE or row_mid(message) != request_mid:
            continue
        try:
            meta = json.loads(message.get("cls") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(meta, dict) and meta.get("request_id") == request_id:
            return message, meta
    return None


async def api_chat_slot_elicitation(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/elicitation -- resolve a pending MCP elicitation."""
    state: DashboardState = request.app["state"]
    # The dashboard owner's browser session carries a PRESENT, empty ``app``
    # claim; an internal-secret caller carries ``internal_auth`` instead and an
    # app token carries its name. Only the first is a person at the card.
    if request.get("internal_auth") is True or request.get("app", None) != "":
        return web.json_response(
            {"error": "only the dashboard user can answer an elicitation", "code": "forbidden"},
            status=403,
        )
    # An empty app claim is necessary, not sufficient: a non-owner dashboard
    # session (a Telegram, Teams or Webex allowlist link) carries it too, and an
    # answer here is the owner's consent to the server's operation.
    denied = await require_owner_dashboard_request(request, "chat_slot_elicitation")
    if denied is not None:
        return denied
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    body, body_err = await read_bounded_json(request, max_bytes=ELICITATION_BODY_MAX_BYTES)
    if body_err is not None:
        return body_err
    assert body is not None
    request_id = body.get("request_id")
    request_mid = body.get("request_mid")
    action = body.get("action")
    if not isinstance(request_id, str) or not request_id:
        return _bad_request("invalid_request_id", "request_id is required")
    if not isinstance(request_mid, str) or not request_mid:
        return _bad_request("invalid_request_mid", "request_mid is required")
    if action not in ELICITATION_ACTIONS:
        return _bad_request("invalid_action", "action must be accept, decline or cancel")
    # The answer binds to the row the person READ, not to whichever request
    # now holds the id: an ACP request id is connection-scoped and a backend
    # may reuse one after the earlier ask expired, so a delayed POST from the
    # old card must not accept the new question. ``approval_instance`` is the
    # same instance check the permission card's native approve runs.
    fut = slot._approval_futures.get(request_id)
    found = _elicitation_row(slot, request_id, request_mid)
    if (
        fut is None
        or fut.done()
        or found is None
        or slot.approval_instance(request_id, found[0]) != request_mid
    ):
        return web.json_response(
            {"error": "no pending elicitation", "code": "elicitation_not_pending"}, status=404
        )
    row, meta = found
    answer: dict[str, Any] | None = None
    if action == ELICITATION_ACCEPT and meta.get("mode") != ELICITATION_MODE_URL:
        try:
            answer = validate_form_content(ElicitationRequest.from_dict(meta), body.get("content"))
        except ElicitationContentError as exc:
            return _bad_request("invalid_content", str(exc))
        # The runner reads the validated answer back off the row once the
        # future resolves; the row is the one place both sides can see.
        meta["answer"] = answer
        row["cls"] = json.dumps(meta)
    logger.info(
        "elicitation answered from the dashboard [slot=%s action=%s req=%s]",
        name,
        action,
        request_id,
    )
    fut.set_result(action)
    return web.json_response({"ok": True, "action": action})
