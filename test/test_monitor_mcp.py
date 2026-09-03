from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import mcp_core, session_directive
from kiro_crew.mcp_tools import control


def test_monitor_watch_is_stateless_and_canonical():
    with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="dashboard:chat-1"):
        result = control.monitor_watch(
            "monitor_watch",
            {
                "kind": "github_pull_request",
                "target": "https://www.github.com/acme/widgets/pull/7",
                "objective": "review_ready",
            },
        )

    args = session_directive.decode(result, "monitor_watch")
    assert args is not None
    assert args["target"] == "https://github.com/acme/widgets/pull/7"
    assert "session_key" not in json.dumps(args)
    assert "loop_id" not in json.dumps(args)


def test_monitor_watch_rejects_native_subagent_binding():
    with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="subagent:child"):
        result = control.monitor_watch(
            "monitor_watch",
            {
                "kind": "github_pull_request",
                "target": "https://github.com/acme/widgets/pull/7",
                "objective": "review_ready",
            },
        )
    assert session_directive.decode(result, "monitor_watch") is None
    assert "only works" in result


def test_monitor_watch_rejects_webex_while_finite_legacy_loop_remains_available():
    audit = MagicMock()
    with (
        patch(
            "kiro_crew.mcp_core._resolve_session_key_strict",
            return_value="webex:kirocrew:direct:operator@example.com",
        ),
        patch("kiro_crew.mcp_core.sel", return_value=audit),
    ):
        structured = control.monitor_watch(
            "monitor_watch",
            {
                "kind": "github_pull_request",
                "target": "https://github.com/acme/widgets/pull/7",
                "objective": "review_ready",
            },
        )
        legacy = control.monitor_start(
            "monitor_start",
            {
                "message": "Check the pull request and stop when ready.",
                "interval_secs": 300,
                "max_cycles": 24,
                "max_runtime_secs": 14_400,
            },
        )
        inspect = control.monitor_inspect("monitor_inspect", {})
        stop = control.monitor_stop("monitor_stop", {"reason": "done"})

    assert structured.startswith("Error:")
    assert session_directive.decode(structured, "monitor_watch") is None
    assert inspect.startswith("Error:")
    assert stop.startswith("Error:")
    assert session_directive.decode(stop, "monitor_stop") is None
    legacy_args = session_directive.decode(legacy, "monitor_start")
    assert legacy_args is not None
    assert legacy_args["max_cycles"] == 24
    assert legacy_args["max_runtime_secs"] == 14_400


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        (
            "monitor_watch",
            {
                "kind": "github_pull_request",
                "target": "https://github.com/acme/widgets/pull/7",
                "objective": "review_ready",
            },
        ),
        ("monitor_inspect", {}),
        ("monitor_update", {"wake_instructions": "Check CI."}),
        ("monitor_stop", {"reason": "done"}),
    ],
)
def test_structured_monitor_session_refusals_are_failed_and_audited(tool_name, args):
    audit = MagicMock()
    with (
        patch("kiro_crew.mcp_core._resolve_session_key", return_value="subagent:child"),
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="subagent:child"),
        patch("kiro_crew.mcp_core.sel", return_value=audit),
        patch("kiro_crew.mcp_shared.sel", return_value=audit),
    ):
        result = mcp_core._call_tool(tool_name, args)

    assert result.startswith("Error:")
    outcomes = [call.kwargs.get("outcome") for call in audit.log_tool_invocation.call_args_list]
    assert "denied" in outcomes
    assert "failed" in outcomes
    assert "completed" not in outcomes


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        (
            "monitor_watch",
            {
                "kind": "github_pull_request",
                "target": "https://github.com/acme/widgets/pull/7",
                "objective": "review_ready",
            },
        ),
        ("monitor_update", {"wake_instructions": "Check CI."}),
        ("monitor_stop", {"reason": "done"}),
    ],
)
def test_structured_monitor_mutations_require_strict_session_identity(tool_name, args):
    audit = MagicMock()
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
        patch("kiro_crew.mcp_core.sel", return_value=audit),
    ):
        result = getattr(control, tool_name)(tool_name, args)

    assert result.startswith("Error:")
    assert session_directive.decode(result, tool_name) is None
    audit.log_tool_invocation.assert_called_once()
    assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "denied"


def test_structured_update_and_stop_reject_native_subagent_binding():
    with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="subagent:child"):
        update = control.monitor_update(
            "monitor_update",
            {"wake_instructions": "Check CI."},
        )
        stop = control.monitor_stop("monitor_stop", {"reason": "done"})

    assert session_directive.decode(update, "monitor_update") is None
    assert session_directive.decode(stop, "monitor_stop") is None
    assert "only works" in update
    assert "only works" in stop


def test_monitor_inspect_passes_strict_identity_without_fallback():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="slack:123"),
        patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "enabled": True,
                "active": True,
                "monitor": {
                    "kind": "github_pull_request",
                    "created_ts": 123.0,
                    "wake_count": 3,
                    "wake_instructions": "large prompt omitted from inspect",
                    "last_wake_reason_code": "checks_failed",
                    "user_stop_reason": "operator stopped",
                    "last_observation": {
                        "head_revision": "abc123",
                        "checks": {"passed": [f"check-{index}" for index in range(20)]},
                    },
                },
            },
        ) as get,
    ):
        result = control.monitor_inspect("monitor_inspect", {})

    get.assert_called_once_with("/api/autonudge/session-monitor", session_key="slack:123")
    payload = json.loads(result)
    assert payload["monitor"]["kind"] == "github_pull_request"
    assert payload["monitor"]["created_ts"] == 123.0
    assert payload["monitor"]["wake_count"] == 3
    assert payload["monitor"]["last_wake_reason_code"] == "checks_failed"
    assert payload["monitor"]["user_stop_reason"] == "operator stopped"
    assert payload["monitor"]["observation"]["checks"]["passed_count"] == 20
    assert "wake_instructions" not in payload["monitor"]
    assert "check-0" not in result


def test_monitor_inspect_never_uses_ancestor_fallback_without_strict_identity():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
        patch("kiro_crew.mcp_core._get") as get,
    ):
        result = control.monitor_inspect("monitor_inspect", {})
    get.assert_not_called()
    assert "unavailable" in result.lower()


def test_monitor_inspect_reports_internal_read_failure_as_error():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="slack:123"),
        patch("kiro_crew.mcp_core._get", return_value={"error": "gateway unavailable"}),
    ):
        result = control.monitor_inspect("monitor_inspect", {})

    assert result == "Error: Monitor inspection failed: gateway unavailable"
