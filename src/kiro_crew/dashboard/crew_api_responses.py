"""``POST /v1/responses``: run one turn for the machine owner, answered as JSON or a stream.

The Crew API's Responses subset (``docs/request-for-change/rfc-crew-api.md``, section
3.3). Each request runs on a session of its own, through the engine entry every turn
uses (``_run_chat``). It is answered with one ``Response`` object once the turn ends,
or, with ``stream: true``, as the stream events of ``crew_api_response_stream`` that
end on that same object.

What this route serves:

* The machine owner alone. A Shared Crew teammate, an app credential and an internal
  caller get ``403 owner_only``.
* A new conversation per request. ``conversation``, ``previous_response_id``,
  ``background``, tools and the phase 2 fields answer
  ``422 capability_unavailable``: the endpoint exists and this Kiro Crew cannot do
  them yet.
* A turn charged to its caller. Fair admission (``shared_crew.turn_gate``) judges the
  turn inside ``_run_chat``, as for every turn. A turn it refuses never reaches the
  engine and is answered ``200`` with ``status: failed`` and the capacity message.

Nothing about a response is stored beyond the session's own transcript, and the
``Idempotency-Key`` header is not read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from aiohttp import web

from kiro_crew.agent_sdk import TURN_STOP_REASON_CANCELLED
from kiro_crew.agent_sdk.drivers.acp_vocab import STOP_REASON_CANCEL_UNACKED
from kiro_crew.dashboard.chat_handlers import (
    _retract_failed_newborn,
    stop_slot_turn,
    write_sse_or_abort,
)
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.chat_utils import _sync_dashboard_slots
from kiro_crew.dashboard.crew_api_auth import OWNER_ONLY_PUBLIC_ROUTES
from kiro_crew.dashboard.crew_api_errors import (
    CREW_API_VERSION,
    REQUEST_ID_HEADER,
    VERSION_HEADER,
    contract_error,
    request_id_for,
)
from kiro_crew.dashboard.crew_api_grant_requests import _host_owner_refusal
from kiro_crew.dashboard.crew_api_models import usable_models_off_loop
from kiro_crew.dashboard.crew_api_owner import owner_route
from kiro_crew.dashboard.crew_api_response_stream import Events, encode
from kiro_crew.dashboard.kiro_readiness import (
    KAS_TURN_REFUSED_CODE,
    reject_if_kiro_unverified,
    rerun_signs_in_via_kiro_cli,
    turn_gate_target,
)
from kiro_crew.dashboard.openai_compat import (
    _MAX_MESSAGES,
    _MAX_PROMPT_BYTES,
    _flatten_messages,
    _redact,
    _TurnOutcome,
)
from kiro_crew.dashboard.state import (
    MAX_LIVE_SLOTS,
    DashboardState,
    is_stop_event_row,
    parse_cls_meta,
)
from kiro_crew.dashboard.turn_dispatch import (
    TurnTimeoutError,
    native_cancel_ack_timeout_secs,
    spawn_guarded_turn,
)
from kiro_crew.sel import sel
from kiro_crew.shared_crew.admission_gate import actor_budget
from kiro_crew.shared_crew.turn_gate import initiated_by

logger = logging.getLogger(__name__)

ROUTE_KEY: Final = ("POST", "/v1/responses")
OPERATION: Final = OWNER_ONLY_PUBLIC_ROUTES[ROUTE_KEY[1]]

# The limits the contract gives ``metadata`` ("as in OpenAI"): 16 pairs, keys to 64
# characters, values to 512.
METADATA_MAX_PAIRS: Final = 16
METADATA_KEY_MAX: Final = 64
METADATA_VALUE_MAX: Final = 512

#: The longest the reader sleeps before it looks again for a caller that left. The
#: Crew API's own keepalive interval for a stream (15 seconds).
WAKE_SECS: float = 15.0

#: The state ``turn_stop`` settles a Stop card on when the press stopped nothing.
_STOP_DECLINED: Final = "stop_declined_compacting"

# How the engine ended the turn, as the slot's ``_last_stop_reason`` says it.
_ENDED_CANCELLED: Final = "cancelled"  # the engine reported ``cancelled``
_ENDED_SILENT: Final = "silent"  # no last word, or ``error: cancel unacked``
_ENDED_ITSELF: Final = "itself"  # any other reason: ``end_turn``, a refusal, a stall
# The Stop card in the turn's rows.
_CARD_NONE: Final = "none"
_CARD_DECLINED: Final = "declined"  # the press stopped nothing
_CARD_PRESSED: Final = "pressed"  # any other state; a late press reads the same

#: Whether the answer is ``cancelled``: the turn's ending crossed with its Stop card.
#: A silent ending is what a turn that absorbed the cancel leaves, so the card
#: decides there and nowhere else.
_STOPPED: Final = {
    (_ENDED_CANCELLED, _CARD_NONE): True,
    (_ENDED_CANCELLED, _CARD_DECLINED): True,
    (_ENDED_CANCELLED, _CARD_PRESSED): True,
    (_ENDED_SILENT, _CARD_NONE): False,
    (_ENDED_SILENT, _CARD_DECLINED): False,
    (_ENDED_SILENT, _CARD_PRESSED): True,
    (_ENDED_ITSELF, _CARD_NONE): False,
    (_ENDED_ITSELF, _CARD_DECLINED): False,
    (_ENDED_ITSELF, _CARD_PRESSED): False,
}

_ROLES: Final = {
    "user": "user",
    "assistant": "assistant",
    "system": "system",
    "developer": "system",
}
#: Fields this route acts on. Any other field is accepted, not acted on and listed in
#: the answer; ``reasoning`` is one of those unless it asks for a summary (a 422).
_READ_FIELDS: Final = frozenset(
    {
        "model",
        "input",
        "instructions",
        "stream",
        "background",
        "metadata",
        "tools",
        "tool_choice",
        "store",
        "text",
        "conversation",
        "previous_response_id",
        "prompt",
    }
)
_INPUT_ITEMS_NOT_SERVED: Final = frozenset({"function_call_output", "mcp_approval_response"})
_CONTENT_PARTS_NOT_SERVED: Final = frozenset({"input_image", "input_file"})
_TOOLS_NOT_SERVED: Final = frozenset({"function", "mcp"})


class Refusal(Exception):
    """A request this route does not serve, as the contract error it is answered with."""

    def __init__(
        self, status: int, code: str, message: str, extra: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def _invalid(message: str) -> Refusal:
    return Refusal(400, "invalid_request", message)


def _not_served(what: str) -> Refusal:
    return Refusal(422, "capability_unavailable", f"{what} is not available on this Kiro Crew yet")


@dataclass(frozen=True)
class ParsedRequest:
    """A request this route serves: who runs it, the prompt, and what the answer echoes."""

    model: str
    prompt: str
    metadata: dict[str, str] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)
    stream: bool = False


def _flag(body: dict[str, Any], name: str) -> bool:
    value = body.get(name)
    if value is None or value is False:
        return False
    if value is not True:
        raise _invalid(f"{name} must be a boolean")
    return True


def _switch(body: dict[str, Any], name: str) -> None:
    if _flag(body, name):
        raise _not_served(f"{name}: true")


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise _invalid("a message's content must be a string or a list of parts")
    texts: list[str] = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if isinstance(kind, str) and kind in _CONTENT_PARTS_NOT_SERVED:
            raise _not_served(f"an {kind} part")
        if kind != "input_text" or not isinstance(part.get("text"), str):
            raise _invalid("a content part must be input_text with a text string")
        texts.append(part["text"])
    return "\n".join(texts)


def _messages(given: object) -> list[dict[str, str]]:
    if isinstance(given, str):
        return [{"role": "user", "content": given}]
    if not isinstance(given, list) or not given:
        raise _invalid("input must be a non-empty string or a non-empty list of items")
    if len(given) > _MAX_MESSAGES:
        raise _invalid(f"input holds too many items (at most {_MAX_MESSAGES})")
    messages: list[dict[str, str]] = []
    for item in given:
        if not isinstance(item, dict):
            raise _invalid("each input item must be an object")
        kind, sender = item.get("type", "message"), item.get("role")
        if isinstance(kind, str) and kind in _INPUT_ITEMS_NOT_SERVED:
            raise _not_served(f"a {kind} item")
        role = _ROLES.get(sender) if isinstance(sender, str) else None
        if kind != "message" or role is None:
            raise _invalid(
                "an input item must be a message from user, assistant, developer or system"
            )
        messages.append({"role": role, "content": _content_text(item.get("content"))})
    return messages


def _metadata(given: object) -> dict[str, str]:
    if given is None:
        return {}
    if not isinstance(given, dict) or len(given) > METADATA_MAX_PAIRS:
        raise _invalid(f"metadata must be an object of at most {METADATA_MAX_PAIRS} pairs")
    for key, value in given.items():
        if (
            len(key) > METADATA_KEY_MAX
            or not isinstance(value, str)
            or len(value) > METADATA_VALUE_MAX
        ):
            raise _invalid(
                f"a metadata key holds at most {METADATA_KEY_MAX} characters and its value is a "
                f"string of at most {METADATA_VALUE_MAX}"
            )
    return dict(given)


def _tools(body: dict[str, Any]) -> None:
    tools = body.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise _invalid("tools must be a list")
        for tool in tools:
            kind = tool.get("type") if isinstance(tool, dict) else None
            if not isinstance(kind, str):
                raise _invalid("each tool must be an object with a type")
            if kind not in _TOOLS_NOT_SERVED:
                raise Refusal(400, "unsupported_tool_type", f"tool type {kind!r} is not supported")
            raise _not_served("tools in the request")
    choice = body.get("tool_choice")
    if choice == "none":
        raise _not_served("tool_choice: none")
    if choice not in (None, "auto"):
        raise _invalid("tool_choice must be auto or none")


def _output_format(body: dict[str, Any]) -> None:
    text = body.get("text")
    if text is None:
        return
    output_format = text.get("format") if isinstance(text, dict) else "invalid"
    kind = output_format.get("type") if isinstance(output_format, dict) else output_format
    if kind == "json_schema":
        raise _not_served("text.format: json_schema")
    if kind not in (None, "text"):
        raise _invalid("text.format.type must be text")


def parse_request(body: object) -> ParsedRequest:
    """*body* as a request this route serves, or the :class:`Refusal` it is answered with."""
    if not isinstance(body, dict):
        raise _invalid("the body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise _invalid("model must be a non-empty string")
    if body.get("store") is False or body.get("prompt") is not None:
        name = "prompt" if body.get("prompt") is not None else "store: false"
        raise Refusal(400, "unsupported_parameter", f"{name} is not supported")
    if body.get("store") is not None and body.get("store") is not True:
        raise _invalid("store must be true")
    for name in ("conversation", "previous_response_id"):
        if body.get(name) is not None:
            raise _not_served(name)
    stream = _flag(body, "stream")
    _switch(body, "background")
    _tools(body)
    _output_format(body)
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("summary") is not None:
        raise _not_served("reasoning.summary")
    instructions = body.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise _invalid("instructions must be a string")
    messages = _messages(body.get("input"))
    if instructions:
        # Context ahead of the conversation: it never replaces the agent's own prompt.
        messages.insert(0, {"role": "system", "content": instructions})
    prompt = _flatten_messages(messages)
    if not prompt:
        raise _invalid("input must hold a user message with text")
    try:
        size = len(prompt.encode("utf-8"))
    except UnicodeEncodeError:
        # JSON can escape half of a surrogate pair; no text holds one.
        raise _invalid("input must be valid Unicode text") from None
    if size > _MAX_PROMPT_BYTES:
        raise Refusal(413, "payload_too_large", "the input is too large")
    return ParsedRequest(
        model=model,
        prompt=prompt,
        metadata=_metadata(body.get("metadata")),
        ignored=sorted(name for name in body if name not in _READ_FIELDS),
        stream=stream,
    )


@dataclass(frozen=True)
class _Answering:
    """One response being answered: what every ``Response`` object of it repeats."""

    response_id: str
    created_at: int
    parsed: ParsedRequest
    item_id: str = field(default_factory=lambda: "msg_" + uuid.uuid4().hex)

    def response(
        self,
        status: str,
        text: str = "",
        *,
        error: str | None = None,
        incomplete: str | None = None,
    ) -> dict[str, Any]:
        """The ``Response`` object: the JSON answer, and what a stream's events carry."""
        output: list[dict[str, Any]] = []
        if text:
            output.append(
                {
                    "type": "message",
                    "id": self.item_id,
                    "role": "assistant",
                    "status": "completed" if status == "completed" else "incomplete",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            )
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.parsed.model,
            "output": output,
            "error": None if error is None else {"code": "server_error", "message": error},
            "incomplete_details": None if incomplete is None else {"reason": incomplete},
            "metadata": self.parsed.metadata,
            # Token counts are not known at this layer: null, never a made-up zero.
            "usage": None,
            "crew": {"ignored_parameters": self.parsed.ignored},
        }


