"""Mid-turn steer on the claude backend rides the ACP ``_session/steering`` request.

claude-agent-acp has no ``_session/steer`` (kiro-cli's method) and sends no
``steering_consumed`` echo. ``AcpClient`` writes the steering request
fire-and-forget, registers its id, and the turn's dispatch loop settles the
answer: an ``injected`` answer read before a clean ``end_turn`` terminal is
reported as ``EVENT_STEER_CONSUMED`` at that terminal, and anything else leaves
the caller's pending entry for the turn's teardown to queue.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient, _steering_advertised
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KIRO,
    EVENT_COMPLETE,
    EVENT_STEER_CONSUMED,
    MAX_STEERING_ANSWERS,
    MAX_STEERING_TEXT_CHARS,
    JsonRpcMessage,
)
from kiro_crew.dashboard.steer_settle import settle_consumed_steers

_PROMPT_REQ_ID = 7


def _client(tmp_path: Path, backend: str = ACP_BACKEND_CLAUDE) -> AcpClient:
    """A claude client mid-turn on prompt ``_PROMPT_REQ_ID``, steering advertised."""
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    client._session_id = "sess-1"
    client._steering_supported = True
    client._turn_done.clear()
    client._steering_prompt_id = _PROMPT_REQ_ID
    client._steering_serving = _PROMPT_REQ_ID
    client._next_id = 100
    client._read_new_tool_results_sync = lambda: []  # type: ignore[method-assign]
    return client


def _written(client: AcpClient) -> list[dict]:
    return [json.loads(call.args[0]) for call in client._process.stdin.write.call_args_list]


def _fake_loop(client: AcpClient, frames: list[JsonRpcMessage]):
    """A _prompt_loop stand-in that classifies each frame the way the real one does."""

    async def _loop(req_id, timeout):
        for msg in frames:
            yield client._process_message(msg, req_id), msg

    return _loop


async def _steer(client: AcpClient, text: str) -> dict:
    assert await client.steer(text) is True
    return _written(client)[-1]


async def _run_turn(client: AcpClient, frames: list[JsonRpcMessage], req_id: int = _PROMPT_REQ_ID):
    client._prompt_loop = _fake_loop(client, frames)  # type: ignore[method-assign]
    return [ev async for ev in client._dispatch_events(req_id, 5.0)]


def _terminal(stop_reason: str, req_id: int = _PROMPT_REQ_ID) -> JsonRpcMessage:
    return JsonRpcMessage(id=req_id, result={"stopReason": stop_reason})


def _answer(request_id: int, outcome: str = "injected") -> JsonRpcMessage:
    return JsonRpcMessage(id=request_id, result={"outcome": outcome})


class TestSteerRequest:
    @pytest.mark.asyncio
    async def test_claude_steer_writes_session_steering(self, tmp_path):
        client = _client(tmp_path)
        req = await _steer(client, "  also bump the version  ")

        assert req["method"] == "_session/steering"
        assert req["params"]["sessionId"] == "sess-1"
        # Bare text: no <user_message> envelope, which only kiro-cli's echo parser expects.
        assert req["params"]["prompt"] == [{"type": "text", "text": "also bump the version"}]
        # promptRequired only: no ``delivery`` key.
        assert req["params"]["_meta"] == {"steering": {"idleBehavior": "promptRequired"}}
        assert client._steering_requests == {req["id"]: ("also bump the version", _PROMPT_REQ_ID)}
        assert client.last_steer_monotonic > 0.0

    @pytest.mark.asyncio
    async def test_kiro_steer_is_unchanged(self, tmp_path):
        client = _client(tmp_path, backend=ACP_BACKEND_KIRO)
        req = await _steer(client, "hello")

        assert req["method"] == "_session/steer"
        assert req["params"]["message"] == "<user_message>\nhello\n</user_message>"
        assert client._steering_requests == {}

    @pytest.mark.asyncio
    async def test_unadvertised_adapter_is_not_sent_the_request(self, tmp_path):
        client = _client(tmp_path)
        client._steering_supported = False
        assert await client.steer("hello") is False
        assert _written(client) == []
        assert client._steering_requests == {}

    @pytest.mark.asyncio
    async def test_no_running_turn_is_not_sent_the_request(self, tmp_path):
        client = _client(tmp_path)
        client._turn_done.set()
        assert await client.steer("hello") is False
        assert _written(client) == []

    @pytest.mark.asyncio
    async def test_text_over_the_bound_is_refused(self, tmp_path):
        client = _client(tmp_path)
        assert await client.steer("x" * (MAX_STEERING_TEXT_CHARS + 1)) is False
        assert await client.steer("x" * MAX_STEERING_TEXT_CHARS) is True

    @pytest.mark.asyncio
    async def test_too_many_unanswered_requests_are_refused(self, tmp_path):
        client = _client(tmp_path)
        for i in range(MAX_STEERING_ANSWERS - 1):
            client._steering_requests[i] = ("t", _PROMPT_REQ_ID)
        assert await client.steer("last one") is True
        assert await client.steer("one too many") is False
        assert len(client._steering_requests) == MAX_STEERING_ANSWERS

    @pytest.mark.asyncio
    async def test_empty_steer_writes_nothing(self, tmp_path):
        client = _client(tmp_path)
        assert await client.steer("   ") is False
        assert _written(client) == []

    @pytest.mark.asyncio
    async def test_failed_write_keeps_the_registration_and_an_injected_answer_settles(
        self, tmp_path
    ):
        """The adapter may have read a request whose write raised: returning False
        would queue a steer it may be running, so the entry settles in the turn."""
        client = _client(tmp_path)
        client._process.stdin.drain = AsyncMock(side_effect=BrokenPipeError())

        assert await client.steer("hello") is True
        [request_id] = client._steering_requests
        assert client._steering_requests[request_id] == ("hello", _PROMPT_REQ_ID)

        events = await _run_turn(client, [_answer(request_id), _terminal("end_turn")])
        assert [(ev.kind, ev.text) for ev in events] == [
            (EVENT_STEER_CONSUMED, "hello"),
            (EVENT_COMPLETE, ""),
        ]


class TestAdvertisement:
    @pytest.mark.parametrize(
        ("init_resp", "advertised"),
        [
            ({"_meta": {"steering": {"supported": True}}}, True),
            ({"_meta": {"steering": {"supported": False}}}, False),
            ({"_meta": {"steering": {"supported": "true"}}}, False),
            ({"_meta": {"steering": True}}, False),
            ({"_meta": "steering"}, False),
            ({}, False),
        ],
    )
    def test_only_a_true_supported_flag_advertises(self, init_resp, advertised):
        assert _steering_advertised(init_resp) is advertised

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("init_meta", "advertised"),
        [
            ({"_meta": {"steering": {"supported": True}}}, True),
            ({"_meta": {"steering": {"supported": "true"}}}, False),
            ({}, False),
        ],
        ids=["supported", "not-true", "absent"],
    )
    async def test_the_initialize_handshake_records_the_advertisement(
        self, tmp_path, init_meta, advertised
    ):
        """The handshake itself sets the flag ``steer`` reads, from ``initialize``."""
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.drain = AsyncMock()
        client._process = proc
        client._steering_supported = not advertised
        sent: list[str] = []
        responses = [
            {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}, **init_meta},
            {"modes": ["chat"]},
        ]

        async def fake_send(method: str, params: dict) -> int:
            sent.append(method)
            return len(sent)

        async def fake_wait(req_id: int, timeout: float = 50.0, *, method="", expected_mcp=None):
            if req_id <= len(responses):
                return responses[req_id - 1]
            return {"sessionId": "fresh"}

        client._send_request = AsyncMock(side_effect=fake_send)  # type: ignore[method-assign]
        client._wait_for_response = AsyncMock(side_effect=fake_wait)  # type: ignore[method-assign]
        client._drain_notifications = AsyncMock()  # type: ignore[method-assign]
        client._resume_session_id = "prior-session"

        await client._initialize_session()

        assert sent[0] == "initialize"
        assert client._steering_supported is advertised


class TestSteeringAnswer:
    @pytest.mark.asyncio
    async def test_answer_is_classified_without_touching_turn_completion(self, tmp_path):
        client = _client(tmp_path)
        req = await _steer(client, "hello")

        stranger = JsonRpcMessage(id=req["id"] + 100, result={})
        assert client._process_message(_answer(req["id"]), _PROMPT_REQ_ID) == "steering_response"
        assert client._process_message(_terminal("end_turn"), _PROMPT_REQ_ID) == "complete"
        assert client._process_message(stranger, _PROMPT_REQ_ID) == "skip"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_id", [[], {}, ["x"], {"a": 1}, True])
    async def test_a_malformed_id_is_not_a_steering_answer(self, tmp_path, bad_id):
        """An adapter id that is not an int is classified, not hashed into a crash."""
        client = _client(tmp_path)
        req = await _steer(client, "hello")
        frame = JsonRpcMessage(id=bad_id, result={"outcome": "injected"})

        assert client._process_message(frame, _PROMPT_REQ_ID) == "skip"
        events = await _run_turn(client, [frame, _terminal("end_turn")])

        assert [ev.kind for ev in events] == [EVENT_COMPLETE]
        assert req["id"] in client._steering_requests

    @pytest.mark.asyncio
    async def test_injected_is_consumed_at_a_clean_end_turn(self, tmp_path):
        client = _client(tmp_path)
        req = await _steer(client, "  also bump the version  ")

        events = await _run_turn(client, [_answer(req["id"]), _terminal("end_turn")])

        # Reported immediately before the terminal, not when the answer is read.
        assert [ev.kind for ev in events] == [EVENT_STEER_CONSUMED, EVENT_COMPLETE]
        assert events[0].text == "also bump the version"
        assert client._steering_requests == {}
        # The dashboard settles the raw pending entry against that echo.
        pending = ["  also bump the version  ", "an unrelated steer"]
        assert settle_consumed_steers(pending, events[0].text) == ["an unrelated steer"]

    @pytest.mark.asyncio
    async def test_each_proven_steer_is_its_own_echo(self, tmp_path):
        client = _client(tmp_path)
        first = await _steer(client, "one")
        second = await _steer(client, "two")

        events = await _run_turn(
            client, [_answer(first["id"]), _answer(second["id"]), _terminal("end_turn")]
        )

        assert [(ev.kind, ev.text) for ev in events] == [
            (EVENT_STEER_CONSUMED, "one"),
            (EVENT_STEER_CONSUMED, "two"),
            (EVENT_COMPLETE, ""),
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_reason", ["cancelled", "refusal", "max_tokens", ""])
    async def test_nothing_is_consumed_at_any_other_stop_reason(self, tmp_path, stop_reason):
        client = _client(tmp_path)
        req = await _steer(client, "hello")

        events = await _run_turn(client, [_answer(req["id"]), _terminal(stop_reason)])

        assert [ev.kind for ev in events] == [EVENT_COMPLETE]
        assert client._steering_requests == {}

    @pytest.mark.asyncio
    async def test_nothing_is_consumed_when_the_turn_was_cancelled(self, tmp_path):
        """A cancel whose terminal still reads ``end_turn`` settles nothing."""
        client = _client(tmp_path)
        req = await _steer(client, "hello")

        async def _loop(req_id, timeout):
            yield client._process_message(_answer(req["id"]), req_id), _answer(req["id"])
            client._cancelled = True
            yield "complete", _terminal("end_turn")

        client._prompt_loop = _loop  # type: ignore[method-assign]
        events = [ev async for ev in client._dispatch_events(_PROMPT_REQ_ID, 5.0)]

        assert [ev.kind for ev in events] == [EVENT_COMPLETE]

    @pytest.mark.asyncio
    async def test_nothing_is_consumed_when_the_turn_errors(self, tmp_path):
        client = _client(tmp_path)
        req = await _steer(client, "hello")
        error = JsonRpcMessage(id=_PROMPT_REQ_ID, error={"code": -32603, "message": "boom"})

        client._prompt_loop = _fake_loop(client, [_answer(req["id"]), error])  # type: ignore[method-assign]
        seen = []
        with pytest.raises(AcpError):
            async for ev in client._dispatch_events(_PROMPT_REQ_ID, 5.0):
                seen.append(ev.kind)
        assert EVENT_STEER_CONSUMED not in seen

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [
            JsonRpcMessage(result={"outcome": "promptRequired", "reason": "noRunningTurn"}),
            JsonRpcMessage(result={"outcome": "startedNewTurn"}),
            JsonRpcMessage(result={"outcome": "failed"}),
            JsonRpcMessage(result="injected"),
            JsonRpcMessage(error={"code": -32602, "message": "invalid params"}),
        ],
        ids=["promptRequired", "startedNewTurn", "failed", "not-an-object", "error"],
    )
    async def test_non_injected_answers_settle_nothing(self, tmp_path, answer):
        client = _client(tmp_path)
        req = await _steer(client, "hello")
        answer.id = req["id"]

        events = await _run_turn(client, [answer, _terminal("end_turn")])

        # No consumption evidence: the pending steer is queued at turn end.
        assert [ev.kind for ev in events] == [EVENT_COMPLETE]
        assert client._steering_requests == {}

    @pytest.mark.asyncio
    async def test_a_proof_does_not_outlive_its_turn(self, tmp_path):
        """An injected answer on a turn that ended uncleanly is not reported by the
        next turn's clean terminal."""
        client = _client(tmp_path)
        req = await _steer(client, "hello")
        assert [
            ev.kind for ev in await _run_turn(client, [_answer(req["id"]), _terminal("cancelled")])
        ] == [EVENT_COMPLETE]

        client._turn_done.clear()
        events = await _run_turn(client, [_terminal("end_turn", 9)], req_id=9)
        assert [ev.kind for ev in events] == [EVENT_COMPLETE]

    @pytest.mark.asyncio
    async def test_a_late_answer_for_an_older_prompt_is_dropped(self, tmp_path):
        client = _client(tmp_path)
        req = await _steer(client, "hello")
        # Prompt 7 ends with the answer still owed; the entry stays registered.
        assert [ev.kind for ev in await _run_turn(client, [_terminal("end_turn")])] == [
            EVENT_COMPLETE
        ]
        assert req["id"] in client._steering_requests

        # It arrives during prompt 9: recognised, dropped, and never reported.
        client._turn_done.clear()
        events = await _run_turn(client, [_answer(req["id"]), _terminal("end_turn", 9)], req_id=9)
        assert [ev.kind for ev in events] == [EVENT_COMPLETE]
        assert client._steering_requests == {}

    @pytest.mark.asyncio
    async def test_entries_older_than_the_previous_prompt_are_forgotten(self, tmp_path):
        client = _client(tmp_path)
        req = await _steer(client, "hello")
        await _run_turn(client, [_terminal("end_turn")])
        await _run_turn(client, [_terminal("end_turn", 9)], req_id=9)
        assert req["id"] in client._steering_requests  # aimed at the previous prompt

        await _run_turn(client, [_terminal("end_turn", 11)], req_id=11)
        assert client._steering_requests == {}

    @pytest.mark.asyncio
    async def test_a_new_steer_is_aimed_at_the_prompt_being_served(self, tmp_path):
        client = _client(tmp_path)
        await _run_turn(client, [_terminal("end_turn")])
        client._turn_done.clear()
        aimed: list[tuple[str, int]] = []

        async def _loop(req_id, timeout):
            req = await _steer(client, "hello")
            aimed.append(client._steering_requests[req["id"]])
            yield "complete", _terminal("end_turn", req_id)

        client._prompt_loop = _loop  # type: ignore[method-assign]
        [ev async for ev in client._dispatch_events(9, 5.0)]
        assert aimed == [("hello", 9)]


