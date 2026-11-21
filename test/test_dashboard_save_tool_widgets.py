"""``dashboard_save`` and a package's ``widgets``: kept when left out, replaced when sent.

Split from ``test_dashboard_save_tool.py`` to keep that file under the line cap. The
helpers and the ``store`` fixture are that file's. Every test here runs with the
``widgets`` preview on; ``test_widgets_preview.py`` pins the off behaviour.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_dashboard_save_tool import _client, _flip, _package_body, _save, _Store, store

from kiro_crew.artifact_store.dashboard_package import parse_package

__all__ = ["store"]  # the fixture, imported so pytest finds it here


@pytest.fixture(autouse=True)
def _preview_on(_floor_monkeypatch):
    from kiro_crew import widgets_preview

    _floor_monkeypatch.setattr(widgets_preview, "widgets_enabled", lambda: True)


def _give_stored_package_widgets(store: _Store) -> str:
    """Write a widget into the caller's stored package, as ``artifact_update`` could."""
    from kiro_crew.artifact_store.dashboard_package import canonical_package_content

    [rec] = store.records.values()
    package = parse_package(rec.content)
    package["widgets"] = [
        {
            "id": "prs",
            "title": "PRs",
            "sizes": ["small"],
            "blocks": [{"type": "metric", "label": "Open PRs", "field": "open_prs"}],
        }
    ]
    rec.content = canonical_package_content(json.dumps(package))
    return rec.slug


@pytest.mark.asyncio
async def test_a_save_keeps_the_widgets_the_stored_package_holds(monkeypatch, store):
    """A save that leaves ``widgets`` out must not erase stored ones."""
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


@pytest.mark.asyncio
async def test_a_save_that_would_break_stored_widgets_is_refused(monkeypatch, store):
    """A new model without a field a stored widget binds is refused, not half-stored."""
    second = _package_body()
    del second["model"]["types"]["open_prs"]
    second["view"]["html"] = '<p data-dashboard-field="headline"></p>'
    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200
        slug = _give_stored_package_widgets(store)
        before = store.records[slug].content
        resp = await _save(client, second)
        body = await resp.json()

    assert resp.status == 400 and body["code"] == "invalid_package"
    assert "widgets your stored package holds" in body["error"]
    assert "'open_prs'" in body["error"]
    assert store.records[slug].content == before


@pytest.mark.asyncio
async def test_sent_widgets_replace_the_stored_ones(monkeypatch, store):
    """``widgets`` in the body replaces what is stored; ``[]`` removes it."""
    second = _package_body()
    second["widgets"] = [
        {
            "id": "headline",
            "title": "Headline",
            "sizes": ["medium"],
            "blocks": [{"type": "title", "field": "headline"}],
        }
    ]
    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200
        slug = _give_stored_package_widgets(store)
        resp = await _save(client, second)
        assert resp.status == 200, await resp.text()
        assert [w["id"] for w in parse_package(store.records[slug].content)["widgets"]] == [
            "headline"
        ]
        second["widgets"] = []
        resp = await _save(client, second)
        assert resp.status == 200, await resp.text()

    assert "widgets" not in parse_package(store.records[slug].content)


@pytest.mark.asyncio
async def test_sent_widgets_are_refused_whole_when_invalid(monkeypatch, store):
    """A widget binding an undeclared field is refused and nothing is stored."""
    body = _package_body()
    body["widgets"] = [
        {
            "id": "x",
            "title": "X",
            "sizes": ["small"],
            "blocks": [{"type": "metric", "label": "Nope", "field": "not_declared"}],
        }
    ]
    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        payload = await resp.json()

    assert resp.status == 400 and payload["code"] == "invalid_package"
    assert "not_declared" in payload["error"]
    assert store.records == {}


def test_the_tool_forwards_widgets_only_when_sent(monkeypatch):
    from kiro_crew import mcp_panel

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("sk", ""))
    monkeypatch.setattr(
        mcp_panel,
        "_post",
        lambda _path, payload, **_kw: sent.append(payload) or {"saved": {"package": "p"}},
    )
    body = _package_body()
    body["widgets"] = []
    mcp_panel._call_tool_inner("dashboard_save", body)
    assert sent[-1]["widgets"] == []
    body["widgets"] = {"id": "not-a-list"}
    out = mcp_panel._call_tool_inner("dashboard_save", body)
    assert out.startswith("Error: `widgets` must be a list") and len(sent) == 1
