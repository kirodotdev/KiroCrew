"""MiMo Code on a real ``AcpClient``: shell deferral, unasked-call tripwire, directives."""

from __future__ import annotations

import asyncio
import copy
import glob
import json
from collections import deque
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import session_directive
from kiro_crew.acp import client_permissions, client_turn
from kiro_crew.acp.client import AcpClient, AcpToolGateUnroutable
from kiro_crew.acp.client_timeouts import _MIMOCODE_PERMISSION_REFINEMENT_TIMEOUT
from kiro_crew.acp.harness import mimocode as mimocode_mod
from kiro_crew.acp.harness_tool_names import (
    MIMOCODE_AMBIGUOUS_MCP_TITLE,
    harness_tool_match_names,
    mimocode_mcp_identity,
    mimocode_real_tool_names,
    mimocode_spelling,
    mimocode_unsafe_rule,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    ACP_BACKEND_MIMOCODE,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    METHOD_KIRO_SESSION_UPDATE,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import ACP_BACKEND_OPENCODE
from kiro_crew.dashboard import directive_queue
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.session_directive_apply import DirectiveOutcome
from kiro_crew.hook_runtime.tool_identity import ToolCall
from kiro_crew.hooks import TOOL_DENY, HookManager
from kiro_crew.providers.mirrors.base import SessionProjection
from kiro_crew.providers.mirrors.mimocode import mimocode_tool_id

# ── acp mimocode shell deferral
# MiMo Code (an OpenCode fork) may send a shell ``session/request_permission`` before the
# ``tool_call_update`` with its command. ``_defer_mimocode_shell_permission`` reads one more
# frame (``_MIMOCODE_PERMISSION_REFINEMENT_TIMEOUT``) and moves ahead only a same-session,
# same-call, in-progress update with a nonempty command and no output, so the trusted cache
# has the command first. MiMo's own copy of opencode's, so each branch is pinned here.
# Contract: docs/system-specs/modules/acp-client.md.

_FIXTURE = Path(__file__).parent / "fixtures" / "acp_frames" / "mimocode" / "session-live.jsonl"
_COMMAND = "gh --version"
_SESSION = "ses1"
_CALL = "tc1"
_PERMISSION_ID = 99
#: Lost-run bound on every await the test unblocks.
_LOST_RUN_TIMEOUT = 30.0
#: Prompt deadline; ``remaining`` undercuts the refinement bound only where a test means it.
_PROMPT_TIMEOUT = 60.0


def _tool_call(tmp_path: Path) -> JsonRpcMessage:
    """The shell call's opening frame, no command yet."""
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": _SESSION,
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": _CALL,
                "title": "bash",
                "kind": "execute",
                "status": "pending",
                "rawInput": {"cwd": str(tmp_path)},
            },
        },
    )


def _permission() -> JsonRpcMessage:
    """Its inline ``rawInput`` command must never reach the trusted cache."""
    return JsonRpcMessage(
        id=_PERMISSION_ID,
        method="session/request_permission",
        params={
            "sessionId": _SESSION,
            "toolCall": {
                "toolCallId": _CALL,
                "title": "bash",
                "kind": "execute",
                "rawInput": {"command": _COMMAND},
            },
            "options": [{"optionId": "reject", "kind": "reject_once"}],
        },
    )


def _command_update(tmp_path: Path) -> JsonRpcMessage:
    """Awaited update: same session/call, in progress, a nonempty command, no content/output."""
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": _SESSION,
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": _CALL,
                "title": "bash",
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"command": _COMMAND, "cwd": str(tmp_path)},
            },
        },
    )


def _prompt_response(req_id: int = 1) -> JsonRpcMessage:
    return JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"})


def _spy_mimocode_deferral(client: AcpClient) -> list:
    """Record each MiMo deferral call's request id."""
    calls: list = []
    real = client._defer_mimocode_shell_permission

    async def spy(msg, remaining):
        calls.append(msg.id)
        return await real(msg, remaining)

    client._defer_mimocode_shell_permission = spy
    return calls


async def _drain_turn(client: AcpClient, frames: deque, *, req_id: int = 1) -> list:
    """Run one turn over ``frames`` via ``_dispatch_events``; return its events.
    Reads serve the buffer (as ``_read_message`` does), then ``frames`` (``None``: a
    timeout), then ``None``; with no process, five empty reads end the loop with
    ``AcpProcessDied``. The read cap and outer bound fail other regressions by name."""
    read_cap = len(frames) + 12
    reads = [0]

    async def fake_read(timeout=20.0):
        reads[0] += 1
        if reads[0] > read_cap:
            raise AssertionError(f"prompt loop read {reads[0]} frames from {read_cap} queued")
        if client._buffer:
            return client._buffer.popleft()
        return frames.popleft() if frames else None

    client._read_message = fake_read
    client._read_new_tool_results = AsyncMock(return_value=[])

    async def collect() -> list:
        events = []
        async with aclosing(
            client._dispatch_events(req_id=req_id, timeout=_PROMPT_TIMEOUT)
        ) as stream:
            async for event in stream:
                events.append(event)
        return events

    return await asyncio.wait_for(collect(), timeout=_LOST_RUN_TIMEOUT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,command_expected",
    [
        ("matching", True),
        ("wrong_call", False),
        ("wrong_session", False),
        ("missing_frame", False),
        ("inline_only", False),
        ("output", False),
        ("other_backend", False),
    ],
)
async def test_mimocode_permission_waits_for_matching_command_refinement(
    tmp_path, scenario, command_expected
):
    client = AcpClient(
        work_dir=tmp_path,
        acp_backend=ACP_BACKEND_KIRO if scenario == "other_backend" else ACP_BACKEND_MIMOCODE,
    )
    client._session_id = _SESSION
    update = _command_update(tmp_path)
    frames = deque([_tool_call(tmp_path), _permission(), update, _prompt_response()])
    if scenario == "wrong_call":
        update.params["update"]["toolCallId"] = "tc2"
    elif scenario == "wrong_session":
        update.params["sessionId"] = "ses2"
    elif scenario == "missing_frame":
        frames[2] = None
    elif scenario == "inline_only":
        update.params["update"]["rawInput"] = {"cwd": str(tmp_path)}
    elif scenario == "output":
        update.params["update"]["rawOutput"] = "already ran"
    mimocode_calls = _spy_mimocode_deferral(client)
    # Separate copies: MiMo must reach its own.
    opencode_deferral = AsyncMock(return_value=False)
    client._defer_opencode_shell_permission = opencode_deferral

    events = await _drain_turn(client, frames)

    opencode_deferral.assert_not_awaited()
    assert events[-1].kind == EVENT_COMPLETE
    permission = next(e for e in events if e.kind == EVENT_PERMISSION_REQUEST)
    assert bool(permission.shell_command) is command_expected
    if command_expected:
        assert permission.shell_command == _COMMAND
        assert permission.raw_params_trusted
        assert events.index(
            next(e for e in events if e.kind == EVENT_TOOL_CALL_UPDATE)
        ) < events.index(permission)
        # Once to reorder, once on the re-read finding the command; no double count.
        assert mimocode_calls == [_PERMISSION_ID, _PERMISSION_ID]
        assert client.last_prompt_stats.event_count == 4
    else:
        # Only this call's update fills the trusted cache, never inline rawInput.
        assert permission.raw_tool_params == {"cwd": str(tmp_path)}
        assert mimocode_calls == ([] if scenario == "other_backend" else [_PERMISSION_ID])
        if scenario in ("wrong_call", "inline_only", "output", "other_backend"):
            # The frame read past the request comes back after it.
            updates = [i for i, e in enumerate(events) if e.kind == EVENT_TOOL_CALL_UPDATE]
            assert updates and min(updates) > events.index(permission)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["non_update_frame", "not_tool_call_update"])
async def test_a_frame_the_deferral_does_not_take_keeps_its_place(tmp_path, scenario):
    """The frame read past the request returns to the buffer front, yielded after it."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    client._session_id = _SESSION
    if scenario == "non_update_frame":
        # The prompt response: the turn completes only if handed back.
        frames = deque([_tool_call(tmp_path), _permission(), _prompt_response()])
    else:
        chunk = JsonRpcMessage(
            method="session/update",
            params={
                "sessionId": _SESSION,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "running it"},
                },
            },
        )
        frames = deque(
            [
                _tool_call(tmp_path),
                _permission(),
                chunk,
                _command_update(tmp_path),
                _prompt_response(),
            ]
        )

    events = await _drain_turn(client, frames)

    permission = next(e for e in events if e.kind == EVENT_PERMISSION_REQUEST)
    assert not permission.shell_command
    assert permission.raw_tool_params == {"cwd": str(tmp_path)}
    assert events[-1].kind == EVENT_COMPLETE
    if scenario == "non_update_frame":
        assert [e.kind for e in events] == [
            EVENT_TOOL_CALL,
            EVENT_PERMISSION_REQUEST,
            EVENT_COMPLETE,
        ]
    else:
        text = next(e for e in events if e.kind == EVENT_TEXT_CHUNK)
        assert text.text == "running it"
        assert events.index(permission) < events.index(text)


# ── Each deferral branch, called directly


def _mimocode_shell_client(tmp_path: Path) -> AcpClient:
    """Trusted cache marks the call shell, no command yet."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    client._tool_call_is_shell[_CALL] = True
    client._tool_call_params[_CALL] = {"cwd": str(tmp_path)}
    return client


async def _defer_once(client, request, following, *, remaining=_PROMPT_TIMEOUT):
    """Defer over a read returning ``following``; return (deferred, read timeouts)."""
    timeouts: list = []

    async def fake_read(timeout=20.0):
        timeouts.append(timeout)
        return following

    client._read_message = fake_read
    deferred = await asyncio.wait_for(
        client._defer_mimocode_shell_permission(request, remaining), timeout=_LOST_RUN_TIMEOUT
    )
    return deferred, timeouts


