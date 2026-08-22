from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.crew_dispatch import (
    INTERNAL_QUALITY_WORKFLOW,
    _quality_request,
    automatic_workflow_source,
)
from kiro_crew.dashboard import chat_runner


def test_quality_request_drops_command_shaped_fields() -> None:
    normalized = _quality_request(
        {
            "request": "Review the release",
            "project_path": "/tmp/project",
            "command": "rm -rf /",
            "argv": ["sh", "-c", "echo unsafe"],
            "shell": True,
            "cwd": "/tmp",
            "executable": "/bin/sh",
        }
    )

    assert normalized == {
        "request": "Review the release",
        "project_path": "/tmp/project",
        "changed_paths": [],
        "acceptance_criteria": [],
        "check_ids": ["playwright_cli_capability"],
        "route": "full_quality_review",
    }


def _slot_messages(slot) -> list[str]:
    return [message.get("content", "") for message in slot.messages]


@pytest.mark.asyncio
async def test_direct_quality_engineering_command_starts_bounded_full_review(tmp_path) -> None:
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("direct-quality")
    slot.project = str(tmp_path)
    service = MagicMock()
    service.start = AsyncMock(return_value={"run_id": "direct-run-1"})
    state.workflow_service = service

    offload = AsyncMock(side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs))
    with patch.object(chat_runner.asyncio, "to_thread", offload):
        await chat_runner._handle_crew_command(
            state,
            slot,
            "/crew quality-engineering Review release readiness",
        )

    offload.assert_awaited_once()
    assert offload.await_args.args[0] is chat_runner._direct_quality_project

    service.start.assert_awaited_once()
    call = service.start.await_args
    assert call.args[0] == automatic_workflow_source()
    assert call.kwargs["name"] == "automatic-crew-routing"
    assert call.kwargs["author"] == "direct-crew-command"
    assert call.kwargs["_allow_native_crew"] is True
    assert call.kwargs["session_key"] == f"dashboard:{slot.key}"
    assert call.kwargs["args"] == {
        "__crew_workflow": INTERNAL_QUALITY_WORKFLOW,
        "route": "full_quality_review",
        "request": "Review release readiness",
        "project_path": str(tmp_path.resolve()),
        "check_ids": ["playwright_cli_capability"],
    }
    assert state._automatic_route_runs[slot.key] == "direct-run-1"
    assert any(
        "Started Quality Engineering full_quality_review" in text for text in _slot_messages(slot)
    )
    assert not state.sessions.get_or_create.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "/crew",
        "/crew software-delivery fix the API",
        "/crew quality-engineering",
    ],
)
async def test_direct_quality_engineering_command_rejects_invalid_syntax(tmp_path, message) -> None:
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("invalid-quality")
    slot.project = str(tmp_path)
    service = MagicMock()
    service.start = AsyncMock(return_value={"run_id": "unexpected"})
    state.workflow_service = service

    await chat_runner._handle_crew_command(state, slot, message)

    service.start.assert_not_awaited()
    assert state._automatic_route_runs == {}
    assert any(
        "Usage: `/crew quality-engineering <request>`" in text for text in _slot_messages(slot)
    )


@pytest.mark.asyncio
async def test_direct_quality_engineering_command_rejects_unbound_project(tmp_path) -> None:
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("unbound-quality")
    slot.project = "relative/project"
    service = MagicMock()
    service.start = AsyncMock(return_value={"run_id": "unexpected"})
    state.workflow_service = service

    await chat_runner._handle_crew_command(
        state,
        slot,
        "/crew quality-engineering Review the project",
    )

    service.start.assert_not_awaited()
    assert any("active slot project must be an absolute" in text for text in _slot_messages(slot))


@pytest.mark.asyncio
async def test_run_chat_handles_direct_command_before_provider_acquisition(tmp_path) -> None:
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("run-chat-quality")
    slot.project = str(tmp_path)
    handler = AsyncMock()

    with (
        patch.object(chat_runner, "_handle_crew_command", handler),
        patch.object(
            chat_runner.KiroCrewConfig,
            "load",
            return_value=SimpleNamespace(agent=SimpleNamespace(provider="kiro")),
        ),
    ):
        await chat_runner._run_chat(
            state,
            slot,
            "/crew quality-engineering Check release readiness",
        )

    handler.assert_awaited_once_with(
        state,
        slot,
        "/crew quality-engineering Check release readiness",
    )
    assert not state.sessions.get_or_create.called
