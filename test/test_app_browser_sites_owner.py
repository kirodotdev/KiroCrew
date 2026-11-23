"""The owner's own edits of the pages an app's agent may browse (``browser_sites.site_change``).

Pinned here: an added site is checked by the rule a declared entry meets and is in
force at once; a removed or turned-off site leaves force at once, a declared one stays
removed across an update, and the op route refuses it at the next op; the list says
where each site came from and where it stands; an uninstall forgets the edits; and the
edit route answers only the owner's dashboard, never an app token, an internal (agent)
caller or another dashboard session, while the read route also answers the app.
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

from kiro_crew.apps import browser_sites, browser_sites_routes, execution, protected_app_state
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    _write_installed,
    enable_app,
    get_app,
    install_app,
    uninstall_app,
    update_app,
)
from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.dashboard.handlers import browser_ops as ops_route
from kiro_crew.dashboard.handlers import source_providers

SITES = ["https://www.example.com/watch", "https://video.example.org/v/*"]
ADDED = "https://music.example.net/song/*"


@pytest.fixture
def trusted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[set[str]]:
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    names: set[str] = {"tunes"}
    monkeypatch.setattr(
        execution,
        "third_party_ceiling_closed",
        lambda name: None if name in names else "untrusted",
    )
    yield names
    names.clear()


def _source(tmp_path: Path, sites: list[str], version: str = "1.0.0") -> Path:
    source = tmp_path / f"source-{version}" / "tunes"
    source.mkdir(parents=True)
    manifest = {
        "name": "tunes",
        "version": version,
        "displayName": "tunes",
        "description": "browses a site",
        "permissions": {"browserSites": sites},
    }
    (source / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return source


def _install(tmp_path: Path, sites: list[str], approve: bool = True) -> None:
    assert install_app(_source(tmp_path, sites)).ok
    meta = _read_installed("tunes")
    assert meta is not None
    meta.enabled = True
    _write_installed("tunes", meta)
    if approve:
        assert enable_app("tunes", grants_consent={"browserSites": sites}).ok


def _owner_added() -> list[str]:
    meta = _read_installed("tunes")
    return protected_app_state.read_owner_browser_sites("tunes", meta.installedAt if meta else "")[
        "added"
    ]


def _rows() -> dict[str, tuple[str, str]]:
    rows = browser_sites.app_site_rows("tunes")
    assert rows is not None
    return {r["site"]: (r["source"], r["status"]) for r in rows}


# --- what the owner's edits do --------------------------------------------------------


def test_an_added_site_is_in_force_at_once_and_listed_as_the_owners(
    trusted: set[str], tmp_path: Path
) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "add", ADDED)
    assert browser_sites.app_browser_sites("tunes") == (*SITES, ADDED)
    assert _rows()[ADDED] == ("owner", "approved")
    assert _rows()[SITES[0]] == ("app", "approved")
    assert browser_sites.url_matches(browser_sites.app_browser_sites("tunes"), ADDED[:-1] + "1")


def test_an_added_site_works_for_an_app_that_declares_none(
    trusted: set[str], tmp_path: Path
) -> None:
    _install(tmp_path, [], approve=False)
    browser_sites.site_change("tunes", "add", ADDED)
    assert browser_sites.app_browser_sites("tunes") == (ADDED,)


@pytest.mark.parametrize(
    "site",
    [
        "*",
        "https://*",
        "https://*/*",
        "https://*.example.com/*",
        "https://com/*",
        "https://co.uk/*",
        "https://localhost/*",
        "https://app.localhost/*",
        "https://10.0.0.1/*",
        "https://192.168.1.10/a",
        "https://[::1]/a",
        "http://music.example.net/*",
        "https://music.example.net",
        "https://music.example.net:8443/*",
        "https://user@music.example.net/*",
        "https://music.example.net/a?x=1",
        "https://music.example.net/../x",
        "javascript:alert(1)",
    ],
)
def test_a_too_wide_or_internal_site_is_refused(
    site: str, trusted: set[str], tmp_path: Path
) -> None:
    _install(tmp_path, SITES)
    with pytest.raises(browser_sites.SiteChangeError) as refused:
        browser_sites.site_change("tunes", "add", site)
    assert refused.value.code == "bad_site"
    assert browser_sites.app_browser_sites("tunes") == tuple(SITES)


def test_turning_a_site_off_ends_it_at_once_and_add_turns_it_back_on(
    trusted: set[str], tmp_path: Path
) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "disable", SITES[0])
    assert browser_sites.app_browser_sites("tunes") == (SITES[1],)
    assert _rows()[SITES[0]] == ("app", "disabled")
    # Not offered for approval again on the detail page either.
    assert get_app("tunes")["browserSitesGrant"]["staged"] == []
    browser_sites.site_change("tunes", "add", SITES[0])
    assert set(browser_sites.app_browser_sites("tunes")) == set(SITES)


def test_a_removed_declared_site_stays_removed_across_an_update(
    trusted: set[str], tmp_path: Path
) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "remove", SITES[0])
    assert browser_sites.app_browser_sites("tunes") == (SITES[1],)
    assert _rows()[SITES[0]] == ("app", "removed")
    assert update_app(_source(tmp_path, SITES, "1.1.0")).ok
    assert browser_sites.app_browser_sites("tunes") == (SITES[1],)
    assert get_app("tunes")["browserSitesGrant"]["staged"] == []


def test_a_removed_declared_site_is_listed_and_only_add_brings_it_back(
    trusted: set[str], tmp_path: Path
) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "remove", SITES[0])
    rows = browser_sites.app_site_rows("tunes")
    assert rows is not None
    assert [r["site"] for r in rows] == SITES
    assert rows[0] == {"site": SITES[0], "source": "app", "status": "removed"}
    for action in ("remove", "disable"):
        with pytest.raises(browser_sites.SiteChangeError) as refused:
            browser_sites.site_change("tunes", action, SITES[0])
        assert refused.value.code == "not_listed"
    assert browser_sites.app_browser_sites("tunes") == (SITES[1],)
    browser_sites.site_change("tunes", "add", SITES[0])
    assert _rows()[SITES[0]] == ("app", "approved")
    assert set(browser_sites.app_browser_sites("tunes")) == set(SITES)


def test_removing_an_added_site_ends_it(trusted: set[str], tmp_path: Path) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "add", ADDED)
    browser_sites.site_change("tunes", "remove", ADDED)
    assert browser_sites.app_browser_sites("tunes") == tuple(SITES)
    assert ADDED not in _rows()


def test_a_pending_declared_site_is_listed_and_add_approves_it(
    trusted: set[str], tmp_path: Path
) -> None:
    _install(tmp_path, SITES, approve=False)
    assert _rows() == {s: ("app", "pending") for s in SITES}
    browser_sites.site_change("tunes", "add", SITES[1])
    assert browser_sites.app_browser_sites("tunes") == (SITES[1],)
    assert _rows()[SITES[1]] == ("app", "approved")


@pytest.mark.parametrize("action", ["remove", "disable"])
def test_an_unlisted_site_cannot_be_removed_or_turned_off(
    action: str, trusted: set[str], tmp_path: Path
) -> None:
    _install(tmp_path, SITES)
    with pytest.raises(browser_sites.SiteChangeError) as refused:
        browser_sites.site_change("tunes", action, ADDED)
    assert refused.value.code == "not_listed"


def test_the_owner_may_add_at_most_the_cap(trusted: set[str], tmp_path: Path) -> None:
    _install(tmp_path, SITES)
    for i in range(browser_sites.MAX_OWNER_SITES):
        browser_sites.site_change("tunes", "add", f"https://s{i}.example.net/a")
    with pytest.raises(browser_sites.SiteChangeError) as refused:
        browser_sites.site_change("tunes", "add", ADDED)
    assert refused.value.code == "too_many"


def test_a_disabled_or_untrusted_app_gets_none_of_the_added_sites(
    trusted: set[str], tmp_path: Path
) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "add", ADDED)
    trusted.discard("tunes")
    assert browser_sites.app_browser_sites("tunes") == ()


def test_an_unreadable_record_grants_nothing(trusted: set[str], tmp_path: Path) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "disable", SITES[0])
    path = protected_app_state._owner_sites_path("tunes")
    path.write_text("[not an object]", encoding="utf-8")
    assert browser_sites.app_browser_sites("tunes") == ()


def test_an_install_record_that_vanishes_for_one_read_grants_nothing(
    trusted: set[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "disable", SITES[0])
    calls: list[str] = []

    def flickering(name: str) -> Any:
        # The app's sandbox moved installed.json aside for the first read only.
        calls.append(name)
        return None if len(calls) == 1 else _read_installed(name)

    monkeypatch.setattr("kiro_crew.apps.manager._read_installed", flickering)
    assert browser_sites.app_browser_sites("tunes") == ()
    assert len(calls) == 1, "the owner record's read is the one that saw no install"
    # Once the record reads back, the owner's turn-off still holds.
    assert browser_sites.app_browser_sites("tunes") == (SITES[1],)


def test_an_uninstall_forgets_the_owners_edits(trusted: set[str], tmp_path: Path) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "add", ADDED)
    assert uninstall_app("tunes").ok
    assert _owner_added() == []


def test_a_record_left_by_a_failed_uninstall_is_cleared_by_the_next_install(
    trusted: set[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "add", ADDED)
    path = protected_app_state._owner_sites_path("tunes")
    with monkeypatch.context() as m:
        # The uninstall's cleanup fails, so the record stays behind.
        m.setattr(protected_app_state, "forget_owner_browser_sites", lambda name: None)
        assert uninstall_app("tunes").ok
    assert path.is_file()
    _install(tmp_path / "again", [], approve=False)
    assert not path.is_file()
    assert browser_sites.app_browser_sites("tunes") == ()


def test_a_record_for_another_install_drops_added_and_keeps_disabled(
    trusted: set[str], tmp_path: Path
) -> None:
    _install(tmp_path, SITES)
    path = protected_app_state._owner_sites_path("tunes")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"installedAt": "2000-01-01T00:00:00Z", "added": [ADDED], "disabled": [SITES[0]]}
        ),
        encoding="utf-8",
    )
    assert browser_sites.app_browser_sites("tunes") == (SITES[1],)
    assert ADDED not in _rows()
    assert _rows()[SITES[0]] == ("app", "disabled")


def test_a_rewritten_installed_at_keeps_the_owners_restrictions(
    trusted: set[str], tmp_path: Path
) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "add", ADDED)
    browser_sites.site_change("tunes", "disable", SITES[0])
    browser_sites.site_change("tunes", "remove", SITES[1])
    assert browser_sites.app_browser_sites("tunes") == (ADDED,)
    # The app's sandboxed code rewrites its own install record.
    meta = _read_installed("tunes")
    assert meta is not None
    meta.installedAt = "2099-01-01T00:00:00Z"
    _write_installed("tunes", meta)
    assert browser_sites.app_browser_sites("tunes") == ()
    rows = _rows()
    assert rows[SITES[0]] == ("app", "disabled")
    assert rows[SITES[1]] == ("app", "removed")
    assert ADDED not in rows


def test_the_record_lives_in_the_gateway_only_store(trusted: set[str], tmp_path: Path) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "add", ADDED)
    path = protected_app_state._owner_sites_path("tunes")
    assert path.parent.parent.name == "member-records"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["added"] == [ADDED]
    assert stored["installedAt"] == _read_installed("tunes").installedAt


# --- the op route sees the edit at the next op ----------------------------------------


@pytest.fixture
def _fresh_hub(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    hub_mod.reset_live_hub()
    monkeypatch.setattr(browser_sites, "_granted", {})
    monkeypatch.setattr(ops_route, "_sel", lambda: MagicMock())
    yield
    hub_mod.reset_live_hub()


def _op_request(body: dict[str, Any]) -> Any:
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={"app-tunes-1": SimpleNamespace(_app="tunes")})
    req = make_mocked_request("POST", "/api/browser/op", app=app)
    req["internal_auth"] = True
    req._read_bytes = json.dumps(body).encode()
    return req


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fresh_hub")
async def test_a_turned_off_site_is_refused_at_the_next_op_and_the_refusal_names_the_list(
    trusted: set[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "add", ADDED)

    async def no_supervisor(*_a: Any) -> Any:
        raise AssertionError("an off-list request must not reach the browser")

    monkeypatch.setattr(ops_route, "_supervisor", no_supervisor)
    browser_sites.site_change("tunes", "disable", ADDED)
    resp = await ops_route.api_browser_op(
        _op_request(
            {
                "op": "navigate",
                "args": {"url": "https://music.example.net/song/1"},
                "session_key": "app-tunes-1",
            }
        )
    )
    body = json.loads(resp.body)
    assert body["code"] == "site_not_allowed"
    # The agent is told where it may look instead.
    assert SITES[0] in body["error"] and ADDED not in body["error"]


# --- who may edit ---------------------------------------------------------------------


def _routes_app(identity: str) -> web.Application:
    """The routes behind a stand-in for ``token_auth`` that sets one caller identity."""

    @web.middleware
    async def auth(request: web.Request, handler: Any) -> web.StreamResponse:
        if identity == "app":
            request["app"] = "tunes"
        elif identity == "internal":
            request["internal_auth"] = True
        elif identity == "owner":
            request["owner_for_test"] = True
        return await handler(request)

    app = web.Application(middlewares=[auth])
    browser_sites_routes.register_browser_sites_routes(app)
    return app


@pytest.fixture
def owner_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        source_providers,
        "is_owner_dashboard_request",
        lambda request: request.get("owner_for_test") is True,
    )


async def _post(identity: str, body: Any) -> tuple[int, dict[str, Any]]:
    async with TestClient(TestServer(_routes_app(identity))) as client:
        resp = await client.post("/api/apps/tunes/browser-sites", json=body)
        return resp.status, await resp.json()


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_gate")
@pytest.mark.parametrize(
    ("identity", "code"),
    [
        ("app", "app_token_forbidden"),
        ("internal", "dashboard_session_required"),
        ("other-dashboard", "owner_only"),
    ],
)
async def test_only_the_owners_dashboard_may_edit(
    identity: str, code: str, trusted: set[str], tmp_path: Path
) -> None:
    _install(tmp_path, SITES)
    for action, site in (("add", ADDED), ("remove", SITES[0]), ("disable", SITES[1])):
        status, body = await _post(identity, {"action": action, "site": site})
        assert (status, body.get("code")) == (403, code)
    assert browser_sites.app_browser_sites("tunes") == tuple(SITES)
    assert _owner_added() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_gate")
async def test_the_owner_edits_and_gets_the_new_list(trusted: set[str], tmp_path: Path) -> None:
    _install(tmp_path, SITES)
    status, body = await _post("owner", {"action": "add", "site": ADDED})
    assert status == 200
    assert body["active"] == [*SITES, ADDED]
    assert {"site": ADDED, "source": "owner", "status": "approved"} in body["sites"]
    status, body = await _post("owner", {"action": "add", "site": "https://*/*"})
    assert (status, body["code"]) == (400, "bad_site")
    status, body = await _post("owner", {"action": "grant", "site": ADDED})
    assert (status, body["code"]) == (400, "bad_action")
    status, body = await _post("owner", ["add", ADDED])
    assert (status, body["code"]) == (400, "bad_request")


@pytest.mark.asyncio
@pytest.mark.usefixtures("owner_gate")
@pytest.mark.parametrize("identity", ["app", "owner"])
async def test_the_app_and_the_owner_read_the_list(
    identity: str, trusted: set[str], tmp_path: Path
) -> None:
    _install(tmp_path, SITES)
    browser_sites.site_change("tunes", "disable", SITES[1])
    async with TestClient(TestServer(_routes_app(identity))) as client:
        resp = await client.get("/api/apps/tunes/browser-sites")
        body = await resp.json()
    assert resp.status == 200
    assert body["active"] == [SITES[0]]
    assert {"site": SITES[1], "source": "app", "status": "disabled"} in body["sites"]


def test_the_segment_is_reserved_from_apps() -> None:
    from kiro_crew.apps.manifest import CORE_APP_ROUTE_SEGMENTS

    assert "browser-sites" in CORE_APP_ROUTE_SEGMENTS
