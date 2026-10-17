"""A codex provider failure ends the turn as a failure, not as an answer.

codex-acp writes a terminal provider error as message text for a client that did
not declare JetBrains AIR's ``sessionFailure`` capability, and then answers the
prompt with ``end_turn``. ``CodexEventHandler.createErrorEvent`` (1.11.0, 2.0.1,
2.1.1 and 2.1.2-preview.5 alike) returns
``createAgentTextMessageChunk(message + "\\n\\n")`` with no ``messageId`` and sets
no failure, so before this fix Crew streamed the error as the assistant's reply
and recorded the turn as a success.

The frames below are what that method returns, captured by calling it in
isolation on each release with a recorded native error: 2.0.1 forwards the
provider's JSON body verbatim, 2.1.1 unwraps it to the message first. Model text
always names its item (``createTextEvent`` passes the native item id), which is
the structural difference the fix reads.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import _dispatch
from kiro_crew.acp.client import AcpError
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_STEER_CONSUMED,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    JsonRpcMessage,
)
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR,
)
from kiro_crew.llm_helpers import acp_error_is_transient

_REQ_ID = 11
_SID = "sess-codex"

_PROVIDER_BODY = json.dumps(
    {
        "type": "error",
        "error": {
            "message": "model 'gpt-6.1-sol' is not enabled in rustponsesapi",
            "type": "invalid_request_error",
            "param": None,
            "code": None,
        },
        "status": 400,
    }
)
_PROVIDER_MESSAGE = "model 'gpt-6.1-sol' is not enabled in rustponsesapi"


def _chunk(text: str, message_id: str | None = None) -> JsonRpcMessage:
    update: dict[str, Any] = {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": text},
    }
    if message_id is not None:
        update["messageId"] = message_id
    return JsonRpcMessage(method="session/update", params={"sessionId": _SID, "update": update})


#: ``createErrorEvent`` on 2.0.1 (and 1.11.0): the provider body, verbatim.
ERROR_CHUNK_2_0 = _chunk(f"{_PROVIDER_BODY}\n\n")
#: ``createErrorEvent`` on 2.1.1: ``readableServiceErrorMessage`` unwraps it.
ERROR_CHUNK_2_1 = _chunk(f"{_PROVIDER_MESSAGE}\n\n")

#: ``createErrorEvent`` for an error that is not the current turn's, or that will
#: be retried: a ``session_info_update`` under ``_meta.codex``, never text.
LATE_ERROR = JsonRpcMessage(
    method="session/update",
    params={
        "sessionId": _SID,
        "update": {
            "sessionUpdate": "session_info_update",
            "_meta": {
                "codex": {
                    "error": {
                        "message": _PROVIDER_BODY,
                        "codexErrorInfo": "other",
                        "additionalDetails": None,
                        "turnId": "turn-0",
                        "willRetry": False,
                    }
                }
            },
        },
    },
)

END_TURN = JsonRpcMessage(id=_REQ_ID, result={"stopReason": STOP_REASON_END_TURN})


class _Runtime:
    """The runtime surface one prompt turn touches, scripted.

    The frames are queued AFTER the request is sent, so the pre-turn drain
    cannot eat them, then the prompt's response closes the turn.
    """

    def __init__(self, queue: asyncio.Queue, frames: list[JsonRpcMessage], backend: str):
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.send_notification = AsyncMock()
        self.supports_image_prompt = False
        self.acp_backend = backend
        self.requests: list[tuple[str, dict]] = []
        self._queue = queue
        self._frames = frames
        self._last_activity = time.monotonic()

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass

    async def send_request(self, method: str, params: dict) -> int:
        self.requests.append((method, params))
        for frame in self._frames:
            self._queue.put_nowait(frame)
        return _REQ_ID


def _handle(frames: list[JsonRpcMessage], backend: str = ACP_BACKEND_CODEX) -> AcpSessionHandle:
    queue: asyncio.Queue = asyncio.Queue()
    return AcpSessionHandle(_SID, queue, _Runtime(queue, frames, backend))


async def _run(
    handle: AcpSessionHandle, message: str = "hello"
) -> tuple[list, BaseException | None]:
    events: list = []
    try:
        async for ev in handle.prompt(message, timeout=5.0):
            events.append(ev)
    except AcpError as exc:
        return events, exc
    return events, None


def _texts(events: list) -> list[str]:
    return [e.text for e in events if e.kind == EVENT_TEXT_CHUNK]


def _completes(events: list) -> list:
    return [e for e in events if e.kind == EVENT_COMPLETE]


# --------------------------------------------------------------------------- #
# The reported failure
# --------------------------------------------------------------------------- #


class TestTerminalErrorIsAFailure:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("chunk", [ERROR_CHUNK_2_0, ERROR_CHUNK_2_1], ids=["2.0", "2.1"])
    async def test_the_turn_raises_instead_of_completing(self, chunk):
        events, exc = await _run(_handle([chunk, END_TURN]))

        assert exc is not None, "a failed native turn was reported as a normal completion"
        assert _completes(events) == []
        assert _texts(events) == [], "the provider error was delivered as assistant text"
        # The provider's own message, not the raw JSON body.
        assert _PROVIDER_MESSAGE in str(exc)
        assert '"invalid_request_error"' not in str(exc)

    @pytest.mark.asyncio
    async def test_a_400_is_not_retried_and_no_model_is_substituted(self):
        _events, exc = await _run(_handle([ERROR_CHUNK_2_0, END_TURN]))

        assert isinstance(exc, AcpError)
        assert acp_error_is_transient(exc) is False
        # The background layer substitutes a served model only for an error
        # tagged with the rejected one; this one keeps the selected model.
        assert exc.rejected_model is None
        assert exc.auth_required is False
        assert exc.usage_limit is False

    @pytest.mark.asyncio
    async def test_a_5xx_takes_the_same_transient_verdict_as_every_harness(self):
        body = json.dumps(
            {
                "type": "error",
                "error": {"message": "upstream overloaded", "type": "api_error"},
                "status": 503,
            }
        )
        _events, exc = await _run(_handle([_chunk(f"{body}\n\n"), END_TURN]))

        assert isinstance(exc, AcpError)
        assert acp_error_is_transient(exc) is True

    @pytest.mark.asyncio
    async def test_streamed_partial_output_is_kept_and_the_turn_still_fails(self):
        frames = [
            _chunk("Here is the first", "msg_1"),
            _chunk(" half of it", "msg_1"),
            ERROR_CHUNK_2_1,
            END_TURN,
        ]
        events, exc = await _run(_handle(frames))

        assert _texts(events) == ["Here is the first", " half of it"]
        assert exc is not None
        assert _completes(events) == []

    @pytest.mark.asyncio
    async def test_a_repeated_error_is_reported_once(self):
        events, exc = await _run(_handle([ERROR_CHUNK_2_1, ERROR_CHUNK_2_1, END_TURN]))

        assert _texts(events) == []
        assert str(exc).count(_PROVIDER_MESSAGE) == 1

    @pytest.mark.asyncio
    async def test_a_quota_error_with_its_own_rpc_failure_is_reported_once(self):
        """``usageLimitExceeded``: the adapter writes the text AND fails the prompt."""
        rpc = JsonRpcMessage(
            id=_REQ_ID,
            error={
                "code": -32603,
                "message": "Internal error",
                "data": {"message": "You've hit your usage limit."},
            },
        )
        events, exc = await _run(_handle([_chunk("You've hit your usage limit.\n\n"), rpc]))

        assert exc is not None
        assert _texts(events) == [], "the quota text was delivered beside its own error"

    @pytest.mark.asyncio
    async def test_a_steer_the_failed_turn_reported_consumed_is_withheld(self):
        handle = _handle([ERROR_CHUNK_2_1, END_TURN])
        handle._release_proven_steers = lambda _reason, _refusal: ["use the other file"]

        events, exc = await _run(handle)

        assert exc is not None
        assert [e for e in events if e.kind == EVENT_STEER_CONSUMED] == []

    @pytest.mark.asyncio
    async def test_the_handle_takes_the_next_turn(self):
        queue: asyncio.Queue = asyncio.Queue()
        rt = _Runtime(queue, [ERROR_CHUNK_2_1, END_TURN], ACP_BACKEND_CODEX)
        handle = AcpSessionHandle(_SID, queue, rt)
        _events, exc = await _run(handle)
        assert exc is not None

        rt._frames = [_chunk("fine now", "msg_2"), END_TURN]
        events, exc = await _run(handle)
        assert exc is None
        assert _texts(events) == ["fine now"]
        assert [c.stop_reason for c in _completes(events)] == [STOP_REASON_END_TURN]

    @pytest.mark.asyncio
    async def test_another_tenants_fanned_out_frame_does_not_release_the_hold(self):
        """On a shared codex process a co-tenant's ownerless frame can land
        between the error chunk and this turn's ``end_turn``."""
        from kiro_crew.acp.types import EVENT_CLEAR_STATUS, AcpEvent

        handle = _handle([END_TURN])
        foreign = AcpEvent(kind=EVENT_CLEAR_STATUS, runtime_global=True)

        async def _fake_dispatch(req_id, timeout, *, extract_command_result=False):
            yield AcpEvent(
                kind=EVENT_TEXT_CHUNK, text=f"{_PROVIDER_MESSAGE}\n\n", unattributed=True
            )
            yield foreign
            yield AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        handle._dispatch_events = _fake_dispatch
        events, exc = await _run(handle)

        assert exc is not None
        assert foreign in events
        assert _texts(events) == []
        assert _completes(events) == []


