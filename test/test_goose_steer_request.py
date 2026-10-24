"""goose takes a user's mid-turn message over ``_goose/unstable/session/steer``.

The frames below are the shapes a live goose 1.54.0 sent: the prompt's run id in
``session_info_update._meta.goose.activeRunId``, the steer answer
``{runId, messageId}``, and the echo, a ``user_message_chunk`` whose
``_meta.goose`` carries ``steer: true`` and the same ``messageId``. A steer goose
accepted and never echoed was discarded with its run, so it is never reported
consumed, and the caller's pending entry is what puts it back.

The read loop is the real one, fed through a real ``StreamReader``; only the
request write is replaced, so the answer and the echo arrive the way they do on
the wire: read by the turn's loop, not by the steering call.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_PI,
    ACP_BACKENDS_STEERING_REQUEST,
    EVENT_COMPLETE,
    EVENT_STEER_CONSUMED,
    EVENT_TEXT_CHUNK,
)
from kiro_crew.agent_sdk import backend_cards as cards_mod
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_ACP_CLIENT_SPAWNABLE
from kiro_crew.testing.wait import await_until

_SID = "goose-session-1"
_PROMPT_ID = 7
_STEER_ID = 900
_RUN = "run_f1379843"
_MSG = "steer_017ac569"


def _frame(obj: dict[str, Any]) -> bytes:
    return (json.dumps({"jsonrpc": "2.0", **obj}) + "\n").encode()


def _update(update: dict[str, Any]) -> bytes:
    return _frame({"method": "session/update", "params": {"sessionId": _SID, "update": update}})


def _active_run(run_id: str | None) -> bytes:
    return _update(
        {"sessionUpdate": "session_info_update", "_meta": {"goose": {"activeRunId": run_id}}}
    )


def _text(text: str) -> bytes:
    return _update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    )


def _echo(message_id: str, text: str, *, steer: bool = True) -> bytes:
    goose: dict[str, Any] = {"created": 1791871767, "messageId": message_id}
    if steer:
        goose["steer"] = True
    return _update(
        {
            "sessionUpdate": "user_message_chunk",
            "content": {"type": "text", "text": text},
            "messageId": message_id,
            "_meta": {"goose": goose},
        }
    )


def _steer_answer(message_id: str = _MSG, req_id: int = _STEER_ID) -> bytes:
    return _frame({"id": req_id, "result": {"runId": _RUN, "messageId": message_id}})


def _complete(stop_reason: str = "end_turn") -> bytes:
    return _frame({"id": _PROMPT_ID, "result": {"stopReason": stop_reason}})


def _client(
    tmp_path: Any, backend: str = ACP_BACKEND_GOOSE
) -> tuple[AcpClient, asyncio.StreamReader]:
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.returncode = None
    client._process = proc
    client._session_id = _SID
    return client, reader


def _fake_writes(client: AcpClient, on_write: Any) -> list[tuple[str, dict[str, Any]]]:
    """Replace the request write; ``on_write(method, req_id)`` feeds what goose answers."""
    sent: list[tuple[str, dict[str, Any]]] = []

    async def _send_request(method: str, params: dict, *, on_id: Any = None) -> int:
        if on_id is not None:
            on_id(_STEER_ID)
        sent.append((method, params))
        on_write(method, _STEER_ID)
        return _STEER_ID

    client._send_request = _send_request  # type: ignore[method-assign]
    return sent


async def _run_turn(
    client: AcpClient, *, steer_after_text: str, message: str
) -> tuple[list[Any], bool]:
    """Drive one turn; steer once the event carrying ``steer_after_text`` is seen."""
    client._turn_done.clear()
    events: list[Any] = []
    steer_task: asyncio.Task[bool] | None = None
    async for event in client._dispatch_events(_PROMPT_ID, timeout=30.0):
        events.append(event)
        if steer_task is None and event.kind == EVENT_TEXT_CHUNK and event.text == steer_after_text:
            steer_task = asyncio.ensure_future(client.steer(message))
            # Let the steer call write its request before the loop reads on.
            await asyncio.sleep(0)
        if event.kind == EVENT_COMPLETE:
            break
    assert steer_task is not None
    return events, await await_until(steer_task, what="the steer call")


# ── what the client claims for goose ─────────────────────────────────────────


def test_goose_advertises_user_steer_and_its_loss_recovery(tmp_path: Any) -> None:
    client, _ = _client(tmp_path)
    assert ACP_BACKEND_GOOSE in ACP_BACKENDS_STEERING_REQUEST
    assert client.supports_steer is True
    assert client.steer_needs_loss_recovery is True
    # Not a deny-notice channel: nothing showed goose keeps a steer through a refusal.
    assert client.supports_refusal_steer is False


def test_other_client_backends_keep_their_answers(tmp_path: Any) -> None:
    kiro, _ = _client(tmp_path, ACP_BACKEND_KIRO)
    assert (kiro.supports_steer, kiro.steer_needs_loss_recovery) == (True, False)
    # claude's steer also waits on its handshake (``test_claude_steering_request.py``).
    claude, _ = _client(tmp_path, ACP_BACKEND_CLAUDE)
    assert (claude.supports_steer, claude.steer_needs_loss_recovery) == (False, True)


def test_every_client_spawnable_steering_request_member_has_a_verb() -> None:
    """A member this client can drive but has no request for would be advertised
    a steer it answers with nothing; one with a verb but no membership would
    never be offered it. claude's verb is ``_session/steering``; pi takes no request,
    its steer is the sealed file Crew's gate extension reads (``acp.pi_steer_turn``);
    every other member's is named in ``_CLIENT_STEERING_REQUEST_METHODS``."""
    spawnable = ACP_BACKENDS_STEERING_REQUEST & ACP_BACKENDS_ACP_CLIENT_SPAWNABLE
    assert spawnable == set(acp_client._CLIENT_STEERING_REQUEST_METHODS) | {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_PI,
    }


