from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from kiro_crew.acp._dispatch_parts.tool_result_events import _build_tool_result_event
from kiro_crew.acp.client_tool_events import _maybe_fire_post_tool_hooks
from kiro_crew.dashboard.chat_turn.turn_spec_hooks import _fire
from kiro_crew.hooks import HOOK_EVENT_POST_TOOL_USE, HOOK_EVENT_USER_PROMPT_SUBMIT
from kiro_crew.mcp_tools import workflows
from kiro_crew.validation import build_tool_response


def test_workflow_run_exposes_launch_identity_as_structured_mcp_content() -> None:
    with (
        patch.object(workflows, "validate_tool_args", side_effect=lambda _args, _schema: _args),
        patch.object(workflows, "_workflow_identity", return_value=("dashboard:chat-1", "")),
        patch.object(
            workflows.mcp_core,
            "_post",
            return_value={"run_id": "run-123", "name": "example"},
        ),
        patch.object(workflows.mcp_core, "sel") as sel,
    ):
        result = workflows.workflow_run("workflow_run", {"source": "print('done')"})

    assert isinstance(result, workflows._StructuredWorkflowToolResult)
    assert result.structured_content == {
        "kirocrew": {"launched_workflow_run_id": "run-123"}
    }
    response = build_tool_response(result)
    assert response["content"][0]["text"] == str(result)
    assert response["structuredContent"] == result.structured_content
    sel.assert_called_once()


def test_acp_result_reads_launch_id_from_structured_mcp_raw_output() -> None:
    event = _build_tool_result_event(
        {
            "toolCallId": "native-call-1",
            "status": "completed",
            "content": [{"type": "text", "text": "Started workflow run."}],
            "rawOutput": {
                "items": [
                    {
                        "Json": {
                            "content": [{"type": "text", "text": "Started workflow run."}],
                            "structuredContent": {
                                "kirocrew": {"launched_workflow_run_id": "run-123"}
                            },
                        }
                    }
                ]
            },
        }
    )

    assert event is not None
    assert event.tool_call_id == "native-call-1"
    assert event.launched_workflow_run_id == "run-123"


def test_acp_result_does_not_infer_launch_id_from_response_text() -> None:
    event = _build_tool_result_event(
        {
            "toolCallId": "native-call-2",
            "status": "completed",
            "rawOutput": {
                "items": [{"Text": "Started workflow run `run-looks-like-an-id`."}]
            },
        }
    )

    assert event is not None
    assert event.launched_workflow_run_id == ""


def test_post_tool_hook_keeps_native_and_launched_run_identities_separate() -> None:
    event = _build_tool_result_event(
        {
            "toolCallId": "native-call-3",
            "status": "completed",
            "rawOutput": {
                "items": [
                    {
                        "Json": {
                            "structuredContent": {
                                "kirocrew": {"launched_workflow_run_id": "run-child"}
                            }
                        }
                    }
                ]
            },
        }
    )
    assert event is not None
    captured: dict = {}

    class HookStore:
        async def fire(self, _event: str, **kwargs) -> None:
            captured.update(kwargs)

    client = SimpleNamespace(
        _audit_source=True,
        _observed_tool_calls={"native-call-3": ("workflow_run", "")},
        _steer_turn=SimpleNamespace(
            telemetry_context={"workflow_run_id": "run-parent", "turn_id": "turn-1"}
        ),
        _session_key="chat-1",
        _session_id="acp-1",
        _agent="",
    )
    with patch(
        "kiro_crew.acp.client_tool_events.get_global_hook_store",
        return_value=HookStore(),
    ):
        asyncio.run(_maybe_fire_post_tool_hooks(client, event))

    assert captured["tool_call_id"] == "native-call-3"
    assert captured["workflow_run_id"] == "run-parent"
    assert captured["launched_workflow_run_id"] == "run-child"
    assert captured["session_key"] == "chat-1"
    assert captured["turn_id"] == "turn-1"


def test_completion_and_launch_ids_reach_dashboard_hook_payloads() -> None:
    calls: list[tuple[str, dict]] = []

    class HookStore:
        async def fire(self, event: str, *_args, **kwargs) -> list:
            calls.append((event, kwargs))
            return []

    turn = SimpleNamespace(
        state=SimpleNamespace(_hook_store=HookStore()),
        slot=SimpleNamespace(key="dashboard:chat-1"),
        session_key="dashboard:chat-1",
        _spec_hooks_unreadable=False,
        _spec_hooks=[],
        _spec_hooks_cwd=None,
        client=SimpleNamespace(session_id="acp-1"),
        _recovery_turn_id="completion-turn",
        _workflow_run_id="completed-run",
    )

    async def exercise() -> None:
        await _fire(turn, HOOK_EVENT_USER_PROMPT_SUBMIT, "workflow result")
        await _fire(
            turn,
            HOOK_EVENT_POST_TOOL_USE,
            tool_name="workflow_run",
            tool_call_id="native-launch-call",
            launched_workflow_run_id="launched-run",
        )

    asyncio.run(exercise())

    prompt_event, post_event = calls
    assert prompt_event[1]["workflow_run_id"] == "completed-run"
    assert prompt_event[1]["turn_id"] == "completion-turn"
    assert post_event[1]["workflow_run_id"] == "completed-run"
    assert post_event[1]["launched_workflow_run_id"] == "launched-run"
    assert post_event[1]["tool_call_id"] == "native-launch-call"
