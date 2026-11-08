"""A Strands session whose backend the CLI disposed: cancelled, reloaded, never put to a person.

The Strands CLI kills its Python worker when a cancel does not stop it within 3s and
then answers that prompt, and every later one on the id, with one ``-32603``. The
frames here are the live capture in ``test/fixtures/acp_frames/strands/``. Each test
drives ``AcpClient``'s own prompt loop with those frames and no process: the reads come
from a list, so nothing waits on a clock.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

from kiro_crew.acp import disposed_session
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_errors import AcpError, AcpProcessDied
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    OUTCOME_CANCELLED,
    STOP_REASON_CANCELLED,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_KIRO,
    ACP_BACKEND_PI,
    ACP_BACKEND_STRANDS,
    ACP_BACKENDS_RELOAD_DISPOSED_SESSION,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "acp_frames" / "strands"


def _frames(name: str) -> list[dict]:
    lines = (_FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _response(name: str, req_id: int) -> dict:
    return next(f for f in _frames(name) if f.get("id") == req_id and "method" not in f)


#: The prompt the cancel hit, and the next prompt on the same process (both -32603).
_KILLED = _response("cancel-dispose-live.jsonl", 3)
_KILLED_AGAIN = _response("cancel-dispose-live.jsonl", 4)
#: The prompt after a cancel that found an approval open: the saved interrupt state.
_WEDGED = _response("cancel-approval-open-live.jsonl", 4)
_PERMISSION = next(
    f
    for f in _frames("cancel-approval-open-live.jsonl")
    if f.get("method") == "session/request_permission"
)
_SESSION = "strands-session-1"


def _client(tmp_path, backend: str = ACP_BACKEND_STRANDS):
    """A client whose reads come from the returned list; *on_read* runs before each one."""
    frames: list[JsonRpcMessage] = []
    hooks: list = []
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    client._session_id = _SESSION
    client.ensure_ready = AsyncMock()
    client._send_response = AsyncMock()
    client._audit_cancelled_approvals = AsyncMock()
    client._is_process_alive = lambda: True

    async def _read(timeout: float = 0.0):
        if hooks:
            hooks.pop(0)()
        return frames.pop(0) if frames else None

    client._read_message = _read
    return client, frames, hooks


def _prompt_id(client: AcpClient, req_id: int) -> None:
    client._send_prompt = AsyncMock(return_value=req_id)
    client._send_request = AsyncMock(return_value=req_id)


def _cancel(client: AcpClient):
    """What ``cancel_session`` leaves on the client once the cancel went out."""

    def _hook() -> None:
        client._cancelled = True

    return _hook


async def _via_stream_events(client: AcpClient) -> list:
    return [e async for e in client.stream_events("next", timeout=5.0)]


async def _via_send_message(client: AcpClient) -> list:
    await client.send_message("next", timeout=5.0)
    return []


async def _via_send_message_stream(client: AcpClient) -> list:
    return [c async for c in client.send_message_stream("next", timeout=5.0)]


_ENTRIES = {
    "stream_events": _via_stream_events,
    "send_message": _via_send_message,
    "send_message_stream": _via_send_message_stream,
}


def test_only_the_texts_the_cli_writes_at_its_disposal_sites_count() -> None:
    assert ACP_BACKENDS_RELOAD_DISPOSED_SESSION == {ACP_BACKEND_STRANDS}
    for frame in (_KILLED, _KILLED_AGAIN):
        assert disposed_session.is_disposed_session_error(ACP_BACKEND_STRANDS, frame["error"])
    exited = {"code": -32603, "data": {"details": "Python agent exited (SIGKILL). boom"}}
    assert disposed_session.is_disposed_session_error(ACP_BACKEND_STRANDS, exited)
    # The saved-interrupt state is not a disposed backend: a reload cannot clear it.
    assert not disposed_session.is_disposed_session_error(ACP_BACKEND_STRANDS, _WEDGED["error"])
    # A model error, another code, or the same text from a harness outside the set.
    other = {"code": -32603, "data": {"details": "ThrottlingException: slow down"}}
    assert not disposed_session.is_disposed_session_error(ACP_BACKEND_STRANDS, other)
    wrong_code = dict(_KILLED["error"], code=-32000)
    assert not disposed_session.is_disposed_session_error(ACP_BACKEND_STRANDS, wrong_code)
    for backend in (ACP_BACKEND_KIRO, ACP_BACKEND_PI):
        assert not disposed_session.is_disposed_session_error(backend, _KILLED["error"])


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", sorted(_ENTRIES))
async def test_a_cancelled_turn_the_cli_killed_ends_cancelled_and_reloads_next(
    tmp_path, entry
) -> None:
    client, frames, hooks = _client(tmp_path)
    _prompt_id(client, 3)
    frames.append(JsonRpcMessage.from_dict(_KILLED))
    hooks.append(_cancel(client))

    events = await _ENTRIES[entry](client)

    if entry == "stream_events":
        complete = [e for e in events if e.kind == EVENT_COMPLETE]
        assert [e.stop_reason for e in complete] == [STOP_REASON_CANCELLED]
    assert client._last_stop_reason == STOP_REASON_CANCELLED
    assert client._stdin_stalled is True
    assert client._resume_session_id == _SESSION


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", sorted(_ENTRIES))
async def test_a_turn_nobody_cancelled_fails_once_with_a_plain_message(tmp_path, entry) -> None:
    client, frames, _hooks = _client(tmp_path)
    _prompt_id(client, 4)
    frames.append(JsonRpcMessage.from_dict(_KILLED_AGAIN))

    with pytest.raises(AcpError) as raised:
        await _ENTRIES[entry](client)

    assert str(raised.value) == disposed_session.DISPOSED_SESSION_TEXT
    assert raised.value.transient is False
    assert client._stdin_stalled is True
    assert client._resume_session_id == _SESSION


@pytest.mark.asyncio
async def test_a_harness_outside_the_set_keeps_its_ordinary_error(tmp_path) -> None:
    client, frames, hooks = _client(tmp_path, backend=ACP_BACKEND_PI)
    _prompt_id(client, 3)
    frames.append(JsonRpcMessage.from_dict(_KILLED))
    hooks.append(_cancel(client))

    with pytest.raises(AcpError) as raised:
        await _via_stream_events(client)

    assert str(raised.value) != disposed_session.DISPOSED_SESSION_TEXT
    assert client._stdin_stalled is False
    assert client._resume_session_id is None


@pytest.mark.asyncio
async def test_an_approval_asked_after_the_cancel_is_answered_cancelled_not_shown(
    tmp_path,
) -> None:
    client, frames, hooks = _client(tmp_path)
    _prompt_id(client, 3)
    hooks.append(_cancel(client))
    frames.append(JsonRpcMessage.from_dict(_PERMISSION))
    frames.append(JsonRpcMessage(id=3, result={"stopReason": STOP_REASON_CANCELLED}))

    events = await _via_stream_events(client)

    assert [e.kind for e in events if e.kind == EVENT_PERMISSION_REQUEST] == []
    client._send_response.assert_awaited_once_with(
        _PERMISSION["id"], {"outcome": {"outcome": OUTCOME_CANCELLED}}
    )
    client._audit_cancelled_approvals.assert_awaited_once_with(
        [_PERMISSION["id"]], cause="asked_after_cancel"
    )


@pytest.mark.asyncio
async def test_an_approval_before_any_cancel_still_reaches_a_person(tmp_path) -> None:
    """Only a request read after the cancel is answered here."""
    client, _frames_, _hooks = _client(tmp_path)

    answered = await disposed_session.answer_late_permission(
        client, JsonRpcMessage.from_dict(_PERMISSION)
    )

    assert answered is False
    client._send_response.assert_not_awaited()


class _LiveProcess:
    returncode = None
    stdin = stdout = stderr = None


def _ready_client(tmp_path):
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_STRANDS)
    client._work_dir_ready = True
    client._process = _LiveProcess()
    client._session_id = _SESSION
    # What settle_disposed_error leaves behind.
    client._stdin_stalled = True
    client._resume_session_id = _SESSION
    client._discard_claude_settings_seed = AsyncMock()
    return client


class _SpawnReached(Exception):
    pass


@pytest.mark.asyncio
async def test_the_next_ensure_ready_replaces_the_process_and_keeps_the_session_to_load(
    tmp_path,
) -> None:
    client = _ready_client(tmp_path)

    async def _kill(*, force: bool = False) -> None:
        assert force is True
        client._process.returncode = -9

    client._kill_process = _kill
    client._spawn = AsyncMock(side_effect=_SpawnReached)

    with pytest.raises(_SpawnReached):
        await client.ensure_ready()

    assert client._process is None
    assert client._stdin_stalled is False
    # Consumed by the initialize that follows the spawn: it sends session/load for it.
    assert client._resume_session_id == _SESSION


@pytest.mark.asyncio
async def test_a_process_that_will_not_die_is_not_replaced_beside_itself(tmp_path) -> None:
    client = _ready_client(tmp_path)
    client._kill_process = AsyncMock()
    client._spawn = AsyncMock(side_effect=_SpawnReached)

    with pytest.raises(AcpProcessDied):
        await client.ensure_ready()

    client._spawn.assert_not_awaited()
    assert client._stdin_stalled is True
    assert client._resume_session_id == _SESSION


# ── Cancel while an approval is open, end to end on the live frames ──
#
# ``cancel-approval-reject-live.jsonl`` is the real Strands CLI behind Crew's sealed
# gate module: the gate asked about ``block``, the client cancelled while that
# approval was open and answered it ``cancelled``, the prompt ended ``cancelled``
# without ``block`` running (the project's run marker stayed absent), and the next
# prompt on the same session answered. ``cancel-approval-open-live.jsonl`` above is
# the same flow before the gate denied the open interrupt: its next prompt got the
# saved-interrupt ``-32603``.

_REJECT = "cancel-approval-reject-live.jsonl"
_REJECT_FRAMES = _frames(_REJECT)


def _reject_frames(prompt_id: int) -> list[JsonRpcMessage]:
    """The agent's frames for the prompt answered as request *prompt_id*, in order."""
    out: list[JsonRpcMessage] = []
    for frame in _REJECT_FRAMES:
        if frame.get("id") in (1, 2) and "method" not in frame:
            continue
        out.append(JsonRpcMessage.from_dict(frame))
        if frame.get("id") == prompt_id and "method" not in frame:
            break
    if prompt_id == 4:
        out = out[out.index(next(m for m in out if m.id == 3 and m.method is None)) + 1 :]
    return out


