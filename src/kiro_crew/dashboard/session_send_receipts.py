"""What a ``session_send`` delivered, recorded as data for the sender's own row.

The sender's transcript draws a ``session_send`` call that delivered as the
outgoing session-message card, naming the slot the gateway RESOLVED (a send
addressed by title must link to the real key). Both facts -- that it delivered,
and where -- are the gateway's, so they come from the gateway as typed fields
rather than out of the tool's human-readable result, which a rewording or a
backend's reshaping of the result body would silently break.

It is the session directives' out-of-band path again, on the same mechanism
(``dashboard.directive_queue``: a :class:`~kiro_crew.dashboard.directive_queue.Store`
of its own, records selected by ``session_directive.call_input_digest``):

* ``POST /api/session-control/send`` (``handlers.session_control``) delivers,
  then :func:`park` records ``{outcome, target}`` for the calling session under
  the digest of the ``target`` and ``message`` it was asked to send. Only a
  request the ``session_send`` tool makes asks for one (:data:`RECEIPT_FIELD`): a
  dispatch seed or an app's send has no tool row to claim it.
* The caller's turn digests the ``target`` and ``message`` of the
  ``session_send`` call it saw the model make (:func:`call_selector` over the
  ACP ``rawInput``), and on that call's terminal result :func:`stamp_result`
  claims the receipt and stamps it on the call's own tool rows as
  ``meta.sessionSend``.

The store is its own rather than the directives' because the directive path
retires a session's whole bucket when the marker applies a directive
(``directive_queue.discard``) and reports every record a directive claim passes
over as a miss: a receipt there would be deleted by an unrelated directive and
logged as a lost one.

Two identical sends in flight at once can each take the other's receipt; both
name the same resolved target, so only the outcome word can swap. A receipt only
ever describes a delivery the gateway made for that session, and only a call
with the very same arguments in that session's turn picks it up; a receipt
nobody claims ages out. A row with no receipt (an older row, a refused send, a
backend that reports no call input) draws the ordinary tool line.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.dashboard import directive_queue
from kiro_crew.session_directive import call_input_digest

logger = logging.getLogger(__name__)

#: The gateway's own MCP server and tool whose calls carry a receipt.
SESSION_SEND_SERVER = "kirocrew-dashboard"
SESSION_SEND_TOOL = "session_send"

#: The row meta key the transcript reads.
META_KEY = "sessionSend"

#: The ``/api/session-control/send`` body flag the ``session_send`` tool sets to
#: ask for a receipt.
RECEIPT_FIELD = "receipt"

#: The outcomes a receipt can name: the target started a turn on the message,
#: took it into the turn it was running, or queued it behind that turn.
OUTCOMES = frozenset({"delivered", "steered", "queued"})

#: The receipts' own store. A conductor may send to many workers in one turn,
#: hence more room per session than a directive needs; and a send the gateway
#: refused parks nothing, so a claim that misses beside its siblings' receipts
#: is expected and logged at debug level.
RECEIPTS = directive_queue.Store(
    "session_send receipt",
    frozenset({SESSION_SEND_TOOL}),
    max_per_session=16,
    miss_log_level=logging.DEBUG,
)

# Backend-authored title prefix (kiro-cli, KAS); see session_directive.
_RUNNING_PREFIX = "Running: "

#: The tool as each backend's wire title spells it when the call carries no
#: ``_meta`` identity: KAS (``@<server>/<tool>``), claude-agent-acp
#: (``mcp__<server>__<tool>``) and opencode (``<server>_<tool>``).
_WIRE_TITLES = frozenset(
    {
        f"@{SESSION_SEND_SERVER}/{SESSION_SEND_TOOL}",
        f"mcp__{SESSION_SEND_SERVER}__{SESSION_SEND_TOOL}",
        f"{SESSION_SEND_SERVER}_{SESSION_SEND_TOOL}",
    }
)


def selector(target: Any, message: Any) -> str | None:
    """The digest a receipt is parked and claimed under, or None for a malformed pair."""
    if not isinstance(target, str) or not target or not isinstance(message, str):
        return None
    return call_input_digest(SESSION_SEND_TOOL, {"target": target, "message": message})


def call_arguments(raw_args: Any) -> dict[str, Any] | None:
    """A ``session_send`` call's own arguments from its raw ACP input.

    Some backends wrap the arguments the server receives: Codex as
    ``{"server", "tool", "arguments"}``, a Tool Search deferred call as
    ``{"tool_id": "<server>::<tool>", "arguments"}``. Unwrapped only when the
    wrapper names this very tool. The dashboard's ``parseSessionSend``
    (``pages/chat/sessionRelay.ts``) reads the row's input the same way.
    """
    if not isinstance(raw_args, dict):
        return None
    inner = raw_args.get("arguments")
    if isinstance(inner, dict) and (
        (
            raw_args.get("server") == SESSION_SEND_SERVER
            and raw_args.get("tool") == SESSION_SEND_TOOL
        )
        or raw_args.get("tool_id") == f"{SESSION_SEND_SERVER}::{SESSION_SEND_TOOL}"
    ):
        return inner
    return raw_args


def is_remote_send(raw_args: Any) -> bool:
    """True for a ``session_send`` naming a ``crew``: another gateway delivers it
    (``mcp_session_crew.run_crew_send``), so no receipt here is ever its own."""
    args = call_arguments(raw_args)
    return args is not None and "crew" in args


def call_selector(raw_args: Any) -> str | None:
    """The selector of a local ``session_send`` call's raw ACP arguments; None for
    a remote one, which must not take a local call's receipt."""
    args = call_arguments(raw_args)
    if args is None or "crew" in args:
        return None
    return selector(args.get("target"), args.get("message"))


