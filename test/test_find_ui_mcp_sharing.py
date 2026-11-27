"""``find_ui`` goldens for MCP server sharing and its launch approval.

The Sharing view, the page-wide share switch and the approval card's button
must each be found by the words a newcomer uses, with the exact path, route
and the condition under which the button is drawn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew import ui_index


@pytest.fixture(autouse=True, scope="module")
def _fresh_cache() -> None:
    ui_index._cache.update(key=None, index=None)


@pytest.fixture(autouse=True)
def _no_auto_tier(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The committed tiers only, whatever dashboard build this checkout has.
    _floor_monkeypatch.setattr(ui_index, "AUTO_INDEX_PATH", tmp_path / "no-auto-tier.json")


def _top(query: str, lang: str) -> dict[str, Any]:
    d = ui_index.find_ui(query, lang)
    assert d["status"] == "ok", d
    return d["results"][0]


SHARING = "/capabilities?tab=mcp-servers"
MCP = "/capabilities?tab=mcp-servers"


@pytest.mark.parametrize(
    ("query", "lang", "loc_id", "path", "route", "conditions"),
    [
        (
            "MCP Management",
            "en",
            "mcp.sharing-view",
            ["Customize", "MCP Servers", "Sharing"],
            SHARING,
            [],
        ),
        (
            "turn on a custom MCP server for Kiro Crew and approve its launch",
            "en",
            "mcp.sharing-view",
            ["Customize", "MCP Servers", "Sharing"],
            SHARING,
            [],
        ),
        (
            "approve a custom mcp server",
            "en",
            "mcp.review-approve",
            ["Customize", "MCP Servers", "Review and approve"],
            MCP,
            ["mcp_launch_awaiting_approval"],
        ),
        (
            "share mcp servers",
            "en",
            "mcp.share-switch",
            ["Customize", "MCP Servers", "Turn on MCP server sharing"],
            MCP,
            [],
        ),
        ("MCP 管理", "zh-CN", "mcp.sharing-view", ["自定义", "MCP 服务器", "共享"], SHARING, []),
        (
            "批准 MCP 启动",
            "zh-CN",
            "mcp.review-approve",
            ["自定义", "MCP 服务器", "查看并批准"],
            MCP,
            ["mcp_launch_awaiting_approval"],
        ),
    ],
)
def test_mcp_sharing_controls_are_found(query, lang, loc_id, path, route, conditions) -> None:
    top = _top(query, lang)
    assert top["id"] == loc_id
    placement = top["placements"][0]
    assert [seg["label"] for seg in placement["path"]] == path
    assert placement["route"] == route
    assert [r["id"] for r in placement["requires"] if r["kind"] == "condition"] == conditions
