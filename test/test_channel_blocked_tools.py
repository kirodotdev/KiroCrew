"""Channel-agent blocked-tool containment boundary.

Channel agents communicate exclusively through channel posts, so
direct-to-user messaging tools are rejected unconditionally — BEFORE any
YOLO / channel-trust auto-approval.  ``send_notification`` reaches the user
like ``send_message`` does (feed publish, badge, sound),
so both must sit behind the same boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from named_cases_helpers import named_cases

from kiro_crew.channel import (
    CHANNEL_AGENT_BLOCKED_DISPATCH_TOOLS,
    CHANNEL_AGENT_BLOCKED_TOOLS,
    _stream_task,
)
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST


def _make_agent():
    return SimpleNamespace(
        id="a1",
        role="dev",
        agent_name="dev",
        session_key="channel:test",
        _approval_future=None,
    )


def _make_channel():
    ch = SimpleNamespace(id="c1", trusted=True, members={})
    ch._broadcast = MagicMock()
    ch.post = AsyncMock()
    return ch


def _make_client(events):
    client = SimpleNamespace()

    async def _stream(message):
        for ev in events:
            yield ev

    client.stream = _stream
    client.approve_tool = AsyncMock()
    client.reject_tool = AsyncMock()
    return client


def test_blocked_tools_cover_both_messaging_tools():
    assert "send_message" in CHANNEL_AGENT_BLOCKED_TOOLS
    assert "send_notification" in CHANNEL_AGENT_BLOCKED_TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["send_message", "send_notification"])
async def test_blocked_tool_rejected_even_on_trusted_channel(monkeypatch, tool):
    sel_mock = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
    events = [
        SimpleNamespace(
            kind=EVENT_PERMISSION_REQUEST,
            text=f"{tool} (kirocrew-core)",
            title="",
            request_id=7,
            tool_input="{}",
        ),
        SimpleNamespace(kind=EVENT_COMPLETE),
    ]
    client = _make_client(events)
    # trusted=True: the block must fire BEFORE channel-trust auto-approval.
    await _stream_task(_make_agent(), _make_channel(), client, "hi")
    client.reject_tool.assert_awaited_once_with(7)
    client.approve_tool.assert_not_awaited()
    outcomes = [kw.get("outcome") for _, kw in sel_mock.log_tool_invocation.call_args_list]
    assert "rejected_blocked_tool" in outcomes


@pytest.mark.parametrize(
    "rendered,expected",
    [
        *named_cases(
            "test_blocked_tool_matcher_precision",
            "rendered,expected",
            [
                # Positive: the tool itself, in every rendered form kiro-cli emits.
                ("send_message", True),
                ("send_notification (kirocrew-core)", True),
                ("kirocrew-core___send_message", True),
                ("mcp__kirocrew-core__send_message", True),  # canonical MCP prefix
                # opencode joins server and tool with ONE underscore (measured on
                # 1.18.30), which the 2+ run normalization leaves intact -- and the
                # boundary lookbehind then refuses the match, so the whole containment
                # list read as absent on that harness. Both Crew servers whose tools are
                # on the list spell it this way.
                ("kirocrew-core_send_message", True),
                ("kirocrew-work_work_report", True),
                ("Running: kirocrew-core_session_send", True),
                ('Tool: "send_notification"', True),
                # Negative: filenames/paths/identifiers that merely
                # CONTAIN a blocked tool name must not trip the containment guard.
                ("Editing send_notification.py", False),
                ("Reading /tmp/send_message_backup.txt", False),
                ("fs_write path=src/send_notification_helpers.py", False),
                ("grep send_message_v2", False),
                # The Crew server prefix is what earns the normalization: a single
                # underscore alone would unblock every identifier ending in a blocked
                # name, and a longer tail is still not the tool.
                ("do_send_message", False),
                ("evil_send_notification", False),
                ("kirocrew-core_send_message_v2", False),
                # A rendered PATH is not a tool call, even when a directory happens to
                # carry the server-prefixed name: the left boundary excludes `/` and `.`
                # exactly as the blocked-tool pattern's own does.
                ("cat /tmp/kirocrew-core_send_message", False),
                ("Reading ./kirocrew-core_send_message.log", False),
            ],
        ),
        *named_cases(
            "test_dispatch_verb_matcher_precision",
            "rendered,expected",
            [
                # Positive: every rendered form kiro-cli and opencode emit.
                ("spawn_run", True),
                ("spawn_sub_agents (kirocrew-core)", True),
                ("kirocrew-core___spawn_run", True),
                ("mcp__kirocrew-core__workflow_run", True),
                ("kirocrew-core_task_run", True),
                ('Tool: "register_hook"', True),
                ("Running: kirocrew-core_spawn_steer", True),
                ("workflow_rerun_subtree", True),
                # Negative: an identifier or filename that merely CONTAINS one of the
                # names is not a tool call. A longer tail keeps the name from standing
                # alone, which is what the boundary lookahead tests.
                ("Editing task_runner.py", False),
                ("Reading /tmp/spawn_run_backup.txt", False),
                ("fs_write path=src/workflow_runner.ts", False),
                ("grep register_hooks", False),
                ("my_task_run", False),
                ("kirocrew-core_spawn_run_v2", False),
                ("cat /tmp/kirocrew-core_spawn_run", False),
            ],
        ),
    ],
)
def test_tool_matcher_precision(rendered, expected):
    from kiro_crew.channel import _blocked_tool_named

    assert _blocked_tool_named(rendered) is expected


def test_every_session_control_tool_is_contained():
    """The whole session-control surface sits behind this boundary, not part of it.

    Pinned against the advertised tool set, so a new verb fails here. `session_create`
    belongs too: it makes a persistent, sidebar-visible session outside containment.
    """
    from kiro_crew.mcp_dashboard import SESSION_CONTROL_TOOLS

    missing = sorted(set(SESSION_CONTROL_TOOLS) - set(CHANNEL_AGENT_BLOCKED_TOOLS))
    assert not missing, f"session-control tools reachable from a channel agent: {missing}"


def test_blocked_tools_cover_every_dispatch_verb():
    """The dispatch verbs reach the interactive guard through the same list.

    A verb missing from ``CHANNEL_AGENT_BLOCKED_TOOLS`` would be refused at MCP
    dispatch yet approved at the permission prompt.
    """
    missing = sorted(set(CHANNEL_AGENT_BLOCKED_DISPATCH_TOOLS) - set(CHANNEL_AGENT_BLOCKED_TOOLS))
    assert not missing, f"dispatch verbs missing from the rendered-title list: {missing}"


def test_dispatch_tuple_names_the_verbs_that_start_work():
    """Pinned by name, because the set IS the security boundary.

    Reading it off the advertised tool list is not possible: the advertised list
    mixes verbs that start work with verbs that only watch it, and only a human
    reading of each verb decides which is which.
    """
    assert set(CHANNEL_AGENT_BLOCKED_DISPATCH_TOOLS) == {
        "spawn_run",
        "spawn_sub_agents",
        "spawn_continue",
        "spawn_steer",
        "workflow_run",
        "workflow_author",
        "workflow_rerun_subtree",
        "task_run",
        "register_hook",
        "pod_up",
        "pod_restart",
    }


@pytest.mark.parametrize(
    "tool",
    [
        "spawn_list",
        "spawn_status",
        "spawn_release",
        "workflow_status",
        "workflow_result",
        "workflow_list",
        "workflow_cancel",
        "workflow_library_list",
        "pod_ls",
        "pod_status",
        "pod_down",
    ],
)
def test_observe_and_teardown_verbs_stay_reachable(tool):
    """Their absence from the boundary is a decision, so it is asserted.

    None starts a turn. ``spawn_status`` returns a transcript, but that is a read-scope
    question, not this boundary's.
    """
    assert tool not in CHANNEL_AGENT_BLOCKED_DISPATCH_TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", list(CHANNEL_AGENT_BLOCKED_DISPATCH_TOOLS))
async def test_dispatch_verb_rejected_even_on_trusted_channel(monkeypatch, tool):
    sel_mock = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
    events = [
        SimpleNamespace(
            kind=EVENT_PERMISSION_REQUEST,
            text=f"{tool} (kirocrew-core)",
            title="",
            request_id=11,
            tool_input="{}",
        ),
        SimpleNamespace(kind=EVENT_COMPLETE),
    ]
    client = _make_client(events)
    steered: list[str] = []

    async def _steer(notice):
        steered.append(notice)
        return True

    client.supports_refusal_steer = True
    client.steer = _steer
    # trusted=True is the case that matters: channel trust auto-approves every
    # request the containment list does not hold back, so no human sees this one.
    await _stream_task(_make_agent(), _make_channel(), client, "hi")
    client.reject_tool.assert_awaited_once_with(11)
    client.approve_tool.assert_not_awaited()
    # The model is told the surface refused the call, and why, before the reject.
    assert len(steered) == 1
    assert tool in steered[0]
    assert "direct-to-user messaging tools do not run here" in steered[0]


def test_session_pin_is_contained():
    """Pinning rearranges the person's sidebar, the reason the tree verbs are blocked."""
    from kiro_crew.channel import _blocked_tool_named

    assert "chat_session_pin" in CHANNEL_AGENT_BLOCKED_TOOLS
    assert _blocked_tool_named("kirocrew-dashboard___chat_session_pin") is True
    assert "chat_session_pin_move" in CHANNEL_AGENT_BLOCKED_TOOLS
    assert _blocked_tool_named("kirocrew-dashboard___chat_session_pin_move") is True


def test_session_move_to_column_is_contained():
    """Moving a card retags another session on the person's board."""
    from kiro_crew.channel import _blocked_tool_named

    assert "session_move_to_column" in CHANNEL_AGENT_BLOCKED_TOOLS
    assert _blocked_tool_named("kirocrew-dashboard___session_move_to_column") is True
