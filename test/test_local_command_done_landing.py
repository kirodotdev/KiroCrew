"""Landing contract for provider-free dashboard slash commands."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_utils import _build_stream_chunk
from kiro_crew.dashboard.remote_relay import relay_remote_turn


async def _relay_rows(rows: list[dict]) -> AsyncIterator[bytes]:
    for row in rows:
        chunk = _build_stream_chunk(row, include_row_meta=True)
        yield f"data: {chunk}\n\n".encode()
    yield b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_remote_workflow_direct_command_lands_without_retry(tmp_path, monkeypatch) -> None:
    """The peer's local command verdict reaches the relay before ``[DONE]``."""
    peer_dir = tmp_path / "peer"
    peer_dir.mkdir()
    peer_state = _make_state(peer_dir)
    peer_slot = peer_state.get_or_create_slot("peer-chat")
    peer_state.workflow_service = MagicMock()
    peer_state.workflow_service.start_definition = AsyncMock(
        return_value={"run_id": "run-1", "slug": "nightly", "revision": 3}
    )
    monkeypatch.setattr(chat_runner, "sel", lambda: MagicMock())

    await chat_runner._handle_workflow_command(
        peer_state,
        peer_slot,
        "/workflow nightly",
        "dashboard:peer-chat",
    )

    peer_state.workflow_service.start_definition.assert_awaited_once()
    done_rows = [row for row in peer_slot.messages if row["role"] == "done"]
    assert len(done_rows) == 1
    assert done_rows[0]["meta"] == {"turn_landed": True}

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    local_state = _make_state(local_dir)
    local_slot = local_state.get_or_create_slot("local-chat")

    landed = await relay_remote_turn(
        local_state,
        local_slot,
        "/workflow nightly",
        chunks=_relay_rows(peer_slot.messages),
    )

    assert landed is True, "a successful direct command must settle, not replay"
    peer_state.workflow_service.start_definition.assert_awaited_once()


def test_all_direct_local_command_terminals_share_the_landed_helper() -> None:
    """Pin every sibling branch so a new raw ``done`` cannot omit the verdict."""
    tree = ast.parse(Path(chat_runner.__file__).read_text(encoding="utf-8"))
    helper_calls: Counter[str] = Counter()
    raw_done_appends: Counter[str] = Counter()

    for function in (
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == (
                "_append_landed_local_command_done"
            ):
                helper_calls[function.name] += 1
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and len(node.args) >= 3
                and all(
                    isinstance(arg, ast.Constant) and arg.value == expected
                    for arg, expected in zip(node.args[:3], ("done", "", "done"))
                )
            ):
                raw_done_appends[function.name] += 1

    assert helper_calls == Counter(
        {
            "_handle_workflow_command": 1,
            "_handle_goal_command": 1,
            "_run_chat": 7,
        }
    )
    assert raw_done_appends == Counter(
        {
            "_append_landed_local_command_done": 1,
            "_finish_queue_cycle": 1,
        }
    )