def _same_frames(buffer, expected) -> bool:
    return len(buffer) == len(expected) and all(a is b for a, b in zip(buffer, expected))


def _shape(branch: str, request: JsonRpcMessage, following: JsonRpcMessage, tmp_path: Path):
    """Alter one thing in a matching pair; return the update."""
    update = following.params["update"]
    if branch == "not_session_update":
        # Same payload, other method: not a session/update.
        return JsonRpcMessage(method=METHOD_KIRO_SESSION_UPDATE, params=following.params)
    if branch == "following_params_not_dict":
        return JsonRpcMessage(method="session/update", params=None)
    if branch == "request_without_session_id":
        # No session on either: equal, yet nothing ties them.
        del request.params["sessionId"]
        del following.params["sessionId"]
    elif branch == "wrong_session":
        following.params["sessionId"] = "ses2"
    elif branch == "not_tool_call_update":
        update["sessionUpdate"] = "tool_call"
    elif branch == "wrong_call":
        update["toolCallId"] = "tc2"
    elif branch == "not_in_progress":
        update["status"] = "completed"
    elif branch == "raw_input_not_dict":
        update["rawInput"] = _COMMAND
    elif branch == "command_missing":
        update["rawInput"] = {"cwd": str(tmp_path)}
    elif branch == "command_not_string":
        update["rawInput"] = {"command": ["gh", "--version"]}
    elif branch == "empty_command":
        update["rawInput"] = {"command": ""}
    elif branch == "content":
        update["content"] = [{"type": "content", "content": {"type": "text", "text": "gh 2"}}]
    elif branch == "raw_output":
        update["rawOutput"] = "gh 2"
    else:
        assert branch == "matching", branch
    return following


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "branch",
    [
        "matching",
        "not_session_update",
        "following_params_not_dict",
        "request_without_session_id",
        "wrong_session",
        "not_tool_call_update",
        "wrong_call",
        "not_in_progress",
        "raw_input_not_dict",
        "command_missing",
        "command_not_string",
        "empty_command",
        "content",
        "raw_output",
    ],
)
async def test_the_deferral_takes_only_this_calls_command_update(tmp_path, branch):
    client = _mimocode_shell_client(tmp_path)
    request = _permission()
    following = _shape(branch, request, _command_update(tmp_path), tmp_path)

    deferred, timeouts = await _defer_once(client, request, following)

    assert timeouts == [_MIMOCODE_PERMISSION_REFINEMENT_TIMEOUT]
    if branch == "matching":
        assert deferred is True
        # Update first, so the command is cached before the request.
        assert _same_frames(client._buffer, [following, request])
    else:
        assert deferred is False
        # Handed back alone at the front; the caller keeps the request.
        assert _same_frames(client._buffer, [following])


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["bash", "external_directory"])
async def test_both_command_less_shell_asks_wait_for_the_command(tmp_path, title):
    """0.1.15 ``tool/bash.ts:399-413`` asks ``external_directory`` and ``bash`` with empty
    metadata: neither carries a command."""
    client = _mimocode_shell_client(tmp_path)
    request = _permission()
    request.params["toolCall"]["title"] = title
    following = _command_update(tmp_path)

    deferred, timeouts = await _defer_once(client, request, following)

    assert deferred is True
    assert timeouts == [_MIMOCODE_PERMISSION_REFINEMENT_TIMEOUT]
    assert _same_frames(client._buffer, [following, request])


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["bash_delete", "edit", "actor", "", None])
async def test_a_request_of_another_category_reads_nothing(tmp_path, title):
    """``bash_delete`` asks with ``{command, deletes}`` once ``bash`` is answered; other
    categories are not shell asks. Neither waits."""
    client = _mimocode_shell_client(tmp_path)
    request = _permission()
    if title is None:
        del request.params["toolCall"]["title"]
    else:
        request.params["toolCall"]["title"] = title

    deferred, timeouts = await _defer_once(client, request, _command_update(tmp_path))

    assert deferred is False
    assert timeouts == []
    assert len(client._buffer) == 0
    # The deferral writes nothing to the trusted cache.
    assert client._tool_call_params[_CALL] == {"cwd": str(tmp_path)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "branch",
    [
        "request_params_not_dict",
        "tool_call_not_dict",
        "tool_call_id_not_string",
        "unknown_call",
        "non_shell_call",
        "command_cached",
    ],
)
async def test_the_deferral_reads_nothing_when_it_cannot_or_need_not_wait(tmp_path, branch):
    client = _mimocode_shell_client(tmp_path)
    request = _permission()
    if branch == "request_params_not_dict":
        request.params = None
    elif branch == "tool_call_not_dict":
        request.params["toolCall"] = None
    elif branch == "tool_call_id_not_string":
        # Refused by type even with a cache entry under it.
        request.params["toolCall"]["toolCallId"] = 7
        client._tool_call_is_shell[7] = True
    elif branch == "unknown_call":
        client._tool_call_is_shell.clear()
    elif branch == "non_shell_call":
        client._tool_call_is_shell[_CALL] = False
    else:
        # Update first (session-live.jsonl's recorded order).
        client._tool_call_params[_CALL] = {"command": _COMMAND, "cwd": str(tmp_path)}

    async def no_read(timeout=20.0):
        raise AssertionError("the deferral read a frame")

    client._read_message = no_read
    deferred = await asyncio.wait_for(
        client._defer_mimocode_shell_permission(request, _PROMPT_TIMEOUT), timeout=_LOST_RUN_TIMEOUT
    )

    assert deferred is False
    assert not client._buffer


@pytest.mark.asyncio
async def test_no_frame_within_the_bound_leaves_the_request_to_the_caller(tmp_path):
    client = _mimocode_shell_client(tmp_path)

    deferred, timeouts = await _defer_once(client, _permission(), None)

    assert deferred is False
    assert timeouts == [_MIMOCODE_PERMISSION_REFINEMENT_TIMEOUT]
    assert not client._buffer


@pytest.mark.asyncio
async def test_the_wait_is_bounded_by_mimos_own_constant(tmp_path, monkeypatch):
    # The contract's two-second bound.
    assert _MIMOCODE_PERMISSION_REFINEMENT_TIMEOUT == 2.0
    # Unlike opencode's 2.0, so it shows which constant the MiMo copy reads.
    monkeypatch.setattr(client_turn, "_MIMOCODE_PERMISSION_REFINEMENT_TIMEOUT", 7.5)
    assert client_turn._OPENCODE_PERMISSION_REFINEMENT_TIMEOUT != 7.5
    client = _mimocode_shell_client(tmp_path)

    _deferred, timeouts = await _defer_once(client, _permission(), None)

    assert timeouts == [7.5]


@pytest.mark.asyncio
async def test_a_prompt_deadline_inside_the_bound_caps_the_wait(tmp_path):
    client = _mimocode_shell_client(tmp_path)
    remaining = _MIMOCODE_PERMISSION_REFINEMENT_TIMEOUT / 4

    _deferred, timeouts = await _defer_once(client, _permission(), None, remaining=remaining)

    assert timeouts == [remaining]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "branch,fillers_short_of_capacity,deferred_expected",
    [("buffer_full", 1, False), ("buffer_room_for_both", 2, True)],
)
async def test_the_deferral_never_overflows_the_read_buffer(
    tmp_path, branch, fillers_short_of_capacity, deferred_expected
):
    """``_read_message`` pops the update off the front; two frames go back only if both
    fit, as a full deque drops from its far end."""
    client = _mimocode_shell_client(tmp_path)
    capacity = client._buffer.maxlen
    assert capacity is not None and capacity >= 3
    request = _permission()
    following = _command_update(tmp_path)
    fillers = [
        _prompt_response(req_id=1000 + i) for i in range(capacity - fillers_short_of_capacity)
    ]
    client._buffer.extend([following, *fillers])

    deferred = await asyncio.wait_for(
        client._defer_mimocode_shell_permission(request, _PROMPT_TIMEOUT), timeout=_LOST_RUN_TIMEOUT
    )

    assert deferred is deferred_expected
    expected = [following, request, *fillers] if deferred_expected else [following, *fillers]
    assert _same_frames(client._buffer, expected)
    assert len(client._buffer) == capacity


@pytest.mark.asyncio
async def test_an_unbounded_buffer_always_has_room(tmp_path):
    client = _mimocode_shell_client(tmp_path)
    request = _permission()
    following = _command_update(tmp_path)
    fillers = [_prompt_response(req_id=1000 + i) for i in range(250)]
    client._buffer = deque([following, *fillers])

    deferred = await asyncio.wait_for(
        client._defer_mimocode_shell_permission(request, _PROMPT_TIMEOUT), timeout=_LOST_RUN_TIMEOUT
    )

    assert deferred is True
    assert _same_frames(client._buffer, [following, request, *fillers])


# ── The recorded order


def _recorded_frames() -> list:
    """The capture's second turn (after the first prompt response): request before command."""
    lines = _FIXTURE.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert list(header) == ["_meta"] and header["_meta"]["backend"] == "mimocode"
    frames = [JsonRpcMessage.from_dict(json.loads(line)) for line in lines[1:] if line.strip()]
    first_end = next(
        i
        for i, f in enumerate(frames)
        if f.method is None and isinstance(f.result, dict) and "stopReason" in f.result
    )
    return frames[first_end + 1 :]


def _carries_command(frame: JsonRpcMessage) -> bool:
    if frame.method != "session/update":
        return False
    update = frame.params["update"]
    raw_input = update.get("rawInput")
    return (
        update.get("sessionUpdate") == "tool_call_update"
        and isinstance(raw_input, dict)
        and bool(raw_input.get("command"))
    )


