"""App browser recipes: fixed clicks on selectors that Crew runs with no model.

``apps/browser_recipes.py`` validates ``permissions.browserRecipes`` and reads the
approved ones; ``browser_harness/recipes.py`` runs one on a tab; the media route
(``action: "recipe"``) and the media watch (``watch`` recipes) are the two callers.
These tests pin what a recipe may hold (clicks and waits on selectors only), that
it never acts off its site or the session's sites, that a failed step names itself,
that an install approves none and any change is a new entry, and that the watch
presses a shown "Skip ad" by itself.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import browser_recipes as br
from kiro_crew.apps import browser_sites, hooks_integration, manager_records
from kiro_crew.apps.manifest_sections import Permissions
from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.browser_harness import ops, recipes
from kiro_crew.dashboard.handlers import browser_media
from kiro_crew.dashboard.handlers import browser_media_watch as watch
from kiro_crew.dashboard.handlers import browser_ops
from kiro_crew.dashboard.routes import browser_live as route_slice
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until, await_until

SITE = "https://www.youtube.com/watch"
WATCH_URL = "https://www.youtube.com/watch?v=abc"
PLAY = {"name": "play", "site": SITE, "steps": [{"click": "button.play"}]}
SKIP = {"name": "skip_ad", "site": SITE, "watch": True, "steps": [{"click": ".skip"}]}


def _one(raw: dict[str, Any]) -> br.Recipe:
    parsed = br.parse_recipes([raw])
    assert len(parsed) == 1
    return parsed[0]


# ── the declaration ─────────────────────────────────────────────────────────


def test_clicks_and_waits_on_selectors_are_a_valid_declaration() -> None:
    wait_then_click = {
        "name": "play",
        "site": SITE,
        "steps": [{"waitFor": "button.play", "timeout": 5}, {"click": "button.play"}],
    }
    assert br.recipe_errors([wait_then_click, SKIP]) == []
    assert Permissions.from_dict({"browserRecipes": [wait_then_click]}).list_errors() == []


@pytest.mark.parametrize(
    "step",
    [
        {"evaluate": "document.body.click()"},
        {"type": "hello"},
        {"navigate": "https://evil.example/"},
        {"click": ".a", "text": "x"},
        {"click": ""},
        {"click": "a\nb"},
        {"click": "x" * (br.MAX_SELECTOR + 1)},
        {"waitFor": ".a", "timeout": 0},
        {"waitFor": ".a", "timeout": br.MAX_WAIT_S + 1},
        {"waitFor": ".a", "timeout": True},
    ],
)
def test_anything_but_a_click_or_a_bounded_wait_is_refused(step: dict[str, Any]) -> None:
    raw = {"name": "play", "site": SITE, "steps": [step, {"click": ".ok"}]}
    assert br.recipe_errors([raw])
    assert br.parse_recipes([raw]) == []


@pytest.mark.parametrize(
    "raw",
    [
        {**PLAY, "site": "http://www.youtube.com/watch"},
        {**PLAY, "site": "https://www.youtube.com"},
        {**PLAY, "name": "Play"},
        {**PLAY, "steps": []},
        {**PLAY, "steps": [{"waitFor": ".a"}]},
        {**PLAY, "script": "x"},
        {**SKIP, "steps": [{"waitFor": ".skip"}, {"click": ".skip"}]},
        {**PLAY, "steps": [{"waitFor": ".a", "timeout": 10}, {"waitFor": ".b", "timeout": 10}]},
    ],
)
def test_a_bad_recipe_admits_no_recipe_at_all(raw: dict[str, Any]) -> None:
    assert br.recipe_errors([PLAY, raw]) or br.recipe_errors([raw])
    assert br.parse_recipes([SKIP, raw]) == []


def test_two_recipes_of_one_name_are_refused() -> None:
    assert br.recipe_errors([PLAY, PLAY])


def test_a_declaration_that_is_not_a_list_is_an_error() -> None:
    perms = Permissions.from_dict({"browserRecipes": {"play": PLAY}})
    assert perms.list_errors() == ["permissions.browserRecipes must be an array of recipes"]


# ── the grant ───────────────────────────────────────────────────────────────


def test_any_change_to_a_recipe_is_a_new_grant_entry() -> None:
    before = br.recipe_entry(_one(PLAY))
    assert br.recipe_entry(_one({**PLAY, "steps": [{"click": "button.other"}]})) != before
    assert br.recipe_entry(_one({**PLAY, "site": "https://www.youtube.com/live"})) != before
    assert br.recipe_entry(_one({**PLAY, "watch": True})) != before
    assert br.recipe_entry(_one(dict(PLAY))) == before


def test_an_install_approves_no_recipe() -> None:
    perms = {"browserRecipes": [PLAY]}
    assert "browserRecipes" in manager_records.STAGED_GRANT_FAMILIES
    assert "browserRecipes" in manager_records._EXPLICIT_ONLY_FAMILIES
    from kiro_crew.apps.point_grants import grant_source

    declared = grant_source({"permissions": perms})
    assert declared is not None and declared["browserRecipes"] == [br.recipe_entry(_one(PLAY))]
    consented = manager_records._consented_grant_entries(declared)
    assert consented["browserRecipes"] == []


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from kiro_crew.apps import execution, manager

    env = SimpleNamespace(
        approved=set(), watch=set(), enabled=True, ceiling=None, recipes=[PLAY, SKIP], preview=True
    )
    monkeypatch.setattr(
        manager,
        "get_app_manifest",
        lambda app: SimpleNamespace(
            permissions=Permissions.from_dict({"browserRecipes": env.recipes})
        ),
    )
    monkeypatch.setattr(manager, "is_app_enabled", lambda app: env.enabled)
    monkeypatch.setattr(
        manager,
        "staged_app_grants",
        lambda app, family, declared: list(
            env.watch if family == br.WATCH_FAMILY else env.approved
        ),
    )
    monkeypatch.setattr(execution, "third_party_ceiling_closed", lambda app: env.ceiling)
    monkeypatch.setattr(br, "preview_on", lambda: env.preview)
    return env


def test_only_recipes_approved_as_declared_run(installed: SimpleNamespace) -> None:
    assert br.app_browser_recipes("tunes") == ()
    installed.approved = {br.recipe_entry(_one(PLAY))}
    assert [r.name for r in br.app_browser_recipes("tunes")] == ["play"]
    # An update that changes the approved recipe holds it until the owner approves again.
    installed.recipes = [{**PLAY, "steps": [{"click": "button.elsewhere"}]}, SKIP]
    assert br.app_browser_recipes("tunes") == ()
    row = br.browser_recipes_grant_row(
        "tunes", {"permissions": {"browserRecipes": installed.recipes}}
    )
    assert len(row["browserRecipesGrant"]["staged"]) == 2


@pytest.mark.parametrize("off", ["disabled", "untrusted"])
def test_an_app_that_is_off_or_untrusted_runs_no_recipe(
    installed: SimpleNamespace, off: str
) -> None:
    installed.approved = {br.recipe_entry(_one(PLAY))}
    if off == "disabled":
        installed.enabled = False
    else:
        installed.ceiling = "untrusted"
    assert br.app_browser_recipes("tunes") == ()


def test_the_preview_off_runs_no_recipe_and_keeps_the_approval(
    installed: SimpleNamespace,
) -> None:
    skip = br.recipe_entry(_one(SKIP))
    installed.approved = {br.recipe_entry(_one(PLAY)), skip}
    installed.watch = {skip}
    installed.preview = False
    assert br.app_browser_recipes("tunes") == ()
    assert br.app_watch_recipes("tunes") == ()
    assert watch._watch_recipes("tunes") == ()
    grant = br.browser_recipes_grant_row("tunes", {"permissions": {"browserRecipes": [PLAY, SKIP]}})
    # Stored, shown as approved, and inert: the page says the preview is off.
    assert grant["browserRecipesGrant"]["preview"] is False
    assert grant["browserRecipesGrant"]["staged"] == []
    installed.preview = True
    assert [r.name for r in br.app_watch_recipes("tunes")] == ["skip_ad"]


def test_a_watch_recipe_runs_by_itself_only_with_the_second_yes(
    installed: SimpleNamespace,
) -> None:
    skip = br.recipe_entry(_one(SKIP))
    installed.approved = {skip}
    # Approved alone: the page may run it, the watch does not.
    assert [r.name for r in br.app_browser_recipes("tunes")] == ["skip_ad"]
    assert br.app_watch_recipes("tunes") == ()
    assert watch._watch_recipes("tunes") == ()
    row = br.browser_recipes_grant_row("tunes", {"permissions": {"browserRecipes": [PLAY, SKIP]}})
    assert row["browserRecipesGrant"]["watchOff"] == [skip]
    assert row["browserRecipesGrant"]["watchOn"] == []
    installed.watch = {skip}
    assert [r.name for r in watch._watch_recipes("tunes")] == ["skip_ad"]
    row = br.browser_recipes_grant_row("tunes", {"permissions": {"browserRecipes": [PLAY, SKIP]}})
    assert row["browserRecipesGrant"]["watchOn"] == [skip]
    # The second yes without the recipe's own approval runs nothing.
    installed.approved = set()
    assert br.app_watch_recipes("tunes") == ()


def test_an_install_gives_no_watch_yes() -> None:
    from kiro_crew.apps.point_grants import grant_source

    assert br.WATCH_FAMILY in manager_records.STAGED_GRANT_FAMILIES
    assert br.WATCH_FAMILY in manager_records._EXPLICIT_ONLY_FAMILIES
    declared = grant_source({"permissions": {"browserRecipes": [PLAY, SKIP]}})
    assert declared is not None
    assert declared[br.WATCH_FAMILY] == [br.recipe_entry(_one(SKIP))]
    assert manager_records._consented_grant_entries(declared)[br.WATCH_FAMILY] == []


@pytest.mark.parametrize(
    ("declared", "approved_names", "expected"),
    [
        ([SKIP, PLAY], ["play"], [PLAY]),
        ([SKIP, PLAY], ["play", "skip_ad"], [SKIP, PLAY]),
        ([PLAY, SKIP], [], []),
        ([PLAY, {**SKIP, "steps": []}], ["play"], []),
        ({"play": PLAY}, ["play"], []),
        (None, ["play"], []),
    ],
)
def test_hook_permissions_keep_only_approved_raw_recipes(
    monkeypatch: pytest.MonkeyPatch,
    declared: Any,
    approved_names: list[str],
    expected: list[dict[str, Any]],
) -> None:
    from kiro_crew.apps import manager

    grants = {
        br.GRANT_FAMILY: [
            br.recipe_entry(_one(r)) for r in (PLAY, SKIP) if r["name"] in approved_names
        ],
        "api": ["/api/browser/media"],
    }
    monkeypatch.setattr(
        manager,
        "get_app_manifest",
        lambda app: SimpleNamespace(
            permissions=Permissions.from_dict(
                {br.GRANT_FAMILY: declared, "api": ["/api/browser/media", "/api/sessions"]}
            )
        ),
    )
    monkeypatch.setattr(manager, "_read_installed", lambda app: {"name": app})
    monkeypatch.setattr(manager, "_effective_approved_grants", lambda app, meta: grants)
    row = {
        "name": "tunes",
        "manifest": {
            "permissions": {
                br.GRANT_FAMILY: [PLAY, SKIP],
                "api": ["/api/browser/media", "/api/sessions"],
            }
        },
    }
    assert manager.approved_manifest_permissions(row) == {
        br.GRANT_FAMILY: expected,
        "api": ["/api/browser/media"],
    }
    assert row["manifest"]["permissions"][br.GRANT_FAMILY] == [PLAY, SKIP]


# ── running one ─────────────────────────────────────────────────────────────


class _Page:
    """A fake tab: its URL, which selectors are shown where, and the clicks it took.

    A ``recipeClick`` on a shown selector records the element's centre as clicked:
    the walker clicks the element itself (``el.click()``).
    """

    def __init__(self, url: str = WATCH_URL, shown: dict[str, Any] | None = None) -> None:
        self.url = url
        self.shown: dict[str, Any] = shown if shown is not None else {}
        self.clicks: list[tuple[float, float]] = []
        self.gotos: list[str] = []
        self.probes = 0
        self.events: list[str] = []
        #: The arguments after the selector of each walker call (``recipeClick``'s site).
        self.sites_seen: list[list[Any]] = []
        #: The ctx's clock: only a sleep moves it, unless a test moves it itself.
        self.clock = 0.0

    async def helper(self, name: str, **kw: Any) -> Any:
        if name == "current_tab":
            return {"url": self.url, "targetId": "T0"}
        if name == "goto":
            self.gotos.append(kw["url"])
            self.url = kw["url"]
            return None
        if name == "switch_tab":
            return None
        if name == "js_isolated":
            m = re.search(r"\.(recipeTarget|recipeClick)\((.*)\)$", kw["expression"])
            assert m, kw["expression"]
            self.events.append(m.group(1))
            self.probes += 1
            args = json.loads(f"[{m.group(2)}]")
            self.sites_seen.append(args[1:])
            found = self.shown.get(args[0])
            if callable(found):
                found = found()
            found = found or {"ok": False, "code": "not_found"}
            if m.group(1) == "recipeClick" and found.get("ok"):
                self.clicks.append((found["x"], found["y"]))
            return found
        raise AssertionError(f"unexpected helper {name}")

    def ctx(self, sites: tuple[str, ...] = (SITE,)) -> ops.OpContext:
        async def sleep(s: float) -> None:
            self.clock += s

        return ops.OpContext(
            helper=self.helper,
            sites=sites,
            url_allowed=lambda u: browser_sites.url_matches(sites, u),
            sleep=sleep,
            now=lambda: self.clock,
        )


DONE_PLAY = {"ok": True, "recipe": "play", "steps": 1, "click": "script"}


@pytest.mark.asyncio
async def test_a_click_is_one_walker_call_bound_to_the_element() -> None:
    page = _Page(shown={"button.play": {"ok": True, "x": 10.0, "y": 20.0}})
    assert await recipes.run(page.ctx(), _one(PLAY)) == DONE_PLAY
    assert page.clicks == [(10.0, 20.0)]
    # No point-based input helper: the walker finds and clicks in the same call, and
    # judges the recipe's own site in it too.
    assert page.events == ["recipeClick"]
    assert page.sites_seen == [[SITE, [SITE]]]


@pytest.mark.asyncio
async def test_frame_shaped_recipe_selectors_stay_in_the_top_document() -> None:
    from unittest.mock import AsyncMock

    page = _Page(shown={"f1e1": {"ok": True, "x": 10.0, "y": 20.0}})
    frames = SimpleNamespace(
        url_for_ref=AsyncMock(return_value=WATCH_URL),
        call=AsyncMock(return_value={"ok": True, "x": 90.0, "y": 80.0}),
    )
    ctx = page.ctx()
    ctx.frames = lambda: frames
    recipe = _one({**PLAY, "steps": [{"waitFor": "f1e1"}, {"click": "f1e1"}]})
    assert await recipes.first_target_shown(ctx, _one({**SKIP, "steps": [{"click": "f1e1"}]}))
    assert await recipes.run(ctx, recipe) == {**DONE_PLAY, "steps": 2}
    frames.call.assert_not_called()
    frames.url_for_ref.assert_not_called()
    assert page.clicks == [(10.0, 20.0)]
    assert page.events == ["recipeTarget", "recipeTarget", "recipeClick"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["covered", "hidden", "password_field", "bad_selector"])
async def test_a_target_the_walker_refuses_gets_no_click(code: str) -> None:
    page = _Page(shown={"button.play": {"ok": False, "code": code}})
    raw = {**PLAY, "steps": [{"click": "button.play"}, {"click": "button.play"}]}
    out = await recipes.run(page.ctx(), _one(raw))
    assert out["ok"] is False and out["code"] == code and out["step"] == 0
    assert page.clicks == []


@pytest.mark.asyncio
async def test_a_missing_element_names_its_step_and_clicks_nothing() -> None:
    page = _Page()
    raw = {**PLAY, "steps": [{"click": "button.play"}, {"click": ".next"}]}
    page.shown["button.play"] = {"ok": True, "x": 1.0, "y": 1.0}
    out = await recipes.run(page.ctx(), _one(raw))
    assert out["ok"] is False and out["code"] == "not_found" and out["step"] == 1
    assert out["recipe"] == "play"
    assert page.clicks == [(1.0, 1.0)]


@pytest.mark.asyncio
async def test_a_page_off_the_recipe_site_is_refused_untouched() -> None:
    page = _Page(
        url="https://www.youtube.com/results?q=x",
        shown={"button.play": {"ok": True, "x": 1.0, "y": 1.0}},
    )
    out = await recipes.run(page.ctx(sites=(SITE, "https://www.youtube.com/results")), _one(PLAY))
    assert out["code"] == "wrong_site"
    assert page.clicks == [] and page.probes == 0


@pytest.mark.asyncio
async def test_a_page_off_the_session_sites_is_blanked_and_refused() -> None:
    page = _Page(shown={"button.play": {"ok": True, "x": 1.0, "y": 1.0}})
    out = await recipes.run(page.ctx(sites=("https://www.youtube.com/other",)), _one(PLAY))
    assert out["code"] == "redirect_refused"
    assert out["recipe"] == "play" and out["step"] == 0 and out["error"]
    assert page.clicks == [] and page.gotos == ["about:blank"]


@pytest.mark.asyncio
async def test_a_page_that_leaves_the_sites_before_a_later_click_names_that_step() -> None:
    page = _Page(shown={"button.play": {"ok": True, "x": 1.0, "y": 1.0}})

    def found_then_moved() -> dict[str, Any]:
        page.url = "https://www.youtube.com/account"
        return {"ok": True, "x": 2.0, "y": 2.0}

    page.shown[".next"] = found_then_moved
    raw = {
        **PLAY,
        "steps": [{"click": "button.play"}, {"waitFor": ".next", "timeout": 1}, {"click": ".next"}],
    }
    out = await recipes.run(page.ctx(), _one(raw))
    assert out["code"] == "redirect_refused" and out["error"]
    assert out["recipe"] == "play" and out["step"] == 2
    assert page.clicks == [(1.0, 1.0)] and page.gotos == ["about:blank"]


@pytest.mark.asyncio
async def test_a_page_that_moves_off_the_recipe_site_before_the_click_gets_none() -> None:
    page = _Page()

    def found_then_moved() -> dict[str, Any]:
        # One of the session's sites, but not the recipe's.
        page.url = "https://www.youtube.com/results?q=x"
        return {"ok": True, "x": 1.0, "y": 1.0}

    page.shown["button.play"] = found_then_moved
    raw = {**PLAY, "steps": [{"waitFor": "button.play", "timeout": 1}, {"click": "button.play"}]}
    out = await recipes.run(page.ctx(sites=(SITE, "https://www.youtube.com/results")), _one(raw))
    assert out["code"] == "wrong_site" and out["step"] == 1
    assert page.clicks == []


@pytest.mark.asyncio
async def test_a_click_that_moves_the_page_off_the_sites_is_blanked_and_refused() -> None:
    page = _Page()

    def clicked_then_moved() -> dict[str, Any]:
        if page.events[-1] == "recipeClick":
            page.url = "https://www.youtube.com/account"
        return {"ok": True, "x": 1.0, "y": 1.0}

    page.shown["button.play"] = clicked_then_moved
    raw = {**PLAY, "steps": [{"waitFor": "button.play", "timeout": 1}, {"click": "button.play"}]}
    out = await recipes.run(page.ctx(), _one(raw))
    assert out["code"] == "redirect_refused" and out["error"]
    assert out["recipe"] == "play" and out["step"] == 1
    assert page.gotos == ["about:blank"]


@pytest.mark.asyncio
async def test_a_page_refused_inside_a_click_names_the_recipe_and_step() -> None:
    page = _Page()
    calls = 0

    def second_refused() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ops._PageRefused(ops.refusal("redirect_refused", "the page left the sites"))
        return {"ok": True, "x": 1.0, "y": 1.0}

    page.shown["button.play"] = second_refused
    raw = {**PLAY, "steps": [{"click": "button.play"}, {"click": "button.play"}]}
    out = await recipes.run(page.ctx(), _one(raw))
    assert out == {
        "ok": False,
        "code": "redirect_refused",
        "error": "the page left the sites",
        "recipe": "play",
        "step": 1,
    }
    assert page.clicks == [(1.0, 1.0)]


@pytest.mark.asyncio
async def test_a_wait_looks_again_until_the_element_shows() -> None:
    answers = iter([None, None, {"ok": True, "x": 5.0, "y": 6.0}])
    page = _Page()
    page.shown["button.play"] = lambda: next(answers, {"ok": True, "x": 5.0, "y": 6.0})
    raw = {**PLAY, "steps": [{"waitFor": "button.play", "timeout": 2}, {"click": "button.play"}]}
    assert (await recipes.run(page.ctx(), _one(raw)))["ok"] is True
    assert page.clicks == [(5.0, 6.0)]


@pytest.mark.asyncio
async def test_a_wait_that_never_sees_its_element_times_out() -> None:
    page = _Page()
    raw = {**PLAY, "steps": [{"waitFor": ".never", "timeout": 1}, {"click": ".never"}]}
    out = await recipes.run(page.ctx(), _one(raw))
    assert out["code"] == "wait_timeout" and out["step"] == 0
    assert page.clicks == []
    # The last sleep is cut to the time left, so the wait ends at its deadline.
    assert page.clock == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_an_element_that_shows_after_the_deadline_is_not_accepted() -> None:
    page = _Page()

    def slow_probe() -> dict[str, Any]:
        # The probe itself runs past the deadline, then finds the element.
        page.clock += 1.5
        return {"ok": True, "x": 1.0, "y": 1.0}

    page.shown["button.play"] = slow_probe
    raw = {**PLAY, "steps": [{"waitFor": "button.play", "timeout": 1}, {"click": "button.play"}]}
    out = await recipes.run(page.ctx(), _one(raw))
    assert out["code"] == "wait_timeout" and out["step"] == 0
    assert page.clicks == [] and page.probes == 1


# ── the route ───────────────────────────────────────────────────────────────

KEY = "app-tunes-1"


@pytest.fixture
def route(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    hub_mod.reset_live_hub()
    env = SimpleNamespace(approved=(), sites=(SITE,), ran=[], preview=True)

    async def run_recipe(_hub: Any, key: str, _sup: Any, _sites: Any, recipe: Any) -> Any:
        env.ran.append((key, recipe.name))
        return {"ok": True, "recipe": recipe.name, "steps": len(recipe.steps)}

    monkeypatch.setattr(browser_ops, "app_browser_sites", lambda app: env.sites)
    monkeypatch.setattr(br, "app_browser_recipes", lambda app: env.approved)
    monkeypatch.setattr(br, "preview_on", lambda: env.preview)
    monkeypatch.setattr(browser_media, "_running_supervisor", lambda: SimpleNamespace())
    monkeypatch.setattr(browser_media, "_run_recipe", run_recipe)
    monkeypatch.setattr(browser_media.browser_live, "_audit", lambda *_a, **_k: None)
    yield env
    hub_mod.reset_live_hub()


def _app() -> web.Application:
    @web.middleware
    async def as_owner(request: web.Request, handler: Any) -> Any:
        request["user"], request["app"] = "local-app", ""
        return await handler(request)

    app = web.Application(middlewares=[as_owner])
    app["state"] = SimpleNamespace(
        owner_id="",
        _slots={KEY: SimpleNamespace(_app="tunes")},
        deliver_ws_owners=lambda *_a, **_k: None,
        deliver_ws_owners_bytes=lambda *_a, **_k: None,
    )
    route_slice.register(app)
    hub_mod.get_live_hub().bind_target(KEY, "T0")
    return app


async def _ask(client: TestClient, name: Any) -> tuple[int, dict[str, Any]]:
    resp = await client.post(
        "/api/browser/media", json={"session_key": KEY, "action": "recipe", "recipe": name}
    )
    return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_the_page_runs_an_approved_recipe_by_name(route: SimpleNamespace) -> None:
    route.approved = (_one(PLAY),)
    async with TestClient(TestServer(_app())) as client:
        status, body = await _ask(client, "play")
    assert status == 200
    assert body == {
        "ok": True,
        "session_key": KEY,
        "recipe": "play",
        "steps": 1,
        "click": "script",
    }
    assert route.ran == [(KEY, "play")]


@pytest.mark.asyncio
async def test_the_recipe_audit_row_names_the_recipe_and_its_script_click(
    route: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(browser_media.browser_live, "_audit", lambda _r, **kw: rows.append(kw))
    route.approved = (_one(PLAY),)
    async with TestClient(TestServer(_app())) as client:
        await _ask(client, "play")
    assert rows[-1]["operation"] == "browser_recipe" and rows[-1]["outcome"] == "allowed"
    assert rows[-1]["resources"] == "/api/browser/media recipe='play' click=script"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["skip_ad", "nope", None])
async def test_a_recipe_the_owner_has_not_approved_is_refused(
    route: SimpleNamespace, name: Any
) -> None:
    route.approved = (_one(PLAY),)
    async with TestClient(TestServer(_app())) as client:
        status, body = await _ask(client, name)
    assert status == 403 and body["code"] == "recipe_not_approved"
    assert route.ran == []


@pytest.mark.asyncio
async def test_a_recipe_with_the_preview_off_is_refused_with_its_code(
    route: SimpleNamespace,
) -> None:
    from kiro_crew.apps.browser_sdk import PREVIEW_OFF_TEXT

    route.approved = (_one(PLAY),)
    route.preview = False
    async with TestClient(TestServer(_app())) as client:
        status, body = await _ask(client, "play")
    assert status == 403
    assert body["code"] == "preview_off" and body["error"] == PREVIEW_OFF_TEXT
    assert route.ran == []


@pytest.mark.asyncio
@pytest.mark.parametrize("withdrawn", ["recipe", "sites", "held_sites", "preview"])
async def test_approval_withdrawn_while_the_recipe_waits_for_the_lock_refuses(
    route: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, withdrawn: str
) -> None:
    route.approved = (_one(PLAY),)
    monkeypatch.setattr(browser_media, "LOCK_WAIT_S", FALLBACK_TIMEOUT_SECS)
    async with TestClient(TestServer(_app())) as client:
        lock = browser_ops._OP_LOCK
        await lock.acquire()
        pending = asyncio.create_task(_ask(client, "play"))
        try:
            await async_wait_until(
                lambda: lock._bound()._waiters or pending.done(),
                describe=lambda: "the recipe has not queued for the browser lock",
            )
            assert not pending.done()
            if withdrawn == "recipe":
                route.approved = ()
            elif withdrawn == "preview":
                route.preview = False
            else:
                route.sites = browser_sites.NO_PAGES if withdrawn == "held_sites" else ()
        finally:
            lock.release()
            status, body = await await_until(pending)
    assert status == 200 and body["ok"] is False
    expected = {"recipe": "recipe_not_approved", "preview": "preview_off"}
    assert body["code"] == expected.get(withdrawn, "not_app_browser")
    if withdrawn == "held_sites":
        assert body["error"] == browser_ops.SITES_REVOKED_TEXT
    assert route.ran == []


@pytest.mark.asyncio
async def test_a_recipe_waits_while_the_person_holds_the_tab(route: SimpleNamespace) -> None:
    route.approved = (_one(PLAY),)
    app = _app()
    hub = hub_mod.get_live_hub()
    async with TestClient(TestServer(app)) as client:
        hub.in_control = lambda key: True  # type: ignore[method-assign]
        status, body = await _ask(client, "play")
    assert status == 409 and body["code"] == "user_in_control"
    assert route.ran == []


# ── the watch ───────────────────────────────────────────────────────────────


@pytest.fixture
def watched(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    hub_mod.reset_live_hub()
    watch._WATCHES.clear()
    watch._ENDED.clear()
    page = _Page(shown={".skip": {"ok": True, "x": 3.0, "y": 4.0}})
    playing = {"ok": True, "paused": False, "ended": False, "endId": None, "origin": ""}
    env = SimpleNamespace(
        page=page, recipes=(_one(SKIP),), hooked=set(), sent=[], reads=0, media=playing
    )

    async def dispatch(app: str, hook: str, event: dict[str, Any]) -> bool:
        env.sent.append((app, hook, event))
        return True

    async def own_tab(_hub: Any, _key: str, _sup: Any, sites: Any) -> Any:
        return page.ctx(sites)

    async def read(*_a: Any) -> dict[str, Any]:
        env.reads += 1
        return dict(env.media)

    monkeypatch.setattr(browser_ops, "app_browser_sites", lambda app: (SITE,))
    monkeypatch.setattr(watch, "_hook_declared", lambda app: app in env.hooked)
    monkeypatch.setattr(watch, "_watch_recipes", lambda app: env.recipes)
    monkeypatch.setattr(watch, "_read", read)
    monkeypatch.setattr(browser_media, "own_tab_context", own_tab)
    monkeypatch.setattr(browser_media, "_running_supervisor", lambda: SimpleNamespace())
    monkeypatch.setattr(browser_ops, "_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(hooks_integration, "dispatch_app_event", dispatch)
    monkeypatch.setattr(watch, "WATCH_S", 0)
    yield env
    for task in watch._WATCHES.values():
        task.cancel()
    watch._WATCHES.clear()
    watch._ENDED.clear()
    hub_mod.reset_live_hub()


def _slot() -> SimpleNamespace:
    hub_mod.get_live_hub().bind_target(KEY, "T0")
    return SimpleNamespace(_slots={KEY: SimpleNamespace(_app="tunes")})


@pytest.mark.asyncio
async def test_the_watch_presses_a_shown_skip_button_with_no_hook(
    watched: SimpleNamespace,
) -> None:
    watch.ensure_watch(_slot(), KEY)
    await async_wait_until(lambda: len(watched.page.clicks) >= 2, describe=lambda: "two skips")
    assert watched.page.clicks[0] == (3.0, 4.0)
    # No hook declared: the watch reads for the recipe, and hands no ending over.
    assert watched.sent == []


@pytest.mark.asyncio
async def test_the_run_by_itself_yes_restarts_the_watch_on_the_apps_open_tabs(
    watched: SimpleNamespace,
) -> None:
    state = _slot()
    state._slots["other-1"] = SimpleNamespace(_app="other")
    hub_mod.get_live_hub().bind_target("other-1", "T1")
    state._slots["tunes-closed"] = SimpleNamespace(_app="tunes")
    watch.watch_app_tabs(state, "tunes")
    assert set(watch._WATCHES) == {KEY}
    await async_wait_until(lambda: watched.page.clicks, describe=lambda: "a skip")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "media",
    [
        {"ok": True, "paused": True, "ended": False},
        {"ok": True, "paused": False, "ended": True, "endId": "d:1"},
        {"ok": False, "code": "no_media"},
    ],
)
async def test_a_watch_recipe_runs_only_while_media_plays(
    watched: SimpleNamespace, media: dict[str, Any]
) -> None:
    watched.media = media
    watch.ensure_watch(_slot(), KEY)
    await async_wait_until(lambda: watched.reads >= 3, describe=lambda: "three ticks")
    assert watched.page.clicks == []


@pytest.mark.asyncio
async def test_the_watch_clicks_nothing_while_the_button_is_hidden(
    watched: SimpleNamespace,
) -> None:
    watched.page.shown = {".skip": {"ok": False, "code": "hidden"}}
    watch.ensure_watch(_slot(), KEY)
    await async_wait_until(lambda: watched.reads >= 3, describe=lambda: "three ticks")
    assert watched.page.clicks == []


@pytest.mark.asyncio
async def test_the_watch_ends_once_the_recipe_is_withdrawn(watched: SimpleNamespace) -> None:
    watch.ensure_watch(_slot(), KEY)
    task = watch._WATCHES[KEY]
    await async_wait_until(lambda: watched.page.clicks, describe=lambda: "a skip")
    watched.recipes = ()
    await async_wait_until(task.done, describe=lambda: "the watch still runs")


@pytest.mark.asyncio
@pytest.mark.parametrize("withdrawn", ["recipes", "sites", "slot"])
async def test_a_watch_tick_rechecks_approval_after_still_watched(
    watched: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, withdrawn: str
) -> None:
    state = _slot()
    current = await watch._still_watched(state, KEY)
    assert current is not None
    app, sites, _hooked, watching = current
    if withdrawn == "recipes":
        watched.recipes = ()
    elif withdrawn == "sites":
        monkeypatch.setattr(browser_ops, "app_browser_sites", lambda app: ())
    else:
        state._slots.clear()
    await watch._tick(state, hub_mod.get_live_hub(), KEY, sites, app, watching)
    assert watched.page.clicks == []
    assert watched.reads == 1


@pytest.mark.asyncio
async def test_the_watch_skips_a_tick_while_the_person_holds_the_tab(
    watched: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = hub_mod.get_live_hub()
    monkeypatch.setattr(hub, "in_control", lambda key: True)
    state = _slot()
    result = await watch._tick(state, hub, KEY, (SITE,), "tunes", watched.recipes)
    assert result is None and watched.page.clicks == []
    del state


@pytest.mark.asyncio
async def test_a_watch_recipe_cut_short_still_writes_its_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[tuple[Any, str, str]] = []

    async def shown(_ctx: Any, _recipe: Any) -> bool:
        return True

    async def broken(_ctx: Any, _recipe: Any) -> dict[str, Any]:
        raise RuntimeError("the browser went away after the click")

    monkeypatch.setattr(recipes, "first_target_shown", shown)
    monkeypatch.setattr(recipes, "run", broken)
    monkeypatch.setattr(
        browser_ops,
        "_audit",
        lambda who, outcome, error: rows.append((who.get("user"), outcome, error)),
    )
    with pytest.raises(RuntimeError):
        await watch._run_watch_recipes(object(), "tunes", (_one(SKIP),))
    assert rows == [("app:tunes", "error", "interrupted")]


# ── turning one watch recipe's run-by-itself yes off ───────────────────────


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from kiro_crew.apps import manager

    skip, play = br.recipe_entry(_one(SKIP)), br.recipe_entry(_one(PLAY))
    env = SimpleNamespace(
        grants={br.GRANT_FAMILY: [play, skip], br.WATCH_FAMILY: [skip], "api": ["/x"]},
        writes=[],
        installed=True,
        skip=skip,
    )
    monkeypatch.setattr(
        manager, "_read_installed", lambda app: SimpleNamespace() if env.installed else None
    )
    monkeypatch.setattr(manager, "_effective_approved_grants", lambda app, meta: env.grants)

    def write(app: str, grants: dict[str, list[str]]) -> None:
        env.writes.append((app, grants))
        env.grants = grants

    monkeypatch.setattr(manager, "_write_approved_grants", write)
    return env


def test_turning_the_watch_off_removes_only_that_yes(stored: SimpleNamespace) -> None:
    assert br.withdraw_watch("tunes", stored.skip) is True
    ((app, grants),) = stored.writes
    assert app == "tunes" and grants[br.WATCH_FAMILY] == []
    # The recipe stays approved for on-demand runs, and nothing else moves.
    assert grants[br.GRANT_FAMILY] == [br.recipe_entry(_one(PLAY)), stored.skip]
    assert grants["api"] == ["/x"]
    assert br.withdraw_watch("tunes", stored.skip) is False
    assert len(stored.writes) == 1


@pytest.mark.parametrize("app", ["../etc", "", "a/b"])
def test_turning_the_watch_off_refuses_an_app_name_that_is_not_one(
    stored: SimpleNamespace, app: str
) -> None:
    with pytest.raises(LookupError):
        br.withdraw_watch(app, stored.skip)
    stored.installed = False
    with pytest.raises(LookupError):
        br.withdraw_watch("tunes", stored.skip)
    assert stored.writes == []


def _watch_off_app(identity: str) -> web.Application:
    from kiro_crew.apps.browser_recipes_routes import register_browser_recipes_routes

    @web.middleware
    async def as_caller(request: web.Request, handler: Any) -> Any:
        request["user"], request["app"] = "local-app", ""
        if identity == "internal":
            request["internal_auth"] = True
        if identity == "app":
            request["user"], request["app"] = "tunes", "tunes"
        return await handler(request)

    app = web.Application(middlewares=[as_caller])
    app["state"] = SimpleNamespace(owner_id="")
    register_browser_recipes_routes(app)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["owner", "internal", "app"])
async def test_only_the_owner_turns_a_watch_off(stored: SimpleNamespace, identity: str) -> None:
    async with TestClient(TestServer(_watch_off_app(identity))) as client:
        resp = await client.post(
            "/api/browser/recipe-watch-off", json={"app": "tunes", "recipe": stored.skip}
        )
        body = await resp.json()
    if identity == "owner":
        assert resp.status == 200 and body == {"ok": True, "removed": True}
        assert stored.grants[br.WATCH_FAMILY] == []
    else:
        assert resp.status == 403
        assert stored.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ("not json", 400, "bad_request"),
        ("[1]", 400, "bad_request"),
        ('{"app": "tunes"}', 400, "bad_request"),
        ('{"app": "", "recipe": "x"}', 400, "bad_request"),
        ('{"app": "tunes", "recipe": "' + "x" * 9000 + '"}', 413, "body_too_large"),
    ],
)
async def test_a_watch_off_with_a_bad_body_changes_nothing(
    stored: SimpleNamespace, body: str, status: int, code: str
) -> None:
    async with TestClient(TestServer(_watch_off_app("owner"))) as client:
        resp = await client.post(
            "/api/browser/recipe-watch-off",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        got = await resp.json()
    assert (resp.status, got["code"]) == (status, code)
    assert stored.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("raised", "status"), [(LookupError, 404), (OSError, 500)])
async def test_a_watch_off_the_store_cannot_apply_answers_why(
    monkeypatch: pytest.MonkeyPatch, raised: type[Exception], status: int
) -> None:
    def fail(_app: str, _entry: str) -> bool:
        raise raised("tunes")

    monkeypatch.setattr(br, "withdraw_watch", fail)
    async with TestClient(TestServer(_watch_off_app("owner"))) as client:
        resp = await client.post(
            "/api/browser/recipe-watch-off", json={"app": "tunes", "recipe": "x"}
        )
    assert resp.status == status
