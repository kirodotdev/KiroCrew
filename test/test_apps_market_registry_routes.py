"""Kiro Space stores as App Store sources: the routes, the factory and the rows.

A store provider is what a build sets at ``app["market_registries"]``; the
open-source build sets none. These tests drive the routes with an in-memory
provider whose client answers canned ``ListingView`` dicts, and the real
``HttpMarketClient`` over an ``httpx.MockTransport`` for the CBOR round trip.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import _make_app_source, _setup_env

import kiro_crew.apps.market_client as market_client_mod
import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps import market_registries as mr
from kiro_crew.apps.market_client import HttpMarketClient, cbor_decode, cbor_encode
from kiro_crew.apps.marketplace_install import MarketInstallError
from kiro_crew.testing.wait import async_wait_until

STORE = "team-store"
OTHER = "other-store"
REGISTRIES = "/api/apps/market/registries"
LISTINGS = "/api/apps/market/listings"
LISTING = "/api/apps/market/listing"
INSTALL = "/api/apps/market/install"
#: Turns a hang into a failure that names itself; the awaited signal decides pass
#: or fail, never this ceiling (test_wait_discipline_ratchet.py).
_HANG_CEILING_S = 60.0


def _view(ref: str = "acme/board", **extra: Any) -> dict[str, Any]:
    ns, _, name = ref.partition("/")
    view = {
        "listingId": f"id-{name}",
        "ref": ref,
        "kind": "app",
        "status": "LISTED",
        "displayName": name.title(),
        "summary": f"{name} summary",
        "category": "productivity",
        "tags": ["a", 7],
        "namespace": ns,
        "name": name,
        "owner": {"kind": "person", "display": ns},
        "latestVersion": 2,
        "latestSemver": "1.1.0",
        "downloads": 0,
        "ratingCount": 1,
        "trust": "COMMUNITY",
        "canPublish": False,
    }
    view.update(extra)
    return view


class FakeReader:
    def __init__(self, pages: list[list[dict[str, Any]]] | None = None) -> None:
        self.pages = pages if pages is not None else [[_view()]]
        self.list_calls = 0
        self.fail: MarketInstallError | None = None

    def list_listings(self, *, kind: str = "app", cursor: str | None = None, limit: int = 100):
        assert kind == "app" and limit == mr.PAGE_LIMIT
        if self.fail is not None:
            raise self.fail
        index = int(cursor or 0)
        self.list_calls += 1
        nxt = str(index + 1) if index + 1 < len(self.pages) else None
        return self.pages[index], nxt

    def get_listing(self, ref: str) -> dict[str, Any]:
        if self.fail is not None:
            raise self.fail
        return _view(ref, downloads=5)


class FakeProvider:
    def __init__(self, offers: list[dict[str, str]] | None = None) -> None:
        self._offers = (
            offers
            if offers is not None
            else [
                {"id": STORE, "label": "Team store", "web": "https://store.example"},
                {"id": OTHER, "label": "Other store"},
            ]
        )
        self.state: dict[str, Any] = {"state": "signed_in", "account": "alice"}
        self.reader = FakeReader()
        self.signed: list[tuple[str, str]] = []

    def offers(self) -> list[dict[str, str]]:
        return self._offers

    def status(self, registry_id: str) -> dict[str, Any]:
        return dict(self.state)

    def client(self, registry_id: str) -> Any:
        if self.state["state"] != "signed_in":
            raise MarketInstallError("market_signed_out", "sign in first")
        return self.reader

    def sign_in(self, registry_id: str) -> dict[str, Any]:
        self.signed.append(("in", registry_id))
        return {
            "state": "pending",
            "code": "ABCD",
            "approvalPage": "https://store.example/approve",
            "token": "never-forwarded",
        }

    def sign_out(self, registry_id: str) -> dict[str, Any]:
        self.signed.append(("out", registry_id))
        return {"state": "signed_out"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _setup_env(tmp_path, monkeypatch)


def _write_added(home: Path, ids: list[str]) -> None:
    cfg = json.loads((home / "config.json").read_text())
    cfg["market_registries"] = ids
    (home / "config.json").write_text(json.dumps(cfg))


def _make(
    provider: Any = None, *, user: str | None = "owner", app_token: str = ""
) -> web.Application:
    from types import SimpleNamespace

    @web.middleware
    async def _identity(request: web.Request, handler: Any) -> web.StreamResponse:
        request["app"] = app_token
        if user is not None:
            request["user"] = user
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = SimpleNamespace(owner_id="owner", broadcast_ws=lambda *a, **k: None)
    # Routes first, provider after: the order the gateway runs them in.
    routes_mod.register_app_routes(app)
    if provider is not None:
        app[mr.MARKET_REGISTRIES_KEY] = provider
    return app


# ---------------------------------------------------------------------------
# No provider: every surface is absent, nothing opens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_provider_the_read_is_empty_and_the_rest_answer_503(home: Path) -> None:
    _write_added(home, [STORE])  # a hand-edited id changes nothing without a provider
    app = _make()
    async with TestClient(TestServer(app)) as http:
        got = await http.get(REGISTRIES)
        assert got.status == 200
        assert await got.json() == {"offers": [], "added": [], "status": {}}
        for method, path, body in [
            ("PUT", REGISTRIES, {"added": [STORE]}),
            ("POST", f"{REGISTRIES}/{STORE}/signin", {}),
            ("POST", f"{REGISTRIES}/{STORE}/signout", {}),
            ("GET", f"{LISTINGS}?registry={STORE}", None),
            ("GET", f"{LISTING}?registry={STORE}&ref=acme/board", None),
        ]:
            resp = await http.request(method, path, json=body)
            assert resp.status == 503, path
            assert (await resp.json())["code"] == "market_registry_unavailable"
        install = await http.post(INSTALL, json={"ref": "acme/board"})
        assert install.status == 503
        assert (await install.json())["code"] == "market_client_unavailable"
    assert "market_client_factory" not in app


@pytest.mark.asyncio
async def test_startup_sets_the_factory_when_a_provider_arrives_after_the_routes(
    home: Path,
) -> None:
    app = _make(FakeProvider())
    assert "market_client_factory" not in app  # not at registration time
    async with TestClient(TestServer(app)):
        assert callable(app["market_client_factory"])


# ---------------------------------------------------------------------------
# The owner gate
# ---------------------------------------------------------------------------


_ALL_ROUTES = [
    ("GET", REGISTRIES, None),
    ("PUT", REGISTRIES, {"added": [STORE]}),
    ("POST", f"{REGISTRIES}/{STORE}/signin", {}),
    ("POST", f"{REGISTRIES}/{STORE}/signout", {}),
    ("GET", f"{LISTINGS}?registry={STORE}", None),
    ("GET", f"{LISTING}?registry={STORE}&ref=acme/board", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["stranger", "app-token", "anonymous"])
async def test_every_route_refuses_anyone_but_the_owner(home: Path, who: str) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    kwargs: dict[str, Any] = {
        "stranger": {"user": "someone-else"},
        "app-token": {"app_token": "board"},
        "anonymous": {"user": None},
    }[who]
    async with TestClient(TestServer(_make(provider, **kwargs))) as http:
        for method, path, body in _ALL_ROUTES:
            resp = await http.request(method, path, json=body)
            assert resp.status in (401, 403), (path, resp.status)
    assert provider.signed == []
    assert provider.reader.list_calls == 0


# ---------------------------------------------------------------------------
# The one check: added AND offered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("added", [[], ["not-offered"]], ids=["not-added", "not-offered"])
async def test_a_store_not_added_or_not_offered_is_503_everywhere(
    home: Path, added: list[str]
) -> None:
    _write_added(home, added)
    provider = FakeProvider()
    async with TestClient(TestServer(_make(provider))) as http:
        for registry in (STORE, "not-offered"):
            for method, path, body in [
                ("POST", f"{REGISTRIES}/{registry}/signin", {}),
                ("GET", f"{LISTINGS}?registry={registry}", None),
                ("GET", f"{LISTING}?registry={registry}&ref=acme/board", None),
            ]:
                resp = await http.request(method, path, json=body)
                assert resp.status == 503, path
                assert (await resp.json())["code"] == "market_registry_unavailable"
        install = await http.post(INSTALL, json={"ref": "acme/board"})
        assert install.status == 503
        assert (await install.json())["code"] == "market_registry_unavailable"
    assert provider.signed == [] and provider.reader.list_calls == 0


@pytest.mark.asyncio
async def test_get_reports_offers_added_and_status(home: Path) -> None:
    _write_added(home, [STORE, "gone"])
    async with TestClient(TestServer(_make(FakeProvider()))) as http:
        body = await (await http.get(REGISTRIES)).json()
    assert [o["id"] for o in body["offers"]] == [STORE, OTHER]
    assert body["offers"][1]["web"] == ""
    assert body["added"] == [STORE]
    assert body["status"] == {STORE: {"state": "signed_in", "account": "alice"}}


@pytest.mark.asyncio
async def test_put_refuses_an_id_the_provider_does_not_offer(home: Path) -> None:
    async with TestClient(TestServer(_make(FakeProvider()))) as http:
        resp = await http.put(REGISTRIES, json={"added": ["made-up"]})
        assert resp.status == 400
        assert (await resp.json())["code"] == "market_registry_unknown"
        bad = await http.put(REGISTRIES, json={"added": "team-store"})
        assert bad.status == 400
    assert "market_registries" not in json.loads((home / "config.json").read_text())


@pytest.mark.asyncio
async def test_put_refuses_a_second_store_because_installs_need_exactly_one(home: Path) -> None:
    async with TestClient(TestServer(_make(FakeProvider()))) as http:
        resp = await http.put(REGISTRIES, json={"added": [STORE, OTHER]})
        assert resp.status == 400
        assert (await resp.json())["code"] == "market_registry_one_only"
    assert "market_registries" not in json.loads((home / "config.json").read_text())


@pytest.mark.asyncio
async def test_put_saves_the_ids_and_a_removed_one_lists_and_installs_nothing(
    home: Path,
) -> None:
    provider = FakeProvider()
    app = _make(provider)
    async with TestClient(TestServer(app)) as http:
        resp = await http.put(REGISTRIES, json={"added": [STORE, STORE]})
        assert (await resp.json()) == {"ok": True, "added": [STORE]}
        assert json.loads((home / "config.json").read_text())["market_registries"] == [STORE]
        await http.put(REGISTRIES, json={"added": []})
        listed = await http.get(f"{LISTINGS}?registry={STORE}")
        assert listed.status == 503
        # Removed: no new install either.
        install = await http.post(INSTALL, json={"ref": "acme/board"})
        assert (await install.json())["code"] == "market_registry_unavailable"


# ---------------------------------------------------------------------------
# Sign-in, sign-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sign_in_takes_an_empty_body_and_answers_only_known_fields(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    async with TestClient(TestServer(_make(provider))) as http:
        refused = await http.post(f"{REGISTRIES}/{STORE}/signin", json={"flow": "auto"})
        assert refused.status == 400
        assert provider.signed == []
        resp = await http.post(f"{REGISTRIES}/{STORE}/signin", json={})
        body = await resp.json()
    assert body == {
        "ok": True,
        "registry": STORE,
        "state": "pending",
        "code": "ABCD",
        "approvalPage": "https://store.example/approve",
    }
    assert provider.signed == [("in", STORE)]


def test_an_approval_page_that_is_not_https_is_dropped() -> None:
    assert mr.sign_in_view({"state": "pending", "approvalPage": "javascript:alert(1)"}) == {
        "state": "pending"
    }


@pytest.mark.asyncio
async def test_sign_out_calls_the_provider(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    async with TestClient(TestServer(_make(provider))) as http:
        resp = await http.post(f"{REGISTRIES}/{STORE}/signout", json={})
    assert resp.status == 200
    assert provider.signed == [("out", STORE)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "code", "message"),
    [
        ("signin", "market_signin_failed", "Kiro Space sign-in did not start"),
        ("signout", "market_signout_failed", "Kiro Space sign-out failed"),
    ],
)
async def test_a_provider_failure_names_the_action_that_failed(
    home: Path, action: str, code: str, message: str
) -> None:
    class _Failing(FakeProvider):
        def sign_in(self, registry_id: str) -> dict[str, Any]:
            raise RuntimeError("detail that stays on the server")

        def sign_out(self, registry_id: str) -> dict[str, Any]:
            raise RuntimeError("detail that stays on the server")

    _write_added(home, [STORE])
    async with TestClient(TestServer(_make(_Failing()))) as http:
        resp = await http.post(f"{REGISTRIES}/{STORE}/{action}", json={})
        assert resp.status == 502
        body = await resp.json()
        bad = await http.post(f"{REGISTRIES}/{STORE}/{action}", json={"x": 1})
        assert bad.status == 400
        assert (await bad.json())["code"] == f"market_{action}_invalid"
    assert (body["code"], body["error"]) == (code, message)
    assert "detail" not in json.dumps(body)


# ---------------------------------------------------------------------------
# The install factory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_factory_refuses_while_signed_out(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    provider.state = {"state": "needs_sign_in"}
    app = _make(provider)
    async with TestClient(TestServer(app)) as http:
        resp = await http.post(INSTALL, json={"ref": "acme/board"})
        assert resp.status == 503
        assert (await resp.json())["code"] == "market_signed_out"


@pytest.mark.asyncio
async def test_the_factory_refuses_two_added_stores(home: Path) -> None:
    _write_added(home, [STORE, OTHER])
    app = _make(FakeProvider())
    async with TestClient(TestServer(app)):
        with pytest.raises(MarketInstallError) as err:
            app["market_client_factory"]()
    assert err.value.code == "market_registry_unavailable"


@pytest.mark.asyncio
async def test_the_factory_returns_the_store_client(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    app = _make(provider)
    async with TestClient(TestServer(app)):
        assert app["market_client_factory"]() is provider.reader


# ---------------------------------------------------------------------------
# Listing rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listing_rows_are_cards_with_no_download_count(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    provider.reader = FakeReader(
        [[_view(rating={"average": 4.3, "count": 3}), _view("acme/skill-x", kind="skill")]]
    )
    async with TestClient(TestServer(_make(provider))) as http:
        body = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert body["status"]["state"] == "signed_in"
    assert body["truncated"] is False
    [row] = body["rows"]  # a non-app listing is not a card
    assert row["ref"] == "acme/board" and row["registry"] == STORE
    assert row["tags"] == ["a"]
    assert row["downloads"] == {"status": "absent"}
    assert row["rating"] == {"status": "ok", "value": 4.3}
    assert row["ratingCount"] == {"status": "ok", "value": 1}
    assert row["installed"] is False and row["nameTaken"] is False


@pytest.mark.asyncio
async def test_a_signed_out_store_lists_nothing_and_says_so(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    provider.state = {"state": "needs_sign_in"}
    async with TestClient(TestServer(_make(provider))) as http:
        body = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert body["rows"] == [] and body["status"]["state"] == "needs_sign_in"
    assert provider.reader.list_calls == 0


@pytest.mark.asyncio
async def test_paging_stops_at_ten_pages_and_marks_truncated(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    provider.reader = FakeReader([[_view(f"acme/app-{i}")] for i in range(12)])
    async with TestClient(TestServer(_make(provider))) as http:
        body = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert provider.reader.list_calls == mr.MAX_PAGES
    assert len(body["rows"]) == mr.MAX_PAGES
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_every_read_goes_to_the_store_and_names_its_account(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    async with TestClient(TestServer(_make(provider))) as http:
        first = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
        await http.get(f"{LISTINGS}?registry={STORE}")
        assert provider.reader.list_calls == 2  # nothing is kept between reads
        provider.state = {"state": "signed_in", "account": "bob"}
        provider.reader.pages = [[_view("bob/notes")]]
        second = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert first["account"] == "alice"
    assert second["account"] == "bob"
    assert [r["ref"] for r in second["rows"]] == ["bob/notes"]


@pytest.mark.asyncio
async def test_a_refused_read_reports_the_fresh_status_and_no_rows(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    provider.reader.fail = MarketInstallError("market_api_refused", "ListListings answered 403")
    statuses = iter([{"state": "signed_in", "account": "alice"}, {"state": "no_access"}])
    provider.status = lambda _id: next(statuses)  # type: ignore[method-assign]

    async with TestClient(TestServer(_make(provider))) as http:
        first = await http.get(f"{LISTINGS}?registry={STORE}")
        assert first.status == 200
        body = await first.json()
    # The status names the reason; the sign-in card shows it, not an error notice.
    assert body["status"] == {"state": "no_access", "account": ""}
    assert "error" not in body
    assert body["rows"] == []
    assert "403" not in json.dumps(body)


@pytest.mark.asyncio
async def test_a_refused_read_while_still_signed_in_keeps_the_code(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    provider.reader.fail = MarketInstallError("market_api_refused", "ListListings answered 403")
    async with TestClient(TestServer(_make(provider))) as http:
        body = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert body["error"] == {"code": "market_api_refused"}
    assert body["rows"] == []


def _probe_down(_id: str) -> dict[str, Any]:
    raise OSError("store CLI timed out")


@pytest.mark.asyncio
async def test_a_read_while_the_status_probe_fails_serves_no_rows(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    app = _make(provider)
    async with TestClient(TestServer(app)) as http:
        first = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
        assert len(first["rows"]) == 1
        provider.status = _probe_down  # type: ignore[method-assign]
        body = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert body["rows"] == []
    assert body["status"]["state"] == "unknown"
    assert body["error"] == {"code": "market_unavailable"}
    assert provider.reader.list_calls == 1  # no new read without a status


@pytest.mark.asyncio
async def test_a_cli_switch_to_b_then_a_failed_probe_shows_no_rows_of_a(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    async with TestClient(TestServer(_make(provider))) as http:
        await http.get(f"{LISTINGS}?registry={STORE}")  # alice's rows saved
        # The CLI now holds bob's sign-in, and the probe fails before bob's first read.
        provider.state = {"state": "signed_in", "account": "bob"}
        provider.status = _probe_down  # type: ignore[method-assign]
        body = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert body["rows"] == []


@pytest.mark.asyncio
async def test_a_cli_sign_out_drops_the_rows_before_a_failed_probe(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    app = _make(provider)
    async with TestClient(TestServer(app)) as http:
        await http.get(f"{LISTINGS}?registry={STORE}")
        provider.state = {"state": "needs_sign_in", "account": ""}  # signed out in the CLI
        signed_out = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
        assert signed_out["rows"] == []
        provider.status = _probe_down  # type: ignore[method-assign]
        body = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert body["rows"] == []


@pytest.mark.asyncio
async def test_a_dashboard_sign_in_as_b_drops_the_rows_of_a(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    app = _make(provider)
    async with TestClient(TestServer(app)) as http:
        await http.get(f"{LISTINGS}?registry={STORE}")
        resp = await http.post(f"{REGISTRIES}/{STORE}/signin", json={})
        assert resp.status == 200
        provider.status = _probe_down  # type: ignore[method-assign]
        body = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert body["rows"] == []


@pytest.mark.asyncio
async def test_a_first_read_while_the_status_probe_fails_reports_the_error(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()

    def _down(_id: str) -> dict[str, Any]:
        raise OSError("store CLI timed out")

    provider.status = _down  # type: ignore[method-assign]
    async with TestClient(TestServer(_make(provider))) as http:
        body = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert body["rows"] == []
    assert body["status"]["state"] == "unknown"
    assert body["error"] == {"code": "market_unavailable"}
    assert provider.reader.list_calls == 0


@pytest.mark.asyncio
async def test_a_read_during_an_outage_answers_the_error_and_no_rows(home: Path) -> None:
    """The read fails and the status probe raises (store down): `unknown`, not signed out."""
    _write_added(home, [STORE])
    provider = FakeProvider()
    app = _make(provider)
    async with TestClient(TestServer(app)) as http:
        first = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
        assert len(first["rows"]) == 1
        provider.reader.fail = MarketInstallError("market_api_unreachable", "down")
        probes = iter([{"state": "signed_in", "account": "alice"}])

        def _status(_id: str) -> dict[str, Any]:
            try:
                return next(probes)
            except StopIteration:
                raise OSError("store CLI timed out") from None

        provider.status = _status  # type: ignore[method-assign]
        again = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert again["rows"] == []
    assert again["error"] == {"code": "market_api_unreachable"}
    assert again["status"]["state"] == "unknown"


@pytest.mark.asyncio
async def test_a_store_that_refuses_the_token_keeps_the_live_status(home: Path) -> None:
    """status() says signed_in alice while the store refuses the token.

    Every answer names alice, as the registries read does, with
    ``market_signed_out`` and no rows, so the page sees no account change and
    does not reload on each load.
    """
    _write_added(home, [STORE])
    provider = FakeProvider()
    app = _make(provider)
    async with TestClient(TestServer(app)) as http:
        first = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
        assert len(first["rows"]) == 1
        provider.reader.fail = MarketInstallError("market_signed_out", "token expired")
        for _ in range(3):  # three page loads
            reg = await (await http.get(REGISTRIES)).json()
            lst = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
            det = await (await http.get(f"{LISTING}?registry={STORE}&ref=acme/board")).json()
            alice = {"state": "signed_in", "account": "alice"}
            assert reg["status"][STORE] == alice
            assert lst["status"] == alice and det["status"] == alice
            assert lst["rows"] == [] and lst["account"] == ""
            assert det["listing"] is None and det["account"] == ""
            assert lst["error"] == det["error"] == {"code": "market_signed_out"}


@pytest.mark.asyncio
async def test_a_client_that_reports_signed_out_keeps_the_live_status(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()

    def _client(_rid: str) -> Any:
        raise MarketInstallError("market_signed_out", "expired")

    provider.client = _client  # type: ignore[method-assign]
    async with TestClient(TestServer(_make(provider))) as http:
        reg = await (await http.get(REGISTRIES)).json()
        lst = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
        det = await (await http.get(f"{LISTING}?registry={STORE}&ref=acme/board")).json()
    assert reg["status"][STORE]["account"] == "alice"
    for body in (lst, det):
        assert body["status"] == {"state": "signed_in", "account": "alice"}
        assert body["error"] == {"code": "market_signed_out"}
    assert lst["rows"] == [] and det["listing"] is None


def test_a_signed_in_status_without_an_account_is_unknown() -> None:
    for account in ("", None, 7):
        assert mr.normalize_status({"state": "signed_in", "account": account}) == {
            "state": "unknown",
            "account": "",
        }
    assert mr.normalize_status({"state": "signed_in"})["state"] == "unknown"
    assert mr.normalize_status({"state": "signed_in", "account": "alice"}) == {
        "state": "signed_in",
        "account": "alice",
    }


@pytest.mark.asyncio
async def test_a_provider_that_names_no_account_serves_no_rows(home: Path) -> None:
    """An unnamed sign-in fails closed: no rows, so a switch cannot go unseen."""
    _write_added(home, [STORE])
    provider = FakeProvider()
    provider.state = {"state": "signed_in", "account": ""}
    async with TestClient(TestServer(_make(provider))) as http:
        reg = await (await http.get(REGISTRIES)).json()
        lst = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
        det = await (await http.get(f"{LISTING}?registry={STORE}&ref=acme/board")).json()
    assert reg["status"][STORE] == {"state": "unknown", "account": ""}
    assert lst["rows"] == [] and lst["error"] == {"code": "market_unavailable"}
    assert det["listing"] is None and det["error"] == {"code": "market_unavailable"}
    assert provider.reader.list_calls == 0


@pytest.mark.asyncio
async def test_saving_keeps_an_id_that_is_not_offered_now(home: Path) -> None:
    _write_added(home, ["paused-store"])
    provider = FakeProvider()
    async with TestClient(TestServer(_make(provider))) as http:
        got = await (await http.get(REGISTRIES)).json()
        assert got["added"] == []  # not offered, so not shown
        await http.put(REGISTRIES, json={"added": [STORE]})
        saved = json.loads((home / "config.json").read_text())["market_registries"]
        assert saved == ["paused-store", STORE]
        # The kept id is not a store to install from: the factory sees one store.
        await http.put(REGISTRIES, json={"added": []})
        assert json.loads((home / "config.json").read_text())["market_registries"] == [
            "paused-store"
        ]


@pytest.mark.asyncio
async def test_the_factory_ignores_an_added_id_that_is_not_offered(home: Path) -> None:
    _write_added(home, ["paused-store", STORE])
    provider = FakeProvider()
    app = _make(provider)
    async with TestClient(TestServer(app)):
        assert app["market_client_factory"]() is provider.reader


def test_same_name_from_another_source_is_its_own_card_marked_name_taken() -> None:
    installed = {"board": {"name": "board", "source": "registry:board"}}
    row = mr.listing_row(_view(), STORE, installed)
    assert row is not None
    assert row["installed"] is False and row["nameTaken"] is True
    mine = {"board": {"name": "board", "source": "market:acme/board@2"}}
    row = mr.listing_row(_view(), STORE, mine)
    assert row is not None and row["installed"] is True and row["installedVersion"] == 2
    other_ns = {"board": {"name": "board", "source": "market:someone/board@2"}}
    row = mr.listing_row(_view(), STORE, other_ns)
    assert row is not None and row["installed"] is False and row["nameTaken"] is True


@pytest.mark.asyncio
async def test_the_detail_route_answers_one_listing_with_downloads(home: Path) -> None:
    _write_added(home, [STORE])
    async with TestClient(TestServer(_make(FakeProvider()))) as http:
        bad = await http.get(f"{LISTING}?registry={STORE}&ref=../x")
        assert bad.status == 400
        body = await (await http.get(f"{LISTING}?registry={STORE}&ref=acme/board")).json()
    assert body["listing"]["ref"] == "acme/board"
    assert body["listing"]["downloads"] == {"status": "ok", "value": 5}


def test_detail_downloads_unavailable_is_not_zero() -> None:
    row = mr.listing_row(_view(downloadsUnavailable=True), STORE, {}, detail=True)
    assert row is not None and row["downloads"] == {"status": "unavailable"}


# ---------------------------------------------------------------------------
# HttpMarketClient: ListListings and GetListing over CBOR
# ---------------------------------------------------------------------------


def _mock_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> HttpMarketClient:
    real = httpx.Client

    def _client(**kwargs: Any) -> httpx.Client:
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(market_client_mod.httpx, "Client", _client)
    return HttpMarketClient("https://store.example", token=lambda: "tok")


def test_list_listings_and_get_listing_speak_cbor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        op = request.url.path.rsplit("/", 1)[1]
        body = cbor_decode(request.content)
        seen.append((op, body))
        assert request.headers["authorization"] == "Bearer tok"
        if op == "ListListings":
            answer: dict[str, Any] = {"items": [_view(), "junk"], "nextCursor": "c2"}
        else:
            answer = {"listing": _view(body["ref"])}
        return httpx.Response(200, content=cbor_encode(answer))

    client = _mock_client(monkeypatch, handler)
    items, cursor = client.list_listings(kind="app", cursor="c1", limit=100)
    assert [i["ref"] for i in items] == ["acme/board"] and cursor == "c2"
    assert client.get_listing("acme/notes")["ref"] == "acme/notes"
    assert seen[0] == ("ListListings", {"kind": "app", "limit": 100, "cursor": "c1"})
    assert seen[1] == ("GetListing", {"ref": "acme/notes"})


def test_a_listing_answer_without_items_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _mock_client(
        monkeypatch, lambda r: httpx.Response(200, content=cbor_encode({"nothing": 1}))
    )
    with pytest.raises(MarketInstallError) as err:
        client.list_listings()
    assert err.value.code == "market_api_invalid"
    with pytest.raises(MarketInstallError):
        client.get_listing("acme/board")


# ---------------------------------------------------------------------------
# Catalog rows do not claim a Kiro Space install
# ---------------------------------------------------------------------------


def test_catalog_rows_give_no_update_over_a_market_install() -> None:
    from kiro_crew.apps.registry_pipeline.catalog import _enrich_with_install_status

    rows = [{"name": "board", "version": "9.0.0"}, {"name": "notes", "version": "9.0.0"}]
    installed = {
        "board": {"name": "board", "version": "1.0.0", "source": "market:acme/board@1"},
        "notes": {"name": "notes", "version": "1.0.0", "source": "registry:notes"},
    }
    board, notes = _enrich_with_install_status(rows, installed)
    assert board["installed"] is True
    assert board["updateAvailable"] is False and board["installedFrom"] == "market"
    assert notes["updateAvailable"] is True and "installedFrom" not in notes


def test_update_notes_skip_a_market_install(tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.apps.registry_pipeline.catalog import _enrich_with_install_status
    from kiro_crew.apps.update_notify import prepare_app_update_notes

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    installed = {"board": {"name": "board", "version": "1.0.0", "source": "market:acme/board@1"}}
    first = _enrich_with_install_status([{"name": "board", "version": "1.0.0"}], installed)
    prepare_app_update_notes(first)  # seeds the ledger
    later = _enrich_with_install_status([{"name": "board", "version": "2.0.0"}], installed)
    assert prepare_app_update_notes(later) == []


# ---------------------------------------------------------------------------
# The registry install routes refuse a Kiro Space install (no clone, no change)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["install", "install-stream"])
async def test_registry_install_routes_refuse_a_market_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    from apps_routes_coverage_helpers import APP, _make_app

    from kiro_crew.apps.manager import _read_installed, install_app
    from kiro_crew.apps.registry_pipeline import install as install_mod

    _setup_env(tmp_path, monkeypatch)
    source = f"market:acme/{APP}@3"
    assert install_app(
        _make_app_source(tmp_path), expected_name=APP, market_source=source, source_commit="c" * 64
    ).ok
    before = _read_installed(APP)
    touched: list[str] = []

    def _no(*_a: Any, **_k: Any) -> Any:
        touched.append("touched")
        raise AssertionError("must not resolve or clone")

    monkeypatch.setattr(install_mod, "_resolve_install_entry", _no)
    monkeypatch.setattr(install_mod, "_clone_build_app", _no)
    monkeypatch.setattr(routes_mod, "sel", lambda: MagicMock())

    path = "/api/apps/registry/install-stream" if stream else "/api/apps/registry/install"
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as http:
        resp = await http.post(path, json={"name": APP})
        text = await resp.text()
    if stream:
        assert resp.status == 200
        done = [
            json.loads(line[len("data: ") :])
            for block in text.split("\n\n")
            if "event: done" in block
            for line in block.splitlines()
            if line.startswith("data: ")
        ]
        assert done and done[-1]["ok"] is False
        assert done[-1]["code"] == "app_update_market_source"
    else:
        assert resp.status == 400
        assert json.loads(text)["code"] == "app_update_market_source"
    assert touched == []
    assert _read_installed(APP) == before


# ---------------------------------------------------------------------------
# The sign-in folder is masked in every sandbox mode
# ---------------------------------------------------------------------------


def test_the_sign_in_folder_is_in_all_three_mask_tables() -> None:
    from kiro_crew import sandbox, security

    leaf = mr.MARKET_REGISTRIES_LEAF
    assert leaf in sandbox._CREW_HIDDEN_LEAVES
    assert leaf in security._CREW_SECRET_LEAVES
    assert leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    for prefix in (".kiro/crew", ".kirocrew"):
        assert security.is_sensitive_path(f"~/{prefix}/{leaf}/{STORE}/credentials.json")


def test_the_data_dir_follows_kirocrew_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    got = mr.registry_data_dir(STORE)
    assert got == (tmp_path / "home" / mr.MARKET_REGISTRIES_LEAF / STORE)
    assert not got.exists()
    with pytest.raises(ValueError):
        mr.registry_data_dir("../x")


def test_config_round_trips_market_registries(tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"market_registries": [STORE, 3, STORE]}))
    cfg = KiroCrewConfig.load()
    assert cfg.market_registries == [STORE]
    assert cfg.to_dict()["market_registries"] == [STORE]


@pytest.mark.asyncio
async def test_a_route_does_not_change_the_started_app(home: Path) -> None:
    import warnings

    _write_added(home, [STORE])
    async with TestClient(TestServer(_make(FakeProvider()))) as http:
        with warnings.catch_warnings():
            warnings.filterwarnings("error", message="Changing state of started")
            resp = await http.get(f"{LISTINGS}?registry={STORE}")
            assert resp.status == 200


@pytest.mark.asyncio
async def test_a_failed_read_answers_the_error_and_no_rows(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    async with TestClient(TestServer(_make(provider))) as http:
        first = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
        assert [r["ref"] for r in first["rows"]] == ["acme/board"]
        provider.reader.fail = MarketInstallError("market_api_unreachable", "down")
        again = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert again["error"] == {"code": "market_api_unreachable"}
    assert again["rows"] == [] and again["account"] == ""


@pytest.mark.asyncio
async def test_a_second_read_answers_what_the_store_holds_now(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    async with TestClient(TestServer(_make(provider))) as http:
        await http.get(f"{LISTINGS}?registry={STORE}")
        provider.reader.pages = [[_view("acme/notes")]]
        again = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert [r["ref"] for r in again["rows"]] == ["acme/notes"]
    assert "error" not in again


def test_an_offer_web_that_is_not_https_is_dropped() -> None:
    provider = FakeProvider([{"id": STORE, "label": "Team", "web": "http://store.example"}])
    assert mr.offered(provider) == [{"id": STORE, "label": "Team", "web": ""}]


@pytest.mark.asyncio
async def test_the_detail_refuses_an_answer_for_another_ref(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    provider.reader.get_listing = lambda ref: _view("acme/other")  # type: ignore[method-assign]
    async with TestClient(TestServer(_make(provider))) as http:
        body = await (await http.get(f"{LISTING}?registry={STORE}&ref=acme/board")).json()
    assert body["listing"] is None
    assert body["error"] == {"code": "market_resolve_invalid"}


class _SlowReader(FakeReader):
    """A reader that holds its first read until the test releases it."""

    def __init__(self) -> None:
        super().__init__([[_view("alice/private-a")]])
        self.entered = threading.Event()
        self.release = threading.Event()

    def list_listings(self, **kw: Any):
        self.entered.set()
        assert self.release.wait(30)  # a hang guard on the reader's own thread
        return super().list_listings(**kw)

    def get_listing(self, ref: str) -> dict[str, Any]:
        self.entered.set()
        assert self.release.wait(30)  # a hang guard on the reader's own thread
        return super().get_listing(ref)


async def _change_hands(http: TestClient, provider: FakeProvider, between: str) -> None:
    if between == "signout":
        provider.state = {"state": "needs_sign_in", "account": ""}
        assert (await http.post(f"{REGISTRIES}/{STORE}/signout", json={})).status == 200
    else:  # the CLI switched to bob, and the sign-in poll sees it
        provider.state = {"state": "signed_in", "account": "bob"}
        await http.get(REGISTRIES)


@pytest.mark.asyncio
@pytest.mark.parametrize("between", ["signout", "cli_switch"])
async def test_a_read_that_ends_after_the_store_changed_hands_answers_nothing_of_a(
    home: Path, between: str
) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    slow = _SlowReader()
    provider.reader = slow
    async with TestClient(TestServer(_make(provider))) as http:
        task = asyncio.create_task(http.get(f"{LISTINGS}?registry={STORE}"))
        try:
            await async_wait_until(slow.entered.is_set)
            await _change_hands(http, provider, between)
        finally:
            slow.release.set()
        body = await (await asyncio.wait_for(task, _HANG_CEILING_S)).json()
    assert body["rows"] == []
    assert body["account"] == ""
    assert body["status"] == provider.state


@pytest.mark.asyncio
@pytest.mark.parametrize("between", ["signout", "cli_switch"])
async def test_a_detail_read_that_ends_after_the_store_changed_hands_answers_no_listing(
    home: Path, between: str
) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    slow = _SlowReader()
    provider.reader = slow
    async with TestClient(TestServer(_make(provider))) as http:
        url = f"{LISTING}?registry={STORE}&ref=alice/private-a"
        task = asyncio.create_task(http.get(url))
        try:
            await async_wait_until(slow.entered.is_set)
            await _change_hands(http, provider, between)
        finally:
            slow.release.set()
        body = await (await asyncio.wait_for(task, _HANG_CEILING_S)).json()
    assert body["listing"] is None
    assert body["account"] == ""
    assert body["status"] == provider.state


@pytest.mark.asyncio
async def test_listing_answers_name_the_account_they_were_read_for(home: Path) -> None:
    _write_added(home, [STORE])
    provider = FakeProvider()
    async with TestClient(TestServer(_make(provider))) as http:
        rows = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
        one = await (await http.get(f"{LISTING}?registry={STORE}&ref=acme/board")).json()
        provider.state = {"state": "needs_sign_in", "account": ""}
        out = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert rows["account"] == "alice" and len(rows["rows"]) == 1
    assert one["account"] == "alice" and one["listing"] is not None
    assert out["account"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["listings", "detail"])
async def test_a_sign_out_during_the_last_step_of_a_read_answers_nothing_of_a(
    home: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """The r7 M-1 probe, flipped: the status check runs after every other await."""
    from kiro_crew.apps import routes_market_registry as rmr

    _write_added(home, [STORE])
    provider = FakeProvider()
    entered = asyncio.Event()
    release = asyncio.Event()
    real = rmr._store_installed

    async def slow_installed() -> dict[str, dict[str, Any]]:
        entered.set()
        await asyncio.wait_for(release.wait(), _HANG_CEILING_S)
        return await real()

    monkeypatch.setattr(rmr, "_store_installed", slow_installed)
    url = (
        f"{LISTINGS}?registry={STORE}"
        if route == "listings"
        else f"{LISTING}?registry={STORE}&ref=acme/board"
    )
    async with TestClient(TestServer(_make(provider))) as http:
        task = asyncio.create_task(http.get(url))
        try:
            await asyncio.wait_for(entered.wait(), _HANG_CEILING_S)
            provider.state = {"state": "needs_sign_in", "account": ""}
            assert (await http.post(f"{REGISTRIES}/{STORE}/signout", json={})).status == 200
        finally:
            release.set()
        body = await asyncio.wait_for(task, _HANG_CEILING_S)
        body = await body.json()
    assert body["account"] == ""
    assert body["status"] == {"state": "needs_sign_in", "account": ""}
    assert not body.get("rows") and not body.get("listing")


@pytest.mark.asyncio
async def test_a_switch_away_and_back_during_a_read_is_not_kept_for_the_next_read(
    home: Path,
) -> None:
    """The r7 M-3 probe, flipped: nothing is cached, so the next read is the store's own."""
    _write_added(home, [STORE])
    provider = FakeProvider()

    class SwitchingReader:
        def list_listings(self, **_: Any):
            provider.state = {"state": "signed_in", "account": "bob"}
            rows = [_view("bob/private-b")]
            provider.state = {"state": "signed_in", "account": "alice"}
            return rows, None

        def get_listing(self, ref: str) -> dict[str, Any]:
            return _view(ref)

    provider.reader = SwitchingReader()  # type: ignore[assignment]
    async with TestClient(TestServer(_make(provider))) as http:
        await http.get(f"{LISTINGS}?registry={STORE}")
        provider.reader = FakeReader([[_view("alice/notes")]])
        again = await (await http.get(f"{LISTINGS}?registry={STORE}")).json()
    assert again["account"] == "alice"
    assert [r["ref"] for r in again["rows"]] == ["alice/notes"]


