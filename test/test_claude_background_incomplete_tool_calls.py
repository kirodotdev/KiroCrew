"""A Claude turn ending while only background agents' calls run completes.

A call of the parent's own that never returned still fails the turn.

claude-agent-acp answers a turn that reached ``end_turn`` with ``-32603`` and
``data.errorKind: "incomplete_tool_call"`` naming every call it counted as the
turn's own and saw no result for. A background Workflow agent's call that reached
the adapter's ``canUseTool`` before its task was registered is counted too, and goes
out on the root session with no ``parentToolUseId`` (``ensureToolCallEmitted`` and
``settleActive`` in claude-agent-acp 0.90.0). Its ``tool_use`` is in that agent's
transcript under ``projects/<project>/<sid>/subagents/``, never in the parent's.

Each test drives ``AcpClient``'s own prompt loop with the frames the adapter sends and
no process, on a Claude config directory written under ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.claude_background_calls import incomplete_tool_call_ids
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import EVENT_COMPLETE, STOP_REASON_END_TURN, JsonRpcMessage
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_PI
from kiro_crew.agent_sdk.claude_transcripts import claude_project_dir_name
from kiro_crew.testing.wait import default_timeout

SESSION = "6f1b0d2e-3c4a-4b5d-8e9f-0a1b2c3d4e5f"
WORKFLOW = "wf_6a29223b-ba6"
#: The parent's own calls: the Workflow launch, and a Read that never returned.
WORKFLOW_CALL = "toolu_bdrk_01ParentWorkflowLaunchA1"
OWN_CALL = "toolu_bdrk_01ParentOwnReadNeverReturnedZ9"
#: Three background agents' Read calls, one per agent transcript.
BACKGROUND_CALLS = (
    "toolu_bdrk_01LTAY2aJs9EPKHSZd4EhNBL",
    "toolu_bdrk_01BgAgentTwoReadCallB2",
    "toolu_bdrk_01BgAgentThreeReadCallC3",
)
_SENTENCE = "Claude ended the turn without returning results for tool calls: "
_REQ_ID = 7


def _tool_use_row(tool_call_id: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_call_id,
                    "name": "Read",
                    "input": {"file_path": "x"},
                }
            ],
        },
    }


def _append(path: Path, *rows: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


@pytest.fixture
def store(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """The work directory and the session's project directory in Claude's store.

    The parent's transcript holds its own calls; each background agent's transcript
    holds that agent's call.
    """
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    work = tmp_path / "work"
    work.mkdir()
    project_name = claude_project_dir_name(str(work))
    assert project_name is not None
    project = config / "projects" / project_name
    _append(project / f"{SESSION}.jsonl", _tool_use_row(WORKFLOW_CALL), _tool_use_row(OWN_CALL))
    agents = project / SESSION / "subagents" / "workflows" / WORKFLOW
    for number, call in enumerate(BACKGROUND_CALLS, start=1):
        _append(agents / f"agent-{number}.jsonl", _tool_use_row(call))
    return work, project


def _client(work: Path, backend: str = ACP_BACKEND_CLAUDE) -> tuple[AcpClient, list]:
    frames: list[JsonRpcMessage] = []
    client = AcpClient(work_dir=work, acp_backend=backend)
    client._session_id = SESSION
    client.ensure_ready = AsyncMock()
    client._send_response = AsyncMock()
    client._is_process_alive = lambda: True
    client._send_prompt = AsyncMock(return_value=_REQ_ID)

    async def _read(timeout: float = 0.0):
        return frames.pop(0) if frames else None

    client._read_message = _read
    return client, frames


def _update(update: dict) -> JsonRpcMessage:
    return JsonRpcMessage(method="session/update", params={"sessionId": SESSION, "update": update})


def _incomplete(ids: tuple[str, ...], data: object = None) -> JsonRpcMessage:
    """The adapter's answer: the stuck calls failed one by one, then the prompt's error."""
    error = {"code": -32603, "message": "Internal error: " + _SENTENCE + ", ".join(ids)}
    error["data"] = {"errorKind": "incomplete_tool_call"} if data is None else data
    return JsonRpcMessage(id=_REQ_ID, error=error)


def _turn(frames: list, ids: tuple[str, ...], data: object = None) -> None:
    """One turn: each call surfaces, the answer is delivered, each call fails, the error."""
    for call in ids:
        frames.append(
            _update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": call,
                    "title": "Read x",
                    "kind": "read",
                    "status": "pending",
                    "rawInput": {"file_path": "x"},
                    "_meta": {"claudeCode": {"toolName": "Read"}},
                }
            )
        )
    frames.append(
        _update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "The workflow runs in the background."},
            }
        )
    )
    for call in ids:
        frames.append(
            _update({"sessionUpdate": "tool_call_update", "toolCallId": call, "status": "failed"})
        )
    frames.append(_incomplete(ids, data))


async def _via_stream_events(client: AcpClient) -> list:
    return [e async for e in client.stream_events("status?", timeout=default_timeout())]


async def _via_send_message(client: AcpClient) -> list:
    await client.send_message("status?", timeout=default_timeout())
    return []


async def _via_send_message_stream(client: AcpClient) -> list:
    return [c async for c in client.send_message_stream("status?", timeout=default_timeout())]


_ENTRIES = {
    "stream_events": _via_stream_events,
    "send_message": _via_send_message,
    "send_message_stream": _via_send_message_stream,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", sorted(_ENTRIES))
async def test_a_turn_whose_only_unfinished_calls_are_background_agents_completes(
    store, entry
) -> None:
    work, _project = store
    client, frames = _client(work)
    _turn(frames, BACKGROUND_CALLS)

    events = await _ENTRIES[entry](client)

    if entry == "stream_events":
        complete = [e for e in events if e.kind == EVENT_COMPLETE]
        assert [e.stop_reason for e in complete] == [STOP_REASON_END_TURN]
    assert client._last_stop_reason == STOP_REASON_END_TURN


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", sorted(_ENTRIES))
async def test_the_parents_own_unfinished_call_still_fails_the_turn(store, entry) -> None:
    work, _project = store
    client, frames = _client(work)
    _turn(frames, (OWN_CALL,))

    with pytest.raises(AcpError) as raised:
        await _ENTRIES[entry](client)

    assert _SENTENCE + OWN_CALL in str(raised.value)


@pytest.mark.asyncio
async def test_one_own_call_beside_background_ones_still_fails_the_turn(store) -> None:
    work, _project = store
    client, frames = _client(work)
    _turn(frames, (*BACKGROUND_CALLS, OWN_CALL))

    with pytest.raises(AcpError):
        await _via_stream_events(client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        pytest.param("incomplete_tool_call", id="data-string"),
        pytest.param({}, id="no-kind"),
        pytest.param({"errorKind": "unknown"}, id="another-kind"),
    ],
)
async def test_the_kind_is_read_from_data_never_from_the_sentence(store, data) -> None:
    work, _project = store
    client, frames = _client(work)
    _turn(frames, BACKGROUND_CALLS, data=data)

    with pytest.raises(AcpError):
        await _via_stream_events(client)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param(
            "Internal error: " + _SENTENCE + "toolu_a1, toolu_B_2",
            ("toolu_a1", "toolu_B_2"),
            id="prefixed",
        ),
        pytest.param(_SENTENCE + "toolu_a1", ("toolu_a1",), id="unprefixed"),
        pytest.param("  " + _SENTENCE + "toolu_a1, toolu_a1\n", ("toolu_a1",), id="padded-repeat"),
        pytest.param(_SENTENCE, (), id="no-ids"),
        pytest.param(_SENTENCE + "toolu_", (), id="bare-prefix"),
        pytest.param(_SENTENCE + "call_a1", (), id="another-prefix"),
        pytest.param(_SENTENCE + "toolu_a-1", (), id="foreign-character"),
        pytest.param(_SENTENCE + "toolu_\u00e9", (), id="non-ascii-letter"),
        pytest.param(_SENTENCE + "toolu_a1,toolu_b2", (), id="no-space-after-comma"),
        pytest.param(_SENTENCE + "toolu_a1, ", (), id="trailing-separator"),
        pytest.param(_SENTENCE + "toolu_a1 and more", (), id="trailing-text"),
        pytest.param("Note: " + _SENTENCE + "toolu_a1", (), id="another-lead"),
        pytest.param(_SENTENCE + ", ".join(f"toolu_{n}" for n in range(65)), (), id="over-the-cap"),
    ],
)
def test_the_ids_are_read_from_the_whole_sentence(message, expected) -> None:
    error = {"code": -32603, "message": message, "data": {"errorKind": "incomplete_tool_call"}}
    assert incomplete_tool_call_ids(error) == expected


@pytest.mark.asyncio
async def test_another_harness_keeps_the_error(store) -> None:
    work, _project = store
    client, frames = _client(work, backend=ACP_BACKEND_PI)
    _turn(frames, BACKGROUND_CALLS)

    with pytest.raises(AcpError):
        await _via_stream_events(client)


@pytest.mark.asyncio
async def test_a_call_past_the_tail_read_keeps_the_error(store, monkeypatch) -> None:
    work, project = store
    monkeypatch.setattr("kiro_crew.acp.claude_background_calls._TRANSCRIPT_TAIL_BYTES", 512)
    # The first agent wrote more than the tail after its call.
    filler = {"type": "user", "message": {"role": "user", "content": "y" * 1024}}
    _append(project / SESSION / "subagents" / "workflows" / WORKFLOW / "agent-1.jsonl", filler)
    client, frames = _client(work)
    _turn(frames, BACKGROUND_CALLS)

    with pytest.raises(AcpError):
        await _via_stream_events(client)
