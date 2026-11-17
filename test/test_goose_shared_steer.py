"""A goose chat on the shared runtime steers into its running turn.

goose takes a user's mid-turn message over its own ``_goose/unstable/session/steer``
request (``{sessionId, expectedRunId, prompt}``, answered ``{runId, messageId}``) and
never advertises ``_session/steering``. The shared handle only spoke the latter, so on
a shared goose chat every mid-turn correction was queued as the next turn while the
same chat on its own ``AcpClient`` steered it in. These pin the handle's goose path:
it is offered, it names the announced run, it reports the steer consumed on goose's
``_meta.goose.steer`` echo, and it falls back to the queue when there is no run.
Reverting ``_steer_via_goose_request`` and its wiring turns the first three red.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_GOOSE,
    EVENT_STEER_CONSUMED,
    METHOD_GOOSE_SESSION_STEER,
    METHOD_SESSION_STEERING,
    JsonRpcMessage,
    wrap_user_message,
)


class _Runtime:
    """The runtime calls a steer makes, recorded; goose advertises no ``_session/steering``."""

    def __init__(self) -> None:
        self.acp_backend = ACP_BACKEND_GOOSE
        self.initialize_result: dict[str, Any] = {"protocolVersion": 1}
        self.is_alive = lambda: True
        self.is_cloud_sandbox = False
        self.answer: asyncio.Future = asyncio.get_running_loop().create_future()
        self.requests: list[tuple[str, dict]] = []
        self.forgotten: list[asyncio.Future] = []

    async def send_request_for_answer(self, method: str, params: dict, on_registered=None):
        self.requests.append((method, params))
        if on_registered is not None:
            on_registered(self.answer)
        return self.answer

    def forget_request(self, future) -> None:
        self.forgotten.append(future)
        if not future.done():
            future.cancel()


def _handle(rt: _Runtime) -> AcpSessionHandle:
    handle = AcpSessionHandle("sess-1", asyncio.Queue(), rt)
    # What ``prompt()`` does at turn start: the turn is open until done.
    handle._turn_done.clear()
    handle._prompt_starts += 1
    handle._prompt_written = True
    return handle


def _info(run_id: str | None) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "session_info_update",
                "_meta": {"goose": {"activeRunId": run_id}},
            },
        },
    )


def _echo(message_id: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": "use the other file"},
                "_meta": {"goose": {"steer": True, "messageId": message_id}},
            },
        },
    )


@pytest.mark.asyncio
async def test_a_shared_goose_chat_is_offered_a_steer():
    assert _handle(_Runtime()).supports_steer is True


@pytest.mark.asyncio
async def test_a_steer_names_the_announced_run_and_settles_on_gooses_echo():
    rt = _Runtime()
    rt.answer.set_result({"runId": "run-7", "messageId": "m-1"})
    handle = _handle(rt)
    handle._note_goose_run(_info("run-7"))

    assert await handle.steer("  use the other file  ") is True
    assert rt.requests == [
        (
            METHOD_GOOSE_SESSION_STEER,
            {
                "sessionId": "sess-1",
                "expectedRunId": "run-7",
                "prompt": [{"type": "text", "text": "use the other file"}],
            },
        )
    ]
    assert all(method != METHOD_SESSION_STEERING for method, _ in rt.requests)
    # Another message's echo settles nothing; this steer's echo is it, once.
    assert handle._goose_steer_echo(_echo("m-other")) is None
    event = handle._goose_steer_echo(_echo("m-1"))
    assert event is not None and event.kind == EVENT_STEER_CONSUMED
    assert event.text == wrap_user_message("use the other file")
    assert handle._goose_steer_echo(_echo("m-1")) is None


@pytest.mark.asyncio
async def test_the_turns_dispatch_reads_the_run_id_and_the_echo():
    """The handle's turn loop reads both off goose's ``session/update`` frames."""
    import ast
    import inspect
    import textwrap

    from kiro_crew.acp.session_handle_dispatch import _dispatch_events

    tree = ast.parse(textwrap.dedent(inspect.getsource(_dispatch_events)))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert {"_note_goose_run", "_goose_steer_echo"} <= called


@pytest.mark.asyncio
async def test_no_run_or_a_refused_steer_is_queued():
    rt = _Runtime()
    handle = _handle(rt)
    # No run announced yet: nothing is sent, and the caller queues the text.
    assert await handle.steer("later") is False
    assert rt.requests == []
    # The run ended (goose sends a null id): queued again.
    handle._note_goose_run(_info("run-1"))
    handle._note_goose_run(_info(None))
    assert await handle.steer("later") is False
    # An older goose answers -32601: queued, and nothing is left to settle.
    handle._note_goose_run(_info("run-2"))
    rt.answer.set_exception(RuntimeError("method not found"))
    assert await handle.steer("later") is False
    assert handle._goose_steers == []


def test_both_drivers_read_gooses_steer_frames_with_one_reader():
    """``_goose/unstable/*`` is unstable: a change to its frames is fixed in one place.

    ``AcpClient`` and the shared handle both read the run id and the steer echo
    through ``steering_wire``; neither parses the frame itself.
    """
    import ast
    import inspect
    import textwrap

    from kiro_crew.acp import client_turn_control as client
    from kiro_crew.acp import session_handle_steering as handle
    from kiro_crew.acp import steering_wire
    from kiro_crew.acp.types import UPDATE_SESSION_INFO, UPDATE_USER_MESSAGE_CHUNK

    def called(fn) -> set[str]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        return {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

    assert "goose_announced_run" in called(client._note_active_run)
    assert "goose_announced_run" in called(handle._note_goose_run)
    assert "goose_steer_echo_id" in called(client._steer_echo_event)
    assert "goose_steer_echo_id" in called(handle._goose_steer_echo)
    # The reader spells the update kinds itself (types imports it); they match.
    assert steering_wire.goose_announced_run(_info("r").params) == (True, "r")
    assert _info("r").params["update"]["sessionUpdate"] == UPDATE_SESSION_INFO
    assert steering_wire.goose_steer_echo_id(_echo("m").params) == "m"
    assert _echo("m").params["update"]["sessionUpdate"] == UPDATE_USER_MESSAGE_CHUNK
