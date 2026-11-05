"""KAS's ``_kiro/userInput``: the model's own question, put to a person on the
elicitation card and always answered ``answered``.

KAS hands the model only an ``answered`` reply with a non-empty ``answer``; any
other reply (``dismissed``, an error, a method-not-found) becomes "move on to the
next phase", which its ``user_input`` tool tells the model to read as yes. So a
Deny, a cancel and a question no person could see must each go back as
``answered`` with text that says it was not agreement. The capability flag is
declared on the KAS handshake, and the handler ships with it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import elicitation as el
from kiro_crew.acp._dispatch import classify_notification
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_turn import _process_message
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_ELICITATION_REQUEST,
    KAS_CLIENT_CAPABILITIES,
    METHOD_KAS_USER_INPUT,
    JsonRpcMessage,
)

_SID = "sess_user_input"
_OPTIONS = [
    {"title": "Postgres", "description": "Managed, in RDS", "recommended": True},
    {"title": "SQLite"},
]


def _ask(rid: int = 7, options: Any = _OPTIONS, **extra: Any) -> JsonRpcMessage:
    params: dict[str, Any] = {
        "sessionId": _SID,
        "toolCallId": "tooluse_q1",
        "question": "**Which database?**",
        "options": options,
    }
    params.update(extra)
    return JsonRpcMessage(id=rid, method=METHOD_KAS_USER_INPUT, params=params)


def _answered(text: str) -> dict[str, str]:
    return {"action": "answered", "answer": text}


_DECLINED = _answered(el.USER_INPUT_DECLINED_ANSWER)
_NO_PERSON = _answered(el.USER_INPUT_NO_PERSON_ANSWER)


# ── the flag ──


def test_kas_declares_user_input_and_kiro_cli_does_not() -> None:
    assert KAS_CLIENT_CAPABILITIES["_meta"]["kiro"]["userInput"] is True
    assert harness_for(ACP_BACKEND_KAS).client_capabilities["_meta"]["kiro"]["userInput"] is True
    assert "_meta" not in harness_for(ACP_BACKEND_KIRO).client_capabilities


def test_the_request_is_classified_as_an_ask_not_an_unknown_method() -> None:
    # An unknown request is answered method-not-found, which KAS reads as yes.
    assert classify_notification(_ask()) == "elicitation"
    assert el.is_elicitation_request(_ask())
    assert _process_message(_ClassifierHost(), _ask(), req_id=1) == "elicitation"
    note = JsonRpcMessage(method=METHOD_KAS_USER_INPUT, params={})
    assert not el.is_elicitation_request(note)


class _ClassifierHost:
    """The slice of ``AcpClient`` ``_process_message`` reads before its method checks."""

    _steer_turn = None

    def _note_active_run(self, msg: JsonRpcMessage) -> None:
        raise AssertionError("a user-input request is not a session update")


# ── the parse ──


def test_options_become_a_choice_beside_free_text() -> None:
    request = el.parse_elicitation_request(_ask())
    assert request.method == METHOD_KAS_USER_INPUT and request.is_form
    assert request.message == "**Which database?**" and request.tool_call_id == "tooluse_q1"
    choice, text = request.fields
    assert (choice.name, choice.kind, choice.required) == ("choice", el.FIELD_KIND_ENUM, False)
    assert [o.value for o in choice.options] == ["Postgres", "SQLite"]
    assert choice.options[0].label == "Postgres (recommended): Managed, in RDS"
    assert (text.name, text.kind, text.required) == ("answer", el.FIELD_KIND_STRING, False)


def test_a_question_without_options_needs_typed_text() -> None:
    for options in (None, []):
        request = el.parse_elicitation_request(_ask(options=options))
        (text,) = request.fields
        assert text.name == "answer" and text.required is True


def test_string_options_and_duplicates_are_read() -> None:
    request = el.parse_elicitation_request(_ask(options=["Yes", "No", "Yes"]))
    assert [o.value for o in request.fields[0].options] == ["Yes", "No"]


@pytest.mark.parametrize(
    "params",
    [
        {"question": ""},
        {"question": None},
        {"sessionId": ""},
        {"options": "Yes"},
        {"options": [{"description": "no title"}]},
        {"options": [{"title": f"o{i}"} for i in range(el.ENUM_OPTIONS_MAX + 1)]},
    ],
)
def test_a_question_this_client_cannot_draw_is_refused(params: dict) -> None:
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_ask(**params))


def test_the_card_row_validates_the_answer_as_the_driver_does() -> None:
    request = el.parse_elicitation_request(_ask())
    back = el.ElicitationRequest.from_dict(request.as_dict())
    assert back.fields == request.fields
    assert el.validate_form_content(back, {"choice": "SQLite"}) == {"choice": "SQLite"}
    with pytest.raises(el.ElicitationContentError):
        el.validate_form_content(back, {"choice": "MySQL"})


# ── the reply ──


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ({"choice": "SQLite"}, _answered("SQLite")),
        ({"answer": " use DynamoDB "}, _answered("use DynamoDB")),
        ({"choice": "Postgres", "answer": "version 16"}, _answered("Postgres: version 16")),
        ({}, _DECLINED),
        ({"answer": "   "}, _DECLINED),
    ],
)
def test_an_accept_sends_the_choice_or_the_text(content: dict, expected: dict) -> None:
    assert el.user_input_response(el.ELICITATION_ACCEPT, content) == expected


def test_no_reply_is_ever_anything_but_answered_with_text() -> None:
    assert el.user_input_response(el.ELICITATION_DECLINE) == _DECLINED
    assert el.user_input_response(el.ELICITATION_DECLINE, person=False) == _NO_PERSON
    assert el.user_input_response(el.ELICITATION_CANCEL) == _NO_PERSON
    for action in el.ELICITATION_ACTIONS:
        for person in (True, False):
            reply = el.elicitation_reply(METHOD_KAS_USER_INPUT, action, person=person)
            assert reply["action"] == "answered" and reply["answer"].strip()
    with pytest.raises(ValueError):
        el.user_input_response("dismissed")
    # An MCP elicitation keeps its own shape.
    assert el.elicitation_reply("_kiro/mcp/elicitation", el.ELICITATION_DECLINE) == {
        "action": "decline"
    }


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
async def test_handle_with_no_person_answers_that_nobody_could() -> None:
    """A cron, channel or sub-agent turn: answered at once, never ``dismissed``."""
    handle, runtime = _handle()
    assert handle.accepts_elicitation is False
    assert await handle._handle_elicitation_request(_ask()) is None
    runtime.send_response.assert_awaited_once_with(7, _NO_PERSON)
    runtime.send_error.assert_not_awaited()
    assert handle._pending_elicitations == {}


@pytest.mark.asyncio
async def test_handle_answers_an_undrawable_question_the_same_way() -> None:
    handle, runtime = _handle()
    handle.accepts_elicitation = True
    assert await handle._handle_elicitation_request(_ask(options="Yes")) is None
    runtime.send_response.assert_awaited_once_with(7, _NO_PERSON)
    runtime.send_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_yields_the_card_and_sends_the_persons_choice() -> None:
    handle, runtime = _handle()
    handle.accepts_elicitation = True
    event = await handle._handle_elicitation_request(_ask())
    assert event is not None and event.kind == EVENT_ELICITATION_REQUEST
    assert event.elicitation and [f["name"] for f in event.elicitation["fields"]] == [
        "choice",
        "answer",
    ]
    runtime.send_response.assert_not_awaited()
    assert await handle.answer_elicitation(7, el.ELICITATION_ACCEPT, {"choice": "SQLite"})
    runtime.send_response.assert_awaited_once_with(7, _answered("SQLite"))


@pytest.mark.asyncio
async def test_handle_deny_tells_the_model_it_was_not_agreement() -> None:
    handle, runtime = _handle()
    handle.accepts_elicitation = True
    await handle._handle_elicitation_request(_ask())
    assert await handle.answer_elicitation(7, el.ELICITATION_DECLINE)
    runtime.send_response.assert_awaited_once_with(7, _DECLINED)


@pytest.mark.asyncio
async def test_handle_a_caller_that_showed_nobody_sends_the_no_person_text() -> None:
    handle, runtime = _handle()
    handle.accepts_elicitation = True
    await handle._handle_elicitation_request(_ask())
    assert await handle.answer_elicitation(7, el.ELICITATION_DECLINE, None, person=False)
    runtime.send_response.assert_awaited_once_with(7, _NO_PERSON)


@pytest.mark.asyncio
async def test_handle_turn_boundary_answers_the_open_question() -> None:
    handle, runtime = _handle()
    handle.accepts_elicitation = True
    await handle._handle_elicitation_request(_ask(rid=8))
    assert await handle.cancel_pending_elicitations() == 1
    runtime.send_response.assert_awaited_once_with(8, _NO_PERSON)


# ── the single-session client ──


def _client(tmp_path) -> AcpClient:
    client = AcpClient(work_dir=tmp_path)
    client._send_response = AsyncMock()  # type: ignore[method-assign]
    client._session_id = "sess-user-input"
    return client


@pytest.mark.asyncio
async def test_client_with_no_person_and_text_only_paths_answer_that_nobody_could(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    assert await client._handle_elicitation_request(_ask(rid=9)) is None
    client._send_response.assert_awaited_once_with(9, _NO_PERSON)
    await client._decline_elicitation(_ask(rid=10))
    client._send_response.assert_awaited_with(10, _NO_PERSON)


@pytest.mark.asyncio
async def test_client_relays_typed_text_and_a_deny(tmp_path) -> None:
    client = _client(tmp_path)
    client.accepts_elicitation = True
    assert await client._handle_elicitation_request(_ask(rid=11, options=None)) is not None
    with pytest.raises(el.ElicitationContentError):
        await client.answer_elicitation(11, el.ELICITATION_ACCEPT, {})
    assert await client.answer_elicitation(11, el.ELICITATION_ACCEPT, {"answer": "blue"})
    client._send_response.assert_awaited_once_with(11, _answered("blue"))
    await client._handle_elicitation_request(_ask(rid=12))
    assert await client.answer_elicitation(12, el.ELICITATION_DECLINE)
    client._send_response.assert_awaited_with(12, _DECLINED)
    await client._handle_elicitation_request(_ask(rid=14))
    assert await client.answer_elicitation(14, el.ELICITATION_DECLINE, None, person=False)
    client._send_response.assert_awaited_with(14, _NO_PERSON)
    await client._handle_elicitation_request(_ask(rid=13))
    assert await client.cancel_pending_elicitations() == 1
    client._send_response.assert_awaited_with(13, _NO_PERSON)