@pytest.mark.asyncio
async def test_the_recorded_request_before_its_command_is_reordered(tmp_path):
    frames = _recorded_frames()
    request_at = next(i for i, f in enumerate(frames) if f.method == "session/request_permission")
    request = frames[request_at]
    command_at = next(i for i, f in enumerate(frames) if _carries_command(f))
    command = frames[command_at].params["update"]["rawInput"]["command"]
    response = frames[-1]
    # Premise: a command-less request before the command update; ends on the prompt response.
    assert request.params["toolCall"]["rawInput"] == {}
    assert request_at < command_at
    assert command == "echo probe-two"
    assert response.method is None and response.result["stopReason"] == "end_turn"
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    client._session_id = request.params["sessionId"]
    mimocode_calls = _spy_mimocode_deferral(client)

    events = await _drain_turn(client, deque(frames), req_id=response.id)

    permission = next(e for e in events if e.kind == EVENT_PERMISSION_REQUEST)
    assert permission.tool_call_id == request.params["toolCall"]["toolCallId"]
    assert permission.shell_command == command
    assert permission.raw_params_trusted is True
    command_update = next(
        e
        for e in events
        if e.kind == EVENT_TOOL_CALL_UPDATE
        and isinstance(e.raw_tool_params, dict)
        and e.raw_tool_params.get("command") == command
    )
    assert events.index(command_update) < events.index(permission)
    assert mimocode_calls == [request.id, request.id]
    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "end_turn"


# ── acp mimocode tripwire frames
# The unasked-call tripwire on 0.1.15 captures from ``test/fixtures/acp_frames/mimocode``,
# replayed in wire order through ``AcpClient._dispatch_events`` on a client armed as
# ``MimoCodeLaunch.resolve_spawn`` leaves it after its routing read-back. The skip-all
# bypass stops the harness; turns whose asks Crew answers run to their end, and stop it
# unanswered. The ``always`` -> ``once`` downgrade is pinned at the stdin frame.

_FRAMES = Path(__file__).parent / "fixtures" / "acp_frames" / "mimocode"
#: Lost-run bound per replay; frames are in memory, so hitting it means hung.
_BOUND = 60.0
#: Prompt id for a capture holding only one turn's tool frames.
_CLOSING_ID = 1000
_TRIPWIRE_SESSION = "mimo-session-1"
_REMEDY = "Something answered or switched off the harness's approvals mid-session"


def _load(name: str) -> list[dict]:
    """The capture's JSON-RPC frames in wire order, minus ``_meta``."""
    frames = []
    for line in (_FRAMES / name).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if "jsonrpc" not in data:
            continue
        frames.append(data)
    return frames


def _turns(name: str) -> list[tuple[int, list[JsonRpcMessage]]]:
    """``(prompt id, frames)`` per turn, each ending at a ``stopReason`` response. Launch
    responses (``initialize``, ``session/new``) are dropped: no prompt loop reads them. A
    tool-frames-only capture is closed under :data:`_CLOSING_ID`."""
    turns: list[tuple[int, list[JsonRpcMessage]]] = []
    current: list[JsonRpcMessage] = []
    for data in _load(name):
        if data.get("method") is None:
            result = data.get("result")
            if isinstance(result, dict) and "stopReason" in result:
                current.append(JsonRpcMessage.from_dict(data))
                turns.append((data["id"], current))
                current = []
            continue
        current.append(JsonRpcMessage.from_dict(data))
    if current:
        current.append(JsonRpcMessage(id=_CLOSING_ID, result={"stopReason": "end_turn"}))
        turns.append((_CLOSING_ID, current))
    return turns


def _permission_frame(name: str) -> JsonRpcMessage:
    """The capture's first ``session/request_permission`` frame."""
    data = next(d for d in _load(name) if d.get("method") == "session/request_permission")
    return JsonRpcMessage.from_dict(data)


def _client(tmp_path: Path, backend: str = ACP_BACKEND_MIMOCODE, *, armed: bool = True):
    """A client as the launch leaves it, stdin writes captured. ``armed`` adds what
    ``MimoCodeLaunch.resolve_spawn`` sets after read-back: the tripwire, and the memory
    root edit tools write under without asking."""
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    client._session_id = _TRIPWIRE_SESSION
    client._session_key = "dashboard:mimo-replay"
    if armed:
        client._mimocode_memory_root = mimocode_mod._mimocode_memory_root(
            {"HOME": str(tmp_path / "home")}
        )
        client._unasked_shell_tripwire = True
    client._kill_process = AsyncMock(return_value=None)
    client._read_new_tool_results = AsyncMock(return_value=[])
    client._process = SimpleNamespace(stdin=object(), returncode=None)
    written: list[dict] = []

    async def _write(data, request_id, *, before_write=None):
        # Bounded-writer contract: recheck right before the bytes.
        if before_write is not None:
            before_write()
        written.append(json.loads(data))

    client._write_response_bounded = _write
    return client, written


def _floor_allows(monkeypatch) -> None:
    """The security floor passes all; it is not under test."""
    monkeypatch.setattr("kiro_crew.permission_floor.refusal_for", lambda event: None)


def _audits(monkeypatch) -> list[dict]:
    """SEL tool records the client writes, as keyword arguments."""
    records: list[dict] = []
    emitter = SimpleNamespace(log_tool_invocation=lambda **kwargs: records.append(kwargs))
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: emitter)
    return records


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=_BOUND))


async def _replay(client: AcpClient, turns, *, answer: bool) -> list:
    """Run the turns through the dispatch loop; return its permission events. With
    *answer*, ``approve_tool`` answers each before the loop reads on, as a gate does."""
    permissions = []
    for req_id, frames in turns:
        queue = deque(frames)

        async def _read(timeout: float = 20.0, _queue=queue):
            if client._buffer:
                return client._buffer.popleft()
            if not _queue:
                raise AssertionError("the capture ran out before its turn ended")
            return _queue.popleft()

        client._read_message = _read
        async with aclosing(client._dispatch_events(req_id=req_id, timeout=_BOUND)) as stream:
            async for event in stream:
                if event.kind != EVENT_PERMISSION_REQUEST:
                    continue
                permissions.append(event)
                if answer:
                    assert await client.approve_tool(event.request_id) is True
    return permissions


def _options(written: list[dict]) -> list[tuple]:
    """``(request id, optionId)`` per answer written to stdin."""
    return [(frame["id"], frame["result"]["outcome"]["optionId"]) for frame in written]


def _trips(audits: list[dict]) -> list[tuple]:
    return [(a["tool_name"], a["outcome"], a["metadata"]["reason"]) for a in audits]


# ── M2: the tripwire on the recorded frames


def test_the_recorded_skip_all_bypass_stops_the_harness(tmp_path, monkeypatch) -> None:
    """After ``POST /permission/skip-all`` a shell call ``completed`` with no ask, so the
    launch's tripwire ends it."""
    client, written = _client(tmp_path)
    audits = _audits(monkeypatch)
    with pytest.raises(AcpToolGateUnroutable) as raised:
        _run(_replay(client, _turns("skip-all-bypass-live.jsonl"), answer=True))
    message = str(raised.value)
    assert "MiMo Code ran shell call call-1 without asking Kiro Crew's gate" in message
    assert _REMEDY in message and "loopback permission route" in message
    client._kill_process.assert_awaited_once_with(force=True)
    assert _trips(audits) == [("call-1", "ran_without_gate", "unasked_shell_tripwire")]
    assert written == []


def test_the_same_bypass_on_an_unarmed_client_runs_through(tmp_path, monkeypatch) -> None:
    """Control: unarmed, the bypass is an ordinary turn, so the arming causes the trip."""
    client, written = _client(tmp_path, armed=False)
    audits = _audits(monkeypatch)
    assert _run(_replay(client, _turns("skip-all-bypass-live.jsonl"), answer=True)) == []
    client._kill_process.assert_not_awaited()
    assert audits == [] and written == []


#: Per capture, each yielded ask as ``(request id, shell command)``. A command proves the
#: refinement read-ahead ran (request first: ``session-live``'s second turn).
_ANSWERED = [
    ("session-live.jsonl", [(0, "echo probe-ok"), (1, "echo probe-two")]),
    ("mcp-stdio-mount-live.jsonl", [(0, "")]),
    ("notebook-edit-live.jsonl", [(3, ""), (7, "")]),
    (
        "actor-subagent-live.jsonl",
        [(2, ""), (7, "echo actor-child-ok > ~/mimo-proj/actor-fg.txt")],
    ),
]


@pytest.mark.parametrize(("name", "asks"), _ANSWERED, ids=[case[0] for case in _ANSWERED])
def test_a_recorded_turn_whose_asks_crew_answers_runs_to_its_end(
    tmp_path, monkeypatch, name, asks
) -> None:
    client, written = _client(tmp_path)
    _floor_allows(monkeypatch)
    audits = _audits(monkeypatch)
    permissions = _run(_replay(client, _turns(name), answer=True))
    assert [(p.request_id, p.shell_command or "") for p in permissions] == asks
    # One "once" answer per ask, in ask order.
    assert _options(written) == [(request_id, "once") for request_id, _ in asks]
    client._kill_process.assert_not_awaited()
    assert audits == []
    # Each asked call's terminal frame consumed its gate state.
    assert client._pi_gate_request_tool == {}
    assert client._pi_gate_asked_ids == set()


