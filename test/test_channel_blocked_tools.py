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

from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_TOOLS, _stream_task
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
    outcomes = [
        kw.get("outcome")
        for _, kw in sel_mock.log_tool_invocation.call_args_list
    ]
    assert "rejected_blocked_tool" in outcomes


@pytest.mark.parametrize(
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
)
def test_blocked_tool_matcher_precision(rendered, expected):
    from kiro_crew.channel import _blocked_tool_named

    assert _blocked_tool_named(rendered) is expected


def test_every_session_control_tool_is_contained():
    """The whole session-control surface sits behind this boundary, not part of it.

    Pinned against the advertised tool set rather than a hand-written list, so a
    fourth verb fails here instead of shipping reachable from a channel agent. The
    omission this guards against is not hypothetical: `session_create` was added
    to the surface and missed here, and nothing else in the suite noticed.

    Create earns its place for a different reason than the other two. It writes
    nothing into an existing conversation, but it puts a persistent,
    sidebar-visible session outside the containment this list holds.
    """
    from kiro_crew.mcp_dashboard import SESSION_CONTROL_TOOLS

    missing = sorted(set(SESSION_CONTROL_TOOLS) - set(CHANNEL_AGENT_BLOCKED_TOOLS))
    assert not missing, f"session-control tools reachable from a channel agent: {missing}"
