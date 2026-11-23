"""A parked session queue keeps one copy of each re-sent state frame.

``session_frame_queue.SessionFrameQueue.drop_superseded_by`` runs on every frame
the reader loop queues while nothing reads the queue. These tests pin that the
queue stays bounded under a stream of re-sent snapshots, that only a redundant
frame ever leaves, and that the between-turns reader (the sign-in tracker) ends
in the same state as it does reading every frame.
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from session_handle_helpers import bare_session_handle

from kiro_crew.acp.session_frame_queue import (
    SessionFrameQueue,
    drop_superseded_if_parked,
    set_parked,
)
from kiro_crew.acp.types import METHOD_KAS_MCP_STATUS, METHOD_SESSION_UPDATE, JsonRpcMessage


def _status(*servers: dict, session_id: str = "s1") -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS,
        params={"sessionId": session_id, "servers": [dict(s) for s in servers]},
    )


def _server(name: str, status: str = "connected", tools: int = 3, **extra) -> dict:
    return {
        "name": name,
        "status": status,
        "tools": [{"name": f"{name}_{i}", "description": "d"} for i in range(tools)],
        "prompts": [],
        **extra,
    }


def _options(current: str, session_id: str = "s1") -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "config_option_update",
                "configOptions": [{"id": "model", "type": "select", "currentValue": current}],
            },
        },
    )


def _chunk() -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={"sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk"}},
    )


def _queue_and_put(frames: list[JsonRpcMessage]) -> SessionFrameQueue:
    queue = SessionFrameQueue()
    for msg in frames:
        queue.drop_superseded_by(msg)
        queue.put_nowait(msg)
    return queue


def _drain(queue: SessionFrameQueue) -> list[JsonRpcMessage]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
        queue.task_done()
    return out


@pytest.mark.asyncio
async def test_a_stream_of_re_sent_state_keeps_one_of_each_and_the_newest():
    frames = []
    for i in range(50):
        frames.append(_status(_server("core", tools=3 + i % 2)))
        frames.append(_options(f"m{i}"))
    queue = _queue_and_put(frames)

    assert queue.qsize() == 2
    kept = _drain(queue)
    assert kept == [frames[-2], frames[-1]]
    # Every unread frame that left was not owed a task_done().
    await asyncio.wait_for(queue.join(), timeout=60)


def test_a_snapshot_that_changes_a_server_state_is_kept():
    connecting = _status(_server("remote", status="failed", failedAuthorization=True))
    connected = _status(_server("remote", status="connected"))
    queue = _queue_and_put([connecting, connected])
    assert _drain(queue) == [connecting, connected]


def test_a_changed_error_or_a_new_server_keeps_the_older_snapshot():
    a = _status(_server("core", status="failed", errorMessage="boom"))
    b = _status(_server("core", status="failed", errorMessage="other"))
    c = _status(_server("core", status="failed", errorMessage="other"), _server("web"))
    queue = _queue_and_put([a, b, c])
    assert _drain(queue) == [a, b, c]


def test_a_state_the_reader_must_pass_through_is_never_skipped():
    """failed -> connected -> failed again: the recovery between them is kept."""
    failed = _status(_server("remote", status="failed", failedAuthorization=True))
    connected = _status(_server("remote", status="connected"))
    failed_again = _status(_server("remote", status="failed", failedAuthorization=True))
    queue = _queue_and_put([failed, connected, failed_again])
    assert _drain(queue) == [failed, connected, failed_again]

    # The same holds with an options update between them.
    queue = _queue_and_put([failed, _options("m1"), connected, _options("m2"), failed_again])
    assert [m.method for m in _drain(queue)].count(METHOD_KAS_MCP_STATUS) == 3


def test_a_recovery_between_two_equal_failures_still_completes_the_sign_in():
    frames = [
        _status(_server("remote", status="failed", failedAuthorization=True)),
        _status(_server("remote", status="connected")),
        _status(_server("remote", status="failed", failedAuthorization=True)),
    ]
    every, coalesced = _sign_in_handle(), _sign_in_handle()
    for msg in copy.deepcopy(frames):
        every._note_mcp_sign_in_status(msg, offer=False)
    for msg in _drain(_queue_and_put(copy.deepcopy(frames))):
        coalesced._note_mcp_sign_in_status(msg, offer=False)
    assert "remote" in every._mcp_sign_in_completed
    assert _tracker_state(coalesced) == _tracker_state(every)


def test_no_frame_moves_past_another_kind_of_frame():
    first = _status(_server("core"))
    chunk = _chunk()
    second = _status(_server("core"))
    queue = _queue_and_put([first, chunk, second])
    assert _drain(queue) == [first, chunk, second]


def test_an_options_update_is_replaced_across_a_status_frame_but_not_across_others():
    old = _options("m1")
    status = _status(_server("core"))
    new = _options("m2")
    queue = _queue_and_put([old, status, new])
    assert _drain(queue) == [status, new]

    queue = _queue_and_put([old, _chunk(), new])
    assert len(_drain(queue)) == 3


def test_a_response_or_another_sessions_frame_is_never_dropped():
    response = JsonRpcMessage(id=4, result={"servers": []})
    other = _status(_server("core"), session_id="s2")
    mine = _status(_server("core"))
    queue = _queue_and_put([response, other, mine])
    assert _drain(queue) == [response, other, mine]


def test_a_queue_is_born_unparked_and_only_a_parked_one_drops_frames():
    queue = SessionFrameQueue()
    assert queue.parked is False
    for msg in (_status(_server("core")), _status(_server("core"))):
        assert drop_superseded_if_parked(queue, msg) is False
        queue.put_nowait(msg)
    assert queue.qsize() == 2

    set_parked(queue, True)
    assert drop_superseded_if_parked(queue, _status(_server("core"))) is True
    assert queue.qsize() == 1

    # Any other queue is left alone.
    plain: asyncio.Queue = asyncio.Queue()
    set_parked(plain, True)
    assert not hasattr(plain, "parked")
    assert drop_superseded_if_parked(plain, _status(_server("core"))) is False


def _sign_in_handle():
    handle = bare_session_handle()
    handle._session_id = "s1"
    handle._runtime = SimpleNamespace(begin_mcp_sign_in=lambda *_: True)
    for name in (
        "_mcp_sign_in_needed",
        "_mcp_sign_in_wanted",
        "_mcp_sign_in_asked",
        "_mcp_sign_in_completed",
        "_mcp_reattempt_waiting",
        "_mcp_reattempt_dropped",
        "_mcp_sign_in_probed",
        "_mcp_sign_in_spent",
        "_oauth_emitted_servers",
    ):
        setattr(handle, name, set())
    handle._mcp_sign_in_ask = ""
    handle._mcp_sign_in_dropped = 0
    handle._mcp_reattempt_counts = {}
    handle._mcp_sign_in_probes = {}
    handle._mcp_launches = {}
    handle._mcp_network_urls = {}
    handle._session_key = ""
    return handle


def _tracker_state(handle) -> tuple:
    return (
        set(handle._mcp_sign_in_needed),
        set(handle._mcp_sign_in_completed),
        set(handle._mcp_sign_in_spent),
        set(handle._mcp_reattempt_waiting),
    )


@pytest.mark.parametrize("tracked", [False, True])
def test_the_sign_in_tracker_ends_where_it_ends_reading_every_frame(tracked):
    frames = [
        _status(_server("core"), _server("remote", status="failed", failedAuthorization=True)),
        _status(_server("core"), _server("remote", status="failed", failedAuthorization=True)),
        _options("m1"),
        _status(_server("core"), _server("remote", status="connected")),
        _options("m2"),
        _status(_server("core", tools=5), _server("remote", status="connected")),
        _status(_server("core", tools=5), _server("remote", status="connected")),
        _status(_server("core", status="disabled")),
        _status(_server("core", status="disabled")),
    ]
    every, coalesced = _sign_in_handle(), _sign_in_handle()
    if tracked:
        every._mcp_sign_in_needed.add("core")
        coalesced._mcp_sign_in_needed.add("core")

    for msg in copy.deepcopy(frames):
        every._note_mcp_sign_in_status(msg, offer=False)
    queue = _queue_and_put(copy.deepcopy(frames))
    kept = _drain(queue)
    for msg in kept:
        coalesced._note_mcp_sign_in_status(msg, offer=False)

    assert len(kept) < len(frames)
    assert "remote" in every._mcp_sign_in_completed, "the frames exercise no sign-in"
    assert _tracker_state(coalesced) == _tracker_state(every)
