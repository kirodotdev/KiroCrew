"""The declared ``Tool.title`` table matches what each own MCP server lists."""

from __future__ import annotations

import pytest

from kiro_crew import mcp_core, mcp_cron, mcp_dashboard
from kiro_crew.mcp_tool_titles import _table, declared_tool_title, with_titles

_SERVERS = {
    "kirocrew-core": mcp_core,
    "kirocrew-dashboard": mcp_dashboard,
    "kirocrew-cron": mcp_cron,
}


@pytest.mark.parametrize("server", sorted(_SERVERS))
def test_every_listed_tool_carries_its_declared_title(server: str, monkeypatch) -> None:
    # The title table covers every DECLARED tool. ``kirocrew-core`` lists an app's
    # tools only while that app is enabled, and the isolated test home has
    # none installed, so the gate is held open here to compare the whole table.
    from kiro_crew.mcp_tools import apps as apps_tools

    monkeypatch.setattr(apps_tools, "app_enabled_state", lambda _name: True)
    listed = _SERVERS[server]._list_tools()
    assert {t["name"]: t.get("title") for t in listed} == _table()[server]


def test_table_names_only_the_three_own_servers() -> None:
    assert set(_table()) == set(_SERVERS)


def test_titles_are_short_single_line_copy() -> None:
    for server, titles in _table().items():
        for tool, title in titles.items():
            assert title.strip() == title and "\n" not in title, (server, tool)
            assert 0 < len(title) <= 40, (server, tool)


def test_lookup_is_keyed_by_server() -> None:
    assert declared_tool_title("kirocrew-core", "spawn_run") == "Start sub-agent"
    assert declared_tool_title("other-mcp", "spawn_run") == ""
    assert declared_tool_title("kirocrew-core", "no_such_tool") == ""


def test_with_titles_copies_and_keeps_a_title_the_descriptor_already_has() -> None:
    own = {"name": "spawn_run", "title": "Custom"}
    bare = {"name": "spawn_run"}
    out = with_titles("kirocrew-core", [own, bare])
    assert out == [own, {"name": "spawn_run", "title": "Start sub-agent"}]
    assert "title" not in bare