def is_session_send_call(server: Any, name: Any, wire_title: Any = "") -> bool:
    """True for the gateway's own ``session_send``.

    By the call's ``_meta`` identity where it has one (a canonical name or a
    server-qualified one), else by the backend's own wire title -- never the
    display title, which a shell call's model-written description can fill.
    """
    if server == SESSION_SEND_SERVER and isinstance(name, str) and name:
        return name == SESSION_SEND_TOOL or name.endswith(f"__{SESSION_SEND_TOOL}")
    if not isinstance(wire_title, str) or not wire_title:
        return False
    if wire_title.startswith(_RUNNING_PREFIX):
        wire_title = wire_title[len(_RUNNING_PREFIX) :]
    return wire_title.strip() in _WIRE_TITLES


def _carries_identity(event: Any) -> bool:
    return any(
        getattr(event, field, "") for field in ("mcp_server_name", "tool_name", "wire_title")
    )


def outcome_of(result: dict[str, Any]) -> str:
    """The receipt outcome of a ``send_to_target`` result."""
    if result.get("steered"):
        return "steered"
    if result.get("started"):
        return "delivered"
    return "queued"


def park(session_key: str, target: Any, message: Any, outcome: str, delivered_to: str) -> bool:
    """Record what a send by *session_key* delivered; False when nothing was parked."""
    key = selector(target, message)
    if not session_key or key is None or outcome not in OUTCOMES or not delivered_to:
        return False
    directive_queue.publish(
        session_key,
        SESSION_SEND_TOOL,
        {"outcome": outcome, "target": delivered_to},
        key,
        store=RECEIPTS,
    )
    return True


def claim(session_key: str, key: str | None, *, not_before: float = 0.0) -> dict[str, str] | None:
    """Remove and return the oldest receipt *session_key* parked under *key* since *not_before*."""
    if not session_key or not key:
        return None
    record = directive_queue.claim(session_key, key, not_before=not_before, store=RECEIPTS)
    if record is None:
        return None
    args = record.get("args") or {}
    return {"outcome": str(args.get("outcome")), "target": str(args.get("target"))}


