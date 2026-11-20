"""``permissions.browserSites``: the pages an app's own agent may browse without a card.

Pinned here: what an entry may say and which URLs it admits; that an install never
approves it and the owner's approval on the app's page does; that the grant is read
fresh (disable, trust, a dropped site end it at once); that the gate row approves
only the app's own ``browser`` call on those pages; that the op route holds such a
session to the pages (a request off them refused, a landing off them blanked); and
that the owner's media route refuses the agent and every session but an app's own.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew.apps import browser_sites, execution
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    _write_installed,
    app_dir,
    disable_app,
    enable_app,
    get_app,
    install_app,
    update_app,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.point_grants import explicit_only_grant_families, point_grant_families
from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.browser_harness import media
from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.dashboard.handlers import browser_media
from kiro_crew.dashboard.handlers import browser_ops as ops_route
from kiro_crew.dashboard.routes import browser_live as route_slice
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, HookManager, HooksConfig, ToolCall

SITES = ["https://www.example.com/watch", "https://video.example.org/v/*"]

# --- what an entry may say -----------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "https://www.example.com",
        "https://www.example.com/a?b=1",
        "https://www.example.com/a#x",
        "https://www.example.com/a%2f",
        "https://www.example.com/../x",
        "https://www.example.com/a//b",
        "http://www.example.com/a",
        "https://www.example.com:8443/a",
        "https://user@example.com/a",
        "https://*.example.com/a",
        "https://localhost/a",
        "https://1.2.3.4/a",
        "https://co.uk/a",
        "https://www.example.com/" + "a" * 300,
        "www.example.com/a",
    ],
)
def test_an_entry_that_is_not_origin_plus_path_is_refused(entry: str) -> None:
    assert browser_sites.browser_site_errors([entry]), entry


def test_valid_entries_validate_round_trip_and_limits() -> None:
    assert browser_sites.browser_site_errors(SITES) == []
    seventeen = [f"https://s{i}.example.com/a" for i in range(17)]
    assert any("exceeds the limit" in e for e in browser_sites.browser_site_errors(seventeen))
    assert browser_sites.browser_site_errors([SITES[0], SITES[0]]) == [
        "permissions.browserSites repeats an entry"
    ]
    manifest = AppManifest.from_dict(
        {
            "name": "tunes",
            "version": "1.0.0",
            "displayName": "T",
            "description": "d",
            "permissions": {"browserSites": SITES},
        }
    )
    assert manifest.validate() == []
    assert manifest.to_dict()["permissions"]["browserSites"] == SITES


@pytest.mark.parametrize("value", ["https://www.example.com/a", {"a": 1}, ["https://a.com/a", 3]])
def test_manifest_refuses_a_non_list_value(value: Any) -> None:
    manifest = AppManifest.from_dict(
        {
            "name": "tunes",
            "version": "1.0.0",
            "displayName": "T",
            "description": "d",
            "permissions": {"browserSites": value},
        }
    )
    assert "permissions.browserSites must be an array of strings" in manifest.validate()


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://www.example.com/watch", True),
        ("https://www.example.com/watch?v=1#t=3", True),
        ("https://WWW.EXAMPLE.COM/watch", True),
        ("https://video.example.org/v/abc/?x=1", True),
        ("https://www.example.com/watch/more", False),
        ("https://www.example.com/account", False),
        ("https://video.example.org/v/../account", False),
        ("https://video.example.org/v/%2e%2e/account", False),
        ("https://video.example.org/v/a%2Fb", False),
        ("http://www.example.com/watch", False),
        ("https://www.example.com:444/watch", False),
        ("https://u:p@www.example.com/watch", False),
        ("https://evil.example.com/watch", False),
        ("https://www.example.com.evil.net/watch", False),
        ("about:blank", False),
        ("", False),
    ],
)
def test_url_matches_only_the_named_pages(url: str, allowed: bool) -> None:
    assert browser_sites.url_matches(SITES, url) is allowed


def test_no_sites_admit_nothing() -> None:
    assert browser_sites.url_matches((), "https://www.example.com/watch") is False


# --- the grant family ------------------------------------------------------------


@pytest.fixture
def trusted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[set[str]]:
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    names: set[str] = set()
    monkeypatch.setattr(
        execution,
        "third_party_ceiling_closed",
        lambda name: None if name in names else "untrusted",
    )
    yield names
    names.clear()


def _source(tmp_path: Path, name: str, sites: list[str], version: str = "1.0.0") -> Path:
    source = tmp_path / f"source-{version}" / name
    source.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": version,
        "displayName": name,
        "description": "browses a site",
        "permissions": {"browserSites": sites},
    }
    (source / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return source


def _install(tmp_path: Path, name: str, sites: list[str]) -> None:
    assert install_app(_source(tmp_path, name, sites)).ok
    meta = _read_installed(name)
    assert meta is not None
    meta.enabled = True
    _write_installed(name, meta)


def _approve(name: str, sites: list[str]) -> None:
    result = enable_app(name, grants_consent={"browserSites": sites})
    assert result.ok, result.error


def test_browser_sites_is_an_explicit_only_staged_family() -> None:
    assert browser_sites.GRANT_FAMILY in point_grant_families()
    assert browser_sites.GRANT_FAMILY in explicit_only_grant_families()


def test_install_approves_nothing_and_the_owner_approves_on_the_app_page(
    trusted: set[str], tmp_path: Path
) -> None:
    trusted.add("tunes")
    _install(tmp_path, "tunes", SITES)
    assert get_app("tunes")["browserSitesGrant"] == {"approved": [], "staged": SITES}
    assert browser_sites.app_browser_sites("tunes") == ()

    _approve("tunes", SITES)
    assert get_app("tunes")["browserSitesGrant"] == {"approved": SITES, "staged": []}
    assert browser_sites.app_browser_sites("tunes") == tuple(SITES)


def test_disable_trust_and_a_dropped_site_end_the_grant_at_once(
    trusted: set[str], tmp_path: Path
) -> None:
    trusted.add("tunes")
    _install(tmp_path, "tunes", SITES)
    _approve("tunes", SITES)

    trusted.discard("tunes")
    assert browser_sites.app_browser_sites("tunes") == ()
    trusted.add("tunes")
    assert browser_sites.app_browser_sites("tunes") == tuple(SITES)

    assert update_app(_source(tmp_path, "tunes", SITES[:1], "1.1.0")).ok
    assert browser_sites.app_browser_sites("tunes") == tuple(SITES[:1])

    assert disable_app("tunes").ok
    assert browser_sites.app_browser_sites("tunes") == ()


def test_an_added_or_edited_site_waits_for_the_owner(trusted: set[str], tmp_path: Path) -> None:
    trusted.add("tunes")
    _install(tmp_path, "tunes", SITES[:1])
    _approve("tunes", SITES[:1])
    manifest_path = app_dir("tunes") / APP_MANIFEST_FILENAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["permissions"]["browserSites"] = [SITES[0], "https://www.example.com/*"]
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    assert browser_sites.app_browser_sites("tunes") == (SITES[0],)
    assert get_app("tunes")["browserSitesGrant"]["staged"] == ["https://www.example.com/*"]


def test_an_invalid_live_edit_grants_nothing(trusted: set[str], tmp_path: Path) -> None:
    trusted.add("tunes")
    _install(tmp_path, "tunes", SITES)
    _approve("tunes", SITES)
    manifest_path = app_dir("tunes") / APP_MANIFEST_FILENAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["permissions"]["browserSites"] = [*SITES, "https://localhost/x"]
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    assert browser_sites.app_browser_sites("tunes") == ()


# --- the gate row ------------------------------------------------------------------


def _browser_call(op: str = "snapshot", **args: Any) -> ToolCall:
    return ToolCall(
        title="Running: @kirocrew-core/browser",
        mcp_server="kirocrew-core",
        mcp_tool="browser",
        identity_trusted=True,
        raw_params={"op": op, "args": args},
    )


@pytest.fixture
def granted(monkeypatch: pytest.MonkeyPatch) -> None:
    def holds(app: str) -> tuple[str, ...]:
        return tuple(SITES) if app == "tunes" else ()

    monkeypatch.setattr(browser_sites, "app_browser_sites", holds)
    monkeypatch.setattr(ops_route, "app_browser_sites", holds)


def _judge(
    call: ToolCall, app: str = "tunes", session_key: str = "dashboard:app-tunes-1", **config: Any
) -> Any:
    return HookManager(HooksConfig(**config)).judge(call, app=app, session_key=session_key)


@pytest.mark.usefixtures("granted")
def test_the_apps_own_browser_call_is_approved_on_its_pages() -> None:
    for call in (
        _browser_call(),
        _browser_call("click", ref="e3"),
        _browser_call("navigate", url="https://www.example.com/watch?v=1"),
        _browser_call("tabs", action="new", url="https://video.example.org/v/1"),
    ):
        verdict = _judge(call)
        assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "app-browser-sites")


@pytest.mark.usefixtures("granted")
@pytest.mark.parametrize(
    ("call", "app"),
    [
        (_browser_call(), "other-app"),
        (_browser_call(), ""),
        (_browser_call("navigate", url="https://www.example.com/account"), "tunes"),
        (_browser_call("tabs", action="new", url="https://evil.example.net/"), "tunes"),
        (
            ToolCall(
                title="Running: @kirocrew-core/browser",
                mcp_server="kirocrew-core",
                mcp_tool="browser",
                identity_trusted=False,
                raw_params={"op": "snapshot", "args": {}},
            ),
            "tunes",
        ),
        (
            ToolCall(
                title="Running: @kirocrew-core/spawn_run",
                mcp_server="kirocrew-core",
                mcp_tool="spawn_run",
                identity_trusted=True,
                raw_params={"op": "snapshot", "args": {}},
            ),
            "tunes",
        ),
        (
            ToolCall(
                title="Running: @tunes:srv/browser",
                mcp_server="tunes:srv",
                mcp_tool="browser",
                identity_trusted=True,
                raw_params={"op": "snapshot"},
            ),
            "tunes",
        ),
        (
            ToolCall(title="Running: @kirocrew-core/browser", is_shell=True, command="ls"),
            "tunes",
        ),
        (
            ToolCall(
                title="Running: @kirocrew-core/browser",
                mcp_server="kirocrew-core",
                mcp_tool="browser",
                identity_trusted=True,
                raw_params=None,
            ),
            "tunes",
        ),
    ],
)
def test_the_grant_leaks_to_no_other_caller_tool_or_page(call: ToolCall, app: str) -> None:
    assert _judge(call, app=app).tier != "app-browser-sites"


@pytest.mark.usefixtures("granted")
@pytest.mark.parametrize("session_key", ["subagent:abc123", "cron:job-1", "slack:1700.1", ""])
def test_a_session_the_op_route_cannot_confine_keeps_its_card(session_key: str) -> None:
    """A subagent or channel session carries the app but no dashboard slot the route finds."""
    assert _judge(_browser_call(), session_key=session_key).tier != "app-browser-sites"


@pytest.mark.usefixtures("granted")
def test_an_operator_confirm_pattern_and_the_read_only_mode_still_ask() -> None:
    assert _judge(_browser_call(), confirm_tools=["@kirocrew-core/browser"]).action == TOOL_ALLOW
    verdict = HookManager().judge(
        _browser_call(), app="tunes", session_key="dashboard:app-tunes-1", classifier_only=True
    )
    assert verdict.tier != "app-browser-sites"


def test_star_matching_stays_linear_on_a_page_chosen_path() -> None:
    sites = ["https://www.example.com/*/*/*/*/*/*/watch"]
    landing = "https://www.example.com/" + "a/" * 5000 + "other"
    assert browser_sites.url_matches(sites, landing) is False
    assert browser_sites.url_matches(sites, "https://www.example.com/a/b/c/d/e/f/watch")
    assert browser_sites.url_matches(
        ["https://www.example.com/a*b*c"], "https://www.example.com/axxbyyc"
    )
    assert not browser_sites.url_matches(
        ["https://www.example.com/a*b*c"], "https://www.example.com/acb"
    )
    assert not browser_sites.url_matches(
        ["https://www.example.com/ab*ba"], "https://www.example.com/aba"
    )
    assert not browser_sites.url_matches(
        ["https://www.example.com/a*b*c"], "https://www.example.com/axxc"
    )


def test_no_approved_sites_means_no_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_sites, "app_browser_sites", lambda app: ())
    assert _judge(_browser_call()).tier != "app-browser-sites"


# --- the op route holds the session to its pages -------------------------------------


@pytest.fixture(autouse=True)
def _fresh_hub(_floor_monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    hub_mod.reset_live_hub()
    _floor_monkeypatch.setattr(browser_sites, "_granted", {})
    _floor_monkeypatch.setattr(ops_route, "_sel", lambda: MagicMock())
    yield
    hub_mod.reset_live_hub()


def _op_request(body: dict[str, Any], slot_app: str) -> Any:
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={"app-tunes-1": SimpleNamespace(_app=slot_app)})
    req = make_mocked_request("POST", "/api/browser/op", app=app)
    req["internal_auth"] = True
    req._read_bytes = json.dumps(body).encode()
    return req


@pytest.mark.asyncio
@pytest.mark.usefixtures("granted")
@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("navigate", {"url": "https://www.example.com/account"}),
        ("tabs", {"action": "new", "url": "https://evil.example.net/"}),
    ],
)
async def test_an_apps_session_cannot_open_a_page_off_its_list(
    op: str, args: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_supervisor(*_a: Any) -> Any:
        raise AssertionError("an off-list request must not reach the browser")

    monkeypatch.setattr(ops_route, "_supervisor", no_supervisor)
    resp = await ops_route.api_browser_op(
        _op_request({"op": op, "args": args, "session_key": "app-tunes-1"}, "tunes")
    )
    body = json.loads(resp.body)
    assert body["ok"] is False and body["code"] == "site_not_allowed"


@pytest.mark.asyncio
@pytest.mark.usefixtures("granted")
async def test_a_persons_session_browses_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    async def none(*_a: Any) -> Any:
        return None

    monkeypatch.setattr(ops_route, "_supervisor", none)
    resp = await ops_route.api_browser_op(
        _op_request(
            {
                "op": "navigate",
                "args": {"url": "https://other.example.net/"},
                "session_key": "app-tunes-1",
            },
            "",
        )
    )
    assert json.loads(resp.body)["code"] == "not_installed"


@pytest.mark.asyncio
async def test_a_landing_off_the_list_is_blanked_and_refused() -> None:
    gotos: list[str] = []

    async def helper(name: str, **kwargs: Any) -> Any:
        if name == "current_tab":
            return {"targetId": "T1", "url": "https://www.example.com/account"}
        if name == "goto":
            gotos.append(kwargs["url"])
        return {}

    supervisor = SimpleNamespace(call=helper, relay=None)
    ctx = ops_route.build_context("app-tunes-1", supervisor, sites=tuple(SITES))
    result = await harness_ops.run_op("snapshot", {}, ctx)
    assert result["code"] == "redirect_refused"
    assert gotos == ["about:blank"]
    # The same landing in a session with no site list is the public page it is.
    assert ops_route.build_context("k", supervisor).url_allowed("https://www.example.com/account")


@pytest.mark.asyncio
async def test_session_sites_read_the_slots_app_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    holding = {"tunes": tuple(SITES)}
    monkeypatch.setattr(ops_route, "app_browser_sites", lambda app: holding.get(app, ()))
    req = _op_request({}, "tunes")
    assert await ops_route.session_sites(req, "app-tunes-1") == tuple(SITES)
    holding.clear()
    assert await ops_route.session_sites(req, "app-tunes-1") == ()
    assert await ops_route.session_sites(req, "not-a-slot") == ()


# --- the owner's media route ---------------------------------------------------------------


def _media_app(identity: str, slot_app: str = "tunes") -> web.Application:
    """The route slice behind a stand-in for ``token_auth`` (as ``test_browser_live_routes``)."""

    @web.middleware
    async def as_caller(request: web.Request, handler: Any) -> Any:
        if identity == "internal":
            request["internal_auth"] = True
        if identity == "app":
            request["user"], request["app"] = "tunes", "tunes"
        else:
            request["user"], request["app"] = "local-app", ""
        return await handler(request)

    app = web.Application(middlewares=[as_caller])
    app["state"] = SimpleNamespace(
        owner_id="",
        _slots={"app-tunes-1": SimpleNamespace(_app=slot_app)},
        deliver_ws_owners=lambda *_a, **_k: None,
        deliver_ws_owners_bytes=lambda *_a, **_k: None,
    )
    route_slice.register(app)
    return app


async def _media(identity: str, body: dict[str, Any]) -> tuple[int, str]:
    async with TestClient(TestServer(_media_app(identity))) as client:
        resp = await client.post("/api/browser/media", json=body)
        return resp.status, str((await resp.json()).get("code") or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "code"), [("internal", "internal_secret_required"), ("app", "owner_only")]
)
async def test_the_media_route_refuses_the_agent_and_an_app_token(identity: str, code: str) -> None:
    status, got = await _media(identity, {"session_key": "app-tunes-1", "action": "pause"})
    assert (status, got) == (403, code)


@pytest.mark.asyncio
async def test_the_media_route_drives_only_an_apps_own_browsing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ops_route, "app_browser_sites", lambda app: ())
    pause = {"session_key": "app-tunes-1", "action": "pause"}
    assert await _media("owner", pause) == (403, "not_app_browser")

    monkeypatch.setattr(ops_route, "app_browser_sites", lambda app: tuple(SITES))
    for body, code in (
        ({"session_key": "app-tunes-1", "action": "click"}, "bad_action"),
        ({"session_key": "app-tunes-1", "action": "volume", "value": 2}, "bad_value"),
        ({"session_key": "app-tunes-1", "action": "seek", "value": True}, "bad_value"),
    ):
        assert await _media("owner", body) == (400, code)
    assert await _media("owner", {"session_key": "chat-9", "action": "pause"}) == (
        404,
        "unknown_session",
    )
    monkeypatch.setattr(browser_media, "_running_supervisor", lambda: None)
    assert await _media("owner", pause) == (409, "no_tab")


@pytest.mark.asyncio
async def test_the_media_route_leaves_a_taken_over_tab_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ops_route, "app_browser_sites", lambda app: tuple(SITES))
    monkeypatch.setattr(browser_media, "_running_supervisor", lambda: SimpleNamespace())
    hub = hub_mod.get_live_hub()
    hub.bind_target("app-tunes-1", "T1")
    monkeypatch.setattr(hub, "in_control", lambda key: key == "app-tunes-1")

    async def must_not_run(*_a: Any) -> Any:
        raise AssertionError("the tab is the person's")

    monkeypatch.setattr(browser_media, "_run", must_not_run)
    assert await _media("owner", {"session_key": "app-tunes-1", "action": "play"}) == (
        409,
        "user_in_control",
    )


class _MediaTab:
    """A tab whose <video> refuses ``play()`` until the tab has had one click."""

    def __init__(self, url: str = "https://www.example.com/watch") -> None:
        self.url = url
        self.paused = True
        self.clicked = 0
        self.expressions: list[str] = []
        self.helpers: list[str] = []

    async def helper(self, name: str, **kwargs: Any) -> Any:
        self.helpers.append(name)
        if name == "current_tab":
            return {"targetId": "T1", "url": self.url}
        if name == "click":
            self.clicked += 1
            return {}
        if name == "goto":
            self.url = kwargs["url"]
            return {}
        if name == "switch_tab":
            return {}
        if name == "js_isolated":
            expr = kwargs["expression"]
            self.expressions.append(expr.rsplit(".media(", 1)[-1])
            if '.media("play"' in expr and self.clicked:
                self.paused = False
            if '.media("pause"' in expr:
                self.paused = True
            return {"ok": True, "paused": self.paused, "center": [10, 20]}
        raise AssertionError(f"unexpected helper {name}")


def _ctx(tab: _MediaTab) -> harness_ops.OpContext:
    async def no_sleep(_s: float) -> None:
        return None

    ctx = ops_route.build_context(
        "app-tunes-1", SimpleNamespace(call=tab.helper, relay=None), sites=tuple(SITES)
    )
    ctx.sleep = no_sleep
    return ctx


@pytest.mark.asyncio
async def test_a_refused_play_clicks_the_element_once_then_plays() -> None:
    tab = _MediaTab()
    result = await media.control(_ctx(tab), "play")
    assert result["paused"] is False and tab.clicked == 1
    # A play that starts on its own clicks nothing.
    await media.control(_ctx(tab), "pause")
    result = await media.control(_ctx(tab), "play")
    assert result["paused"] is False and tab.clicked == 1


@pytest.mark.asyncio
async def test_a_play_that_starts_a_moment_later_is_not_clicked() -> None:
    tab = _MediaTab()
    tab.clicked = 1  # the browser lets this tab play; its element just starts late
    calls = 0
    real = tab.helper

    async def late(name: str, **kwargs: Any) -> Any:
        nonlocal calls
        answer = await real(name, **kwargs)
        if name == "js_isolated":
            calls += 1
            if calls == 1:
                # play() was called; the element reports paused until it starts.
                return {**answer, "paused": True}
        return answer

    tab.helper = late  # type: ignore[method-assign]
    result = await media.control(_ctx(tab), "play")
    assert result["paused"] is False and tab.clicked == 1


@pytest.mark.asyncio
async def test_a_tab_that_moved_to_another_site_is_unlocked_again() -> None:
    tab = _MediaTab()
    await media.control(_ctx(tab), "play")
    tab.url, tab.clicked, tab.paused = "https://video.example.org/v/2", 0, True
    result = await media.control(_ctx(tab), "play")
    assert tab.clicked == 1 and result["paused"] is False


@pytest.mark.asyncio
async def test_a_state_poll_writes_no_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[str] = []
    monkeypatch.setattr(ops_route, "app_browser_sites", lambda app: tuple(SITES))
    monkeypatch.setattr(browser_media, "_running_supervisor", lambda: SimpleNamespace())
    monkeypatch.setattr(
        browser_media.browser_live,
        "_audit",
        lambda _r, *, operation, outcome, error="": rows.append(operation),
    )
    hub_mod.get_live_hub().bind_target("app-tunes-1", "T1")

    async def ran(*_a: Any) -> Any:
        return {"ok": True, "paused": True}

    monkeypatch.setattr(browser_media, "_run", ran)
    async with TestClient(TestServer(_media_app("owner"))) as client:
        # The state read a player polls is a GET, which the mutating-call audit
        # middleware never records, and the handler writes no row for it either.
        resp = await client.get("/api/browser/media", params={"session_key": "app-tunes-1"})
        assert resp.status == 200 and (await resp.json())["ok"] is True
        assert rows == []
        # A POST is an action: ``state`` is refused there, so no poll goes that way.
        resp = await client.post(
            "/api/browser/media",
            json={"session_key": "app-tunes-1", "action": "state"},
        )
        assert resp.status == 400 and (await resp.json())["code"] == "bad_action"
        resp = await client.get("/api/browser/media", params={"session_key": "chat-9"})
        assert resp.status == 404
    assert (await _media("owner", {"session_key": "app-tunes-1", "action": "pause"}))[0] == 200
    assert rows == ["browser_media"]


@pytest.mark.asyncio
async def test_media_on_a_page_off_the_list_is_blanked_and_refused() -> None:
    tab = _MediaTab("https://www.example.com/account")
    result = await media.control(_ctx(tab), "pause")
    assert result["code"] == "redirect_refused"
    assert tab.url == "about:blank"
    assert "js_isolated" not in tab.helpers


@pytest.mark.asyncio
async def test_media_reads_only_the_walkers_media_method() -> None:
    tab = _MediaTab()
    await media.control(_ctx(tab), "volume", 0.5)
    assert tab.expressions == ['"volume", 0.5)']


def _media_supervisor(tab: _MediaTab, ledger: list[str]) -> SimpleNamespace:
    return SimpleNamespace(call=tab.helper, relay=SimpleNamespace(agent_targets=lambda: ledger))


@pytest.mark.asyncio
async def test_the_media_route_runs_on_the_sessions_own_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tab = _MediaTab()
    tab.clicked = 1  # this tab may play
    monkeypatch.setattr(ops_route, "app_browser_sites", lambda app: tuple(SITES))
    monkeypatch.setattr(
        browser_media, "_running_supervisor", lambda: _media_supervisor(tab, ["T7"])
    )
    hub_mod.get_live_hub().bind_target("app-tunes-1", "T7")
    async with TestClient(TestServer(_media_app("owner"))) as client:
        resp = await client.post(
            "/api/browser/media",
            json={
                "session_key": "app-tunes-1",
                "action": "seek",
                "value": 42,
            },
        )
        body = await resp.json()
    assert resp.status == 200 and body["ok"] is True
    assert body["media"]["paused"] is True and "center" not in body["media"]
    # The daemon was on another tab: it was pointed at this session's, then the media ran.
    assert tab.helpers[:2] == ["current_tab", "switch_tab"]
    assert tab.expressions[-1] == '"seek", 42.0)'


@pytest.mark.asyncio
async def test_the_media_route_refuses_a_tab_the_browser_no_longer_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tab = _MediaTab()
    monkeypatch.setattr(ops_route, "app_browser_sites", lambda app: tuple(SITES))
    monkeypatch.setattr(browser_media, "_running_supervisor", lambda: _media_supervisor(tab, []))
    hub_mod.get_live_hub().bind_target("app-tunes-1", "T7")
    async with TestClient(TestServer(_media_app("owner"))) as client:
        resp = await client.post(
            "/api/browser/media",
            json={"session_key": "app-tunes-1", "action": "pause"},
        )
        body = await resp.json()
    assert body["code"] == "no_tab" and tab.helpers == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "code"), [(RuntimeError("daemon"), "browser_error"), (TimeoutError(), "timeout")]
)
async def test_a_failing_media_action_answers_a_code(
    raised: BaseException, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops_route, "app_browser_sites", lambda app: tuple(SITES))
    monkeypatch.setattr(browser_media, "_running_supervisor", lambda: SimpleNamespace())
    hub_mod.get_live_hub().bind_target("app-tunes-1", "T1")

    async def fails(*_a: Any) -> Any:
        raise raised

    monkeypatch.setattr(browser_media, "_run", fails)
    async with TestClient(TestServer(_media_app("owner"))) as client:
        resp = await client.post(
            "/api/browser/media",
            json={"session_key": "app-tunes-1", "action": "pause"},
        )
        body = await resp.json()
    assert resp.status == 200 and body["ok"] is False and body["code"] == code


@pytest.mark.parametrize(
    ("action", "raw", "ok"),
    [
        ("seek", 0, True),
        ("seek", 7 * 24 * 3600 + 1, False),
        ("seek", -1, False),
        ("seek", float("nan"), False),
        ("seek", 10**309, False),
        ("volume", 1, True),
        ("volume", "0.5", False),
        ("play", "anything", True),
    ],
)
def test_media_values_are_bounded(action: str, raw: Any, ok: bool) -> None:
    assert browser_media._value(action, raw)[0] is ok


def test_the_media_route_never_starts_a_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.browser_harness import supervisor as sup

    monkeypatch.setattr(browser_media.browser_live, "_current_relay", lambda: None)
    assert browser_media._running_supervisor() is None
    running = SimpleNamespace()
    monkeypatch.setattr(browser_media.browser_live, "_current_relay", lambda: object())
    monkeypatch.setattr(sup, "get_supervisor", lambda home: running)
    assert browser_media._running_supervisor() is running


@pytest.mark.usefixtures("granted")
def test_a_granted_call_marks_its_slot_and_a_refused_one_does_not() -> None:
    _judge(_browser_call("navigate", url="https://www.example.com/account"))
    assert not browser_sites.session_was_granted("app-tunes-1")
    _judge(_browser_call(), app="other-app")
    assert not browser_sites.session_was_granted("app-tunes-1")
    assert _judge(_browser_call()).tier == "app-browser-sites"
    assert browser_sites.session_was_granted("app-tunes-1")


@pytest.mark.asyncio
async def test_a_slot_whose_grant_vanished_after_approval_stays_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate approved on the grant; the app then dropped its sites before the op ran."""
    browser_sites.note_granted_session("app-tunes-1")
    monkeypatch.setattr(ops_route, "app_browser_sites", lambda app: ())

    async def no_supervisor(*_a: Any) -> Any:
        raise AssertionError("a held slot with no pages must not reach the browser")

    monkeypatch.setattr(ops_route, "_supervisor", no_supervisor)
    resp = await ops_route.api_browser_op(
        _op_request(
            {
                "op": "navigate",
                "args": {"url": "https://www.example.com/watch"},
                "session_key": "dashboard:app-tunes-1",
            },
            "tunes",
        )
    )
    assert json.loads(resp.body)["code"] == "site_not_allowed"
    req = _op_request({}, "tunes")
    sites = await ops_route.session_sites(req, "app-tunes-1")
    assert sites == browser_sites.NO_PAGES
    assert not ops_route.build_context(
        "k", SimpleNamespace(call=None, relay=None), sites=sites
    ).url_allowed("https://www.example.com/watch")
    # The media buttons treat it as no browsing session at all.
    assert await _media("owner", {"session_key": "app-tunes-1", "action": "pause"}) == (
        403,
        "not_app_browser",
    )


