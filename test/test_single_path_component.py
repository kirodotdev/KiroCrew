"""The shared rule for a roster key or member id that is used as one file or folder name.

A key is acceptable only when it is exactly one path component. It is deliberately not the
crew-id grammar: a key already stored with dots, spaces or CJK characters stays valid.
"""

from __future__ import annotations

import pytest

from kiro_crew.members_parts.member_space import is_single_path_component


@pytest.mark.parametrize(
    "value",
    ["alice", "my.pkg", "my crew", "小明", "a-b_c", "..x", "x..", ".hidden", "ab:c"],
)
def test_one_component_is_accepted(value):
    assert is_single_path_component(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "a/b",
        "../x",
        "/abs",
        "a\\b",
        "a\x00b",
        "a\nb",
        "a\rb",
        "D:payload",
        "d:",
        None,
        7,
        ["a"],
    ],
)
def test_anything_else_is_refused(value):
    assert is_single_path_component(value) is False


def test_the_overlay_file_id_rule_is_the_same_function():
    """The crewmate overlay folder reads the shared rule rather than keeping a copy."""
    from kiro_crew.mcp_gateway import mate_overlays

    assert mate_overlays._is_file_id("a/b") is False
    assert mate_overlays._is_file_id("my.pkg") is True
    assert mate_overlays._is_file_id("a\nb") is False
