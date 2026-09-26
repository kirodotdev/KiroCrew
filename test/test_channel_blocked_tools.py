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

from kiro_crew.channel import (
    CHANNEL_AGENT_BLOCKED_DISPATCH_OPERATIONS,
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


def test_blocked_tools_cover_every_dispatch_verb():
    """The dispatch verbs reach the interactive guard through the same list.

    They are appended to ``CHANNEL_AGENT_BLOCKED_TOOLS`` rather than respelled
    there, so this asserts the concatenation actually happened: a verb present in
    the dispatch tuple but absent from the rendered-title list would be refused
    at MCP dispatch and approved at the permission prompt, which is two answers
    to one question.
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
    }


def test_operation_map_names_the_operations_that_start_work():
    """A passthrough tool is held by operation, so the operations are pinned too.

    ``ops_mission_control_api`` carries a whole API surface behind one name, and
    only ``POST /rotation/arm`` starts work that outlives the turn: it arms the
    app's crons, which fire unattended afterwards. Every other operation reads, or
    writes a record inside the turn, so the tool itself is not on the name list.
    """
    assert CHANNEL_AGENT_BLOCKED_DISPATCH_OPERATIONS == {
        "ops_mission_control_api": (("POST", "/rotation/arm"),),
    }


def test_blocked_operations_are_real_operations_of_their_tool():
    """A typo here would deny nothing and read as protection.

    The operation is matched against the arguments a caller sends, so a method or
    path that the tool's own schema does not accept can never match, and the deny
    would be silently dead. The tool's validator holds the authoritative surface,
    so the pair is asserted against it rather than against a second spelling.
    """
    from kiro_crew.validation import OPS_MISSION_CONTROL_ALLOWED_CALLS

    for tool, operations in CHANNEL_AGENT_BLOCKED_DISPATCH_OPERATIONS.items():
        assert tool == "ops_mission_control_api", f"no known surface for {tool}"
        for operation in operations:
            assert operation in OPS_MISSION_CONTROL_ALLOWED_CALLS, operation


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

    Each of these reads a context that already exists or ends one; none starts a
    turn, so none is on the dispatch list. ``spawn_status`` is the one worth
    naming: it returns a retained transcript, and how widely that read is scoped
    is a question about read scope rather than about this boundary. A change that
    decides to contain it should fail here and say so.
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
    # trusted=True is the case that matters: channel trust auto-approves every
    # request the containment list does not hold back, so no human sees this one.
    await _stream_task(_make_agent(), _make_channel(), client, "hi")
    client.reject_tool.assert_awaited_once_with(11)
    client.approve_tool.assert_not_awaited()


@pytest.mark.parametrize(
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
)
def test_dispatch_verb_matcher_precision(rendered, expected):
    from kiro_crew.channel import _blocked_tool_named

    assert _blocked_tool_named(rendered) is expected
