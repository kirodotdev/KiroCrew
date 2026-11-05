"""``contributes.selectionActions``: the manifest item, its grant, and its frame binding.

A selection action runs in the same opaque-origin frame as a link preview. What
this pins is what the new point adds to that frame model:

* the manifest item is refused, never coerced, when a field has the wrong type
  or leaves its bounds;
* its grant entry names everything that widens it, so a widening update is held
  back and the item has no grant until the owner approves it;
* a frame and a capability minted for one point are bound to that point: a
  selection frame is never served the link preview's entry, a link-preview
  capability never reaches a selection frame's fetch, and a revoked grant stops
  the mint, the frame and the fetch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import app_frame as af
from kiro_crew.apps import selection_action_frame as saf
from kiro_crew.apps import selection_action_grants as sag
from kiro_crew.apps.manager_records import (
    STAGED_GRANT_FAMILIES,
    _approved_grants_after_manifest_change,
    _declared_grant_entries,
    _held_back_entries,
)
from kiro_crew.apps.manifest_contributes import Contributes, SelectionActionConfig
from kiro_crew.apps.point_grants import grant_source, point_grant_families
from kiro_crew.apps.selection_action_grants import (
    selection_action_entry,
    selection_action_grant_entries,
)

APP = "notes-app"


def _item(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": "save", "title": "Save to notes", "entry": "dist/sel.mjs"}
    base.update(over)
    return base


def _errors(**over: Any) -> list[str]:
    return SelectionActionConfig.from_dict(_item(**over)).validate()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_a_minimal_item_is_valid_and_serializes_its_defaults() -> None:
    cfg = SelectionActionConfig.from_dict(_item())
    assert cfg.validate() == []
    assert cfg.to_dict() == {
        "id": "save",
        "title": "Save to notes",
        "entry": "dist/sel.mjs",
        "sources": ["chat"],
        "minLength": 1,
        "maxLength": 8000,
        "effects": [],
        "popover": {"width": 360, "maxHeight": 480},
    }


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"maxLength": "8000"}, "maxLength has the wrong type"),
        ({"maxLength": True}, "maxLength has the wrong type"),
        ({"minLength": 1.5}, "minLength has the wrong type"),
        ({"sources": "chat"}, "sources has the wrong type"),
        ({"effects": ["copy", 1]}, "effects has the wrong type"),
        ({"popover": []}, "popover has the wrong type"),
        ({"title": 5}, "title has the wrong type"),
        ({"sources": ["dom"]}, "unknown source 'dom'"),
        ({"sources": []}, "sources must name at least one source"),
        ({"effects": ["clipboard"]}, "unknown effect 'clipboard'"),
        ({"effects": ["copy", "copy"]}, "effects repeats an entry"),
        ({"maxLength": 8001}, "maxLength must be 1 to 8000"),
        ({"minLength": 0}, "minLength must be 1 to 8000"),
        ({"minLength": 50, "maxLength": 10}, "minLength exceeds maxLength"),
        ({"title": "x" * 41}, "title exceeds 40 characters"),
        ({"icon": "Book Mark"}, "icon must be a host icon name"),
        ({"popover": {"width": 100}}, "popover.width must be 200 to 480"),
        ({"popover": {"maxHeight": 900}}, "popover.maxHeight must be 120 to 600"),
        ({"id": "Save"}, "id must be a storage-safe slug"),
        ({"entry": ""}, "missing entry"),
    ],
)
def test_a_bad_field_is_refused_not_coerced(over: dict[str, Any], needle: str) -> None:
    errors = _errors(**over)
    assert any(needle in e for e in errors), errors


def test_the_block_caps_items_and_refuses_duplicates_and_bad_shapes() -> None:
    three = Contributes.from_dict({"selectionActions": [_item(id=f"a{i}") for i in range(3)]})
    assert any("exceeds the limit of 2" in e for e in three.validate())
    dup = Contributes.from_dict({"selectionActions": [_item(), _item()]})
    assert any("duplicate id 'save'" in e for e in dup.validate())
    assert any(
        "must be an array" in e
        for e in Contributes.from_dict({"selectionActions": {"id": "x"}}).validate()
    )
    assert any(
        "1 entry must be an object" in e
        for e in Contributes.from_dict({"selectionActions": [_item(), "x"]}).validate()
    )
    assert "selectionActions" in Contributes.from_dict({"selectionActions": [_item()]}).to_dict()


# ---------------------------------------------------------------------------
# Grant
# ---------------------------------------------------------------------------


def _manifest(**over: Any) -> dict[str, Any]:
    return {"name": APP, "contributes": {"selectionActions": [_item(**over)]}}


def test_the_entry_names_every_field_that_widens_the_item() -> None:
    entry = selection_action_grant_entries(_manifest()["contributes"])["selectionActions"]
    assert entry == ["save:chat:1-8000:"]
    assert (
        selection_action_entry(
            {
                "id": "s",
                "sources": ["chat"],
                "effects": ["open", "copy"],
                "minLength": 3,
                "maxLength": 9,
            }
        )
        == "s:chat:3-9:copy,open"
    )
    assert "selectionActions" in point_grant_families()
    assert "selectionActions" in STAGED_GRANT_FAMILIES


@pytest.mark.parametrize(
    "over",
    [{"effects": ["copy"]}, {"effects": ["open"]}, {"maxLength": 4000}, {"minLength": 2}],
)
def test_a_changed_item_is_held_back_until_approved(over: dict[str, Any]) -> None:
    old = grant_source(_manifest())
    new = grant_source(_manifest(**over))
    approved = _approved_grants_after_manifest_change(
        existing_approved=_declared_grant_entries(old), old_permissions=old, new_permissions=new
    )
    # The changed item keeps no grant until the owner approves its new entry.
    assert approved["selectionActions"] == []
    assert _held_back_entries(approved, new)["selectionActions"] == (
        selection_action_grant_entries(_manifest(**over)["contributes"])["selectionActions"]
    )


def test_dropping_an_item_needs_nothing() -> None:
    old = grant_source(_manifest())
    new = grant_source({"name": APP, "contributes": {}})
    assert _held_back_entries(_declared_grant_entries(old), new)["selectionActions"] == []


def test_the_row_and_the_frame_resolver_read_the_same_approved_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An item is offered and served only while its CURRENT entry is approved."""

    approved: list[str] = ["save:chat:1-8000:"]
    monkeypatch.setattr(
        "kiro_crew.apps.manager.staged_app_grants",
        lambda name, family, read: [e for e in read() if e in approved],
    )
    manifest = _manifest()
    assert sag.selection_action_grant_row(APP, manifest) == {
        "selectionActionGrant": {"approved": ["save"], "staged": []}
    }
    assert sag.approved_selection_action(APP, manifest, "save") is not None
    widened = _manifest(effects=["copy"])
    assert sag.selection_action_grant_row(APP, widened) == {
        "selectionActionGrant": {"approved": [], "staged": ["save:chat:1-8000:copy"]}
    }
    assert sag.approved_selection_action(APP, widened, "save") is None
    assert sag.selection_action_grant_row(APP, {"contributes": {}}) == {}


