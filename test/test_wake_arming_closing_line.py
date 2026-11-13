"""Monitor and spawn acks ask for a one-line reply before the turn ends.

``monitor_start``, ``monitor_watch``, ``spawn_run`` and ``spawn_continue`` hand the
rest of the work to a later wake. The turn-end contract (a turn ends with a
closing text or ``nothing_to_do``) still holds for these turns: a bare stop keeps
the empty-response recovery, so each ack tells the model to write one closing
line before it ends the turn.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew import session_directive

_CLOSE = "reply with one line"


def _lower(text: str) -> str:
    return session_directive.strip_marker(text).lower()


def test_monitor_start_ack_asks_for_a_closing_line():
    from kiro_crew.mcp_tools.control_parts.monitor import monitor_start

    out = _lower(monitor_start("monitor_start", {"message": "watch the build"}))
    assert _CLOSE in out
    assert "end your turn now" not in out


def test_monitor_watch_ack_asks_for_a_closing_line():
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools.control_parts import monitor as mon

    with (
        patch.object(mcp_core, "require_strict_session_key", return_value=("chat-1", "")),
        patch.object(mcp_core, "_structured_monitor_binding_key", return_value="chat-1"),
        patch.object(mon, "_arming_preflight_refusal", return_value=""),
    ):
        out = _lower(
            mon.monitor_watch(
                "monitor_watch",
                {
                    "kind": "github_pull_request",
                    "target": "https://github.com/o/r/pull/1",
                    "objective": "review_ready",
                },
            )
        )
    assert _CLOSE in out
    assert "end your turn now" not in out


def test_armed_monitor_outcome_asks_for_a_closing_line():
    """The applied result replaces the ack in the transcript, so it says the same."""
    import inspect

    from kiro_crew.dashboard import session_directive_apply_monitor as m

    src = inspect.getsource(m._monitor_start).lower()
    assert "reply with one line saying the watch is armed" in src
    assert "end your turn now" not in src


def test_monitor_start_schema_asks_for_a_closing_line():
    from kiro_crew.mcp_tools.control_parts.schemas import schemas

    desc = next(t for t in schemas() if t["name"] == "monitor_start")["description"]
    assert "reply with one line and end your turn" in desc.lower()


@pytest.mark.parametrize("can_work", [True, False])
def test_spawn_run_receipt_asks_for_a_closing_line(can_work):
    from kiro_crew.mcp_tools.spawn import _spawn_run_receipt

    out = _spawn_run_receipt(
        agent_ids=["s1"],
        agent_names=[""],
        agent_tasks=["one"],
        effort_drops=[],
        effort_applies=[],
        effort_pendings=[],
        queued_reasons={},
        errors=[],
        transport_errors=[],
        parent_session="dashboard:chat-1",
        can_work=can_work,
        keep=False,
    ).lower()
    assert _CLOSE in out
    assert "end your turn now" not in out


def test_spawn_continue_receipt_asks_for_a_closing_line():
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools.spawn import spawn_continue

    with (
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "_post", return_value={"id": "r2"}),
    ):
        out = spawn_continue("spawn_continue", {"conversation": "c1", "task": "go on"})
    assert _CLOSE in out.lower()
