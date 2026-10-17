"""The kiro-cli <-> goose / opencode tool-name tables a spec hook matcher reads."""

from __future__ import annotations

import pytest

from kiro_crew.acp import _dispatch
from kiro_crew.acp._dispatch import harness_tool_name as harness_tool_name_of
from kiro_crew.acp.harness_tool_names import (
    CLAUDE_READ_ONLY_BUILTINS,
    GOOSE_TOOL_IDS_BY_KIRO_TOOL,
    HARNESS_TOOL_TABLES,
    OPENCODE_TOOL_IDS_BY_KIRO_TOOL,
    harness_tool_match_names,
    qualified_harness_tool_id,
    split_harness_tool_id,
)
from kiro_crew.acp.kas_permissions import KAS_TOOL_IDS_BY_KIRO_TOOL, kas_tool_match_names
from kiro_crew.acp.types import JsonRpcMessage


def test_a_shell_call_answers_to_its_kiro_cli_name():
    assert harness_tool_match_names("goose#shell") == ("execute_bash", "shell")
    assert harness_tool_match_names("opencode#bash") == ("execute_bash", "bash", "shell")
    assert harness_tool_match_names("opencode#edit") == ("fs_write", "edit", "write")
    assert harness_tool_match_names("goose#tree") == ("fs_read", "tree", "read")


def test_a_tool_no_row_names_keeps_only_its_own_name():
    assert harness_tool_match_names("opencode#todowrite") == ("todowrite",)
    assert harness_tool_match_names("goose#remember_memory") == ("remember_memory",)


@pytest.mark.parametrize(
    ("tool_id", "kiro"),
    [
        ("goose#analyze", "fs_read"),
        ("opencode#list", "fs_read"),
        ("opencode#apply_patch", "fs_write"),
        ("opencode#patch", "fs_write"),
        ("opencode#multiedit", "fs_write"),
    ],
)
def test_every_reader_and_writer_answers_to_its_kiro_cli_name(tool_id, kiro):
    assert harness_tool_match_names(tool_id)[0] == kiro


def test_an_unqualified_id_is_not_read_through_these_tables():
    # A KAS id stays KAS's: None here, so spec_hooks falls back to KAS's table.
    assert harness_tool_match_names("run_command") is None
    assert harness_tool_match_names("bash") is None
    assert harness_tool_match_names("claude#Bash") is None
    assert harness_tool_match_names("goose#") is None
    assert split_harness_tool_id("kas#run_command") is None


def test_a_kas_id_can_never_carry_the_separator():
    params = {"_meta": {"kiro": {"toolId": "goose#shell"}}}
    assert _dispatch._permission_tool_id(params) == ""
    assert kas_tool_match_names("run_command")[0] == "execute_bash"


def test_only_a_backend_with_a_table_gets_a_qualified_id():
    assert qualified_harness_tool_id("goose", "shell") == "goose#shell"
    assert qualified_harness_tool_id("opencode", "bash") == "opencode#bash"
    assert qualified_harness_tool_id("claude", "Bash") == ""
    assert qualified_harness_tool_id("", "bash") == ""
    assert qualified_harness_tool_id("goose", "") == ""


@pytest.mark.parametrize(
    "table",
    [GOOSE_TOOL_IDS_BY_KIRO_TOOL, OPENCODE_TOOL_IDS_BY_KIRO_TOOL],
    ids=["goose", "opencode"],
)
def test_no_harness_tool_is_reached_from_two_kiro_cli_names(table):
    seen: dict[str, str] = {}
    for name, ids in table.items():
        for tool_id in ids:
            assert tool_id not in seen, (tool_id, seen.get(tool_id), name)
            seen[tool_id] = name


def test_every_row_is_a_kiro_cli_tool_kas_also_names():
    # Same vocabulary on the left, so a matcher means one thing on every backend.
    for table in HARNESS_TOOL_TABLES.values():
        assert set(table) <= set(KAS_TOOL_IDS_BY_KIRO_TOOL)


# ── claude's read built-ins, named on the permission event ──


def _claude_permission_event(
    update_meta: dict, kind: str, *, backend: str = "claude", side_read_only: bool = True
):
    """One claude tool_call frame through the real parser, then its permission
    request, shaped like the recorded ``acp_frames/claude/session.jsonl`` pair."""
    names: dict = {}
    _dispatch.parse_session_update(
        {
            "_meta": update_meta,
            "toolCallId": "toolu_1",
            "sessionUpdate": "tool_call",
            "rawInput": {},
            "status": "pending",
            "title": "Read",
            "kind": kind,
            "content": [],
            "locations": [],
        },
        harness_tool_name_cache=names,
    )
    msg = JsonRpcMessage(
        id=0,
        method="session/request_permission",
        params={
            "toolCall": {"toolCallId": "toolu_1", "title": "Read", "kind": kind, "rawInput": {}},
            "options": [
                {"optionId": "allow-once", "name": "Yes", "kind": "allow_once"},
                {"optionId": "reject-once", "name": "No", "kind": "reject_once"},
            ],
        },
    )
    event, _ = _dispatch.build_permission_event(
        msg,
        harness_tool_name_cache=names,
        harness_backend=backend,
        side_read_only=side_read_only,
    )
    assert event is not None
    return event


@pytest.mark.parametrize(
    ("tool", "kind", "builtin"),
    [
        ("WebSearch", "fetch", "web_search"),
        ("WebFetch", "fetch", "web_fetch"),
        ("Read", "read", "fs_read"),
        ("Write", "edit", ""),
        ("Bash", "execute", ""),
        ("mcp__docs__search", "other", ""),
    ],
)
def test_a_claude_permission_event_names_the_read_builtin_its_frame_stamped(tool, kind, builtin):
    event = _claude_permission_event({"claudeCode": {"toolName": tool}}, kind)
    assert event.harness_builtin_tool == builtin
    # Still no qualified id: claude has no HARNESS_TOOL_TABLES row.
    assert event.harness_tool_id == ""


def test_a_claude_frame_without_its_stamp_is_not_named_by_its_title():
    # The adapter builds the title from the call's arguments, so "Read" there is
    # not a tool name.
    assert harness_tool_name_of({"_meta": {"claudeCode": {}}, "title": "Read"}) == ""
    assert _claude_permission_event({"claudeCode": {}}, "read").harness_builtin_tool == ""


def test_only_the_claude_backend_reads_the_claude_table():
    event = _claude_permission_event({"claudeCode": {"toolName": "WebSearch"}}, "fetch", backend="")
    assert event.harness_builtin_tool == ""


def test_a_main_chat_claude_permission_event_names_no_builtin():
    # Outside Side Chat the name would bind kiro-cli-named deny rules to claude.
    event = _claude_permission_event(
        {"claudeCode": {"toolName": "Read"}}, "read", side_read_only=False
    )
    assert event.harness_builtin_tool == ""


def test_every_claude_read_builtin_maps_to_a_host_read_only_tool():
    from kiro_crew.hooks import _HOST_READ_ONLY_BUILTIN_TOOLS

    assert set(CLAUDE_READ_ONLY_BUILTINS.values()) <= _HOST_READ_ONLY_BUILTIN_TOOLS