# ---------------------------------------------------------------------------
# On the shared frame
# ---------------------------------------------------------------------------


def test_the_point_is_registered_on_the_shared_frame() -> None:
    assert "selectionActions" in af.registered_frame_points()
    point = af._POINTS["selectionActions"]
    assert point.script is saf.SELECTION_ACTION_SCRIPT
    assert point.admit is saf.admit_selection_action


def _served(max_length: int = 8000) -> dict[str, Any]:
    """The serialized ``save`` item the fake resolver approves."""
    return {
        "id": "save",
        "entry": "./dist/sel.mjs",
        "sources": ["chat"],
        "minLength": 1,
        "maxLength": max_length,
        "effects": ["copy"],
    }


#: The mint query naming the grant entry the dashboard resolved ``save`` at.
_SAVE = "/frames/selectionActions/save?grant=" + quote(sag.selection_action_entry(_served()))


@asynccontextmanager
async def _frames(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]
) -> AsyncIterator[tuple[TestClient, list[str]]]:
    calls: list[str] = []

    class _Registry:
        async def dispatch(self, request: web.Request) -> web.Response:
            calls.append(request.match_info["path"])
            return web.json_response({"ok": True})

    def item(name: str, item_id: str) -> dict[str, Any] | None:
        return (
            _served(state.get("maxLength", 8000))
            if name == APP and item_id == "save" and state.get("granted", True)
            else None
        )

    manifest = type(
        "M",
        (),
        {"name": APP, "backend": type("B", (), {"entryPoint": ""})(), "mcpServers": {}},
    )()
    monkeypatch.setattr(saf, "is_app_enabled", lambda name: name == APP)
    monkeypatch.setattr(saf, "get_app_manifest", lambda name: manifest)
    monkeypatch.setattr(
        saf, "approved_selection_action", lambda name, m, item_id: item(name, item_id)
    )
    monkeypatch.setattr("kiro_crew.apps.hooks_integration.get_route_registry", _Registry)
    monkeypatch.setattr("kiro_crew.dashboard.server._embed_parent_port", lambda r, a: None)

    @web.middleware
    async def auth(request: web.Request, handler: Any) -> web.StreamResponse:
        if request.path.startswith("/api/"):
            request["is_dashboard_user"] = True
        return await handler(request)

    app = web.Application(middlewares=[auth])
    af.register_app_frame_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, calls
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_an_approved_item_gets_a_frame_with_its_entry_and_the_invoke_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _frames(monkeypatch, {}) as (client, calls):
        resp = await client.post(f"/api/apps/{APP}{_SAVE}")
        assert resp.status == 200
        minted = await resp.json()
        page = await client.get(minted["frame"])
        assert page.status == 200
        html = await page.text()
        assert f'"entry": "/apps/{APP}/ui/dist/sel.mjs"' in html
        assert "m.type !== 'selection.invoke'" in html
        assert "invocationControls(m.invocationId)" in html
        fetched = await client.get(
            f"{af.FETCH_PREFIX}{APP}/notes", headers={af.CAPABILITY_HEADER: minted["capability"]}
        )
        assert fetched.status == 200 and calls == ["notes"]
        # Another item of the same app has no frame.
        assert (
            await client.post(f"/api/apps/{APP}/frames/selectionActions/other?grant=x")
        ).status == 404