class TestServedPrompt:
    """A steering request is sent only while a dispatch loop serves a prompt."""

    @pytest.mark.asyncio
    async def test_a_steer_while_the_prompt_is_written_is_refused(self, tmp_path):
        """``stream_events`` clears ``_turn_done`` before writing the prompt, so the
        turn reads as active while the loop has not started yet."""
        client = _client(tmp_path)
        client._steering_serving = None
        client._turn_done.set()
        refused: list[bool] = []

        async def _send_prompt(message):
            assert client.has_active_turn() is True
            refused.append(await client.steer("too early") is False)
            return 9

        client.ensure_ready = AsyncMock()  # type: ignore[method-assign]
        client._send_prompt = _send_prompt  # type: ignore[method-assign]
        client._prompt_loop = _fake_loop(client, [_terminal("end_turn", 9)])  # type: ignore[method-assign]

        events = [ev async for ev in client.stream_events("hi", timeout=5.0)]

        assert refused == [True]
        assert _written(client) == []
        assert client._steering_requests == {}
        assert [ev.kind for ev in events] == [EVENT_COMPLETE]

    @pytest.mark.asyncio
    async def test_a_steer_after_the_loop_exits_is_refused(self, tmp_path):
        client = _client(tmp_path)
        await _run_turn(client, [_terminal("end_turn")])
        # Only the served-prompt mark stands between this steer and the wire.
        client._turn_done.clear()
        assert client.has_active_turn() is True
        assert await client.steer("too late") is False
        assert _written(client) == []

    @pytest.mark.asyncio
    async def test_the_served_prompt_is_cleared_when_the_loop_raises(self, tmp_path):
        client = _client(tmp_path)
        error = JsonRpcMessage(id=_PROMPT_REQ_ID, error={"code": -32603, "message": "boom"})
        with pytest.raises(AcpError):
            await _run_turn(client, [error])
        assert client._steering_serving is None

    @pytest.mark.asyncio
    async def test_the_served_prompt_is_cleared_when_the_consumer_closes(self, tmp_path):
        client = _client(tmp_path)
        client._prompt_loop = _fake_loop(  # type: ignore[method-assign]
            client, [JsonRpcMessage(method="session/update", params={}), _terminal("end_turn")]
        )
        events = client._dispatch_events(_PROMPT_REQ_ID, 5.0)
        client._extract_text_chunk = lambda msg: ("partial", False)  # type: ignore[method-assign]
        await events.__anext__()
        assert client._steering_serving == _PROMPT_REQ_ID
        await events.aclose()
        assert client._steering_serving is None


