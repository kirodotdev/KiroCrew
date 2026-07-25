"""Channel-agent blocked-tool containment boundary (PR #422 round 15).

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
        ("mcp__kirocrew-core__send_message", True),  # canonical MCP prefix (round 20)
        ('Tool: "send_notification"', True),
        # Negative (GPT 5.6 round 19): filenames/paths/identifiers that merely
        # CONTAIN a blocked tool name must not trip the containment guard.
        ("Editing send_notification.py", False),
        ("Reading /tmp/send_message_backup.txt", False),
        ("fs_write path=src/send_notification_helpers.py", False),
        ("grep send_message_v2", False),
    ],
)
def test_blocked_tool_matcher_precision(rendered, expected):
    from kiro_crew.channel import _blocked_tool_named

    assert _blocked_tool_named(rendered) is expected
