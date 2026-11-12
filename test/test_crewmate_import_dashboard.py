"""A ``crewmate.import`` card applies the bundle's built-in dashboard.

When the bundle names ``builtin:<id>`` and the receiver ships that template, the
card's plan gets one more apply step that adopts it on the new Bot, and the id
leaves "Not brought over". An id the receiver lacks stays out, as before.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import change_card_catalog as catalog
from kiro_crew.crewmate_bundle import crewmate_import_params

BUNDLE = {
    "crewmate": {
        "format": "kirocrew.kirobot",
        "formatVersion": 1,
        "name": "Standup Buddy",
        "description": "Turns open work into a standup note.",
        "skills": [],
        "dashboardTemplate": "builtin:standup",
    },
    "agent": {"mcpServers": {}, "skills": []},
}
STEP_PATH = "/api/members/{{step0.member_id}}/dashboard/template"
SLUG = "scout"
MEMBER = "Standup Buddy"


def _bundle(template: str) -> dict[str, Any]:
    bundle = copy.deepcopy(BUNDLE)
    bundle["crewmate"]["dashboardTemplate"] = template
    return bundle


def _preview(template: str) -> tuple[dict[str, Any], dict[str, Any]]:
    params = crewmate_import_params(_bundle(template))
    return params, catalog.build_preview("crewmate.import", params, {"exists": False}, {})


def test_a_shipped_builtin_dashboard_gets_an_apply_step_and_leaves_not_brought_over() -> None:
    params, preview = _preview("builtin:standup")
    assert "not_imported" not in params
    create, step = preview["apply"]
    assert create["path"] == "/api/agents"
    assert (step["method"], step["path"]) == ("POST", STEP_PATH)
    assert step["body"]["template_id"] == "standup"
    rows = {r["field"]: r["after"] for r in preview["changes"]}
    assert rows["dashboard_template"] == "builtin:standup"
    assert "not_imported" not in rows


def test_a_builtin_id_the_receiver_does_not_ship_stays_out() -> None:
    params, preview = _preview("builtin:no-such-page")
    assert params["not_imported"] == ["dashboardTemplate"]
    assert [s["path"] for s in preview["apply"]] == ["/api/agents"]


def test_the_step_is_in_the_import_undo_roles() -> None:
    params, _ = _preview("builtin:standup")
    assert catalog.import_step_roles(params) == ["create", "dashboard"]


@pytest.fixture
def home(tmp_path: Path, _floor_monkeypatch: Any) -> Path:
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    return tmp_path


async def _client(monkeypatch: Any, *, armed: bool) -> TestClient:
    from kiro_crew.dashboard.handlers import member_import_dashboard as route

    async def owner(_req: Any, _op: str, member: Any, *, write: bool) -> Any:
        return SLUG, member, None

    monkeypatch.setattr(route, "_owner_member", owner)

    @web.middleware
    async def arm(request: web.Request, handler: Any) -> Any:
        if armed:
            request[route.REQ_CARD_IMPORT_DASHBOARD] = True
        return await handler(request)

    app = web.Application(middlewares=[arm])
    app.router.add_post("/api/members/{slug}/dashboard/template", route.api_member_import_dashboard)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_the_card_step_sets_the_dashboard_on_the_new_bot(home: Path, monkeypatch) -> None:
    from kiro_crew.dashboard_templates import instance

    client = await _client(monkeypatch, armed=True)
    try:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/template",
            json={"member": MEMBER, "template_id": "standup"},
        )
        assert resp.status == 200, await resp.text()
    finally:
        await client.close()
    assert instance.read(SLUG).template_id == "standup"


@pytest.mark.asyncio
@pytest.mark.parametrize("armed", [False, True])
async def test_only_the_card_step_and_only_a_shipped_template_are_accepted(
    home: Path, monkeypatch, armed: bool
) -> None:
    from kiro_crew.dashboard_templates import instance

    client = await _client(monkeypatch, armed=armed)
    try:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/template",
            json={"member": MEMBER, "template_id": "no-such-page" if armed else "standup"},
        )
        assert resp.status == (400 if armed else 403)
    finally:
        await client.close()
    assert instance.read(SLUG).template_id == ""


def test_the_route_is_hooked_and_writes_no_memory_line() -> None:
    route = ("POST", "/api/members/{slug}/dashboard/template")
    assert route in catalog.HOOKED_ROUTES and route not in catalog.MEMORY_ROUTES