#: Per capture and first replayed turn: the tripwire's name for the first call ending with
#: its ask outstanding; ``None`` where that call only ``failed``.
_UNANSWERED = [
    ("session-live.jsonl", 0, "shell call call-1"),
    ("mcp-stdio-mount-live.jsonl", 0, "tool call call-1"),
    ("notebook-edit-live.jsonl", 0, "tool call call-1"),
    ("actor-subagent-live.jsonl", 0, "shell call call-2"),
    ("session-live.jsonl", 1, None),
]


@pytest.mark.parametrize(
    ("name", "start", "named"),
    _UNANSWERED,
    ids=[f"{case[0]}-turn{case[1] + 1}" for case in _UNANSWERED],
)
def test_a_recorded_call_completing_before_crew_answered_stops_the_harness(
    tmp_path, monkeypatch, name, start, named
) -> None:
    """Negative control: unanswered, the frames read as ``POST /permission/:id/reply``."""
    client, written = _client(tmp_path)
    _floor_allows(monkeypatch)
    audits = _audits(monkeypatch)
    turns = _turns(name)[start:]
    if named is None:
        # Rejected, it ends ``failed``: nothing ran or trips; the ask is consumed.
        assert len(_run(_replay(client, turns, answer=False))) == 1
        client._kill_process.assert_not_awaited()
        assert audits == [] and client._pi_gate_request_tool == {}
        return
    with pytest.raises(AcpToolGateUnroutable) as raised:
        _run(_replay(client, turns, answer=False))
    message = str(raised.value)
    assert f"MiMo Code ran {named} before Kiro Crew answered its permission request" in message
    assert _REMEDY in message
    client._kill_process.assert_awaited_once_with(force=True)
    call = named.rsplit(" ", 1)[1]
    assert _trips(audits) == [(call, "ran_before_answer", "unasked_shell_tripwire")]
    assert written == []


# ── M9: an "always" answer on a watched session

#: The options every recorded ask advertises (``packages/cli/src/acp/agent.ts``
#: ``permissionOptions``).
_MIMOCODE_OPTIONS = {"once": "once", "always": "always", "reject": "reject"}


def _asked_client(tmp_path, monkeypatch, backend=ACP_BACKEND_MIMOCODE, *, armed=True):
    """A client holding the built ``session-live`` request (id 0) and its options."""
    client, written = _client(tmp_path, backend, armed=armed)
    _floor_allows(monkeypatch)
    assert client._build_permission_event(_permission_frame("session-live.jsonl")) is not None
    assert client._permission_options[0] == _MIMOCODE_OPTIONS
    return client, written


def test_an_explicit_always_option_is_written_as_once_on_an_armed_session(
    tmp_path, monkeypatch
) -> None:
    client, written = _asked_client(tmp_path, monkeypatch)
    assert _run(client.approve_tool(0, option_id="always")) is True
    assert _options(written) == [(0, "once")]


def test_always_true_without_recorded_options_falls_back_to_the_canonical_once(
    tmp_path, monkeypatch
) -> None:
    """With no options on record, the canonical always id is still downgraded."""
    client, written = _asked_client(tmp_path, monkeypatch)
    client._permission_options.pop(0)
    assert _run(client.approve_tool(0, always=True)) is True
    assert _options(written) == [(0, "allow_once")]


def test_an_explicit_always_option_without_recorded_options_is_written_as_once(
    tmp_path, monkeypatch
) -> None:
    client, written = _asked_client(tmp_path, monkeypatch)
    client._permission_options.pop(0)
    assert _run(client.approve_tool(0, option_id="always")) is True
    assert _options(written) == [(0, "once")]


def test_an_unarmed_opencode_session_sends_its_always_option(tmp_path, monkeypatch) -> None:
    """Control: the downgrade is the tripwire's alone."""
    client, written = _asked_client(tmp_path, monkeypatch, ACP_BACKEND_OPENCODE, armed=False)
    assert client._unasked_shell_tripwire is False
    assert _run(client.approve_tool(0, always=True)) is True
    assert _options(written) == [(0, "always")]


def test_a_mimocode_session_without_the_tripwire_sends_its_always_option(
    tmp_path, monkeypatch
) -> None:
    """Control: the flag, not the backend id, decides the downgrade."""
    client, written = _asked_client(tmp_path, monkeypatch, armed=False)
    assert client._unasked_shell_tripwire is False
    assert _run(client.approve_tool(0, option_id="always")) is True
    assert _options(written) == [(0, "always")]


# ── session directive mimocode
# MiMo Code (``mimo acp`` 0.1.15) titles an MCP tool ``sanitize(server) + "_" +
# sanitize(tool)`` (``providers.mirrors.mimocode.mimocode_tool_id``) with no ``_meta``, so
# only the title names a directive tool, which lands out of band.
# ``test/fixtures/acp_frames/mimocode/mcp-stdio-mount-live.jsonl`` titles its call
# ``crew-probe_crew_probe_echo``; the turn retitles its ``tool_call`` (``{}``),
# ``session/request_permission``, ``in_progress`` and untitled ``completed`` frames for a
# ``kirocrew-core`` tool, arguments
# in ``rawInput: part.state.input`` on each ``tool_call_update`` (0.1.15
# ``packages/opencode/src/acp/agent.ts``). MiMo runs on ``AcpClient`` (not in
# ``ACP_BACKENDS_ACP_RUNTIME``): a ``mimocode`` client parses, ``chat_runner._run_chat`` consumes.

MOUNT_CAPTURE = (
    Path(__file__).parent / "fixtures" / "acp_frames" / "mimocode" / "mcp-stdio-mount-live.jsonl"
)
#: The capture's call id and harness-given title.
CAPTURED_CALL_ID = "call-1"
CAPTURED_TITLE = "crew-probe_crew_probe_echo"

# The model's raw arguments, as ``rawInput`` carries them (no ``_meta``).
CALL_ARGS = {
    "message": 'Patrol cycle. Report only real signals; call autonudge_stop with reason "done".',
    "interval_secs": 600,
    "max_cycles": 36,
    "max_runtime_secs": 43200,
    "banner": "Patrolling round 1",
}
# The VALIDATED, parked args: plus a schema default, to differ on purpose.
VALIDATED_ARGS = {**CALL_ARGS, "gate": True}
#: No directive marker: only the call input can claim.
RESULT_TEXT = "Monitor loop requested."


@pytest.fixture
def clean_queue():
    directive_queue.reset()
    yield
    directive_queue.reset()


# ── the MiMo Code turn, from the live mount capture


def _call_body(frame: dict) -> dict:
    """A frame's tool-call object: the update or the permission's call."""
    params = frame.get("params") or {}
    return params.get("update") or params.get("toolCall") or {}


def _captured_call_frames() -> list[dict]:
    """The MCP call's frames in wire order; a re-capture of another shape fails here."""
    lines = MOUNT_CAPTURE.read_text(encoding="utf-8").splitlines()[1:]
    frames = [
        frame
        for frame in (json.loads(line) for line in lines)
        if _call_body(frame).get("toolCallId") == CAPTURED_CALL_ID
    ]
    shape = [
        (
            f["method"],
            _call_body(f).get("sessionUpdate", ""),
            _call_body(f)["status"],
            _call_body(f)["title"],
            _call_body(f)["rawInput"],
        )
        for f in frames
    ]
    assert shape == [
        ("session/update", "tool_call", "pending", CAPTURED_TITLE, {}),
        ("session/request_permission", "", "pending", CAPTURED_TITLE, {}),
        ("session/update", "tool_call_update", "in_progress", CAPTURED_TITLE, {}),
        ("session/update", "tool_call_update", "completed", "", {}),
    ], "the mount capture's call frames changed shape"
    return frames


def _mimocode_directive_frames(title: str) -> list[dict]:
    """The captured call retitled *title* (``completed`` stays untitled), both updates'
    ``rawInput`` ``{}`` set to :data:`CALL_ARGS`, and the result text replaced."""
    frames = copy.deepcopy(_captured_call_frames())
    for frame in frames:
        body = _call_body(frame)
        if body["title"] == CAPTURED_TITLE:
            body["title"] = title
        if body.get("sessionUpdate") == "tool_call_update":
            body["rawInput"] = dict(CALL_ARGS)
        if body.get("status") == "completed":
            body["content"][0]["content"]["text"] = RESULT_TEXT
            body["rawOutput"]["output"] = RESULT_TEXT
    return frames


