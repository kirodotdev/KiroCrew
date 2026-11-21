"""The ``widgets`` feature preview gates everything an agent learns about phone widgets.

Off (the default): the served dashboard skill says nothing about widgets,
``dashboard_save``'s schema has no ``widgets``, the tool refuses one without calling
the gateway, and the save route refuses one with ``widgets_preview_off`` and stores
nothing. A save without ``widgets`` still keeps stored widgets. On: the schema
offers ``widgets`` with the guide, as ``test_dashboard_skill_widgets.py`` and
``test_dashboard_save_tool_widgets.py`` pin.

Each guarantee and the one-line change that reddens its test:

* skill text -- putting the guide back in the dashboard SKILL.md reddens
  ``test_the_served_dashboard_skill_never_mentions_widgets``;
* schema drop -- ``with_widgets_schema`` returning *tools* unchanged reddens
  ``test_off_the_schema_does_not_offer_widgets``;
* tool refusal -- ``mcp_refusal`` returning ``None`` reddens
  ``test_off_the_tool_refuses_widgets_without_calling_the_gateway``;
* route refusal -- deleting the preview check in ``api_dashboard_save`` reddens
  ``test_off_the_route_refuses_widgets_and_stores_nothing``;
* default off -- ``widgets_enabled`` returning ``True`` on a missing name reddens
  ``test_the_preview_reads_off_unless_set``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from test_dashboard_save_tool import _client, _flip, _package_body, _save, _Store, store
from test_dashboard_save_tool_widgets import _give_stored_package_widgets

from kiro_crew import mcp_panel, widgets_preview
from kiro_crew.artifact_store.dashboard_package import parse_package

__all__ = ["store"]  # the fixture, imported so pytest finds it here

SKILL_DIR = Path(__file__).resolve().parents[1] / "src/kiro_crew/builtin_skills/dashboard"

_WIDGET = {
    "id": "prs",
    "title": "PRs",
    "sizes": ["small"],
    "blocks": [{"type": "metric", "label": "Open PRs", "field": "open_prs"}],
}


@pytest.fixture
def preview(monkeypatch):
    def _set(on: bool) -> None:
        monkeypatch.setattr(widgets_preview, "widgets_enabled", lambda: on)

    _set(False)
    return _set


def _save_tool() -> dict[str, Any]:
    (tool,) = [t for t in mcp_panel._list_tools() if t["name"] == "dashboard_save"]
    return tool


def test_the_preview_reads_off_unless_set(tmp_path):
    cfg = tmp_path / "config.json"
    for stored, expected in (
        ({}, False),
        ({"dashboard": {"feature_previews": {}}}, False),
        ({"dashboard": {"feature_previews": {"widgets": True}}}, True),
    ):
        cfg.write_text(json.dumps(stored), encoding="utf-8")
        with patch("kiro_crew.config.loader.config_path", return_value=cfg):
            assert widgets_preview.widgets_enabled() is expected, stored


def test_the_served_dashboard_skill_never_mentions_widgets():
    """The skill is copied to disk whole, so no file in it may teach widgets."""
    for path in SKILL_DIR.rglob("*"):
        if path.is_file() and path.suffix in {".md", ".json", ".html"}:
            text = path.read_text("utf-8").lower()
            assert "widget" not in text, path
            assert "home-screen" not in text and "kiro mobile" not in text, path


def test_off_the_schema_does_not_offer_widgets(preview):
    tool = _save_tool()
    assert "widgets" not in tool["inputSchema"]["properties"]
    assert "widgets" not in tool["inputSchema"].get("required", [])
    flat = json.dumps(tool).lower()
    assert "widget" not in flat and "home-screen" not in flat


def test_on_the_schema_offers_widgets_with_the_guide(preview):
    preview(True)
    prop = _save_tool()["inputSchema"]["properties"]["widgets"]
    assert widgets_preview.GUIDE in prop["description"]
    # The other panel tools are untouched either way.
    preview(False)
    off = {t["name"]: t for t in mcp_panel._list_tools() if t["name"] != "dashboard_save"}
    preview(True)
    on = {t["name"]: t for t in mcp_panel._list_tools() if t["name"] != "dashboard_save"}
    assert off == on


def test_off_the_tool_refuses_widgets_without_calling_the_gateway(monkeypatch, preview):
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("sk", ""))
    monkeypatch.setattr(
        mcp_panel,
        "_post",
        lambda _path, payload, **_kw: sent.append(payload) or {"saved": {"package": "p"}},
    )
    body = _package_body()
    body["widgets"] = []
    out = mcp_panel._call_tool_inner("dashboard_save", body)
    assert out.startswith("Error: widgets is not taken") and "widgets_preview_off" in out
    assert sent == []
    # Without widgets the save goes through, off or on.
    del body["widgets"]
    mcp_panel._call_tool_inner("dashboard_save", body)
    assert len(sent) == 1 and "widgets" not in sent[0]


@pytest.mark.asyncio
async def test_off_the_route_refuses_widgets_and_stores_nothing(monkeypatch, store, preview):
    body = _package_body()
    body["widgets"] = [dict(_WIDGET)]
    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        payload = await resp.json()

    assert resp.status == 403 and payload["code"] == widgets_preview.PREVIEW_OFF_CODE
    assert "not enabled" in payload["error"]
    assert store.records == {}


@pytest.mark.asyncio
async def test_off_a_save_without_widgets_keeps_the_stored_ones(
    monkeypatch, store: _Store, preview
):
    """With the preview off, a save that leaves ``widgets`` out still keeps them."""
    second = _package_body()
    _flip(second)
    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200
        slug = _give_stored_package_widgets(store)
        resp = await _save(client, second)
        assert resp.status == 200, await resp.text()

    stored = parse_package(store.records[slug].content)
    assert stored["view"]["html"] == second["view"]["html"]
    assert [w["id"] for w in stored["widgets"]] == ["prs"]
