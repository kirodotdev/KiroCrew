"""The kiro-cli <-> goose / opencode tool-name tables a spec hook matcher reads."""

from __future__ import annotations

import pytest

from kiro_crew.acp import _dispatch
from kiro_crew.acp.harness_tool_names import (
    GOOSE_TOOL_IDS_BY_KIRO_TOOL,
    HARNESS_TOOL_TABLES,
    OPENCODE_TOOL_IDS_BY_KIRO_TOOL,
    harness_tool_match_names,
    qualified_harness_tool_id,
    split_harness_tool_id,
)
from kiro_crew.acp.kas_permissions import KAS_TOOL_IDS_BY_KIRO_TOOL, kas_tool_match_names


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