def _parse_on_a_mimocode_client(tmp_path, frames: list[dict]):
    """Parse as a MiMo ``AcpClient``: ``client_turn``'s extractors in its order (call,
    result, refinement), asks via its builder with its backend. Returns (stream, asks)."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    assert client.backend == ACP_BACKEND_MIMOCODE
    events: list[AcpEvent] = []
    permissions: list[AcpEvent] = []
    for frame in frames:
        msg = JsonRpcMessage.from_dict(frame)
        if frame["method"] == "session/request_permission":
            permissions.append(client._build_permission_event(msg))
            continue
        for extract in (
            client._extract_tool_event,
            client._extract_tool_call_update,
            client._extract_tool_call_refinement,
        ):
            event = extract(msg)
            if event is not None:
                events.append(event)
    return events, permissions


def _turn(events: list[AcpEvent]) -> list[AcpEvent]:
    """*events*, closing text, turn end."""
    return [*events, AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"), AcpEvent(kind=EVENT_COMPLETE)]


# ── harness: the real consumer loop


def _stub_state(tmp_path):
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    state.slack_client = None
    return state


async def _drive(state, slot, events, monkeypatch):
    """Stream *events* through ``_run_chat``; return the apply spy. The record is parked
    mid-turn (the claim refuses older ones) for THIS slot, under the CALL_ARGS digest."""
    from kiro_crew.dashboard import chat_runner

    parked = []

    async def _stream(_msg):
        if not parked:
            parked.append(True)
            directive_queue.publish(
                effective_session_key(slot),
                "monitor_start",
                VALIDATED_ARGS,
                session_directive.call_input_digest("monitor_start", CALL_ARGS),
            )
        for ev in events:
            yield ev

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    spy = AsyncMock(return_value=DirectiveOutcome("[applied]"))
    monkeypatch.setattr(chat_runner, "apply_session_directive_outcome", spy)
    await asyncio.wait_for(
        chat_runner._run_chat(state, slot, "go", _directive_user_origin=True), timeout=30
    )
    task = getattr(slot, "task", None)
    if task is not None:
        await asyncio.wait_for(task, timeout=30)
    assert parked, "the stream was never consumed, so no record was parked"
    return spy


MIMO_MONITOR_START = mimocode_tool_id(session_directive.CORE_MCP_SERVER, "monitor_start")


class TestMimoCodeBackendResolvesTheTool:
    """Every fused directive title must resolve, or a MiMo session with Crew's control
    plane mounted applies none."""

    @pytest.mark.parametrize("tool", sorted(session_directive.DIRECTIVE_TOOLS))
    def test_every_directive_tool_resolves_from_the_mimocode_title(self, tool):
        title = mimocode_tool_id(session_directive.CORE_MCP_SERVER, tool)
        assert session_directive.directive_tool_from_call("", "", title) == tool

    def test_the_mimocode_turn_parses_to_a_call_named_only_by_its_title(self, tmp_path):
        """No ``_meta``: the title names the tool; arguments come on the refinement; no
        result marker. The ask names the call by MiMo Code's split of the fused title."""
        assert MIMO_MONITOR_START == "kirocrew-core_monitor_start"
        events, permissions = _parse_on_a_mimocode_client(
            tmp_path, _mimocode_directive_frames(MIMO_MONITOR_START)
        )
        assert [e.kind for e in events] == [
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            EVENT_TOOL_RESULT,
            EVENT_TOOL_CALL_UPDATE,
        ]
        call, running, result, completed = events
        assert (call.mcp_server_name, call.tool_name) == ("", "")
        assert call.wire_title == MIMO_MONITOR_START
        assert call.raw_tool_params == {}
        resolved = session_directive.directive_tool_from_call(
            call.mcp_server_name, call.tool_name, call.wire_title
        )
        assert resolved == "monitor_start"
        assert running.wire_title == MIMO_MONITOR_START
        assert running.raw_tool_params == CALL_ARGS
        assert result.tool_output == RESULT_TEXT and result.tool_final
        assert session_directive.SENTINEL not in result.tool_output
        assert completed.wire_title == ""
        assert completed.raw_tool_params == CALL_ARGS

        [permission] = permissions
        assert permission.kind == EVENT_PERMISSION_REQUEST
        assert permission.tool_call_id == CAPTURED_CALL_ID
        assert permission.harness_tool_id == (
            "mimocode#@kirocrew-core/monitor_start|@kirocrew-core_monitor/start"
        )
        names = harness_tool_match_names(permission.harness_tool_id)
        assert "@kirocrew-core/monitor_start" in names
        assert "mcp__kirocrew-core__monitor_start" in names

    @pytest.mark.asyncio
    async def test_the_directive_lands_out_of_band_on_a_mimocode_turn(
        self, tmp_path, monkeypatch, caplog, clean_queue
    ):
        """Through the REAL consumer: the fused title records the digest of arguments
        that arrive on ``in_progress`` (``tool_call`` had ``{}``), which claims the record.
        The marker-free result text is not consulted; the ask is not streamed."""
        events, _permissions = _parse_on_a_mimocode_client(
            tmp_path, _mimocode_directive_frames(MIMO_MONITOR_START)
        )
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("mimo-directive")
        slot._titled = True
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"):
            spy = await _drive(state, slot, _turn(events), monkeypatch)
        spy.assert_awaited_once()
        assert spy.call_args.args[3] == "monitor_start"
        assert spy.call_args.args[4] == VALIDATED_ARGS, "the RECORD's payload is applied"
        assert directive_queue.depth(effective_session_key(slot)) == 0
        assert "UNCLAIMED_AT_TURN_END" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_third_party_servers_same_named_tool_claims_nothing(
        self, tmp_path, monkeypatch, caplog, clean_queue
    ):
        """``evil-mcp_monitor_start``'s server half fails: no digest, and the record stays
        parked, named at turn end."""
        title = mimocode_tool_id("evil-mcp", "monitor_start")
        assert title == "evil-mcp_monitor_start"
        events, _permissions = _parse_on_a_mimocode_client(
            tmp_path, _mimocode_directive_frames(title)
        )
        assert events[0].kind == EVENT_TOOL_CALL and events[0].wire_title == title
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("mimo-lookalike")
        slot._titled = True
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"):
            spy = await _drive(state, slot, _turn(events), monkeypatch)
        spy.assert_not_called()
        assert directive_queue.depth(effective_session_key(slot)) == 1, "record left parked"
        warnings = [
            record.getMessage()
            for record in caplog.records
            if "UNCLAIMED_AT_TURN_END" in record.getMessage()
        ]
        assert len(warnings) == 1
        digest = session_directive.call_input_digest("monitor_start", CALL_ARGS)[:12]
        assert "count=1" in warnings[0]
        assert f"record_digests=('monitor_start:{digest}',)" in warnings[0]
        assert f"tool_identities=('{CAPTURED_CALL_ID}:-:-',)" in warnings[0]


# ── an MCP call's server and tool reach governance


def _probe_client(tmp_path, known: tuple[str, ...], placed: tuple[str, ...] = ()) -> AcpClient:
    """MiMo client mounting *known* natively, *placed* from Crew. All native names go in
    ``_mimocode_native_mounts``, rewritten ones in ``_mimocode_config_mcp_servers``."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    client._mimocode_native_mounts = frozenset(known)
    client._mimocode_config_mcp_servers = mimocode_mod._mimocode_config_mcp_server_names(
        {"mcp": {name: {} for name in known}}
    )[0]
    client._placed_mcp_server_names = lambda: placed  # type: ignore[method-assign]
    return client


def _probe_permission(client: AcpClient, title: str = CAPTURED_TITLE, asked: str = "") -> AcpEvent:
    """The call's ask event, both frames titled *title*; *asked* retitles the ask alone."""
    frames = copy.deepcopy(_captured_call_frames())
    tool_call, permission = frames[0], frames[1]
    tool_call["params"]["update"]["title"] = title
    permission["params"]["toolCall"]["title"] = asked or title
    client._extract_tool_event(JsonRpcMessage.from_dict(tool_call))
    event = client._build_permission_event(JsonRpcMessage.from_dict(permission))
    assert event is not None
    return event


def _judged_under_a_ceiling_denying(monkeypatch, ref: str, event: AcpEvent):
    """*event* through the real gate under a real ``mcp`` ceiling denying *ref*."""
    import kiro_crew.hooks as hooks_mod
    from kiro_crew.platform import current_context
    from kiro_crew.platform.governance import (
        MODE_DENY,
        GovernanceCeiling,
        ScopedRuleset,
        parse_policy,
    )

    ceiling = GovernanceCeiling(
        version=1,
        boot=parse_policy({"version": 1, "boot": {"fail_closed": True}}).boot,
        controls={"mcp": ScopedRuleset(mode=MODE_DENY, deny=(ref,), matcher="mcp")},
    )
    real = current_context()
    monkeypatch.setattr(
        hooks_mod,
        "current_context",
        lambda: SimpleNamespace(security=real.security, governance=ceiling),
    )
    return HookManager().on_tool_call(event.title, **ToolCall.from_event(event).kwargs())


@pytest.mark.parametrize(
    ("title", "known", "expected"),
    [
        (CAPTURED_TITLE, ("crew-probe",), ("crew-probe", "crew_probe_echo")),
        ("docs_server_search", ("docs.server",), ("docs.server", "search")),
        (CAPTURED_TITLE, ("other",), None),
        ("", ("crew-probe",), None),
        (CAPTURED_TITLE, ("crew-probe", "crew-probe_crew"), MIMOCODE_AMBIGUOUS_MCP_TITLE),
        ("docs_server_search", ("docs.server", "docs_server"), MIMOCODE_AMBIGUOUS_MCP_TITLE),
        ("skill_search", ("skill",), MIMOCODE_AMBIGUOUS_MCP_TITLE),
    ],
    ids=[
        "one-server",
        "server-spelling",
        "unknown-server",
        "no-title",
        "two-prefixes",
        "one-spelling",
        "native-id",
    ],
)
def test_a_fused_title_is_attributed_to_one_known_server_or_none(title, known, expected) -> None:
    assert mimocode_mcp_identity(title, known) == expected


def test_a_ceiling_denying_the_captured_tool_denies_its_call(tmp_path, monkeypatch) -> None:
    """The fused title's server and tool are a trusted identity, so an ``mcp`` deny on
    ``@crew-probe/crew_probe_echo`` denies the call before an approval could run it."""
    event = _probe_permission(_probe_client(tmp_path, ("crew-probe",)))
    assert (event.mcp_server_name, event.tool_name) == ("crew-probe", "crew_probe_echo")
    assert event.mcp_identity_trusted
    result = _judged_under_a_ceiling_denying(monkeypatch, "@crew-probe/crew_probe_echo", event)
    assert result.action == TOOL_DENY
    result = _judged_under_a_ceiling_denying(monkeypatch, "@crew-probe", event)
    assert result.action == TOOL_DENY


