"""Saved workflow slash-command parsing and local execution."""

from __future__ import annotations

import threading

import pytest

from kiro_crew.dashboard.chat_runner import _handle_workflow_command
from kiro_crew.dashboard.chat_utils import parse_workflow_command


class FakeSlot:
    key = "main"
    agent = "kirocrew"

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def append(self, role, content, cls):
        self.messages.append((role, content, cls))


class FakeService:
    def __init__(self) -> None:
        self.started = None
        self.list_thread_id = None

    def list_definitions(self):
        self.list_thread_id = threading.get_ident()
        return [
            {
                "id": "wfd_1",
                "slug": "debug-project",
                "name": "Debug Project",
                "description": "Investigate a failure",
                "revision": 2,
            }
        ]

    async def start_definition(self, workflow_ref, **kwargs):
        self.started = (workflow_ref, kwargs)
        return {"run_id": "wf_1", "slug": workflow_ref, "revision": 2}


class FakeState:
    def __init__(self) -> None:
        self.workflow_service = FakeService()
        self.pushed = 0

    def push_slots_update(self):
        self.pushed += 1


def test_parse_workflow_command_keeps_free_form_input() -> None:
    assert parse_workflow_command("/workflow debug-project failing login on staging") == (
        "debug-project",
        "failing login on staging",
    )
    assert parse_workflow_command("/workflow") == ("", "")
    assert parse_workflow_command("/workflows") is None


@pytest.mark.asyncio
async def test_bare_workflow_command_lists_saved_slugs(monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: _FakeSel())
    state = FakeState()
    slot = FakeSlot()

    event_loop_thread_id = threading.get_ident()
    await _handle_workflow_command(state, slot, "/workflow", "dashboard:main")

    assert "/workflow debug-project" in slot.messages[0][1]
    assert state.workflow_service.list_thread_id != event_loop_thread_id
    assert slot.messages[-1][0] == "done"


@pytest.mark.asyncio
async def test_named_workflow_runs_exact_definition_and_maps_input(monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: _FakeSel())
    state = FakeState()
    slot = FakeSlot()

    await _handle_workflow_command(
        state,
        slot,
        "/workflow debug-project failing login",
        "dashboard:main",
    )

    assert state.workflow_service.started == (
        "debug-project",
        {
            "input_text": "failing login",
            "author": "dashboard:main",
            "session_key": "dashboard:main",
        },
    )
    assert "wf_1" in slot.messages[0][1]


class _FakeSel:
    def log_tool_invocation(self, **kwargs):
        return None
