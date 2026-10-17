"""The owner gate on Personal Shopper's nine write routes.

A dashboard caller (``app == ""``) or a request with no app claim must be the
dashboard owner, or it gets the shared 403 ``owner_only`` before any store or
sites.json write. An app token keeps today's result: the token middleware has
already confined it to the routes its manifest grants. Reads stay open.

No network: the shared embedder is replaced with None, so the store stays on its
keyword path. All writes land in tmp_path.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.personal_shopper.backend import routes
from kiro_crew.apps.builtins.personal_shopper.backend import store as store_mod
from kiro_crew.apps.builtins.personal_shopper.backend.store import PreferenceStore

BASE = "/api/apps/personal-shopper"
OWNER = "owner-1"
NON_OWNER = "allowlisted-2"
_NO_APP_CLAIM = object()

MUTATIONS = [
    ("POST", "/preferences", {"text": "planted"}),
    ("PUT", "/preferences/{pref}", {"text": "rewritten"}),
    ("DELETE", "/preferences/{pref}", None),
    ("POST", "/preferences/reembed", {}),
    ("POST", "/groups", {"name": "Planted"}),
    ("DELETE", "/groups/{group}", None),
    ("POST", "/history", {"problem": "planted"}),
    ("PUT", "/history/{hist}/feedback", {"product": "Shoe A", "feedback": "purchased"}),
    ("PUT", "/sites", {"sites": [{"id": "x", "name": "Other", "url": "https://other.example"}]}),
]
_IDS = [f"{m} {p}" for m, p, _ in MUTATIONS]

READS = [
    ("GET", "/preferences", None),
    ("POST", "/preferences/search", {"query": "shoe"}),
    ("GET", "/groups", None),
    ("GET", "/history", None),
    ("GET", "/sites", None),
]


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "get_shared_embedder", None)
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)
    db_path = tmp_path / "preferences.db"
    store = PreferenceStore(db_path=db_path)
    ids = {
        "pref": store.add("allergic to latex; shoe size US 10", tags=[]),
        "group": store.add_group("Running"),
        "hist": store.add_history("knee pain on runs", products=[{"name": "Shoe A"}]),
    }
    sites_file = tmp_path / "sites.json"
    sites_file.write_text(
        json.dumps({"sites": [{"id": "s1", "name": "Owner Shop", "url": "https://shop.example"}]}),
        encoding="utf-8",
    )

    async def _get_store():
        return store

    monkeypatch.setattr(routes, "_get_store", _get_store)
    monkeypatch.setattr(routes, "_sites_path", lambda: sites_file)

    def snapshot():
        # A second connection sees committed rows (WAL included), so this is the
        # whole persisted store, not the in-process view of it.
        conn = sqlite3.connect(db_path)
        try:
            rows = list(conn.iterdump())
        finally:
            conn.close()
        return rows, sites_file.read_bytes()

    yield SimpleNamespace(store=store, ids=ids, snapshot=snapshot)
    store.close()


async def _request(seeded, method, path, body, *, user, app):
    @web.middleware
    async def _identity(request, handler):
        # Stand-in for the token middleware's authenticated claims.
        request["user"] = user
        if app is not _NO_APP_CLAIM:
            request["app"] = app
        return await handler(request)

    server_app = web.Application(middlewares=[_identity])
    server_app["state"] = SimpleNamespace(owner_id=OWNER)
    routes.register_routes(server_app)
    async with TestClient(TestServer(server_app)) as client:
        resp = await client.request(method, BASE + path.format(**seeded.ids), json=body)
        return resp.status, await resp.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS, ids=_IDS)
async def test_non_owner_dashboard_caller_is_refused_and_nothing_changes(
    seeded, method, path, body
):
    before = seeded.snapshot()
    status, payload = await _request(seeded, method, path, body, user=NON_OWNER, app="")
    assert (status, payload.get("code")) == (403, "owner_only")
    assert seeded.snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS, ids=_IDS)
async def test_missing_app_claim_is_refused_and_nothing_changes(seeded, method, path, body):
    before = seeded.snapshot()
    status, payload = await _request(seeded, method, path, body, user=OWNER, app=_NO_APP_CLAIM)
    assert (status, payload.get("code")) == (403, "owner_only")
    assert seeded.snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS, ids=_IDS)
@pytest.mark.parametrize(
    ("user", "app"),
    [(OWNER, ""), ("app:personal-shopper", "personal-shopper"), ("app:granted", "granted-app")],
    ids=["owner", "own-app-token", "foreign-token-granted-by-middleware"],
)
async def test_allowed_callers_still_write(seeded, method, path, body, user, app):
    status, payload = await _request(seeded, method, path, body, user=user, app=app)
    assert 200 <= status < 300, (status, payload)


@pytest.mark.asyncio
async def test_owner_write_lands(seeded):
    status, _ = await _request(
        seeded, "PUT", "/preferences/{pref}", {"text": "size US 11"}, user=OWNER, app=""
    )
    assert status == 200
    assert seeded.store.list_all()[0]["text"] == "size US 11"


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "body"), READS, ids=[f"{m} {p}" for m, p, _ in READS])
async def test_non_owner_reads_are_unchanged(seeded, method, path, body):
    status, _ = await _request(seeded, method, path, body, user=NON_OWNER, app="")
    assert status == 200