def test_a_plain_named_native_server_is_attributed_too(tmp_path, monkeypatch) -> None:
    """Native ``github`` is kept as is, not in the rewritten-only list; attribution reads
    every native mount, so an ``mcp`` deny on ``@github/create_issue`` binds."""
    client = _probe_client(tmp_path, ("github",))
    assert client._mimocode_config_mcp_servers == ()
    event = _probe_permission(client, "github_create_issue")
    assert (event.mcp_server_name, event.tool_name) == ("github", "create_issue")
    result = _judged_under_a_ceiling_denying(monkeypatch, "@github/create_issue", event)
    assert result.action == TOOL_DENY


@pytest.mark.asyncio
async def test_a_native_server_beside_a_placed_one_makes_the_title_ambiguous(tmp_path) -> None:
    """Crew places ``a``; the harness mounts ``a_b`` serving ``c``; both spell ``a_b_c``."""
    client = _probe_client(tmp_path, ("a_b",), placed=("a",))
    event = _probe_permission(client, "a_b_c")
    assert event.mcp_server_name == ""
    client.reject_tool = AsyncMock()
    client._audit_spec_restriction = MagicMock()
    assert await client._refuse_identity_drift(event) is True
    client.reject_tool.assert_awaited_once_with(event.request_id)


def test_a_title_no_known_server_prefixes_names_no_server(tmp_path) -> None:
    event = _probe_permission(_probe_client(tmp_path, ("other",)))
    assert event.mcp_server_name == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "known", "asked"),
    [
        (CAPTURED_TITLE, ("crew-probe", "crew-probe_crew"), ""),
        ("skill_search", ("skill",), ""),
        (CAPTURED_TITLE, ("crew-probe",), "crew-probe_other"),
    ],
    ids=["two-servers", "native-id", "frames-disagree"],
)
async def test_an_unattributable_mcp_title_is_refused(tmp_path, title, known, asked) -> None:
    """Either reading could pass what the other's rule denies."""
    client = _probe_client(tmp_path, known)
    event = _probe_permission(client, title, asked)
    assert event.mcp_server_name == ""
    client.reject_tool = AsyncMock()
    client._audit_spec_restriction = MagicMock()
    assert await client._refuse_identity_drift(event) is True
    client.reject_tool.assert_awaited_once_with(event.request_id)
    reason = client._audit_spec_restriction.call_args.kwargs["reason"]
    assert reason == "spec_disabled_tool_ambiguous_server"


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [("crew-probe",), ("other",)], ids=["named", "not-mcp"])
async def test_a_named_or_non_mcp_title_is_not_refused(tmp_path, known) -> None:
    client = _probe_client(tmp_path, known)
    event = _probe_permission(client)
    client.reject_tool = AsyncMock()
    assert await client._refuse_identity_drift(event) is False
    client.reject_tool.assert_not_awaited()


def test_no_other_backend_recovers_a_fused_title(tmp_path) -> None:
    """Recovery is MiMo Code's alone (H13): an opencode client keeps its reading."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
    client._opencode_config_mcp_servers = ("crew-probe",)
    event = _probe_permission(client)
    assert event.mcp_server_name == ""


# ── a tool name the harness rewrote is judged by its real name


_DOTTED_DENY = glob.escape("@ops/repo?delete")


@pytest.mark.parametrize(
    ("tool", "narrowing", "widening", "unsafe"),
    [
        ("repo_delete", ["*.delete"], [], "*.delete"),
        ("repo_delete", ["@ops/repo.delete"], [], "@ops/repo.delete"),
        ("repo_delete", ["mcp__ops__repo.delete"], [], "mcp__ops__repo.delete"),
        ("repo_delete", ["Running: @ops/repo.*"], [], "Running: @ops/repo.*"),
        ("repo_delete", ["@OPS/Repo.Delete"], [], "@OPS/Repo.Delete"),
        ("repo__delete", ["@ops/repo?delete"], [], "@ops/repo?delete"),
        ("a_b_c", ["@ops/a.b c"], [], "@ops/a.b c"),
        ("x__y", ["@ops/x\U0001f600y"], [], "@ops/x\U0001f600y"),
        ("repo_delete", [_DOTTED_DENY], [], _DOTTED_DENY),
        ("repo_delete", ["@ops/repo_delete", "@ops/*", "@ops/repo[._]delete"], [], ""),
        ("repo_delete", ["*/.ssh/*", "ops_*", "fs_write"], [], ""),
        ("x_y", ["@ops/x\U0001f600y"], [], ""),
        ("repo_delete", [], ["@ops/repo_delete"], "@ops/repo_delete"),
        ("repo_delete", [], ["@ops/repo?delete"], "@ops/repo?delete"),
        ("repo_delete", [], ["@ops/repo[._]delete"], "@ops/repo[._]delete"),
        ("repo_delete", [], ["@ops/*", "@ops/repo*", "*", "ops_*", "@ops/repo.delete"], ""),
        ("repodelete", ["*.delete"], ["@ops/repodelete"], ""),
    ],
    ids=[
        "unprefixed-glob",
        "dotted",
        "mcp-title-form",
        "running-form",
        "case",
        "two-unit-rewrite",
        "two-characters",
        "astral",
        "literal-governance",
        "deny-met-as-written",
        "no-real-name-matches",
        "astral-needs-two",
        "exact-grant",
        "one-char-grant",
        "class-grant",
        "grants-every-real-name",
        "no-underscore",
    ],
)
def test_the_rules_that_need_the_real_name(tool, narrowing, widening, unsafe) -> None:
    """A deny that may name a real name the title misses, or a grant the title meets
    that another real name need not, in ``fnmatch``'s full grammar."""
    assert mimocode_unsafe_rule("ops", tool, narrowing, widening) == unsafe


@pytest.mark.parametrize(
    ("regex", "unsafe"),
    [(r"repo\.delete", True), (r"repo.delete", False), ("rm -rf", False)],
    ids=["names-a-real-name", "met-as-written", "unrelated"],
)
def test_a_denied_regex_is_tried_at_each_underscore(regex, unsafe) -> None:
    assert bool(mimocode_unsafe_rule("ops", "repo_delete", [], [], [regex])) is unsafe


@pytest.mark.parametrize(
    ("name", "spelled"),
    [
        ("repo.delete", "repo_delete"),
        ("repo_delete", "repo_delete"),
        ("a-b", "a-b"),
        ("文件 读", "____"),
        ("emoji\U0001f600", "emoji__"),
    ],
    ids=["dot", "plain", "dash", "bmp", "astral"],
)
def test_a_name_is_spelled_as_the_harness_spells_it(name, spelled) -> None:
    """``replace`` walks UTF-16 code units, so an astral character becomes two ``_``."""
    assert mimocode_spelling(name) == spelled


def test_an_astral_server_name_is_split_at_its_own_spelling() -> None:
    assert mimocode_mcp_identity("s___tool_x", ["s\U0001f600"]) == ("s\U0001f600", "tool_x")


@pytest.mark.parametrize(
    ("listed", "real"),
    [
        (["repo.delete", "other"], ("repo.delete",)),
        (["repo_delete"], ("repo_delete",)),
        (["repo_delete", "repo.delete"], ("repo.delete", "repo_delete")),
        (["Repo.delete", "", 7, None], ()),
        ([], ()),
    ],
    ids=["dotted", "as-sent", "two-real-names", "case-and-junk", "empty"],
)
def test_the_real_names_behind_a_spelling_are_read_off_the_list(listed, real) -> None:
    assert mimocode_real_tool_names("repo_delete", listed) == real


def _ceiling_context(monkeypatch, ref: str) -> None:
    """A real ``mcp`` ceiling denying *ref*, where the rule reader looks."""
    import kiro_crew.platform as platform_mod
    from kiro_crew.platform.governance import (
        MODE_DENY,
        GovernanceCeiling,
        ScopedRuleset,
        parse_policy,
    )

    ceiling = GovernanceCeiling(
        version=1,
        boot=parse_policy({"version": 1, "boot": {"fail_closed": True}}).boot,
        controls={"mcp": ScopedRuleset(mode=MODE_DENY, deny=(ref,), matcher="mcp")},
    )
    real = platform_mod.current_context()
    monkeypatch.setattr(
        platform_mod,
        "current_context",
        lambda: SimpleNamespace(security=real.security, governance=ceiling),
    )


async def _refused(client: AcpClient, event: AcpEvent) -> bool:
    client.reject_tool = AsyncMock()
    client._audit_spec_restriction = MagicMock()
    refused = await client._refuse_identity_drift(event)
    if refused:
        client.reject_tool.assert_awaited_once_with(event.request_id)
    else:
        client.reject_tool.assert_not_awaited()
    return refused


@pytest.mark.asyncio
async def test_a_ceiling_on_the_real_dotted_name_refuses_the_rewritten_call(
    tmp_path, monkeypatch
) -> None:
    """``repo.delete`` arrives as ``ops_repo_delete``; ``@ops/repo.delete`` cannot meet
    ``@ops/repo_delete`` at the gate, so the call is refused first."""
    _ceiling_context(monkeypatch, "@ops/repo.delete")
    client = _probe_client(tmp_path, ("ops",))
    event = _probe_permission(client, "ops_repo_delete")
    assert (event.mcp_server_name, event.tool_name) == ("ops", "repo_delete")
    assert await _refused(client, event) is True
    reason = client._audit_spec_restriction.call_args.kwargs["reason"]
    assert reason == "spec_disabled_tool_rewritten_name"