# --------------------------------------------------------------------------- #
# What must keep its current behaviour
# --------------------------------------------------------------------------- #


class TestNothingElseIsReclassified:
    @pytest.mark.asyncio
    async def test_a_successful_turn_is_untouched(self):
        events, exc = await _run(_handle([_chunk("All done.", "msg_1"), END_TURN]))

        assert exc is None
        assert _texts(events) == ["All done."]
        assert [c.stop_reason for c in _completes(events)] == [STOP_REASON_END_TURN]

    @pytest.mark.asyncio
    async def test_assistant_text_that_quotes_the_error_json_is_an_answer(self):
        """Model text carries its item id, whatever it says."""
        events, exc = await _run(_handle([_chunk(f"{_PROVIDER_BODY}\n\n", "msg_1"), END_TURN]))

        assert exc is None
        assert _texts(events) == [f"{_PROVIDER_BODY}\n\n"]
        assert len(_completes(events)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            "Warning: Model metadata for `llama32-32k:latest` not found.\n\n",
            "Config warning: unknown key `foo`\n\n",
            "*Context compacted to fit the model's context window.*\n\n",
        ],
    )
    async def test_the_adapters_own_notices_still_arrive_as_text(self, text):
        events, exc = await _run(_handle([_chunk(text), END_TURN]))

        assert exc is None
        assert _texts(events) == [text]
        assert len(_completes(events)) == 1

    @pytest.mark.asyncio
    async def test_slash_command_output_is_not_a_failure(self):
        """``/status`` and the ``/review`` result are unattributed text too."""
        events, exc = await _run(
            _handle([_chunk("Model: gpt-6.1-sol\n\n"), END_TURN]), message="/status"
        )

        assert exc is None
        assert _texts(events) == ["Model: gpt-6.1-sol\n\n"]

    @pytest.mark.asyncio
    async def test_held_text_followed_by_content_is_delivered_in_order(self):
        frames = [_chunk("odd adapter line\n\n"), _chunk("the answer", "msg_1"), END_TURN]
        events, exc = await _run(_handle(frames))

        assert exc is None
        assert _texts(events) == ["odd adapter line\n\n", "the answer"]

    @pytest.mark.asyncio
    async def test_streamed_unattributed_prose_is_not_a_failure(self):
        """An adapter that left the id off its model text would stream in pieces;
        ``createErrorEvent`` writes one whole chunk ending in a blank line."""
        events, exc = await _run(_handle([_chunk("Hel"), _chunk("lo there"), END_TURN]))

        assert exc is None
        assert _texts(events) == ["Hel", "lo there"]

    @pytest.mark.asyncio
    async def test_a_cancelled_turn_delivers_the_held_text(self):
        cancelled = JsonRpcMessage(id=_REQ_ID, result={"stopReason": "cancelled"})
        events, exc = await _run(_handle([ERROR_CHUNK_2_1, cancelled]))

        assert exc is None
        assert _texts(events) == [f"{_PROVIDER_MESSAGE}\n\n"]
        assert [c.stop_reason for c in _completes(events)] == ["cancelled"]

    @pytest.mark.asyncio
    async def test_a_late_error_for_another_turn_does_not_fail_this_one(self):
        events, exc = await _run(_handle([LATE_ERROR, _chunk("ok", "msg_1"), END_TURN]))

        assert exc is None
        assert _texts(events) == ["ok"]
        assert len(_completes(events)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "backend",
        [ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE, ACP_BACKEND_OPENCODE],
    )
    async def test_other_harnesses_are_not_reinterpreted(self, backend):
        """kiro-cli and claude leave the id off their model text, so the same
        frame there is an answer."""
        events, exc = await _run(_handle([ERROR_CHUNK_2_1, END_TURN], backend=backend))

        assert exc is None
        assert _texts(events) == [f"{_PROVIDER_MESSAGE}\n\n"]