@pytest.mark.asyncio
async def test_a_revoked_item_stops_its_mint_frame_and_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {}
    async with _frames(monkeypatch, state) as (client, calls):
        minted = await (await client.post(f"/api/apps/{APP}{_SAVE}")).json()
        state["granted"] = False
        assert (await client.post(f"/api/apps/{APP}{_SAVE}")).status == 404
        assert (await client.get(minted["frame"])).status == 404
        resp = await client.get(
            f"{af.FETCH_PREFIX}{APP}/notes", headers={af.CAPABILITY_HEADER: minted["capability"]}
        )
        assert resp.status == 401 and calls == []


@pytest.mark.asyncio
async def test_a_mint_must_name_the_items_current_grant_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {}
    async with _frames(monkeypatch, state) as (client, calls):
        bare = f"/api/apps/{APP}/frames/selectionActions/save"
        # No grant, or one the item never had: refused before any text is sent.
        assert (await client.post(bare)).status == 403
        assert (await client.post(bare + "?grant=save%3Achat%3A1-9999%3Acopy")).status == 403
        minted = await (await client.post(f"/api/apps/{APP}{_SAVE}")).json()
        # The owner approves a narrower item in another tab: a pick or a renewal made
        # against the old item is refused, and so is its capability's next renewal.
        state["maxLength"] = 100
        assert (await client.post(f"/api/apps/{APP}{_SAVE}")).status == 403
        renew = f"/api/apps/{APP}{_SAVE}&frame={minted['frameId']}"
        assert (await client.post(renew)).status == 403
        fresh = "?grant=" + quote(sag.selection_action_entry(_served(100)))
        assert (await client.post(bare + fresh)).status == 200


def test_the_resolver_refuses_a_disabled_app_and_an_entry_leaving_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = type(
        "M", (), {"name": APP, "backend": type("B", (), {"entryPoint": ""})(), "mcpServers": {}}
    )()
    monkeypatch.setattr(saf, "get_app_manifest", lambda name: manifest)
    monkeypatch.setattr(
        saf, "approved_selection_action", lambda name, m, item_id: {"entry": "../x.mjs"}
    )
    monkeypatch.setattr(saf, "is_app_enabled", lambda name: True)
    assert saf.resolve_selection_action(APP, "save") is None
    monkeypatch.setattr(
        saf, "approved_selection_action", lambda name, m, item_id: {"entry": "dist/s.mjs"}
    )
    assert saf.resolve_selection_action(APP, "save") == ("dist/s.mjs", False, APP)
    monkeypatch.setattr(saf, "is_app_enabled", lambda name: False)
    assert saf.resolve_selection_action(APP, "save") is None