@pytest.mark.asyncio
async def test_a_rule_that_meets_the_name_as_sent_is_left_to_the_gate(
    tmp_path, monkeypatch
) -> None:
    _ceiling_context(monkeypatch, "@ops/repo_delete")
    client = _probe_client(tmp_path, ("ops",))
    event = _probe_permission(client, "ops_repo_delete")
    assert await _refused(client, event) is False
    result = _judged_under_a_ceiling_denying(monkeypatch, "@ops/repo_delete", event)
    assert result.action == TOOL_DENY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rules", [("@ops/repo.*",), ("mcp__ops__repo.delete",)], ids=["deny-glob", "title"]
)
async def test_an_operator_hook_rule_on_the_real_name_refuses_too(
    tmp_path, monkeypatch, rules
) -> None:
    """``auto_deny_tools`` / ``confirm_tools`` globs are gate rules too."""
    monkeypatch.setattr(client_permissions, "_mimocode_mcp_rules", lambda key, agent: (rules, ()))
    client = _probe_client(tmp_path, ("ops",))
    assert await _refused(client, _probe_permission(client, "ops_repo_delete")) is True


@pytest.mark.asyncio
async def test_unreadable_rules_refuse_a_call_whose_name_may_be_rewritten(
    tmp_path, monkeypatch
) -> None:
    def _broken(key, agent):
        raise OSError("config.json unreadable")

    monkeypatch.setattr(client_permissions, "_mimocode_mcp_rules", _broken)
    client = _probe_client(tmp_path, ("ops",))
    assert await _refused(client, _probe_permission(client, "ops_repo_delete")) is True


@pytest.mark.asyncio
async def test_a_tool_name_with_no_underscore_reads_no_rules(tmp_path, monkeypatch) -> None:
    """Nothing rewritten, so no rules read."""

    def _unexpected(key, agent):
        raise AssertionError("rules read for a name the harness cannot have rewritten")

    monkeypatch.setattr(client_permissions, "_mimocode_mcp_rules", _unexpected)
    client = _probe_client(tmp_path, ("ops",))
    event = _probe_permission(client, "ops_deploy")
    assert (event.mcp_server_name, event.tool_name) == ("ops", "deploy")
    assert await _refused(client, event) is False


def _listing(monkeypatch, client: AcpClient, listed: tuple[str, ...] | None) -> list:
    """Launch native ``ops``, list *listed* (``None``: unreadable); return probed targets."""
    client._mimocode_session_mcp_servers = lambda: []  # type: ignore[method-assign]
    client._mimocode_native_mount_launches = {
        "ops": mimocode_mod.MimoCodeMountLaunch(("ops-mcp", "--stdio"), {})
    }
    asked: list = []

    async def _probe(target):
        asked.append(target)
        return listed

    monkeypatch.setattr(client_permissions, "mimocode_mcp_tool_list", _probe)
    return asked


def _install_ceiling(monkeypatch, mode: str, ref: str) -> None:
    """A real ``mcp`` ceiling, *mode* over *ref*, for the rule reader and the gate."""
    import kiro_crew.hooks as hooks_mod
    import kiro_crew.platform as platform_mod
    from kiro_crew.platform.governance import GovernanceCeiling, ScopedRuleset, parse_policy

    listed = {"allow": (ref,)} if mode == "allow" else {"deny": (ref,)}
    ceiling = GovernanceCeiling(
        version=1,
        boot=parse_policy({"version": 1, "boot": {"fail_closed": True}}).boot,
        controls={"mcp": ScopedRuleset(mode=mode, matcher="mcp", **listed)},
    )
    real = platform_mod.current_context()
    context = SimpleNamespace(security=real.security, governance=ceiling)
    monkeypatch.setattr(platform_mod, "current_context", lambda: context)
    monkeypatch.setattr(hooks_mod, "current_context", lambda: context)


def _judged(event: AcpEvent, **config) -> str:
    from kiro_crew.hooks import HooksConfig

    manager = HookManager(HooksConfig(**config)) if config else HookManager()
    return manager.on_tool_call(event.title, **ToolCall.from_event(event).kwargs()).action


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listed", "real", "denied"),
    [(("repo.delete", "other"), "repo.delete", True), (("repo_delete",), "repo_delete", False)],
    ids=["dotted-real-name", "underscore-real-name"],
)
async def test_an_allow_list_entry_is_judged_against_the_real_name(
    tmp_path, monkeypatch, listed, real, denied
) -> None:
    """Only ``@ops/repo_delete`` is allowed; the server's list settles which tool
    ``ops_repo_delete`` is, and ``repo.delete`` is outside the allow-list."""
    _install_ceiling(monkeypatch, "allow", "@ops/repo_delete")
    client = _probe_client(tmp_path, ("ops",))
    _listing(monkeypatch, client, listed)
    event = _probe_permission(client, "ops_repo_delete")
    assert await _refused(client, event) is False
    assert (event.mcp_server_name, event.tool_name) == ("ops", real)
    assert client._tool_call_tool_name[event.tool_call_id] == real
    assert (_judged(event) == TOOL_DENY) is denied


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listed",
    [("repo_delete", "repo.delete"), None, ("other",)],
    ids=["two-real-names", "list-unreadable", "not-listed"],
)
async def test_a_call_an_allow_entry_could_grant_another_tool_for_is_refused(
    tmp_path, monkeypatch, listed
) -> None:
    _install_ceiling(monkeypatch, "allow", "@ops/repo_delete")
    client = _probe_client(tmp_path, ("ops",))
    _listing(monkeypatch, client, listed)
    event = _probe_permission(client, "ops_repo_delete")
    assert await _refused(client, event) is True
    reason = client._audit_spec_restriction.call_args.kwargs["reason"]
    assert reason == "spec_disabled_tool_rewritten_name"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listed", "granted"),
    [(("repo.delete",), False), (("repo_delete",), True)],
    ids=["another-tool", "the-named-tool"],
)
async def test_an_auto_approve_entry_grants_only_the_tool_it_names(
    tmp_path, monkeypatch, listed, granted
) -> None:
    rules = ((), ("@ops/repo_delete",))
    monkeypatch.setattr(client_permissions, "_mimocode_mcp_rules", lambda key, agent: rules)
    client = _probe_client(tmp_path, ("ops",))
    _listing(monkeypatch, client, listed)
    event = _probe_permission(client, "ops_repo_delete")
    assert await _refused(client, event) is False
    from kiro_crew.hooks import TOOL_AUTO_APPROVE

    action = _judged(event, auto_approve_tools=["@ops/repo_delete"])
    assert (action == TOOL_AUTO_APPROVE) is granted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listed", "denied"),
    [(("repo.delete",), True), (("repo_delete",), False)],
    ids=["the-denied-tool", "another-tool"],
)
async def test_a_deny_on_the_dotted_name_meets_the_settled_real_name(
    tmp_path, monkeypatch, listed, denied
) -> None:
    """Gate-denied only when the real name is the denied one; never refused first."""
    _install_ceiling(monkeypatch, "deny", "@ops/repo.delete")
    client = _probe_client(tmp_path, ("ops",))
    _listing(monkeypatch, client, listed)
    event = _probe_permission(client, "ops_repo_delete")
    assert await _refused(client, event) is False
    assert (_judged(event) == TOOL_DENY) is denied


@pytest.mark.asyncio
async def test_a_later_frame_of_a_settled_call_reads_as_the_same_call(
    tmp_path, monkeypatch
) -> None:
    """The cached real name spells as the title: not drift."""
    _install_ceiling(monkeypatch, "allow", "@ops/repo_delete")
    client = _probe_client(tmp_path, ("ops",))
    asked = _listing(monkeypatch, client, ("repo.delete",))
    first = _probe_permission(client, "ops_repo_delete")
    assert await _refused(client, first) is False
    assert first.tool_name == "repo.delete"
    frames = copy.deepcopy(_captured_call_frames())
    frames[1]["params"]["toolCall"]["title"] = "ops_repo_delete"
    second = client._build_permission_event(JsonRpcMessage.from_dict(frames[1]))
    assert second is not None and second.tool_name == "repo.delete"
    assert await _refused(client, second) is False
    assert len(asked) == 1