@pytest.mark.asyncio
async def test_the_unlock_click_never_lands_on_a_page_that_moved_off_the_list() -> None:
    tab = _MediaTab()
    real = tab.helper
    reads = 0

    async def moves(name: str, **kwargs: Any) -> Any:
        nonlocal reads
        answer = await real(name, **kwargs)
        if name == "js_isolated":
            reads += 1
            if reads == 2:
                # The settle read answered; the page then redirected off the list.
                tab.url = "https://www.example.com/account"
        return answer

    tab.helper = moves  # type: ignore[method-assign]
    result = await media.control(_ctx(tab), "play")
    assert result["code"] == "redirect_refused"
    assert tab.clicked == 0 and tab.url == "about:blank"


@pytest.mark.asyncio
async def test_a_browse_denied_media_action_is_audited_and_a_poll_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[bool] = []

    async def denied(*, audit: bool = True, **_kw: Any) -> web.Response:
        asked.append(audit)
        return web.json_response({"code": "policy_denied"}, status=403)

    monkeypatch.setattr(browser_media, "browse_refusal", denied)
    async with TestClient(TestServer(_media_app("owner"))) as client:
        post = await client.post(
            "/api/browser/media",
            json={"session_key": "app-tunes-1", "action": "pause"},
        )
        get = await client.get("/api/browser/media", params={"session_key": "app-tunes-1"})
    assert post.status == get.status == 403
    assert asked == [True, False]