class _Stdin:
    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


@pytest.mark.asyncio
async def test_a_cancel_while_its_approval_is_open_rejects_it_and_the_next_turn_answers(
    tmp_path, monkeypatch
) -> None:
    from kiro_crew.acp.types import EVENT_TEXT_CHUNK, EVENT_TOOL_CALL_UPDATE

    client, frames, _hooks = _client(tmp_path)
    client._process = type("_Proc", (), {"stdin": _Stdin(), "returncode": None})()

    # Turn 1: the gate asks about ``block``; a person presses Stop on the open card.
    _prompt_id(client, 3)
    frames.extend(_reject_frames(3))
    first: list = []
    async for event in client.stream_events("please block", timeout=5.0):
        first.append(event)
        if event.kind == EVENT_PERMISSION_REQUEST:
            await client.cancel_session()

    asked = [e for e in first if e.kind == EVENT_PERMISSION_REQUEST]
    assert len(asked) == 1
    # The pending approval resolves as a refusal: answered ``cancelled``, audited.
    client._send_response.assert_awaited_once_with(
        asked[0].request_id, {"outcome": {"outcome": OUTCOME_CANCELLED}}
    )
    client._audit_cancelled_approvals.assert_awaited_once_with([asked[0].request_id])
    # The tool never ran: the live CLI sent no tool_call_update for ``block`` (its run
    # marker stayed absent in the capture), and the turn ended cancelled.
    assert not [
        f
        for f in _REJECT_FRAMES
        if (f.get("params") or {}).get("update", {}).get("sessionUpdate") == "tool_call_update"
    ]
    assert not [e for e in first if e.kind == EVENT_TOOL_CALL_UPDATE]
    assert [e.stop_reason for e in first if e.kind == EVENT_COMPLETE] == [STOP_REASON_CANCELLED]

    # Turn 2 on the same session: it answers, and nothing marked the session dead.
    client._send_response.reset_mock()
    _prompt_id(client, 4)
    frames.extend(_reject_frames(4))
    second = [e async for e in client.stream_events("hello again", timeout=5.0)]

    assert "".join(e.text for e in second if e.kind == EVENT_TEXT_CHUNK) == "pong"
    assert [e.stop_reason for e in second if e.kind == EVENT_COMPLETE] == ["end_turn"]
    assert not [e for e in second if e.kind == EVENT_PERMISSION_REQUEST]
    client._send_response.assert_not_awaited()
    assert client._stdin_stalled is False
    assert client._resume_session_id is None
