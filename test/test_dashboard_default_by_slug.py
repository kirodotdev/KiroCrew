"""A crewmate that adopted nothing gets the page named for it, then the default.

The rule is the crew panel's: a template whose id is the crew's slug is that crew's
view. ``instance.default_instance`` is the one place it is decided, and both the read
route and the ``[DASHBOARD]`` turn block go through it, so the page a reader sees and
the page the crewmate is told about stay one page. An adopted page beats both.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import dashboard_agentic
from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard_templates import catalog, instance

SLUG = "fleet-conductor"
MEMBER = "Fleet Conductor"
PAGE = '<div><b data-dashboard-field="phase"></b></div>'


def _write(root: Path, template_id: str) -> None:
    manifest = {
        "id": template_id,
        "version": 1,
        "title": template_id,
        "description": "A template these tests own.",
        "source": "builtin",
        "fields": {"phase": {"type": "string", "source": {"agentic": True}}},
    }
    directory = root / template_id
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "template.html").write_text(PAGE, encoding="utf-8")


@pytest.fixture
def builtin(tmp_path: Path, _floor_monkeypatch: Any) -> Path:
    """An isolated data home, and a registry holding the default and two more pages."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    root = tmp_path / "builtin"
    for template_id in (instance.DEFAULT_TEMPLATE_ID, SLUG, "fixture-other"):
        _write(root, template_id)
    _floor_monkeypatch.setattr(catalog, "builtin_dir", lambda: root)

    cfg = SimpleNamespace(agents={MEMBER: SimpleNamespace(member_id="")})
    _floor_monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    _floor_monkeypatch.setattr(routes.members_mod, "member_slug", lambda name, config=None: SLUG)
    _floor_monkeypatch.setattr(routes.members_mod, "validate_slug", lambda slug: slug)
    _floor_monkeypatch.setattr(routes.members_mod, "is_dispatchable_member_name", bool)
    _floor_monkeypatch.setattr(routes, "_member_names_for_slug", lambda cfg, slug: [MEMBER])
    _floor_monkeypatch.setattr(routes, "_deny_app_caller", _none)
    _floor_monkeypatch.setattr(routes, "_owner_only", _none)
    _floor_monkeypatch.setattr(routes, "_write_session", lambda slug, member: "")
    return root


async def _none(*_a: Any, **_k: Any) -> None:
    return None


@asynccontextmanager
async def _client() -> AsyncIterator[TestClient]:
    app = web.Application()
    routes.register_member_dashboard_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def _route_template(slug: str = SLUG) -> str:
    async with _client() as client:
        resp = await client.get(f"/api/members/{slug}/dashboard?member=Fleet+Conductor")
        assert resp.status == 200, await resp.text()
        return str((await resp.json())["template"]["id"])


class TestThePick:
    def test_the_page_named_for_the_crewmate_wins(self) -> None:
        assert instance.default_template_id(SLUG, [SLUG, "project-report"]) == SLUG

    def test_any_other_slug_gets_the_default(self) -> None:
        assert instance.default_template_id("mate", [SLUG, "project-report"]) == (
            instance.DEFAULT_TEMPLATE_ID
        )

    def test_an_empty_slug_gets_the_default(self) -> None:
        assert instance.default_template_id("", ["", "project-report"]) == "project-report"

    def test_a_registry_without_the_default_answers_none(self) -> None:
        assert instance.default_template_id("mate", ["fixture-other"]) is None


class TestTheSeam:
    def test_the_slug_page_is_the_default_instance(self, builtin: Path) -> None:
        page = instance.default_instance(SLUG)
        assert page is not None
        assert page.template_id == SLUG
        assert page.instance_version == 0 and page.state == instance.STATE_EMPTY

    def test_a_crewmate_with_no_page_of_its_own_gets_the_default(self, builtin: Path) -> None:
        page = instance.default_instance("mate")
        assert page is not None and page.template_id == instance.DEFAULT_TEMPLATE_ID

    @pytest.mark.asyncio
    async def test_the_read_route_serves_the_slug_page(self, builtin: Path) -> None:
        assert await _route_template() == SLUG

    @pytest.mark.asyncio
    async def test_the_read_route_serves_the_default_without_one(self, builtin: Path) -> None:
        (builtin / SLUG / "manifest.json").unlink()
        assert await _route_template() == instance.DEFAULT_TEMPLATE_ID

    def test_the_turn_block_names_the_slug_page(self, builtin: Path) -> None:
        block = dashboard_agentic.turn_block(SLUG)
        assert f"template {SLUG} v1" in block

    def test_the_turn_block_names_the_default_for_any_other_crewmate(self, builtin: Path) -> None:
        # The generic default is not drawn on the tab: the suggestions page is, and
        # the block says so instead of naming a page nobody sees.
        block = dashboard_agentic.turn_block("mate")
        assert dashboard_agentic.SUGGESTIONS_LINE in block
        assert instance.DEFAULT_TEMPLATE_ID not in block