@pytest.mark.asyncio
async def test_the_list_is_read_once_per_server_however_many_calls_ask(
    tmp_path, monkeypatch
) -> None:
    """A caller during the read awaits it; a later one reuses it."""
    client = _probe_client(tmp_path, ("ops",))
    client._mimocode_session_mcp_servers = lambda: []  # type: ignore[method-assign]
    client._mimocode_native_mount_launches = {
        "ops": mimocode_mod.MimoCodeMountLaunch(("ops-mcp", "--stdio"), {})
    }
    started, release = asyncio.Event(), asyncio.Event()
    asked: list = []

    async def _probe(target):
        asked.append(target)
        started.set()
        await release.wait()
        return ("repo_delete",)

    monkeypatch.setattr(client_permissions, "mimocode_mcp_tool_list", _probe)
    first = asyncio.ensure_future(client._mimocode_mcp_tool_list("ops"))
    await asyncio.wait_for(started.wait(), _LOST_RUN_TIMEOUT)
    second = asyncio.ensure_future(client._mimocode_mcp_tool_list("ops"))
    release.set()
    lists = await asyncio.wait_for(asyncio.gather(first, second), _LOST_RUN_TIMEOUT)
    assert lists == [("repo_delete",), ("repo_delete",)]
    assert await client._mimocode_mcp_tool_list("ops") == ("repo_delete",)
    assert len(asked) == 1
    assert (asked[0].command, asked[0].args) == ("ops-mcp", ["--stdio"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rules", [((), (), ()), (("@ops/*",), ("@ops",), ())], ids=["none", "server"]
)
async def test_the_list_is_read_whatever_the_rules(tmp_path, monkeypatch, rules) -> None:
    """The real name is put on every ``_`` call, so a rule in any grammar meets it."""
    monkeypatch.setattr(client_permissions, "_mimocode_mcp_rules", lambda key, agent: rules)
    client = _probe_client(tmp_path, ("ops",))
    asked = _listing(monkeypatch, client, ("repo.delete",))
    event = _probe_permission(client, "ops_repo_delete")
    assert await _refused(client, event) is False
    assert len(asked) == 1 and event.tool_name == "repo.delete"


@pytest.mark.asyncio
async def test_an_unprefixed_deny_glob_meets_the_real_name(tmp_path, monkeypatch) -> None:
    """``auto_deny_tools: ["*.delete"]`` with ``auto_approve_tools: ["@ops/*"]``."""
    client = _probe_client(tmp_path, ("ops",))
    _listing(monkeypatch, client, ("repo.delete",))
    event = _probe_permission(client, "ops_repo_delete")
    assert await _refused(client, event) is False
    assert _judged(event, auto_deny_tools=["*.delete"], auto_approve_tools=["@ops/*"]) == TOOL_DENY


@pytest.mark.asyncio
async def test_a_crew_server_s_list_is_read_without_a_spawn(tmp_path, monkeypatch) -> None:
    from kiro_crew.mcp_discovery_managed import _managed_tools_in_process

    client = _probe_client(tmp_path, ("kirocrew-core",))
    asked = _listing(monkeypatch, client, None)
    client._mimocode_session_mcp_servers = lambda: [{"name": "kirocrew-core"}]  # type: ignore
    listed = await client._mimocode_mcp_tool_list("kirocrew-core")
    assert asked == [] and listed == tuple(_managed_tools_in_process("kirocrew-core"))


@pytest.mark.asyncio
async def test_a_respawn_drops_the_lists_read_for_the_old_process(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    probe = asyncio.ensure_future(asyncio.Event().wait())
    client._mimocode_mcp_tool_lists = {"ops": probe}
    client._mimocode_native_mount_launches = {"ops": mimocode_mod.MimoCodeMountLaunch(("x",), {})}
    client._reset_state()
    await asyncio.wait({probe}, timeout=_LOST_RUN_TIMEOUT)
    assert probe.cancelled()
    assert client._mimocode_mcp_tool_lists == {}
    assert client._mimocode_native_mount_launches == {}


# ── where a MiMo Code server's tool list is read from


def test_only_an_enabled_local_native_server_has_a_launch() -> None:
    servers = {
        "local": {"type": "local", "command": ["npx", "-y", "srv"], "environment": {"K": "v"}},
        "stdio": {"type": "stdio", "command": ["srv"]},
        "remote": {"type": "remote", "url": "https://mcp.example.com/mcp"},
        "off": {"type": "local", "command": ["srv"], "enabled": False},
        "no-command": {"type": "local", "command": []},
        "bad-command": {"type": "local", "command": ["srv", 3]},
    }
    launch = mimocode_mod.MimoCodeMountLaunch
    assert mimocode_mod._mimocode_native_mount_launches(servers) == {
        "local": launch(("npx", "-y", "srv"), {"K": "v"}),
        "stdio": launch(("srv",), {}),
    }
    assert mimocode_mod._mimocode_native_mount_launches(None) == {}


def _placed_stdio() -> dict:
    """Crew's untagged stdio element for ``ops``, as the harness receives it."""
    from kiro_crew.acp.session_mcp import acp_server_element
    from kiro_crew.providers.mirrors.mimocode import _mimocode_local_element

    spec = {"command": "/usr/bin/ops-mcp", "args": ["--stdio"], "env": {"TOKEN": "t"}}
    element = _mimocode_local_element(acp_server_element("ops", spec))
    assert "type" not in element
    return element


_PLACED_STDIO = _placed_stdio()


@pytest.mark.parametrize(
    ("placed", "native", "expected"),
    [
        (
            [_PLACED_STDIO],
            {"ops": ("native-ops",)},
            ("/usr/bin/ops-mcp", ["--stdio"], {"TOKEN": "t"}, ""),
        ),
        (
            [
                {
                    "name": "ops",
                    "type": "http",
                    "url": "https://h/mcp",
                    "headers": [{"name": "A", "value": "b"}],
                }
            ],
            {},
            ("", None, {}, "https://h/mcp"),
        ),
        ([{"name": "ops", "type": "sse", "url": "https://h/sse"}], {"ops": ("native-ops",)}, None),
        (
            [{"name": "other", "type": "stdio", "command": "x"}],
            {"ops": ("native-ops", "-v")},
            ("native-ops", ["-v"], {}, ""),
        ),
        ([], {}, None),
        ([{**_PLACED_STDIO, "type": "stdio"}], {}, None),
    ],
    ids=["placed-first", "placed-http", "placed-sse", "native-launch", "nothing", "tagged"],
)
def test_the_tool_list_target_is_the_placed_element_then_the_native_launch(
    placed, native, expected
) -> None:
    """An ACP-placed server replaces the harness's own entry of that name."""
    launches = {n: mimocode_mod.MimoCodeMountLaunch(cmd, {}) for n, cmd in native.items()}
    target = mimocode_mod.mimocode_mcp_tool_list_target("ops", placed, launches)
    if expected is None:
        assert target is None
        return
    assert target.name == "ops"
    assert (target.command, target.args, target.env, target.url) == expected
    if target.url:
        assert target.headers == {"A": "b"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "listed"), [("ok", ("a.b", "c")), ("error", None)], ids=["read", "failed"]
)
async def test_the_tool_list_is_read_through_the_probe_under_its_own_client(
    monkeypatch, status, listed
) -> None:
    """Its own ``clientInfo`` keeps the answer out of the dashboard's probe cache."""
    import kiro_crew.mcp_discovery as discovery

    seen: dict = {}

    async def _probe_server(server, *, client_info=None):
        seen["client_info"] = client_info
        server.status, server.tools = status, ["a.b", "c"]
        return server

    monkeypatch.setattr(discovery, "probe_server", _probe_server)
    target = discovery.McpServerInfo(name="ops", command="ops-mcp")
    assert await mimocode_mod.mimocode_mcp_tool_list(target) == listed
    assert seen["client_info"] == {"name": "kirocrew-mimocode-tool-names", "version": "1"}


@pytest.mark.parametrize("mode", ["allow", "deny"])
def test_the_rules_are_split_into_narrowing_and_widening(monkeypatch, mode) -> None:
    """Ceiling denies and hooks' deny/confirm lists narrow; other ceiling entries and
    operator auto-approve widen."""
    import kiro_crew.hooks as hooks_mod
    from kiro_crew.hooks import HooksConfig

    _install_ceiling(monkeypatch, mode, "@ops/repo?delete")
    hooks = HooksConfig(
        auto_approve_tools=["@ops/grant_me"],
        auto_deny_tools=["@ops/deny_me"],
        confirm_tools=["@ops/ask_me"],
    )
    monkeypatch.setattr(hooks_mod, "hooks_config_from_config_dict", lambda section: hooks)
    narrowing, widening, regexes = client_permissions._mimocode_mcp_rules("", "")
    # The denied-command regexes the deny tier searches, the shipped ones included.
    assert regexes and all(isinstance(r, str) for r in regexes)
    assert {"@ops/deny_me", "@ops/ask_me"} <= set(narrowing)
    assert "@ops/grant_me" in widening and "@ops/grant_me" not in narrowing
    # A governance entry is matched literally, so it arrives escaped into a glob.
    ceiling_side = widening if mode == "allow" else narrowing
    assert glob.escape("@ops/repo?delete") in ceiling_side
    assert glob.escape("@ops/repo?delete") not in (narrowing if mode == "allow" else widening)


# ── a server switched off in Crew that MiMo Code mounts itself ──────────────


@pytest.mark.parametrize(
    ("native", "refused"),
    [({"ops": {}}, True), ({"ops": {"enabled": False}}, False), ({"other": {}}, False)],
    ids=["mounted", "natively-off", "not-mounted"],
)
def test_the_read_back_refuses_a_native_mount_of_a_disabled_server(
    tmp_path, monkeypatch, native, refused
) -> None:
    import json as json_mod

    document = json_mod.dumps({"permission": {"*": "ask"}, "mcp": native})
    monkeypatch.setattr(
        mimocode_mod.subprocess_mod,
        "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout=document, stderr=""),
    )
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    client._mimocode_disabled_servers = frozenset({"ops"})
    seed = mimocode_mod._mimocode_routing_config(ACP_BACKEND_MIMOCODE)
    issue, remedy = mimocode_mod._verify_mimocode_routing(
        client, ACP_BACKEND_MIMOCODE, ["mimo", "debug", "config"], seed
    )
    assert ("'ops' is switched off in Kiro Crew" in issue) is refused
    assert ("switch it back on" in remedy) is refused
    assert ("ops" in client._mimocode_native_mounts) is (native == {"ops": {}})


def test_a_re_projection_that_disables_a_native_mount_refuses(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    client._mimocode_native_mounts = frozenset({"ops"})
    assert mimocode_mod.refuse_native_mount_of_unhonoured(client) is None
    client._mimocode_disabled_servers = frozenset({"ops"})
    with pytest.raises(AcpToolGateUnroutable, match="'ops' is switched off"):
        mimocode_mod.refuse_native_mount_of_unhonoured(client)


def test_the_projection_records_the_servers_it_switched_off(tmp_path, monkeypatch) -> None:
    """From the same projection the array came out of, on a MiMo Code session only."""
    from kiro_crew.providers.mirrors import mimocode as mirror_mod

    seen = SessionProjection(params={"mcpServers": []}, disabled_servers=frozenset({"ops"}))
    monkeypatch.setattr(mirror_mod.MimoCodeMirror, "session_projection", lambda *a, **k: seen)
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    monkeypatch.setattr(client, "_pooled_broker_stubs", lambda: [])
    client._resolve_session_mcp_servers()
    assert client._mimocode_disabled_servers == frozenset({"ops"})
