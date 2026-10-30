"""Stop an ``AcpClient`` harness whose state-changing tool call ran without asking.

The unasked-call watch of a harness with an ``ACP_BACKEND_MUST_ASK_TOOL_KINDS`` row:
each open tool call's kind and whether it asked, held per ``toolCallId`` within
``UNASKED_WATCH_LIMIT``, and the tripwire that stops the harness when a must-ask call
reports ``completed`` without a permission request, when such a call's id is too long
to hold, or when the watch is full, each with its SEL record.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_unasked_watch``.

Instance fields read: ``_session_id``, ``_session_key``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew import acp_tool_gate
from kiro_crew import sel as sel_module
from kiro_crew.acp._dispatch_parts.tool_call_provenance import TOOL_CALL_CACHE_MAX_KEY_CHARS
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.agent_sdk.backends import ACP_BACKEND_MUST_ASK_TOOL_KINDS

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


#: How many open tool calls one session's unasked-call watch holds. A call leaves
#: the watch at its terminal update, and only a call that can trip is held. A held
#: call is never dropped to make room, because a dropped ask would later read as no
#: ask and a dropped kind as no kind; one new call past the limit ends the session
#: instead (``_tripwire_unasked_call``). Each held field is bounded too: the key
#: is a ``toolCallId`` of at most ``TOOL_CALL_CACHE_MAX_KEY_CHARS``, the kind one of
#: the harness's own must-ask strings (``_watched_kind``), the ask a bool.
UNASKED_WATCH_LIMIT = 256


def _watched_kind(call: dict, must_ask: frozenset[str]) -> str:
    """The kind the unasked-call watch keeps for a call, or ``""``.

    ``mcp`` for a call carrying the MCP ``_meta`` mark, the frame's own kind when it
    is in the harness's must-ask row, and ``""`` for anything else. Only those values
    decide whether a call can trip, so a kind outside the row is never kept: every
    retained kind is one of the row's bounded strings, never a backend-authored one.
    """
    meta = call.get("_meta")
    if isinstance(meta, dict) and (meta.get("is_mcp_tool_call") is True or "mcp" in meta):
        return "mcp"
    kind = call.get("kind")
    return kind if isinstance(kind, str) and kind in must_ask else ""


def _watch_tool_call(self: AcpClient, call: object, *, asked: bool) -> None:
    """Record a tool call's kind, and whether it asked, for ``_tripwire_unasked_call``.

    Fed by both frames that name a call: a ``tool_call`` / ``tool_call_update``
    (``asked=False``) and a ``session/request_permission`` (``asked=True``). A
    must-ask kind, once stated, is kept, so a later frame cannot relabel the call
    as a read; an ask is never undone by a later frame. A call that is neither
    asked nor of a must-ask kind is not held, because its terminal update cannot
    trip. A new call is held while the watch has room for it plus one: that one
    past ``UNASKED_WATCH_LIMIT`` is the mark on which the next frame's
    ``_tripwire_unasked_call`` stops the session, and an ask, which cannot stop it
    here, may only add that mark. A ``toolCallId`` longer than
    ``TOOL_CALL_CACHE_MAX_KEY_CHARS`` is never held, and is not cut to fit either: a
    cut key could answer for another call. A must-ask call stated with such an id
    stops the harness at that frame instead (``_tripwire_unasked_call``), so a call
    the watch refused is never later judged as one it never saw. Called only for a
    member of ``ACP_BACKEND_MUST_ASK_TOOL_KINDS`` (the callers test it, H13).
    """
    if not isinstance(call, dict):
        return
    call_id = call.get("toolCallId")
    if not isinstance(call_id, str) or not call_id:
        return
    if len(call_id) > TOOL_CALL_CACHE_MAX_KEY_CHARS:
        return
    watch = getattr(self, "_unasked_watch", None)
    if watch is None:
        watch = self._unasked_watch = {}
    must_ask = ACP_BACKEND_MUST_ASK_TOOL_KINDS.get(self.backend, frozenset())
    kind = _watched_kind(call, must_ask)
    entry = watch.get(call_id)
    if entry is None:
        if not asked and kind not in must_ask:
            return
        if len(watch) > UNASKED_WATCH_LIMIT:
            return
        entry = watch[call_id] = {"kind": "", "asked": False}
    if kind == "mcp" or (kind and entry["kind"] != "mcp" and entry["kind"] not in must_ask):
        entry["kind"] = kind
    entry["asked"] = entry["asked"] or asked


async def _tripwire_unasked_call(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Stop a harness whose state-changing tool call completed without asking.

    The routing write and the mode tripwire hold the harness in its asking state;
    neither proves it then asks. A call of a kind in the harness's
    ``ACP_BACKEND_MUST_ASK_TOOL_KINDS`` row that reports ``completed`` with no
    ``session/request_permission`` for its ``toolCallId`` ran with none of Crew's
    controls consulted. It cannot be undone, so it is made the last one: the
    harness is killed and the turn fails, as in :func:`_tripwire_pi_gate`.

    Decided at the terminal update, not at first sight, so an ask that arrives
    after ``in_progress`` still counts. The terminal update consumes the id
    whichever way the call ended, because ids may recur in a session and an old ask
    must not vouch for a new call. ``failed`` never trips: the call did not run. A
    denied call is still an asked call, because how Antigravity reports a rejected
    call is not captured. A call whose kind was never stated, or is not in the row
    (reads, searches), never trips. A new call past ``UNASKED_WATCH_LIMIT`` held
    calls stops the harness too, with its own reason and before any call is
    judged, so a call is never judged on state the watch had no room for. So does
    a frame stating a must-ask kind for a ``toolCallId`` longer than
    ``TOOL_CALL_CACHE_MAX_KEY_CHARS``, whether or not that call asked: the watch never
    holds such an id, so its ``completed`` report could not be judged; neither stop
    writes the over-long id into the log or the audit row. A table lookup first, so
    a harness outside it gains no work (harness-parity H13).
    """
    must_ask = ACP_BACKEND_MUST_ASK_TOOL_KINDS.get(self.backend)
    if must_ask is None:
        return
    params = msg.params if isinstance(msg.params, dict) else {}
    update = params.get("update")
    if not isinstance(update, dict) or update.get("sessionUpdate") not in (
        "tool_call",
        "tool_call_update",
    ):
        return
    self._watch_tool_call(update, asked=False)
    watch = getattr(self, "_unasked_watch", {})
    call_id = update.get("toolCallId")
    label = acp_tool_gate.label_for(self.backend)
    over_long = isinstance(call_id, str) and len(call_id) > TOOL_CALL_CACHE_MAX_KEY_CHARS
    if over_long and _watched_kind(update, must_ask) in must_ask:
        reason, kind, call_id = "unasked_watch_oversized_id", "", ""
        logger.error(
            "%s: a tool call of a kind that must ask has an id longer than %d characters; "
            "the unasked-call watch does not hold such an id, so the harness is being "
            "stopped [session=%s]",
            label,
            TOOL_CALL_CACHE_MAX_KEY_CHARS,
            self._session_id,
        )
        problem = (
            f"{label} sent a tool call whose id is longer than Kiro Crew tracks "
            f"({TOOL_CALL_CACHE_MAX_KEY_CHARS} characters), so it could not tell whether "
            "that call asked for permission; the harness was stopped. Start a new chat."
        )
    elif len(watch) > UNASKED_WATCH_LIMIT:
        reason, kind = "unasked_watch_overflow", ""
        call_id = call_id if isinstance(call_id, str) and not over_long else ""
        logger.error(
            "%s: more than %d tool calls are open at once; the unasked-call watch cannot "
            "hold another without losing a call's state, so the harness is being stopped "
            "[session=%s]",
            label,
            UNASKED_WATCH_LIMIT,
            self._session_id,
        )
        problem = (
            f"{label} had more tool calls open at once than Kiro Crew tracks "
            f"({UNASKED_WATCH_LIMIT}), so it could no longer tell which of them asked "
            "for permission; the harness was stopped. Start a new chat."
        )
    else:
        status = update.get("status")
        if status not in ("completed", "failed"):
            return
        if not isinstance(call_id, str):
            return
        entry = watch.pop(call_id, None)
        if status != "completed" or entry is None or entry["asked"]:
            return
        kind = entry["kind"]
        if kind not in must_ask:
            return
        reason = "unasked_call_tripwire"
        logger.error(
            "%s: %s tool call %s COMPLETED without a permission request; the harness "
            "ran it without Kiro Crew's gate, so it is being stopped [session=%s]",
            label,
            kind,
            call_id,
            self._session_id,
        )
        problem = (
            f"{label} ran {kind} tool call {call_id} without a permission request, so "
            "Kiro Crew's gate never saw it; the harness was stopped. Start a new chat, "
            "and if it recurs check that the harness is the verified release and still "
            "asks before every tool of this kind."
        )
    try:
        sel_module.sel().log_tool_invocation(
            session_key=self._session_key or "",
            source="acp",
            tool_name=call_id,
            tool_kind=kind,
            outcome="denied",
            metadata={
                "reason": reason,
                "backend": self.backend,
                "tool_call_id": call_id,
            },
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.debug("unasked-call tripwire: audit failed", exc_info=True)
    watch.clear()
    await self._kill_process(force=True)
    raise AcpToolGateUnroutable(problem)