@pytest.mark.asyncio
async def test_the_install_account_names_who_is_signed_in_now(home: Path) -> None:
    from kiro_crew.apps.routes_market_install import MARKET_ACCOUNT_KEY

    _write_added(home, [STORE])
    provider = FakeProvider()
    app = _make(provider)
    async with TestClient(TestServer(app)):
        account_now = app[MARKET_ACCOUNT_KEY]
        assert await account_now() == "alice"
        provider.state = {"state": "signed_in", "account": "bob"}
        assert await account_now() == "bob"
        provider.state = {"state": "needs_sign_in", "account": ""}
        assert await account_now() == ""


@pytest.mark.asyncio
async def test_the_install_account_raises_store_unavailable_when_the_probe_fails(
    home: Path,
) -> None:
    from kiro_crew.apps.routes_market_install import MARKET_ACCOUNT_KEY

    _write_added(home, [STORE])
    provider = FakeProvider()

    def _broken(registry_id: str) -> dict[str, Any]:
        raise OSError("probe failed")

    app = _make(provider)
    async with TestClient(TestServer(app)):
        account_now = app[MARKET_ACCOUNT_KEY]
        provider.status = _broken  # type: ignore[method-assign]
        with pytest.raises(MarketInstallError) as failed:
            await account_now()
        assert failed.value.code == "market_unavailable"
        provider.state = {"state": "something-else"}
        del provider.status  # the class's own probe again, on an odd state
        with pytest.raises(MarketInstallError):
            await account_now()


@pytest.mark.asyncio
async def test_the_install_store_names_the_one_added_store(home: Path) -> None:
    from kiro_crew.apps.routes_market_install import MARKET_STORE_KEY

    _write_added(home, [STORE])
    app = _make(FakeProvider())
    async with TestClient(TestServer(app)):
        store_now = app[MARKET_STORE_KEY]
        assert store_now() == STORE
        _write_added(home, [])
        assert store_now() == ""
