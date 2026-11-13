"""``AcpClient.new_conversation``: a fresh claude conversation on the live adapter.

The replies are the ones claude-agent-acp 0.94.0 gave on one process
(``acp_frames/claude/new-conversation-live.jsonl``): ``session/close`` answers ``{}``
and ``session/new`` answers a new id. The settings seed and the array are prepared
again before the old session is closed, the new session is set up as a cold start sets
one up, and any failure on the way ends the process instead of leaving a half-set-up
session behind.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import client as client_facade
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import METHOD_SESSION_CLOSE, METHOD_SESSION_NEW
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_OPENCODE


def _live_claude(tmp_path, monkeypatch, steps: list[str]) -> AcpClient:
    """A claude client on session ``sess-1`` whose wire and setup steps are recorded."""
    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE, work_dir=tmp_path)
    client._session_id = "sess-1"
    client.last_prompt_stats.context_pct = 60.0
    client._between_turn_credits = 2.0
    client._buffer.append(MagicMock(name="a frame the old session left"))
    client._is_process_alive = lambda: True  # type: ignore[method-assign]

    def step(name: str) -> AsyncMock:
        return AsyncMock(side_effect=lambda *a, **k: steps.append(name))

    async def send_request(method: str, params: dict) -> int:
        steps.append(f"{method} {params.get('sessionId')}")
        return 4

    async def new_session() -> dict:
        steps.append(f"{METHOD_SESSION_NEW} (buffer {len(client._buffer)})")
        return {"sessionId": "sess-2"}

    monkeypatch.setattr(
        client_facade,
        "require_unchanged_derived_spec",
        lambda snapshot: steps.append("derived spec"),
    )
    client._seed_session_settings = step("seed")  # type: ignore[method-assign]
    client._prepare_claude_side_read_denies = step("side denies")  # type: ignore[method-assign]
    client._prepare_session_mcp = step("mcp array")  # type: ignore[method-assign]
    client._send_request = send_request  # type: ignore[method-assign]
    client._wait_for_response = AsyncMock(return_value={})  # type: ignore[method-assign]
    client._new_session_following_substitution = new_session  # type: ignore[method-assign]
    client._persist_advertised_models_if_changed = AsyncMock()  # type: ignore[method-assign]
    client._reseed_after_capture = AsyncMock()  # type: ignore[method-assign]
    client._pin_claude_starting_mode = step("pin mode")  # type: ignore[method-assign]
    client._start_mount_check = MagicMock()  # type: ignore[method-assign]
    client.confirm_member_projection = step("confirm projection")  # type: ignore[method-assign]
    client._apply_startup_model = step("model")  # type: ignore[method-assign]
    client._drain_notifications = step("drain")  # type: ignore[method-assign]
    client._cleanup_failed_live_spawn = step("kill")  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
async def test_the_old_claude_session_is_closed_before_the_new_one_is_set_up(tmp_path, monkeypatch):
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)

    await client.new_conversation()

    assert steps == [
        "seed",
        "side denies",
        "mcp array",
        f"{METHOD_SESSION_CLOSE} sess-1",
        f"{METHOD_SESSION_NEW} (buffer 0)",
        "pin mode",
        "derived spec",
        "confirm projection",
        "model",
        "drain",
    ]
    assert client._session_id == "sess-2"
    assert client.last_prompt_stats.context_pct == 0.0
    assert client.last_prompt_stats.cost_baseline_sid == "sess-2"
    assert client._between_turn_credits == 0.0


@pytest.mark.asyncio
async def test_the_fresh_conversation_holds_no_picture_the_old_one_was_sent(tmp_path, monkeypatch):
    client = _live_claude(tmp_path, monkeypatch, [])
    staged = client._sent_images.staging()
    staged.stage(["a" * 64])
    staged.commit()

    await client.new_conversation()

    assert not client._sent_images.holds("a" * 64)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [AcpError("model refused"), asyncio.CancelledError()])
async def test_a_failure_on_the_way_ends_the_process(tmp_path, monkeypatch, failure):
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._apply_startup_model = AsyncMock(side_effect=failure)  # type: ignore[method-assign]

    with pytest.raises(type(failure)):
        await client.new_conversation()

    assert steps[-1] == "kill"
    assert client._session_id is None


@pytest.mark.asyncio
async def test_a_replaced_settings_file_gives_way_to_a_cold_start(tmp_path, monkeypatch):
    """Only a fresh client leaves a replaced settings file out and pins the asking mode."""
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._claude_settings_authored = True

    async def reseed() -> None:
        steps.append("seed")
        client._claude_settings_authored = False

    client._seed_session_settings = reseed  # type: ignore[method-assign]

    with pytest.raises(AcpError, match="settings file was replaced"):
        await client.new_conversation()

    assert steps == ["seed", "kill"]


@pytest.mark.asyncio
async def test_a_backend_outside_the_set_raises_naming_it_and_starts_nothing(tmp_path):
    client = AcpClient(acp_backend=ACP_BACKEND_OPENCODE, work_dir=tmp_path)
    client._new_session_following_substitution = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(NotImplementedError, match="'opencode' backend has no fresh-conversation"):
        await client.new_conversation()

    client._new_session_following_substitution.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_the_closed_sessions_connection_bridges_go_after_session_close(
    tmp_path, monkeypatch, fails
):
    """The fresh array projects its own bridges; the closed session's would only pile
    up in the gateway's shared bridge table until the process ended."""
    from kiro_crew.acp import kas_env_launch

    steps: list[str] = []
    discarded: list[list[str]] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._crew_bridge_snapshots = ["old-bridge"]
    monkeypatch.setattr(
        kas_env_launch, "discard_batch", lambda agents, written: discarded.append(list(written))
    )

    async def fresh_array() -> None:
        steps.append("mcp array")
        client._crew_bridge_snapshots.append("new-bridge")

    client._prepare_session_mcp = fresh_array  # type: ignore[method-assign]
    if fails:
        client._wait_for_response = AsyncMock(side_effect=AcpError("close refused"))  # type: ignore[method-assign]
        with pytest.raises(AcpError):
            await client.new_conversation()
        # Never closed: the process end removes both batches together.
        assert discarded == [["new-bridge", "old-bridge"]]
    else:
        await client.new_conversation()
        assert discarded == [["old-bridge"]]
        assert client._crew_bridge_snapshots == ["new-bridge"]