@pytest.mark.asyncio
async def test_a_held_slot_says_its_sites_were_withdrawn_and_how_to_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_sites.note_granted_session("app-tunes-1")
    monkeypatch.setattr(ops_route, "app_browser_sites", lambda app: ())
    resp = await ops_route.api_browser_op(
        _op_request(
            {
                "op": "navigate",
                "args": {"url": "https://www.example.com/watch"},
                "session_key": "dashboard:app-tunes-1",
            },
            "tunes",
        )
    )
    body = json.loads(resp.body)
    assert body["code"] == "site_not_allowed"
    assert body["error"] == ops_route.SITES_REVOKED_TEXT
    assert "approve its sites again" in body["error"]
    async with TestClient(TestServer(_media_app("owner"))) as client:
        media_resp = await client.post(
            "/api/browser/media",
            json={"session_key": "app-tunes-1", "action": "pause"},
        )
        media_body = await media_resp.json()
    assert media_resp.status == 403 and media_body["error"] == ops_route.SITES_REVOKED_TEXT


@pytest.mark.asyncio
async def test_a_held_slots_blanked_page_names_the_withdrawal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_sites.note_granted_session("app-tunes-1")
    monkeypatch.setattr(ops_route, "app_browser_sites", lambda app: ())
    monkeypatch.setattr(ops_route, "_supervisor", lambda *_a: _async_value(SimpleNamespace()))

    async def fenced(*_a: Any, **_kw: Any) -> Any:
        return harness_ops.refusal("redirect_refused", "the page redirected")

    monkeypatch.setattr(ops_route, "_fenced_op", fenced)
    resp = await ops_route.api_browser_op(
        _op_request({"op": "snapshot", "args": {}, "session_key": "dashboard:app-tunes-1"}, "tunes")
    )
    body = json.loads(resp.body)
    assert body["code"] == "redirect_refused" and body["error"] == ops_route.SITES_REVOKED_TEXT


async def _async_value(value: Any) -> Any:
    return value