def _client_gone(request: web.Request) -> bool:
    """Whether the caller's connection is lost. Nothing cancels a handler for it."""
    return request.transport is None


async def _settled(task: "asyncio.Task[Any]") -> BaseException | None:
    """How *task* ended: its exception, a ``CancelledError`` when it was cancelled,
    ``None`` for a clean end or a task still running when the wait ran out.

    The ``done`` row is written inside the turn, so the task ends just after it. The
    wait is bounded by the engine's own cancel-acknowledgement budget.
    """
    await asyncio.wait({task}, timeout=native_cancel_ack_timeout_secs())
    if not task.done():
        return None
    if task.cancelled():
        return asyncio.CancelledError()
    return task.exception()


def _stop_took_effect(slot: Any) -> bool:
    """Whether a Stop cut this turn short, by the turn's own account.

    The whole rule is :data:`_STOPPED`: the engine's last word on the slot, crossed
    with the Stop card in the turn's rows.
    """
    reason = slot._last_stop_reason
    if reason == TURN_STOP_REASON_CANCELLED:
        ending = _ENDED_CANCELLED
    elif reason in ("", STOP_REASON_CANCEL_UNACKED):
        ending = _ENDED_SILENT
    else:
        ending = _ENDED_ITSELF
    card = _CARD_NONE
    for row in slot.messages:
        if is_stop_event_row(row):
            state = (parse_cls_meta(row.get("cls") or "") or {}).get("state")
            if state != _STOP_DECLINED:
                card = _CARD_PRESSED
                break
            card = _CARD_DECLINED
    return _STOPPED[ending, card]