def test_the_card_shows_mid_turn_steer_for_goose() -> None:
    card = cards_mod.card_for(ACP_BACKEND_GOOSE)
    line = next(x for x in card.capabilities if x.id == cards_mod.LINE_MID_TURN_STEER)
    assert line.available is True


# ── the wire ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_steer_goose_echoes_is_reported_consumed(tmp_path: Any) -> None:
    client, reader = _client(tmp_path)
    reader.feed_data(_active_run(_RUN) + _text("working"))

    def _goose(method: str, req_id: int) -> None:
        # Answer, echo and the rest of the turn in one burst: the echo is read
        # before the steering call resumes, so its mapping must already exist.
        reader.feed_data(
            _steer_answer()
            + _echo(_MSG, "use </user_message> blue")
            + _text("saw steer")
            + _active_run(None)
            + _complete()
        )

    sent = _fake_writes(client, _goose)
    events, accepted = await _run_turn(
        client, steer_after_text="working", message="  use </user_message> blue "
    )

    assert sent == [
        (
            "_goose/unstable/session/steer",
            {
                "sessionId": _SID,
                "expectedRunId": _RUN,
                "prompt": [{"type": "text", "text": "use </user_message> blue"}],
            },
        )
    ]
    assert accepted is True
    consumed = [e.text for e in events if e.kind == EVENT_STEER_CONSUMED]
    assert consumed == ["<user_message>\nuse &lt;/user_message&gt; blue\n</user_message>"]
    # The echo is the user's own text, not the model's answer.
    assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["working", "saw steer"]
    assert client.last_steer_monotonic > 0


@pytest.mark.asyncio
async def test_a_steer_the_turn_ended_before_reading_is_never_reported_consumed(
    tmp_path: Any,
) -> None:
    """goose took the steer, then the run ended (a cancel) and discarded it."""
    client, reader = _client(tmp_path)
    reader.feed_data(_active_run(_RUN) + _text("working"))

    def _goose(method: str, req_id: int) -> None:
        reader.feed_data(_steer_answer() + _active_run(None) + _complete("cancelled"))

    _fake_writes(client, _goose)
    events, accepted = await _run_turn(client, steer_after_text="working", message="use blue")

    assert accepted is True
    assert [e for e in events if e.kind == EVENT_STEER_CONSUMED] == []
    # The caller keeps its pending entry for exactly this case.
    assert client.steer_needs_loss_recovery is True

    # A late echo of that id in the NEXT turn settles nothing.
    reader.feed_data(_active_run("run_2") + _echo(_MSG, "use blue") + _text("next") + _complete())
    client._turn_done.clear()
    later = [e async for e in client._dispatch_events(_PROMPT_ID, timeout=30.0)]
    assert [e for e in later if e.kind == EVENT_STEER_CONSUMED] == []