class TestTheVocabulary:
    def test_codex_is_the_only_member(self):
        assert ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR == frozenset({ACP_BACKEND_CODEX})
        assert ACP_BACKEND_GOOSE not in ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR

    def test_the_envelope_keeps_its_status(self):
        err = _dispatch.unattributed_terminal_error([f"{_PROVIDER_BODY}\n\n"])
        assert err == {
            "code": -32603,
            "message": "Internal error",
            "data": f"{_PROVIDER_MESSAGE} (HTTP 400)",
        }

    @pytest.mark.parametrize(
        "body",
        [
            '{"type": "error", "status": true, "error": {"message": "x"}}',
            '{"type": "message", "status": 400, "error": {"message": "x"}}',
            '{"type": "error", "status": 400, "error": {"message": "  "}}',
            "[1, 2]",
        ],
    )
    def test_a_body_that_is_not_the_envelope_is_kept_whole(self, body):
        err = _dispatch.unattributed_terminal_error([f"{body}\n\n"])
        assert err is not None and err["data"] == body

    @pytest.mark.parametrize(
        "params, expected",
        [
            ({"prompt": [{"type": "text", "text": "  /review"}]}, True),
            ({"prompt": [{"type": "text", "text": "please /review"}]}, False),
            ({"prompt": [{"type": "image", "data": ""}, {"type": "text", "text": "/x"}]}, False),
            ({"prompt": []}, False),
            ({}, False),
        ],
    )
    def test_slash_command_detection_follows_the_adapter(self, params, expected):
        assert _dispatch.prompt_is_adapter_command(params) is expected