async def _route_body(slug: str) -> dict[str, Any]:
    async with _client() as client:
        resp = await client.get(f"/api/members/{slug}/dashboard?member=Fleet+Conductor")
        assert resp.status == 200, await resp.text()
        return dict(await resp.json())


class TestTheSuggestionsDecisionIsMadeOnce:
    """The read route sends `suggestions` and the turn block says the same thing,
    because both ask `dashboard_agentic.shows_suggestions`."""

    @pytest.mark.asyncio
    async def test_the_generic_default_is_the_suggestions_page_on_both(self, builtin: Path) -> None:
        # No page named for this crewmate, so the read falls to the generic default.
        (builtin / SLUG / "manifest.json").unlink()
        assert (await _route_body(SLUG))["suggestions"] is True
        assert dashboard_agentic.SUGGESTIONS_LINE in dashboard_agentic.turn_block(SLUG)

    @pytest.mark.asyncio
    async def test_the_page_named_for_the_crewmate_is_drawn_on_both(self, builtin: Path) -> None:
        assert (await _route_body(SLUG))["suggestions"] is False
        assert dashboard_agentic.SUGGESTIONS_LINE not in dashboard_agentic.turn_block(SLUG)

    @pytest.mark.asyncio
    async def test_an_adopted_page_is_drawn_on_both(self, builtin: Path) -> None:
        instance.adopt(SLUG, "fixture-other")
        assert (await _route_body(SLUG))["suggestions"] is False
        assert dashboard_agentic.SUGGESTIONS_LINE not in dashboard_agentic.turn_block(SLUG)

    def test_a_bound_package_is_not_the_suggestions_page(self, builtin: Path) -> None:
        # The tab serves the package page, so the crewmate must not be told otherwise.
        with patch.object(dashboard_agentic, "bound_package", return_value="my-pkg"):
            block = dashboard_agentic.turn_block("mate")
        assert dashboard_agentic.SUGGESTIONS_LINE not in block, block

    def test_a_locked_crewmate_serves_no_package_so_it_is_the_suggestions_page(
        self, builtin: Path
    ) -> None:
        # The read route clears the package under a lock and draws the template
        # instance; the block follows the same rule.
        instance.set_structure_lock("mate", True)
        with patch.object(dashboard_agentic, "bound_package", return_value="my-pkg"):
            block = dashboard_agentic.turn_block("mate")
        assert dashboard_agentic.SUGGESTIONS_LINE in block, block

    def test_the_profile_view_claims_no_pointer_for_the_suggestions_page(
        self, builtin: Path
    ) -> None:
        from kiro_crew import dashboard_upkeep

        view = dashboard_upkeep.view("mate")
        assert view is not None and view["pointer"] == ""


class TestAnOperatorPageNeverTakesOverByName:
    def test_a_page_in_the_operator_folder_named_like_the_slug_is_not_the_default(
        self, builtin: Path
    ) -> None:
        from kiro_crew import agent_panel

        directory = agent_panel.data_home() / agent_panel.TEMPLATES_DIRNAME / "mate"
        directory.mkdir(parents=True)
        manifest = {
            "id": "mate",
            "version": 1,
            "title": "mate",
            "description": "An operator page that shares a crewmate's name.",
            "source": "user",
            "fields": {"phase": {"type": "string", "source": {"agentic": True}}},
        }
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (directory / "template.html").write_text(PAGE, encoding="utf-8")
        assert "mate" in catalog.list_templates().by_id, "the operator page did not load"
        page = instance.default_instance("mate")
        assert page is not None and page.template_id == instance.DEFAULT_TEMPLATE_ID


class TestAnAdoptedPageWins:
    @pytest.mark.asyncio
    async def test_over_the_slug_page_on_the_route(self, builtin: Path) -> None:
        instance.adopt(SLUG, "fixture-other")
        assert await _route_template() == "fixture-other"

    def test_over_the_slug_page_in_the_turn_block(self, builtin: Path) -> None:
        instance.adopt(SLUG, "fixture-other")
        block = dashboard_agentic.turn_block(SLUG)
        assert "template fixture-other v1" in block
        assert f"template {SLUG} " not in block

    def test_over_the_default_in_the_turn_block(self, builtin: Path) -> None:
        instance.adopt("mate", "fixture-other")
        assert "template fixture-other v1" in dashboard_agentic.turn_block("mate")


class TestTheShippedRegistry:
    def test_no_shipped_page_is_named_default(self) -> None:
        """A crewmate named ``default`` exists in real installs, and a page of that id
        would take its tab over with no one choosing it."""
        assert "default" not in catalog.list_templates().by_id

    def test_the_conductor_gets_its_board(self) -> None:
        page = instance.default_instance("kirocrew-conductor")
        assert page is not None and page.template_id == "kirocrew-conductor"

    def test_a_crewmate_named_default_gets_the_default_page(self) -> None:
        page = instance.default_instance("default")
        assert page is not None and page.template_id == instance.DEFAULT_TEMPLATE_ID
