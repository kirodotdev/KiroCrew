"""Grant matching shared by worker exclusions and mate capability previews."""

from __future__ import annotations

import pytest

from kiro_crew.agent_sdk.mcp_refs import entry_reaches


@pytest.mark.parametrize(
    "entry, expected",
    [
        ("@notes", True),
        ("@notes/", True),
        ("@notes/*", True),
        ("@notes/write_*", True),
        ("@notes/write_not?", True),
        ("@notes/write_[n]ote", True),
        ("@NOTES/WRITE_*", True),
        ("*", True),
        ("write_*", True),
        ("write_not?", True),
        ("@notes/read_*", False),
        ("@other/write_*", False),
        ("@notes/writ?", False),
        ("@", False),
        ("@/write_note", False),
        ("", False),
    ],
)
def test_entry_reaches_tool_part_globs(entry, expected):
    assert entry_reaches(entry, "@notes/write_note") is expected


@pytest.mark.parametrize("entry", ["@notes", "@notes/", "@notes/*", "*"])
@pytest.mark.parametrize("ref", ["@notes", "@notes/", "@notes/*"])
def test_entry_reaches_whole_server_selectors(entry, ref):
    assert entry_reaches(entry, ref)