async def _answer(
    request: web.Request,
    state: DashboardState,
    slot: Any,
    task: "asyncio.Task[Any]",
    answering: _Answering,
    said: "Callable[[str | None], Awaitable[None]] | None" = None,
) -> dict[str, Any]:
    """Read the turn's rows until it ends, and build the ``Response`` it is answered with.

    *said* is how a stream follows the turn: it is awaited with each piece of reply
    text as it is read, and with ``None`` when a wait ended with nothing new.
    """
    collected: list[str] = []
    outcome = _TurnOutcome()
    ended = False
    while not ended:
        # Recovery hands the cycle to a new task. Keep its ending, not the
        # predecessor's; the terminal done row clears slot.task before settling.
        task = slot.task or task
        # Read before the drain: a turn that ended meanwhile has written its last row.
        finished = task.done()
        for row in slot.drain():
            outcome.observe(row)
            if row.get("cls") == "done":
                ended = True
                break
            if row.get("role") == "chunk" or (row.get("role") == "assistant" and not collected):
                collected.append(row.get("content") or "")
                if said is not None:
                    await said(collected[-1])
        if ended or finished:
            break
        if _client_gone(request):
            await stop_slot_turn(state, slot, source="crew_api", escalate=False)
            await _settled(task)
            raise Refusal(500, "internal_error", "the caller closed the connection")
        # A new row or the turn's end, whichever comes first: a turn that dies
        # without a row sets no event.
        row_written = asyncio.ensure_future(slot.event.wait())
        try:
            woke, _ = await asyncio.wait(
                {row_written, task}, timeout=WAKE_SECS, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            row_written.cancel()
        if not woke and said is not None:
            await said(None)
    failure = await _settled(task)
    text = _redact("".join(collected))
    if isinstance(failure, TurnTimeoutError):
        return answering.response("incomplete", text, incomplete="turn_time_limit")
    cancelled = isinstance(failure, asyncio.CancelledError)
    if failure is not None and not cancelled:
        # It raised: the same answer whether or not a Stop was pressed meanwhile.
        logger.warning("Crew API turn %s failed: %s", answering.response_id, type(failure).__name__)
        raise Refusal(500, "internal_error", "the turn could not be run")
    # The owner can stop this session from another client: the turn absorbs the
    # cancel and still ends its cycle.
    if cancelled or _stop_took_effect(slot):
        return answering.response("cancelled", text)
    if not ended:
        # It ended without the row that closes a turn's cycle.
        logger.warning("Crew API turn %s failed: no end", answering.response_id)
        raise Refusal(500, "internal_error", "the turn could not be run")
    if outcome.reason is not None:
        return answering.response("failed", error=outcome.reason)
    return answering.response("completed", text)


async def _stream(
    request: web.Request,
    state: DashboardState,
    slot: Any,
    task: "asyncio.Task[Any]",
    answering: _Answering,
) -> web.StreamResponse:
    """Answer the turn as Responses stream events.

    The events end on the ``Response`` the JSON answer would be. The headers leave
    before the turn ends, so a turn that cannot be answered is an ``error`` event.
    """
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    # The owner gate adds these after the handler returns, when a stream's headers have left.
    resp.headers[REQUEST_ID_HEADER] = request_id_for(request)
    resp.headers[VERSION_HEADER] = CREW_API_VERSION
    events = Events(answering.item_id)

    async def _write(batch: list[dict[str, Any]]) -> None:
        for event in batch:
            await write_sse_or_abort(request, resp, encode(event))

    async def _said(text: str | None) -> None:
        if text is None:
            await write_sse_or_abort(request, resp, b": keepalive\n\n")
        else:
            await _write(events.text(text))

    try:
        await resp.prepare(request)
        await _write(events.start(answering.response("in_progress")))
        try:
            final = await _answer(request, state, slot, task, answering, _said)
        except Refusal as refusal:
            await _write(events.error(refusal.message))
        else:
            await _write(events.end(final))
    except ConnectionResetError:
        # The reader is gone: nobody is left to read this turn. A recovery may have
        # handed it to a new task meanwhile.
        task = slot.task or task
        if not task.done():
            await stop_slot_turn(state, slot, source="crew_api", escalate=False)
            await _settled(task)
    return resp


async def _engine_refusal(request: web.Request, state: DashboardState, slot: Any) -> Refusal | None:
    """Why the engine cannot take this turn, or ``None``. Asked as the frozen route asks."""
    if not await rerun_signs_in_via_kiro_cli(state.sessions, slot, reuses_live=True):
        return None
    harness_turn, turn_session = turn_gate_target(state, slot.key)
    blocked = await reject_if_kiro_unverified(
        request, harness_turn=harness_turn, session_key=turn_session
    )
    if blocked is None:
        return None
    message = "Kiro CLI setup or sign-in is required before a turn can run."
    try:
        gate = json.loads(blocked.text or "{}")
    except ValueError:
        gate = {}
    if not isinstance(gate, dict):
        gate = {}
    if gate.get("code") == KAS_TURN_REFUSED_CODE and isinstance(gate.get("error"), str):
        message = gate["error"]
    return Refusal(503, "instance_unavailable", message)


async def _audit(request: web.Request, response_id: str, slot_key: str, model: str) -> None:
    caller = str(request.get("user") or "unknown")

    def write() -> None:
        sel().log_api_access(
            caller=caller,
            operation=OPERATION,
            outcome="allowed",
            source="crew_api",
            resources=f"response={response_id} slot={slot_key} model={model}",
        )

    try:
        # Off the loop: the audit log can open its files on the first call.
        await asyncio.to_thread(write)
    except Exception:  # the audit row must never change the outcome
        logger.warning("No audit row written for %s %s", OPERATION, response_id)


async def _run(request: web.Request) -> web.StreamResponse:
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except ValueError:
        raise _invalid("the body must be JSON") from None
    parsed = parse_request(body)
    if parsed.model not in {row["id"] for row in await usable_models_off_loop()}:
        raise Refusal(404, "model_not_found", "no agent or Bot has that name")
    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        raise Refusal(
            429, "concurrency_limit", "too many sessions are live", extra={"limit": "turns"}
        )
    response_id = "resp_" + uuid.uuid4().hex
    slot = state.get_or_create_slot("resp-" + response_id.removeprefix("resp_"))
    # One request's session, removed when it returns: nobody views its card.
    slot._dashboard_card_exempt = True
    slot.agent = parsed.model
    task: asyncio.Task[Any] | None = None
    try:
        refusal = await _engine_refusal(request, state, slot)
        if refusal is not None:
            _retract_failed_newborn(state, slot)
            raise refusal
        await _audit(request, response_id, slot.key, parsed.model)
        # The gate and the audit write can yield: the session may have been closed meanwhile.
        if state._slots.get(slot.key) is not slot or slot.is_closing:
            raise Refusal(500, "internal_error", "the turn could not be run")
        slot.append("user", parsed.prompt, "msg msg-u")
        with slot.pending_consumer():
            # The turn is charged to its caller: fair admission judges it inside
            # ``_run_chat``, and an unnamed person's turn would go to a shared budget.
            with initiated_by(actor_budget(request)):
                task = spawn_guarded_turn(
                    state,
                    slot,
                    _run_chat(
                        state,
                        slot,
                        parsed.prompt,
                        _directive_user_origin=True,
                        # The prompt is composed from the request's items, not typed.
                        _message_is_typed=False,
                    ),
                )
            slot.task = task
            with state.suspend_slots_push():
                state.push_slots_update()
            answering = _Answering(response_id, int(time.time()), parsed)
            if parsed.stream:
                return await _stream(request, state, slot, task, answering)
            return web.json_response(await _answer(request, state, slot, task, answering))
    finally:
        task = slot.task or task
        if task is not None and not task.done():
            task.cancel()
        if state._slots.pop(slot.key, None) is not None:
            _sync_dashboard_slots(state)


async def _create_response(request: web.Request) -> web.StreamResponse:
    denied = await _host_owner_refusal(request, OPERATION)
    if denied is not None:
        return denied
    try:
        return await _run(request)
    except Refusal as refusal:
        return contract_error(
            refusal.status,
            refusal.code,
            refusal.message,
            request_id_for(request),
            extra=refusal.extra,
        )


#: POST /v1/responses. Served by the owner gate, then by the machine-owner check above.
api_responses = owner_route(_create_response, OPERATION)
