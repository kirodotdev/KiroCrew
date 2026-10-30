"""MCP elicitation on the ACP client: the wire shapes, the fail-closed parse, the
answer validation, and the handle's three outcomes (decline unrenderable, decline
without a consumer, yield and answer with one).

Both wires are driven: standard ACP ``elicitation/create`` (fields flat on params)
and KAS ``_kiro/mcp/elicitation`` (fields nested under ``params.elicitation``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import elicitation as el
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_ELICITATION_REQUEST,
    METHOD_CANCEL,
    METHOD_ELICITATION_CREATE,
    METHOD_KAS_MCP_ELICITATION,
    METHOD_PROMPT,
    JsonRpcMessage,
)
from kiro_crew.testing.wait import await_until

_SID = "sess_elicit"

_FORM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirm": {"type": "boolean", "title": "Confirm", "description": "Delete it"},
        "reason": {"type": "string", "maxLength": 40},
        "count": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
        "colour": {
            "type": "string",
            "oneOf": [{"const": "r", "title": "Red"}, {"const": "g", "title": "Green"}],
        },
        "tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
    },
    "required": ["confirm"],
}


def _acp_form(rid: int = 5, **extra: Any) -> JsonRpcMessage:
    params = {
        "sessionId": _SID,
        "toolCallId": "toolu_1",
        "mode": "form",
        "message": "Really delete?",
        "requestedSchema": _FORM_SCHEMA,
    }
    params.update(extra)
    return JsonRpcMessage(id=rid, method=METHOD_ELICITATION_CREATE, params=params)


def _kas_form(rid: int = 6) -> JsonRpcMessage:
    return JsonRpcMessage(
        id=rid,
        method=METHOD_KAS_MCP_ELICITATION,
        params={
            "sessionId": _SID,
            "toolCallId": "toolu_2",
            "elicitation": {"mode": "form", "message": "Really?", "requestedSchema": _FORM_SCHEMA},
        },
    )


def _kas_url(rid: int = 8, url: str = "https://example.com/confirm") -> JsonRpcMessage:
    return JsonRpcMessage(
        id=rid,
        method=METHOD_KAS_MCP_ELICITATION,
        params={
            "sessionId": _SID,
            "toolCallId": "toolu_3",
            "elicitation": {
                "mode": "url",
                "message": "Go confirm",
                "url": url,
                "elicitationId": "e1",
            },
        },
    )


# ── parse ──


def test_both_wires_parse_to_the_same_request_shape() -> None:
    acp = el.parse_elicitation_request(_acp_form())
    kas = el.parse_elicitation_request(_kas_form())
    assert acp.is_form and kas.is_form
    assert [f.name for f in acp.fields] == [f.name for f in kas.fields]
    assert acp.tool_call_id == "toolu_1" and kas.tool_call_id == "toolu_2"
    by_name = {f.name: f for f in acp.fields}
    assert by_name["confirm"].kind == el.FIELD_KIND_BOOLEAN and by_name["confirm"].required
    assert by_name["reason"].max_length == 40 and not by_name["reason"].required
    assert by_name["count"].kind == el.FIELD_KIND_INTEGER and by_name["count"].default == 2
    assert [o.label for o in by_name["colour"].options] == ["Red", "Green"]
    assert by_name["tags"].kind == el.FIELD_KIND_MULTI_ENUM


def test_a_mode_less_form_takes_the_mcp_default() -> None:
    """The MCP server may omit ``mode``; a ``requestedSchema`` means form."""
    msg = _acp_form()
    del msg.params["mode"]
    assert el.parse_elicitation_request(msg).is_form


def test_url_mode_parses_and_refuses_a_non_web_url() -> None:
    req = el.parse_elicitation_request(_kas_url())
    assert req.mode == el.ELICITATION_MODE_URL and req.url.startswith("https://")
    assert req.elicitation_id == "e1"
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_kas_url(url="javascript:alert(1)"))
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_kas_url(url="file:///etc/passwd"))
    # A well-formed URL on a scheme a browser does not open as a page.
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_kas_url(url="ftp://example.com/x"))
    # A host urlsplit itself refuses (a broken IPv6 bracket) is a refusal, not
    # a ValueError escaping into the dispatch loop.
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_kas_url(url="https://[broken]/confirm"))


@pytest.mark.parametrize(
    "prop",
    [
        {"type": "object", "properties": {}},
        {"type": "array", "items": {"type": "string"}},
        {"type": "null"},
        {"type": "string", "enum": [1, 2]},
        {"type": "string", "enum": []},
        {},
        # A bound past the float range: refused, never an OverflowError escaping
        # the parse-error handler and ending the turn.
        {"type": "integer", "minimum": 10**400},
        {"type": "number", "maximum": 10**400},
        # A choice longer than the limit is refused, never cut down: the value
        # is sent back verbatim as the answer.
        {"type": "string", "enum": ["x" * (el.STRING_VALUE_MAX_CHARS + 1)]},
        {
            "type": "string",
            "oneOf": [{"const": "y" * (el.STRING_VALUE_MAX_CHARS + 1), "title": "Y"}],
        },
    ],
)
def test_a_property_this_client_cannot_render_is_refused(prop: dict) -> None:
    schema = {"type": "object", "properties": {"x": prop}}
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(requestedSchema=schema))


def test_bounds_and_missing_parts_are_refused() -> None:
    too_many = {
        "type": "object",
        "properties": {f"p{i}": {"type": "string"} for i in range(el.PROPERTIES_MAX + 1)},
    }
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(requestedSchema=too_many))
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(message=""))
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(sessionId=""))
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(mode="hologram"))
    # Unhashable discriminants are refused, never a TypeError out of `in`.
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(mode=["form"]))
    unhashable_type = {"type": "object", "properties": {"x": {"type": {"a": 1}}}}
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(requestedSchema=unhashable_type))
    assert not el.is_elicitation_request(JsonRpcMessage(id=1, method=["elicitation/create"]))
    missing_required = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["b"],
    }
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(requestedSchema=missing_required))
    # A notification on the method is not a request (no id): nothing to answer.
    # The standard `elicitation/complete` notification shares the prefix and has no id.
    notif = JsonRpcMessage(method="elicitation/complete", params={"elicitationId": "e1"})
    assert not el.is_elicitation_request(notif)
    assert not el.is_elicitation_request(
        JsonRpcMessage(method=METHOD_ELICITATION_CREATE, params={})
    )


def test_a_string_format_outside_the_spec_set_is_dropped() -> None:
    """``format`` reaches the dashboard row as a type hint, unredacted, so only a
    value the MCP schema names is kept; free text a server writes there is not."""
    schema = {
        "type": "object",
        "properties": {
            "when": {"type": "string", "format": "date-time"},
            "leak": {"type": "string", "format": "AKIAIOSFODNN7EXAMPLE"},
        },
    }
    req = el.parse_elicitation_request(_acp_form(requestedSchema=schema))
    formats = {f.name: f.format for f in req.fields}
    assert formats == {"when": "date-time", "leak": ""}
    assert "AKIA" not in json.dumps(req.as_dict())
    # A row read back keeps the same rule.
    back = el.ElicitationField.from_dict({"name": "x", "kind": "string", "format": "secret"})
    assert back.format == ""


def test_the_message_is_bounded_for_display() -> None:
    req = el.parse_elicitation_request(_acp_form(message="x" * (el.MESSAGE_MAX_CHARS + 50)))
    assert len(req.message) == el.MESSAGE_MAX_CHARS


def test_as_dict_round_trips_through_from_dict() -> None:
    req = el.parse_elicitation_request(_acp_form())
    back = el.ElicitationRequest.from_dict(req.as_dict())
    assert back.fields == req.fields
    assert back.mode == req.mode and back.message == req.message
    url = el.parse_elicitation_request(_kas_url())
    assert el.ElicitationRequest.from_dict(url.as_dict()).url == url.url


# ── validate ──


def test_a_titled_multi_select_in_the_mcp_spelling_is_rendered() -> None:
    """The MCP schema titles a multi-select's choices under ``items.anyOf``
    (``oneOf`` is the single-select spelling); it renders, never declines."""
    schema = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {
                    "anyOf": [{"const": "a", "title": "Alpha"}, {"const": "b", "title": "Beta"}]
                },
            }
        },
    }
    req = el.parse_elicitation_request(_acp_form(requestedSchema=schema))
    (field_,) = req.fields
    assert field_.kind == el.FIELD_KIND_MULTI_ENUM
    assert [(o.value, o.label) for o in field_.options] == [("a", "Alpha"), ("b", "Beta")]
    assert el.validate_form_content(req, {"tags": ["b"]}) == {"tags": ["b"]}
    bad = {**schema["properties"]["tags"], "items": {"anyOf": [{"const": 1, "title": "One"}]}}
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(
            _acp_form(requestedSchema={"type": "object", "properties": {"tags": bad}})
        )


def test_multi_select_bounds_are_kept_and_enforced() -> None:
    """``minItems`` / ``maxItems`` on an array field survive normalisation, the
    round trip, and the answer check -- content outside the requested count is
    refused rather than sent to a server whose schema would reject it."""
    schema = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {"type": "string", "enum": ["a", "b", "c"]},
            }
        },
    }
    req = el.parse_elicitation_request(_acp_form(requestedSchema=schema))
    field = req.fields[0]
    assert (field.min_items, field.max_items) == (2, 2)
    assert el.ElicitationRequest.from_dict(req.as_dict()).fields[0] == field
    assert el.validate_form_content(req, {"tags": ["a", "b"]}) == {"tags": ["a", "b"]}
    for bad in (["a"], ["a", "b", "c"]):
        with pytest.raises(el.ElicitationContentError):
            el.validate_form_content(req, {"tags": bad})


def test_form_content_is_checked_and_shaped() -> None:
    req = el.parse_elicitation_request(_acp_form())
    out = el.validate_form_content(
        req, {"confirm": True, "reason": "ok", "count": 3.0, "colour": "g", "tags": ["a", "a", "b"]}
    )
    assert out == {"confirm": True, "reason": "ok", "count": 3, "colour": "g", "tags": ["a", "b"]}
    # An optional field left out is omitted, not defaulted.
    assert el.validate_form_content(req, {"confirm": False}) == {"confirm": False}


@pytest.mark.parametrize(
    "content",
    [
        {},  # required missing
        {"confirm": "yes"},  # wrong type
        {"confirm": True, "count": 9},  # above maximum
        {"confirm": True, "count": 2.5},  # not whole
        {"confirm": True, "reason": "x" * 41},  # too long
        {"confirm": True, "colour": "blue"},  # not offered
        {"confirm": True, "tags": ["z"]},  # not offered
        {"confirm": True, "extra": 1},  # unknown field
        # ``1e400`` decodes to infinity; sent on, it would be written as the JSON
        # extension ``Infinity`` the engine refuses whole.
        {"confirm": True, "ratio": float("inf")},
        {"confirm": True, "ratio": float("nan")},
        {"confirm": True, "count": float("inf")},
    ],
)
def test_content_the_schema_refuses_is_an_error(content: dict) -> None:
    schema = {
        **_FORM_SCHEMA,
        "properties": {**_FORM_SCHEMA["properties"], "ratio": {"type": "number"}},
    }
    req = el.parse_elicitation_request(_acp_form(requestedSchema=schema))
    with pytest.raises(el.ElicitationContentError):
        el.validate_form_content(req, content)


def test_a_non_finite_default_is_dropped_not_shown() -> None:
    """``default: 1e400`` reaches the parser as infinity. Kept, the card's frame
    would carry ``Infinity``, which the browser's ``JSON.parse`` refuses, so the
    person would see no card at all. The default is dropped; the field stays."""
    schema = {
        "type": "object",
        "properties": {
            "ratio": {"type": "number", "default": float("inf")},
            "count": {"type": "integer", "default": float("nan")},
            "ok": {"type": "number", "default": 0.5},
        },
    }
    req = el.parse_elicitation_request(_acp_form(requestedSchema=schema))
    defaults = {f.name: f.default for f in req.fields}
    assert defaults == {"ratio": None, "count": None, "ok": 0.5}
    # What goes to the dashboard is strict JSON.
    json.dumps(req.as_dict(), allow_nan=False)


def test_response_carries_content_only_on_accept() -> None:
    assert el.elicitation_response(el.ELICITATION_ACCEPT, {"a": 1}) == {
        "action": "accept",
        "content": {"a": 1},
    }
    assert el.elicitation_response(el.ELICITATION_DECLINE, {"a": 1}) == {"action": "decline"}
    assert el.elicitation_response(el.ELICITATION_CANCEL) == {"action": "cancel"}
    with pytest.raises(ValueError):
        el.elicitation_response("maybe")


# ── the session handle ──


class _Runtime:
    def __init__(self) -> None:
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.send_notification = AsyncMock()
        self.send_request = AsyncMock(return_value=1)
        self.send_response = AsyncMock()
        self.send_error = AsyncMock()
        self.supports_image_prompt = False
        self.acp_backend = ""
        self._last_activity = time.monotonic()

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        return None


def _handle() -> tuple[AcpSessionHandle, _Runtime]:
    runtime = _Runtime()
    return AcpSessionHandle(_SID, asyncio.Queue(), runtime), runtime


@pytest.mark.asyncio
async def test_handle_declines_when_its_consumer_cannot_ask_a_person() -> None:
    handle, runtime = _handle()
    assert handle.accepts_elicitation is False
    assert await handle._handle_elicitation_request(_kas_form()) is None
    runtime.send_response.assert_awaited_once_with(6, {"action": "decline"})
    assert handle._pending_elicitations == {}


@pytest.mark.asyncio
async def test_handle_declines_an_unrenderable_request_even_with_a_consumer() -> None:
    handle, runtime = _handle()
    handle.accepts_elicitation = True
    assert await handle._handle_elicitation_request(_kas_url(url="javascript:x")) is None
    runtime.send_response.assert_awaited_once_with(8, {"action": "decline"})
    runtime.send_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_yields_the_event_and_sends_the_validated_answer() -> None:
    handle, runtime = _handle()
    handle.accepts_elicitation = True
    event = await handle._handle_elicitation_request(_kas_form())
    assert event is not None and event.kind == EVENT_ELICITATION_REQUEST
    assert event.request_id == 6 and event.tool_call_id == "toolu_2"
    assert event.elicitation and event.elicitation["mode"] == "form"
    assert [f["name"] for f in event.elicitation["fields"]][:2] == ["confirm", "reason"]
    runtime.send_response.assert_not_awaited()
    # A bad answer leaves the ask pending; nothing goes on the wire.
    with pytest.raises(el.ElicitationContentError):
        await handle.answer_elicitation(6, el.ELICITATION_ACCEPT, {"confirm": "nope"})
    assert 6 in handle._pending_elicitations
    runtime.send_response.assert_not_awaited()
    assert await handle.answer_elicitation(6, el.ELICITATION_ACCEPT, {"confirm": True, "count": 4})
    runtime.send_response.assert_awaited_once_with(
        6, {"action": "accept", "content": {"confirm": True, "count": 4}}
    )
    assert handle._pending_elicitations == {}
    # Answered once: a second answer finds nothing pending.
    assert not await handle.answer_elicitation(6, el.ELICITATION_DECLINE)


@pytest.mark.asyncio
async def test_handle_cancels_what_is_left_pending() -> None:
    handle, runtime = _handle()
    handle.accepts_elicitation = True
    await handle._handle_elicitation_request(_kas_form(rid=20))
    await handle._handle_elicitation_request(_acp_form(rid=21))
    assert await handle.cancel_pending_elicitations() == 2
    assert sorted(c.args for c in runtime.send_response.await_args_list) == [
        (20, {"action": "cancel"}),
        (21, {"action": "cancel"}),
    ]
    assert await handle.cancel_pending_elicitations() == 0


@pytest.mark.asyncio
async def test_a_url_answer_carries_no_content() -> None:
    handle, runtime = _handle()
    handle.accepts_elicitation = True
    await handle._handle_elicitation_request(_kas_url(rid=30))
    assert await handle.answer_elicitation(30, el.ELICITATION_ACCEPT, {"ignored": True})
    runtime.send_response.assert_awaited_once_with(30, {"action": "accept"})


# ── Every exit answers what is still pending ──
#
# The engine holds an elicitation's tool call open until the request is answered,
# so each path that ends or abandons a turn must put ``cancel`` on the wire, on
# both drivers: the per-turn reset (an ask the last turn left open), the cancel
# (session/cancel first, then the open request it leaves behind -- the order the
# ACP spec gives for permission requests), and the handle's destroy.


class _OneTurnRuntime(_Runtime):
    """``send_request`` ends the turn at once; a prompt runs its resets and returns."""

    def __init__(self, queue: asyncio.Queue) -> None:
        super().__init__()
        self._queue = queue
        self.terminate_session = AsyncMock()
        self.wire: list[tuple[str, Any]] = []
        self.send_response = AsyncMock(side_effect=lambda rid, body: self.wire.append(("r", rid)))
        self.send_notification = AsyncMock(
            side_effect=lambda method, params: self.wire.append(("n", method))
        )
        self.send_request = self._end_turn_at_once

    async def _end_turn_at_once(self, method: str, params: dict) -> int:
        self.wire.append(("q", method))
        self._queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))
        return 1


def _one_turn_handle() -> tuple[AcpSessionHandle, _OneTurnRuntime]:
    queue: asyncio.Queue = asyncio.Queue()
    runtime = _OneTurnRuntime(queue)
    handle = AcpSessionHandle(_SID, queue, runtime)
    handle.accepts_elicitation = True
    return handle, runtime


@pytest.mark.asyncio
async def test_the_handle_cancel_answers_the_open_ask_after_session_cancel() -> None:
    handle, runtime = _one_turn_handle()
    await handle._handle_elicitation_request(_kas_form(rid=40))
    await handle.cancel()
    assert runtime.wire == [("n", METHOD_CANCEL), ("r", 40)]
    runtime.send_response.assert_awaited_once_with(40, {"action": "cancel"})
    assert handle._pending_elicitations == {}


@pytest.mark.asyncio
async def test_the_handle_per_turn_reset_answers_an_ask_the_last_turn_left_open() -> None:
    handle, runtime = _one_turn_handle()
    await handle._handle_elicitation_request(_acp_form(rid=41))
    events = [ev async for ev in handle.prompt("next", timeout=5.0)]
    assert events and events[-1].kind == EVENT_COMPLETE
    # Answered before the new prompt goes out, never after it.
    assert runtime.wire[:2] == [("r", 41), ("q", METHOD_PROMPT)]
    runtime.send_response.assert_awaited_once_with(41, {"action": "cancel"})


@pytest.mark.asyncio
async def test_the_handle_destroy_answers_the_open_ask_before_the_session_goes() -> None:
    handle, runtime = _one_turn_handle()
    await handle._handle_elicitation_request(_kas_url(rid=42))
    await handle.destroy()
    runtime.send_response.assert_awaited_once_with(42, {"action": "cancel"})
    runtime.terminate_session.assert_awaited_once()
    assert handle._pending_elicitations == {}


def _client(tmp_path) -> AcpClient:
    client = AcpClient(work_dir=tmp_path)
    client._send_response = AsyncMock()  # type: ignore[method-assign]
    client._session_id = "sess-elicit"
    return client


@pytest.mark.asyncio
async def test_the_client_declines_when_its_consumer_cannot_ask_a_person(tmp_path) -> None:
    client = _client(tmp_path)
    assert client.accepts_elicitation is False
    assert await client._handle_elicitation_request(_acp_form(rid=50)) is None
    client._send_response.assert_awaited_once_with(50, {"action": "decline"})
    assert client._pending_elicitations == {}
    # The text-only prompt APIs take the same answer without a parse.
    await client._decline_elicitation(_kas_form(rid=51))
    client._send_response.assert_awaited_with(51, {"action": "decline"})


@pytest.mark.asyncio
async def test_the_clients_audit_resolves_sel_off_the_event_loop(tmp_path, monkeypatch) -> None:
    """An unwarmed ``sel()`` initialises on the thread that calls it, so the
    client resolves it inside an off-loop hop, as its permission audit does."""
    import threading

    from kiro_crew import sel as sel_module

    resolved_on: list[threading.Thread] = []
    recorder = MagicMock()

    def _sel() -> MagicMock:
        resolved_on.append(threading.current_thread())
        return recorder

    monkeypatch.setattr(sel_module, "sel", _sel)
    client = _client(tmp_path)
    await client._handle_elicitation_request(_acp_form(rid=55))
    assert resolved_on and threading.main_thread() not in resolved_on
    assert recorder.log_tool_invocation.call_args.kwargs["outcome"] == "decline"


@pytest.mark.asyncio
async def test_the_client_yields_the_event_and_relays_the_answer(tmp_path) -> None:
    client = _client(tmp_path)
    client.accepts_elicitation = True
    event = await client._handle_elicitation_request(_acp_form(rid=52))
    assert event is not None and event.kind == EVENT_ELICITATION_REQUEST
    assert event.request_id == 52 and 52 in client._pending_elicitations
    client._send_response.assert_not_awaited()
    assert await client.answer_elicitation(52, el.ELICITATION_DECLINE)
    client._send_response.assert_awaited_once_with(52, {"action": "decline"})
    assert not await client.answer_elicitation(52, el.ELICITATION_DECLINE)


@pytest.mark.asyncio
async def test_the_client_cancel_session_answers_the_open_ask_after_session_cancel(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    client.accepts_elicitation = True
    proc = MagicMock()
    proc.returncode = None
    proc.stdin.drain = AsyncMock()
    client._process = proc
    await client._handle_elicitation_request(_kas_form(rid=53))
    await client.cancel_session()
    written = [
        json.loads(line)
        for c in proc.stdin.write.call_args_list
        for line in c.args[0].decode().splitlines()
    ]
    assert [w.get("method") for w in written] == [METHOD_CANCEL]
    client._send_response.assert_awaited_once_with(53, {"action": "cancel"})
    assert client._pending_elicitations == {}


@pytest.mark.asyncio
async def test_the_client_per_turn_reset_answers_an_ask_the_last_turn_left_open(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path)
    client.accepts_elicitation = True
    await client._handle_elicitation_request(_acp_form(rid=54))

    async def _no_frames(req_id: int, timeout: float):
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(client, "_prompt_loop", _no_frames)
    with contextlib.suppress(Exception):
        async for _ev in client._dispatch_events(7, 1.0):
            pass
    client._send_response.assert_awaited_once_with(54, {"action": "cancel"})
    assert client._pending_elicitations == {}


@pytest.mark.asyncio
async def test_a_turn_cancelled_while_answering_the_left_over_ask_leaves_the_handle_idle() -> None:
    """The per-turn reset answers an ask the last turn left open with a write that
    can be cancelled. It runs before the turn is marked active, so a cancellation
    there cannot leave the handle refusing every later prompt."""
    handle, runtime = _one_turn_handle()
    await handle._handle_elicitation_request(_acp_form(rid=43))
    writing = asyncio.Event()
    never = asyncio.Event()

    async def _stuck_write(rid: Any, body: Any) -> None:
        writing.set()
        await never.wait()

    runtime.send_response = AsyncMock(side_effect=_stuck_write)

    async def _drive() -> None:
        async for _ev in handle.prompt("next", timeout=5.0):
            pass

    task = asyncio.create_task(_drive())
    await await_until(writing, what="the left-over ask's cancel write")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(task, what="the cancelled prompt")
    assert not handle.is_turn_active
    assert ("q", METHOD_PROMPT) not in runtime.wire
