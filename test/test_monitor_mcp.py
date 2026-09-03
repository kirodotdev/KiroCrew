from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import mcp_core, session_directive
from kiro_crew.mcp_tools import control


def test_monitor_watch_does_not_offer_an_unenforced_evidence_scope():
    schema = next(item for item in control.schemas() if item["name"] == "monitor_watch")

    assert "evidence_scope" not in schema["inputSchema"]["properties"]


def test_monitor_watch_is_stateless_and_canonical(gateway_posts):
    with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="dashboard:chat-1"):
        result = mcp_core._call_tool(
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
    # BOTH halves of the delivery contract: the marker above, and the
    # out-of-band record parked for a consumer that never sees the marker —
    # carrying the same canonicalized payload the marker carries.
    # The CALL is reported (tool + raw args); the gateway derives the record.
    assert len(gateway_posts) == 1
    assert gateway_posts[0][0] == "/api/session-directive"
    assert gateway_posts[0][1]["tool"] == "monitor_watch"
    assert gateway_posts[0][1]["raw_args"]["target"] == "https://www.github.com/acme/widgets/pull/7"


def test_monitor_watch_rejects_native_subagent_binding(gateway_posts):
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
    # A refusal must not publish: no marker, no parked record.
    assert gateway_posts == []


def test_monitor_watch_rejects_webex_while_finite_legacy_loop_remains_available(gateway_posts):
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
        legacy = mcp_core._call_tool(
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
    # Only the accepted legacy loop publishes; the three refusals park nothing.
    assert [(p, b["tool"]) for p, b in gateway_posts] == [
        ("/api/session-directive", "monitor_start")
    ]


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


def test_structured_update_and_stop_reject_native_subagent_binding(gateway_posts):
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
    # A refusal must not publish: no marker, no parked record.
    assert gateway_posts == []


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
                    "token_usage_known": False,
                    "wake_instructions": "large prompt omitted from inspect",
                    "last_wake_reason_code": "checks_failed",
                    "user_stop_reason": "operator stopped",
                    "last_observation_status": "pending",
                    "last_observation_reason_code": "checks_pending",
                    "last_observation_summary": "Two checks are pending.",
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
    assert payload["monitor"]["token_usage_known"] is False
    assert payload["monitor"]["last_observation_status"] == "pending"
    assert payload["monitor"]["last_observation_reason_code"] == "checks_pending"
    assert "last_observation_summary" not in payload["monitor"]
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


def test_monitor_inspect_surfaces_armed_autonudge_loop():
    """monitor_inspect must let a caller see an armed auto-nudge loop.

    The gateway reports ``monitor: None`` (no structured monitor) together with
    a truthful ``autonudge_loop`` reading; the compact projection must pass that
    reading through so the caller can tell armed-auto-nudge from nothing armed.
    """
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="dashboard:chat-1-1"),
        patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "enabled": True,
                "monitor": None,
                "autonudge_loop": {
                    "id": "lp-9",
                    "active": True,
                    "idle_secs": 300,
                    "cycle_count": 4,
                    "last_fire_ts": 123.0,
                },
            },
        ),
    ):
        result = control.monitor_inspect("monitor_inspect", {})

    payload = json.loads(result)
    assert payload["monitor"] is None
    assert payload["autonudge_loop"] == {
        "id": "lp-9",
        "active": True,
        "idle_secs": 300,
        "cycle_count": 4,
        "last_fire_ts": 123.0,
    }


def test_monitor_inspect_reports_no_loop_distinctly_from_armed():
    """The no-loop reading carries ``autonudge_loop: None``, distinct from armed."""
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="dashboard:chat-1-1"),
        patch(
            "kiro_crew.mcp_core._get",
            return_value={"enabled": True, "monitor": None, "autonudge_loop": None},
        ),
    ):
        result = control.monitor_inspect("monitor_inspect", {})

    payload = json.loads(result)
    assert payload["monitor"] is None
    assert payload["autonudge_loop"] is None