def note_call(selectors: dict[str, str], event: Any, *, initial: bool) -> None:
    """Record the selector of a ``session_send`` call frame, by its id.

    Called for the initial ``tool_call``, its refinement and its permission
    request: some backends stream the arguments only on the update, or name the
    tool only on the request. A frame naming another tool forgets the id (a
    reused id); a frame naming none keeps what an earlier frame recorded, and an
    identified call with no arguments yet is recorded as ``""``.
    """
    tool_call_id = getattr(event, "tool_call_id", "") or ""
    if not tool_call_id:
        return
    identified = is_session_send_call(
        getattr(event, "mcp_server_name", ""),
        getattr(event, "tool_name", ""),
        getattr(event, "wire_title", ""),
    )
    if not identified:
        if initial and _carries_identity(event):
            selectors.pop(tool_call_id, None)
        if tool_call_id not in selectors:
            return
    raw_args = getattr(event, "raw_tool_params", None)
    if is_remote_send(raw_args):
        # Delivered by another crew's gateway: it holds no receipt here.
        selectors.pop(tool_call_id, None)
        return
    key = call_selector(raw_args)
    if key is not None:
        selectors[tool_call_id] = key
    else:
        selectors.setdefault(tool_call_id, "")


def stamp_result(
    state: Any,
    slot: Any,
    selectors: dict[str, str],
    session_key: str,
    event: Any,
    rows: list[dict],
    *,
    terminal: bool,
    not_before: float,
) -> dict[str, str] | None:
    """On a ``session_send`` call's terminal result, stamp its receipt on *rows*.

    *rows* are the call's own tool rows this turn appended. Only a COMPLETED
    call stamps one. The row also gets the call's canonical identity where its
    own frames spelled it otherwise or not at all (a server-qualified name, a
    backend naming the tool only in its wire title), which is what the dashboard
    checks before drawing the card. Open tabs get both by
    ``chat_message_update``, the patch the reducer merges into the row's meta.
    Returns the receipt.
    """
    tool_call_id = getattr(event, "tool_call_id", "") or ""
    if not terminal or tool_call_id not in selectors:
        # A mid-stream frame, or a call that is not a session_send.
        return None
    key = selectors.pop(tool_call_id)
    if not key:
        return None
    receipt = claim(session_key, key, not_before=not_before)
    if (
        receipt is None
        or not getattr(event, "tool_final", False)
        or getattr(event, "refusal", None)
    ):
        # A call that failed or was refused stamps nothing; a receipt its request
        # parked (it delivered, then the harness reported a failure) is retired
        # with it, so a later identical call the gateway refused cannot wear it.
        return None
    for row in rows:
        meta = row.setdefault("meta", {})
        fields: dict[str, Any] = {META_KEY: dict(receipt)}
        if (
            meta.get("tool_name") != SESSION_SEND_TOOL
            or meta.get("mcp_server") != SESSION_SEND_SERVER
        ):
            # Canonical, whatever the frames spelled (a server-qualified name, or
            # none at all): the dashboard checks exactly these two values.
            fields.update(tool_name=SESSION_SEND_TOOL, mcp_server=SESSION_SEND_SERVER)
        meta.update(fields)
        ts = str(row.get("ts") or "")
        mid = str(meta.get("mid") or "")
        if not ts and not mid:
            continue
        # Addressed by the row's own id where it has one: two rows can share a ts.
        patch: dict[str, Any] = {"slot": slot.key, "ts": ts, "meta": dict(fields)}
        if mid:
            patch["mid"] = mid
        try:
            state.broadcast_ws("chat_message_update", patch)
        except Exception:
            # The row is already right; a client reconciles on its next fetch.
            logger.debug("session_send receipt broadcast failed", exc_info=True)
    return receipt


def reset() -> None:
    """Drop every parked receipt (tests)."""
    with RECEIPTS.lock:
        RECEIPTS.pending.clear()
