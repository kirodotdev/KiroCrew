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
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import client as client_facade
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import METHOD_SESSION_CLOSE, METHOD_SESSION_NEW, METHOD_SET_MODE
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_OPENCODE,
    CC_PERMISSION_MODE_ENV,
    resolve_cc_permission_mode,
)

#: A live claude-agent-acp 0.100.0 capture of a session that requested ``auto``.
_REQUESTED_AUTO = (
    Path(__file__).parent / "fixtures" / "claude_mode_pin" / "requested-auto-applied.jsonl"
)


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


def _recorded_requested_auto_modes() -> dict:
    """The ``modes`` of the recorded ``session/new`` answer: ``default``, offering ``auto``."""
    rows = [json.loads(line) for line in _REQUESTED_AUTO.read_text(encoding="utf-8").splitlines()]
    modes = next(row["result"]["modes"] for row in rows if row.get("id") == 2)
    assert modes["currentModeId"] == "default"
    assert "auto" in {m["id"] for m in modes["availableModes"]}
    return modes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("started", "pinned"),
    [("default", None), ("bypassPermissions", "default")],
    ids=["reported-default", "pinned-to-default"],
)
@pytest.mark.parametrize("opted_in", [True, False], ids=["opt-in", "no-opt-in"])
async def test_a_requested_auto_is_applied_to_the_fresh_session_right_after_the_pin(
    tmp_path, monkeypatch, opted_in, started, pinned
):
    """``KIROCREW_CC_PERMISSION_MODE=auto`` reaches a fresh session as it reaches a cold one.

    The new session answers as the recorded 0.100.0 session did. With the opt-in it is
    moved to ``auto`` right after the pin, from the mode the pin returned when the pin
    moved it; without the opt-in no ``session/set_mode`` is sent.
    """
    if opted_in:
        monkeypatch.setenv(CC_PERMISSION_MODE_ENV, "auto")
    else:
        monkeypatch.delenv(CC_PERMISSION_MODE_ENV, raising=False)
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client._permission_mode = resolve_cc_permission_mode(None, ACP_BACKEND_CLAUDE)
    modes = {**_recorded_requested_auto_modes(), "currentModeId": started}
    sent: list[tuple[str, dict]] = []
    record_step = client._send_request

    async def send_request(method: str, params: dict) -> int:
        sent.append((method, params))
        return await record_step(method, params)

    async def new_session() -> dict:
        steps.append(f"{METHOD_SESSION_NEW} (buffer {len(client._buffer)})")
        return {"sessionId": "sess-2", "modes": modes}

    async def pin(_resp: dict) -> str | None:
        steps.append("pin mode")
        return pinned

    client._send_request = send_request  # type: ignore[method-assign]
    client._new_session_following_substitution = new_session  # type: ignore[method-assign]
    client._pin_claude_starting_mode = pin  # type: ignore[method-assign]

    await client.new_conversation()

    applied = [f"{METHOD_SET_MODE} sess-2"] if opted_in else []
    assert steps == [
        "seed",
        "side denies",
        "mcp array",
        f"{METHOD_SESSION_CLOSE} sess-1",
        f"{METHOD_SESSION_NEW} (buffer 0)",
        "pin mode",
        *applied,
        "derived spec",
        "confirm projection",
        "model",
        "drain",
    ]
    expected = [{"sessionId": "sess-2", "modeId": "auto"}] if opted_in else []
    assert [p for m, p in sent if m == METHOD_SET_MODE] == expected
    assert client._session_id == "sess-2"


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