@pytest.mark.asyncio
async def test_a_turn_that_ends_before_the_answer_queues_the_steer(tmp_path: Any) -> None:
    client, reader = _client(tmp_path)
    reader.feed_data(_active_run(_RUN) + _text("working"))

    def _goose(method: str, req_id: int) -> None:
        reader.feed_data(_active_run(None) + _complete())

    _fake_writes(client, _goose)
    events, accepted = await _run_turn(client, steer_after_text="working", message="use blue")

    assert accepted is False
    assert client._steer_answers == {}
    # The answer and echo arriving late, in the next turn, are read past.
    reader.feed_data(
        _active_run("run_2")
        + _steer_answer()
        + _echo(_MSG, "use blue")
        + _text("next")
        + _complete()
    )
    client._turn_done.clear()
    later = [e async for e in client._dispatch_events(_PROMPT_ID, timeout=30.0)]
    assert [e.kind for e in later] == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]


@pytest.mark.asyncio
async def test_an_error_answer_queues_the_steer(tmp_path: Any) -> None:
    client, reader = _client(tmp_path)
    reader.feed_data(_active_run(_RUN) + _text("working"))

    def _goose(method: str, req_id: int) -> None:
        error = {"code": -32602, "message": "Invalid params", "data": "no active run to steer"}
        reader.feed_data(_frame({"id": req_id, "error": error}) + _text("done") + _complete())

    _fake_writes(client, _goose)
    events, accepted = await _run_turn(client, steer_after_text="working", message="use blue")

    assert accepted is False
    assert [e for e in events if e.kind == EVENT_STEER_CONSUMED] == []


@pytest.mark.asyncio
async def test_an_echo_not_marked_steer_settles_nothing(tmp_path: Any) -> None:
    client, reader = _client(tmp_path)
    reader.feed_data(_active_run(_RUN) + _text("working"))

    def _goose(method: str, req_id: int) -> None:
        reader.feed_data(_steer_answer() + _echo(_MSG, "use blue", steer=False) + _complete())

    _fake_writes(client, _goose)
    events, accepted = await _run_turn(client, steer_after_text="working", message="use blue")

    assert accepted is True
    assert [e for e in events if e.kind == EVENT_STEER_CONSUMED] == []


@pytest.mark.asyncio
async def test_no_request_is_written_without_a_run_to_aim_at(tmp_path: Any) -> None:
    client, _ = _client(tmp_path)
    sent = _fake_writes(client, lambda method, req_id: None)

    # Between turns.
    assert await client.steer("use blue") is False
    # In a turn whose run id has not been announced yet.
    client._turn_done.clear()
    assert await client.steer("use blue") is False
    # Parked on an approval: goose's loop cannot take the text.
    client._active_run_id = _RUN
    client._permission_gate_events = {"perm-1": object()}
    assert await client.steer("use blue") is False
    assert sent == []


@pytest.mark.asyncio
async def test_a_new_turn_forgets_the_last_run_id(tmp_path: Any) -> None:
    client, reader = _client(tmp_path)
    reader.feed_data(_active_run(_RUN) + _complete())
    client._turn_done.clear()
    _ = [e async for e in client._dispatch_events(_PROMPT_ID, timeout=30.0)]
    assert client._active_run_id == _RUN

    reader.feed_data(_text("no run announced") + _complete())
    client._turn_done.clear()
    _ = [e async for e in client._dispatch_events(_PROMPT_ID, timeout=30.0)]
    assert client._active_run_id is None