class TestProviderWrapper:
    @pytest.mark.asyncio
    async def test_the_provider_wrapper_refuses_a_claude_steer(self, tmp_path):
        """Channels, Side Chat and ``spawn_steer`` steer AcpProvider, which refuses
        for claude, so they queue; only the dashboard composer steers the client."""
        from kiro_crew.providers.acp import AcpProvider

        inner = _client(tmp_path)
        outer = AcpProvider.__new__(AcpProvider)
        outer._client = inner  # type: ignore[assignment]

        assert inner.supports_steer is True
        assert inner.supports_refusal_steer is False
        assert inner.steer_needs_loss_recovery is True
        assert outer.supports_steer is False
        assert await outer.steer("hi") is False
        assert _written(inner) == []


class TestSteeringLogs:
    @pytest.mark.asyncio
    async def test_started_new_turn_is_a_warning(self, tmp_path, caplog):
        """``promptRequired`` rules this answer out, so seeing it is not routine."""
        client = _client(tmp_path)
        req = await _steer(client, "hello")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            await _run_turn(client, [_answer(req["id"], "startedNewTurn"), _terminal("end_turn")])
        assert any("startedNewTurn" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_started_new_turn_cancels_the_adapter_owned_turn(self, tmp_path):
        client = _client(tmp_path)
        req = await _steer(client, "hello")

        events = await _run_turn(
            client, [_answer(req["id"], "startedNewTurn"), _terminal("end_turn")]
        )

        assert [ev.kind for ev in events] == [EVENT_COMPLETE]
        assert client._steering_requests == {}
        assert _written(client)[-1] == {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": "sess-1"},
        }
        # The serving turn is not marked cancelled by it.
        assert client._cancelled is False

    @pytest.mark.asyncio
    async def test_a_started_new_turn_for_an_older_prompt_sends_no_cancel(self, tmp_path):
        """``session/cancel`` names no turn, so it would hit the prompt now running."""
        client = _client(tmp_path)
        req = await _steer(client, "hello")
        await _run_turn(client, [_terminal("end_turn")])
        writes = len(_written(client))

        # Prompt 7's answer arrives while prompt 9 runs.
        client._turn_done.clear()
        await _run_turn(
            client, [_answer(req["id"], "startedNewTurn"), _terminal("end_turn", 9)], req_id=9
        )

        assert client._steering_requests == {}
        assert len(_written(client)) == writes

    @pytest.mark.asyncio
    async def test_a_failed_cancel_write_does_not_end_the_turn(self, tmp_path, caplog):
        client = _client(tmp_path)
        req = await _steer(client, "hello")
        client._process.stdin.write.side_effect = BrokenPipeError()

        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            events = await _run_turn(
                client, [_answer(req["id"], "startedNewTurn"), _terminal("end_turn")]
            )

        assert [ev.kind for ev in events] == [EVENT_COMPLETE]
        assert client._steering_requests == {}
        assert any("cancel of adapter-owned turn" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_write_failure_names_a_dead_process(self, tmp_path, caplog):
        client = _client(tmp_path)
        client._process.stdin.drain = AsyncMock(side_effect=BrokenPipeError())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            assert await client.steer("hello") is True
        [record] = [r for r in caplog.records if "write failed" in r.getMessage()]
        assert "the ACP process died" in record.getMessage()
