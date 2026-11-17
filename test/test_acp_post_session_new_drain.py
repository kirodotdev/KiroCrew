"""The drain after ``session/new`` on a harness that sends no MCP init frame.

claude-agent-acp reports no MCP server state over ACP, so its new chats do not wait
the drain's idle window on a quiet pipe before the first prompt
(``ACP_BACKENDS_NO_MCP_INIT_FRAMES``). What the start buffered is still folded, and a
frame that arrives later is still there for the first turn's reader. kiro-cli keeps
the live drain its servers' reports need.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import client as client_facade
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import METHOD_SESSION_UPDATE, JsonRpcMessage
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO


def _config_update() -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "s1",
            "update": {"sessionUpdate": "config_option_update", "configOptions": []},
        },
    )


def _wired(client: AcpClient, reads: list[float]) -> list[JsonRpcMessage]:
    """Record every live read; the first ends any drain loop. Returns the folded configs."""

    async def read(timeout: float = 2.0, **_kwargs: object) -> JsonRpcMessage | None:
        reads.append(timeout)
        raise AcpError("no more frames in this test")

    folded: list[JsonRpcMessage] = []
    client._read_message = read  # type: ignore[method-assign]
    client._handle_config_option_update = folded.append  # type: ignore[method-assign]
    return folded


def _process() -> MagicMock:
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    return proc


async def _cold_start(tmp_path, backend: str, reads: list[float]) -> list[JsonRpcMessage]:
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    client._process = _process()
    client._next_req_id = MagicMock(side_effect=range(1, 100))
    frame = _config_update()

    async def answer(req_id, timeout=50.0, *, method="", expected_mcp=None):
        if req_id == 1:
            return {"protocolVersion": 1, "agentCapabilities": {}}
        # A frame that arrives while the start waits for its own answers is
        # buffered for the drain, as _wait_for_response buffers it.
        client._mcp_notifications.append(frame)
        return {"sessionId": "s1"}

    client._wait_for_response = AsyncMock(side_effect=answer)  # type: ignore[method-assign]
    folded = _wired(client, reads)
    await client._initialize_session()
    assert client._session_id == "s1"
    return folded


@pytest.fixture
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)


def test_only_claude_skips_the_idle_window() -> None:
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_NO_MCP_INIT_FRAMES

    assert ACP_BACKENDS_NO_MCP_INIT_FRAMES == frozenset({ACP_BACKEND_CLAUDE})


@pytest.mark.asyncio
@pytest.mark.usefixtures("_home")
async def test_a_claude_start_folds_what_it_buffered_and_reads_no_more(tmp_path) -> None:
    reads: list[float] = []

    folded = await _cold_start(tmp_path, ACP_BACKEND_CLAUDE, reads)

    assert folded and all(
        m.params["update"]["sessionUpdate"] == "config_option_update" for m in folded
    )
    assert reads == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_home")
async def test_a_kiro_start_still_waits_for_its_servers_reports(tmp_path) -> None:
    reads: list[float] = []

    folded = await _cold_start(tmp_path, ACP_BACKEND_KIRO, reads)

    assert folded
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_a_claude_fresh_conversation_reads_no_more_after_its_answers(
    tmp_path, monkeypatch
) -> None:
    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE, work_dir=tmp_path)
    client._session_id = "sess-1"
    client._is_process_alive = lambda: True  # type: ignore[method-assign]
    frame = _config_update()

    async def new_session() -> dict:
        client._mcp_notifications.append(frame)
        return {"sessionId": "sess-2"}

    monkeypatch.setattr(client_facade, "require_unchanged_derived_spec", lambda snapshot: None)
    for name in (
        "_seed_session_settings",
        "_prepare_claude_side_read_denies",
        "_prepare_session_mcp",
        "_persist_advertised_models_if_changed",
        "_reseed_after_capture",
        "_pin_claude_starting_mode",
        "confirm_member_projection",
        "_apply_startup_model",
    ):
        setattr(client, name, AsyncMock())
    client._send_request = AsyncMock(return_value=4)  # type: ignore[method-assign]
    client._wait_for_response = AsyncMock(return_value={})  # type: ignore[method-assign]
    client._new_session_following_substitution = new_session  # type: ignore[method-assign]
    client._start_mount_check = MagicMock()  # type: ignore[method-assign]
    reads: list[float] = []
    folded = _wired(client, reads)

    await client.new_conversation()

    assert client._session_id == "sess-2"
    assert folded == [frame]
    assert reads == []


@pytest.mark.asyncio
async def test_a_frame_after_the_drain_is_still_read_by_the_next_reader() -> None:
    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE)
    reader = asyncio.StreamReader()
    proc = _process()
    proc.stdout = reader
    client._process = proc
    late = {
        "jsonrpc": "2.0",
        "method": METHOD_SESSION_UPDATE,
        "params": {"sessionId": "s1", "update": {"sessionUpdate": "available_commands_update"}},
    }
    reader.feed_data((json.dumps(late) + "\n").encode())

    await client._drain_notifications(idle_exit=0.0)
    # Bounded by _read_message's own read timeout; the frame is already buffered.
    msg = await client._read_message()

    assert msg is not None
    assert msg.params["update"]["sessionUpdate"] == "available_commands_update"
