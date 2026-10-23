"""An MCP server's elicitation a dashboard turn puts to its user: the card, the wait
and the answer sent back.

The server, not Kiro Crew, declared that a person must confirm or fill something
in before the tool call it gates may go on. So no trust tier, hook or grant
answers here: a person does, or the window runs out and the ask is cancelled.
The row's ``request_id`` is registered as a pending approval on the slot (the
same registry the permission card uses), so the Board's Blocked lane, Continue's
409 and a Stop all treat it as the open question it is; the answer arrives
through ``POST /api/chat/slots/{slot}/elicitation``, which validates a form's
content against the requested fields before it resolves the future. Every other
resolver of that registry speaks the permission vocabulary, and none of those
words can mean ``accept`` here (:func:`read_elicitation_answer`).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from kiro_crew.agent_sdk.elicitation import (
    ELICITATION_ACCEPT,
    ELICITATION_CANCEL,
    ELICITATION_DECLINE,
    ElicitationContentError,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
        tool_approval_timeout_secs,
    )
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn

#: The permission registry's own refusal outcomes: a Stop or a reject button is a
#: person saying no to whatever was pending, an elicitation included.
_FUTURE_DECLINES = frozenset({"rejected", "rejected_once"})

#: The field keys a person reads and nothing validates: redacted for display.
_DISPLAY_KEYS = ("title", "description")
#: The field keys the answer is validated against: a credential in one of these
#: cannot be rewritten (the wire answer would then fail the schema), so a
#: request carrying one is declined instead of shown. The NAME is one of them:
#: it is the key the answer is sent under, and the label the card falls back to
#: when the server gave no title.
_VALUE_KEYS = ("name", "default")


def read_elicitation_answer(outcome: str, meta: dict[str, Any]) -> tuple[str, dict | None]:
    """The elicitation action and content a resolved future's *outcome* stands for.

    Only the elicitation endpoint resolves the future with ``accept``, after it
    validated the person's answer and left it on the row (``meta["answer"]``).
    Every other resolver speaks the permission registry's vocabulary and is
    mapped: a rejection declines; an approval -- the generic approve route, the
    trust/yolo sweep -- CANCELS, URL ask or form, because it is not a person
    answering this question, and the server must never hear ``accept`` for a
    confirmation nobody gave.
    """
    if outcome == ELICITATION_ACCEPT:
        answer = meta.get("answer")
        return ELICITATION_ACCEPT, answer if isinstance(answer, dict) else None
    if outcome == ELICITATION_DECLINE or outcome in _FUTURE_DECLINES:
        return ELICITATION_DECLINE, None
    return ELICITATION_CANCEL, None


def _redact_display(text: Any) -> str:
    safe, _ = redact_exfiltration_urls(str(text))
    safe, _ = redact_credentials(safe)
    return safe


def _redact_fields_for_display(fields: Any) -> tuple[list[dict[str, Any]], bool]:
    """Fields with their display text redacted, and whether a VALUE was tainted.

    Titles, descriptions and option labels are read by the person and judged by
    nothing, so they take the same redaction every backend-authored title gets.
    Option values and defaults feed the answer the server validates: if redaction
    would change one, the request is reported tainted and the caller declines it
    rather than showing a rewritten value the schema would refuse.
    """
    out: list[dict[str, Any]] = []
    tainted = False
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, dict):
            continue
        safe = dict(field)
        for key in _DISPLAY_KEYS:
            if isinstance(safe.get(key), str):
                safe[key] = _redact_display(safe[key])
        for key in _VALUE_KEYS:
            value = safe.get(key)
            if isinstance(value, str) and _redact_display(value) != value:
                tainted = True
        options = safe.get("options")
        if isinstance(options, list):
            safe_options = []
            for option in options:
                if not isinstance(option, dict):
                    continue
                value = option.get("value")
                if isinstance(value, str) and _redact_display(value) != value:
                    tainted = True
                safe_options.append({**option, "label": _redact_display(option.get("label", ""))})
            safe["options"] = safe_options
        out.append(safe)
    return out, tainted


async def _on_elicitation_request(turn: _ChatTurn, event) -> None:
    """Put the elicitation to the user: the card, the wait and what the answer does."""
    client = turn.client
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    request_id = str(event.request_id)
    payload = dict(event.elicitation or {})
    # The server's opaque URL-ask token is wire detail the card never reads and
    # the answer route never sends back (the driver answers by request id). It
    # is also the one server-authored string no display redaction touches, so
    # it does not travel to the dashboard at all.
    payload.pop("elicitation_id", None)
    # Everything the person reads is server-authored: the message, the field
    # labels, the option labels and the URL all take display redaction.
    safe_message = _redact_display(payload.get("message") or event.title or "")
    fields, tainted = _redact_fields_for_display(payload.get("fields"))
    if tainted:
        # A value the answer is validated against looks like a credential or an
        # exfiltration URL. It cannot be shown redacted and still be sent back
        # verbatim, so the server hears that the person was not asked.
        logger.warning(
            "MCP elicitation %r carries a value display redaction would change; declining",
            request_id,
        )
        _audit_runner_decline(turn, event, "tainted_value")
        await client.answer_elicitation(event.request_id, ELICITATION_DECLINE, None)
        return
    meta: dict[str, Any] = {
        **payload,
        "request_id": request_id,
        "tool_call_id": event.tool_call_id or "",
        "message": safe_message,
    }
    # WHO is asking: the server and tool of the call this ask belongs to, read
    # from the trusted identity the turn recorded for that call (the same
    # ``_meta.kiro`` pair the permission row carries), never from the server's
    # own text. A URL ask sends the person off the app, and a card that cannot
    # name the sender is one the person rightly refuses to follow.
    identity = turn._seen_tool_identity.get(event.tool_call_id or "")
    if identity:
        server_name, tool_name = identity
        if server_name:
            meta["mcp_server"] = _redact_display(server_name)
        if tool_name:
            meta["tool_name"] = _redact_display(tool_name)
    if "fields" in payload:
        meta["fields"] = fields
    if isinstance(meta.get("url"), str):
        safe_url = _redact_display(meta["url"])
        if safe_url != meta["url"]:
            # A URL the redactor rewrites is one the person must not be sent to.
            logger.warning(
                "MCP elicitation %r names a URL display redaction would change; declining",
                request_id,
            )
            _audit_runner_decline(turn, event, "tainted_url")
            await client.answer_elicitation(event.request_id, ELICITATION_DECLINE, None)
            return
    # Pre-declined when there is no window to wait in, like a permission row.
    if tool_approval_timeout_secs() <= 0:
        meta["resolved"] = ELICITATION_CANCEL
    row = slot.append("elicitation", safe_message, json.dumps(meta))
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()
    slot.register_approval(request_id, fut, row)
    # After registering, so the slots snapshot already reads pending_approval.
    state.push_slots_update()
    window = min(state.approval_timeout_for(slot), tool_approval_timeout_secs())
    action = ELICITATION_CANCEL
    answer: dict | None = None
    # The exit path, tracked where it happens: ``wait_for`` CANCELS the future on
    # timeout and a Stop cancels the turn, so by the audit ``fut.done()`` is true
    # on every path and says nothing about who decided.
    reason = "no_turn_budget"
    try:
        if fut.done():
            action, answer = read_elicitation_answer(fut.result(), meta)
            reason = "resolved"
        elif window <= 0:
            logger.warning(
                "Cancelling MCP elicitation %r without waiting: no turn budget left",
                request_id,
            )
        else:
            reason = "turn_cancelled"
            outcome = await asyncio.wait_for(fut, timeout=window)
            # The endpoint wrote the validated answer onto the row; re-read it.
            try:
                meta = json.loads(row.get("cls") or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            action, answer = read_elicitation_answer(outcome, meta)
            reason = "resolved"
    except asyncio.TimeoutError:
        reason = "window_expired"
        logger.warning("MCP elicitation %r unanswered for %.0fs; cancelling it", request_id, window)
    finally:
        owns = slot.unregister_approval(request_id, fut)
        # A Stop rejected every pending future on the slot and recorded their ids
        # in ``_approval_stopped`` so the permission path can tell the host's
        # decision from a person's. Read and CLEAR this ask's entry here, as that
        # path does for its own ids: an ACP request id is small and reused after
        # a respawn, so a stale entry would later brand a person's answer to a
        # permission as the host's and leave an auto-nudge approval hold in place.
        if request_id in slot._approval_stopped:
            slot._approval_stopped.discard(request_id)
            # The future DID resolve, but the host resolved it: not a person.
            reason = "stopped"
            # A Stop ends the turn; nobody chose to refuse the server. That is
            # MCP's ``cancel`` (dismissed without a choice), the answer every
            # other turn boundary sends, never a ``decline``.
            action, answer = ELICITATION_CANCEL, None
        try:
            await client.answer_elicitation(event.request_id, action, answer)
        except ElicitationContentError:
            # The endpoint validated before resolving, so this is a row someone
            # else wrote: decline rather than send content the schema refuses.
            logger.warning("MCP elicitation %r answer does not fit its form; declining", request_id)
            action, answer = ELICITATION_DECLINE, None
            try:
                await client.answer_elicitation(event.request_id, action, None)
            except Exception:
                logger.warning("Failed to answer MCP elicitation %r", request_id, exc_info=True)
        except Exception:
            logger.warning("Failed to answer MCP elicitation %r", request_id, exc_info=True)
        # Every elicitation decision leaves a SEL record, as every permission
        # decision does: an accept lets a server's gated operation go ahead.
        # Audit here, at the one convergence point of every exit, with the
        # action actually sent. Guarded so a failed audit cannot skip the row
        # stamp and the resolution frame below, which would leave the card
        # actionable for an ask that is over.
        try:
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name=ELICITATION_AUDIT_TOOL,
                outcome=action,
                request_id=event.request_id,
                metadata={
                    "tool_call_id": event.tool_call_id or "",
                    "mode": str(meta.get("mode") or ""),
                    "reason": reason,
                    "answered_fields": sorted(answer) if isinstance(answer, dict) else [],
                },
            )
        except Exception:
            logger.warning("SEL audit of MCP elicitation %r failed", request_id, exc_info=True)
        if owns:
            # Stamp THIS row, not the oldest row carrying the id: an ACP request
            # id is connection-scoped and a backend may reuse it, so a lookup by
            # id alone would mark an earlier card and leave this one pending.
            mark_elicitation_row_resolved(row, action, answer)
            slot._dirty = True
            frame: dict[str, Any] = {"id": request_id, "action": action, "slot": slot.key}
            mid = (row.get("meta") or {}).get("mid")
            if isinstance(mid, str) and mid:
                frame["mid"] = mid
            if action == ELICITATION_ACCEPT and isinstance(answer, dict):
                # The card shows what was sent without waiting for a transcript
                # reload. Display-redacted like the row's own text: the values
                # are the person's, but the frame goes to every dashboard tab.
                frame["answer"] = {
                    k: (_redact_display(v) if isinstance(v, str) else v) for k, v in answer.items()
                }
            state.broadcast_ws("elicitation_resolved", frame)
            state.push_slots_update()


#: The SEL operation name every elicitation decision is recorded under, shared
#: with both ACP drivers so one query finds them all.
ELICITATION_AUDIT_TOOL = "mcp_elicitation"


def _audit_runner_decline(turn: _ChatTurn, event, reason: str) -> None:
    """SEL record for a decline this runner made before any card existed."""
    sel().log_tool_invocation(
        session_key=turn.session_key,
        agent=turn.slot.agent or "kirocrew",
        source="dashboard",
        tool_name=ELICITATION_AUDIT_TOOL,
        outcome=ELICITATION_DECLINE,
        request_id=event.request_id,
        metadata={
            "tool_call_id": event.tool_call_id or "",
            "mode": str((event.elicitation or {}).get("mode") or ""),
            "reason": reason,
        },
    )


def mark_elicitation_row_resolved(row: dict[str, Any], action: str, answer: dict | None) -> None:
    """Write the decision into *row*'s ``cls`` JSON, in place."""
    try:
        meta = json.loads(row.get("cls") or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta["resolved"] = action
    if answer is not None:
        meta["answer"] = answer
    row["cls"] = json.dumps(meta)
