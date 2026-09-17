"""RED-first product-path regression: the REAL knowledge search route
(GET /api/knowledge/items) over the REAL auth middleware and the REAL W01
store/resolver -- NOT a hand-built principal, always-FRESH stub, or a replaced
_keyword_search.

The bug this pins: an authenticated dashboard owner who ALSO holds a real W01
binding for a managed item was denied FOREVER, because the query-principal
producer minted the owner as local_library=True (which the resolver
short-circuits to deny). The fix makes the authenticated owner a VERIFIED
NON-local principal so its managed candidates go through the real binding check.

These tests exercise the true request path end to end:
  HTTP GET -> token_auth middleware -> list_items -> _knowledge_query_principal
  (from the validated request identity) -> _knowledge_binding_resolver (real
  ControlPlaneBindingResolver over a real BindingStore, via the bridge) ->
  HybridRetriever gate.

No fake principal, no lambda, no monkeypatched search. The item is found by the
real FTS keyword leg on its real text. The W01 package is not on this PR's
branch, so the module importorskips (green standalone; runs in the merged
candidate).
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytest.importorskip("kiro_crew.connections.control_plane.acl_binding_resolver")

from kiro_crew.connections.control_plane.acl_binding_resolver import (  # noqa: E402
    ControlPlaneBindingResolver,
)
from kiro_crew.connections.control_plane.binding import create_binding  # noqa: E402
from kiro_crew.connections.control_plane.lifecycle import BindingStore  # noqa: E402
from kiro_crew.dashboard import token_auth  # noqa: E402
from kiro_crew.dashboard.handlers import knowledge as kh  # noqa: E402
from kiro_crew.knowledge.acl import (  # noqa: E402
    ProviderResourceRef,
    RevalidationOutcome,
    principal_is_verified,
)
from kiro_crew.knowledge.store import KnowledgeStore  # noqa: E402

SERVICE = "salesforce"
ACCOUNT = "acme-org"
DEPLOYMENT = "sf-deploy-acme"
INSTANCE_URL = "https://acme.my.salesforce.com"
# The dashboard owner's validated user id (token subject) -> the producer mints
# principal_id = "user:<uid>", which must equal the binding's kiro_principal.
OWNER_UID = "local-owner"
OWNER_PRINCIPAL_ID = "user:local-owner"


def _verifier(*, claimed_subject, claimed_tenant, service_id):
    return {"subject_ref": "sf-owner", "tenant_ref": "t-A"}


def _seed_owner_binding(bstore, *, kiro_principal=OWNER_PRINCIPAL_ID):
    binding = create_binding(
        service_id=SERVICE,
        claimed_subject="c",
        claimed_tenant="c",
        credential_mode="oauth_user",
        verifier=_verifier,
        slug="salesforce",
    )
    return bstore.insert(
        binding,
        deployment_id=DEPLOYMENT,
        kiro_principal=kiro_principal,
        account=ACCOUNT,
        endpoint=INSTANCE_URL,
    )


class _Fresh:
    def revalidate(self, ctx, item_id, grant):
        return RevalidationOutcome.FRESH


def _managed_item(store, title, text, *, instance_url=INSTANCE_URL):
    src = store.add_source(f"sf-{title}", SERVICE, f"salesforce://{title}")
    item = store.add_item(title, text, "record", source_id=src)
    store.set_item_acl(
        item,
        ["sf-owner"],
        tenant="t-A",
        managed=True,
        fresh_as_of=0.0,
        resource_ref=ProviderResourceRef(
            provider=SERVICE,
            account=ACCOUNT,
            resource_id=title,
            locator={"instanceUrl": instance_url, "sobjectType": "Account", "recordId": title},
        ),
    )
    return item


def _app(store, *, install_resolver=True, bstore=None):
    """A real app: list_items route + real token_auth middleware. When a resolver
    is installed it is the REAL ControlPlaneBindingResolver over the real store,
    bridged and wired on the app exactly as a shared deployment host would."""
    app = web.Application()
    state = type("S", (), {})()
    state.knowledge_store = store
    app["state"] = state
    app["knowledge_embedder"] = None  # keyword leg only; no model needed
    if install_resolver and bstore is not None:
        resolver = ControlPlaneBindingResolver(bstore, principal_verified=principal_is_verified)
        app["knowledge_binding_resolver"] = resolver
        app["knowledge_revalidator"] = _Fresh()
    app.router.add_get("/api/knowledge/items", kh.list_items)
    app.router.add_get("/api/knowledge/items/{id}", kh.get_item)
    app.router.add_get("/api/knowledge/items/{id}/content", kh.get_item_content)
    app.router.add_get("/api/knowledge/items/{id}/related", kh.get_related_items)
    app.router.add_get("/api/knowledge/items/{id}/export", kh.export_item)
    app.router.add_get("/api/knowledge/export", kh.export_all)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    return app


async def _search_titles(client, token, q="alpha"):
    resp = await client.get("/api/knowledge/items", params={"token": token, "q": q})
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    items = body.get("items", body if isinstance(body, list) else [])
    return {it.get("title") for it in items}


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    s.close()


@pytest.mark.asyncio
async def test_authenticated_bound_owner_sees_managed_item_over_http(store, tmp_path):
    # The regression: an authenticated owner WITH a real W01 binding must SEE the
    # managed item through the real route. (Pre-fix -- owner local_library=True --
    # this returned 0; the producer fix makes the owner verified NON-local so the
    # real binding check runs and admits it.)
    bstore = BindingStore(path=tmp_path / "b.json")
    _seed_owner_binding(bstore)
    item_title = "Deal"
    _managed_item(store, item_title, "alpha content about the deal")
    tok = token_auth.generate_token(OWNER_UID)  # dashboard-user token (no app)
    async with TestClient(TestServer(_app(store, bstore=bstore))) as client:
        titles = await _search_titles(client, tok)
        assert item_title in titles


@pytest.mark.asyncio
async def test_unbound_owner_gets_zero_over_http(store, tmp_path):
    # Same authenticated owner, but the store holds NO binding -> the managed item
    # is denied (fail-closed), 0 results.
    bstore = BindingStore(path=tmp_path / "b.json")  # empty
    _managed_item(store, "Deal", "alpha content about the deal")
    tok = token_auth.generate_token(OWNER_UID)
    async with TestClient(TestServer(_app(store, bstore=bstore))) as client:
        assert "Deal" not in await _search_titles(client, tok)


@pytest.mark.asyncio
async def test_wrong_endpoint_owner_gets_zero_over_http(store, tmp_path):
    # Bound owner, but the item's ref carries an unregistered instanceUrl ->
    # endpoint discriminator denies.
    bstore = BindingStore(path=tmp_path / "b.json")
    _seed_owner_binding(bstore)
    _managed_item(
        store, "Deal", "alpha content about the deal", instance_url="https://evil.my.salesforce.com"
    )
    tok = token_auth.generate_token(OWNER_UID)
    async with TestClient(TestServer(_app(store, bstore=bstore))) as client:
        assert "Deal" not in await _search_titles(client, tok)


@pytest.mark.asyncio
async def test_no_resolver_owner_sees_local_but_not_managed_over_http(store, tmp_path):
    # No binding_resolver installed (standalone host): the non-local owner must
    # STILL see a trusted-LOCAL file (local behaviour preserved) but NOT the
    # managed item (no leak -- the gate drops a managed candidate under the
    # bypass access_context when no resolver is wired).
    local_src = store.add_source("Vault", "local_folder", "file:///v")
    store.add_item("LocalDoc", "alpha content local note", "doc", source_id=local_src)
    _managed_item(store, "Deal", "alpha content about the deal")
    tok = token_auth.generate_token(OWNER_UID)
    async with TestClient(TestServer(_app(store, install_resolver=False))) as client:
        titles = await _search_titles(client, tok)
        assert "LocalDoc" in titles  # trusted-local visible
        assert "Deal" not in titles  # managed denied (no resolver, no leak)


# --------------------------------------------------------------------------
# Non-search read/export routes must apply the SAME per-item ACL gate.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_item_detail_route_gated(store, tmp_path):
    # GET /items/{id} must NOT return a managed item to an UNBOUND owner (404,
    # not 403 -- existence must not leak); a BOUND owner sees it.
    item = _managed_item(store, "Deal", "alpha content about the deal")
    tok = token_auth.generate_token(OWNER_UID)

    empty = BindingStore(path=tmp_path / "empty.json")
    async with TestClient(TestServer(_app(store, bstore=empty))) as client:
        resp = await client.get(f"/api/knowledge/items/{item}", params={"token": tok})
        assert resp.status == 404  # denied -> not found

    bound = BindingStore(path=tmp_path / "bound.json")
    _seed_owner_binding(bound)
    async with TestClient(TestServer(_app(store, bstore=bound))) as client:
        resp = await client.get(f"/api/knowledge/items/{item}", params={"token": tok})
        assert resp.status == 200


@pytest.mark.asyncio
async def test_item_content_route_gated(store, tmp_path):
    # GET /items/{id}/content must not hand raw content to an unbound owner.
    item = _managed_item(store, "Deal", "alpha content about the deal")
    tok = token_auth.generate_token(OWNER_UID)
    empty = BindingStore(path=tmp_path / "empty.json")
    async with TestClient(TestServer(_app(store, bstore=empty))) as client:
        resp = await client.get(f"/api/knowledge/items/{item}/content", params={"token": tok})
        assert resp.status == 404


@pytest.mark.asyncio
async def test_item_export_route_gated(store, tmp_path):
    # GET /items/{id}/export must not export a managed item to an unbound owner.
    item = _managed_item(store, "Deal", "alpha content about the deal")
    tok = token_auth.generate_token(OWNER_UID)
    empty = BindingStore(path=tmp_path / "empty.json")
    async with TestClient(TestServer(_app(store, bstore=empty))) as client:
        resp = await client.get(f"/api/knowledge/items/{item}/export", params={"token": tok})
        assert resp.status == 404


@pytest.mark.asyncio
async def test_browse_list_route_gated(store, tmp_path):
    # GET /items with NO q (browse listing, reads store directly) must filter
    # managed items an unbound owner may not see, while keeping trusted-local.
    local_src = store.add_source("Vault", "local_folder", "file:///v")
    store.add_item("LocalDoc", "local note", "doc", source_id=local_src)
    _managed_item(store, "Deal", "managed deal content")
    tok = token_auth.generate_token(OWNER_UID)
    empty = BindingStore(path=tmp_path / "empty.json")
    async with TestClient(TestServer(_app(store, bstore=empty))) as client:
        resp = await client.get("/api/knowledge/items", params={"token": tok})
        assert resp.status == 200
        titles = {it.get("title") for it in (await resp.json()).get("items", [])}
        assert "LocalDoc" in titles
        assert "Deal" not in titles


@pytest.mark.asyncio
async def test_export_all_route_filters_managed(store, tmp_path):
    # GET /export must not carry a managed item an unbound owner cannot see.
    local_src = store.add_source("Vault", "local_folder", "file:///v")
    store.add_item("LocalDoc", "local note", "doc", source_id=local_src)
    managed = _managed_item(store, "Deal", "managed deal content")
    tok = token_auth.generate_token(OWNER_UID)
    empty = BindingStore(path=tmp_path / "empty.json")
    async with TestClient(TestServer(_app(store, bstore=empty))) as client:
        resp = await client.get("/api/knowledge/export", params={"token": tok})
        assert resp.status == 200
        bundle = await resp.json()
        exported_ids = {it.get("id") for it in bundle.get("items", [])}
        assert managed not in exported_ids


@pytest.mark.asyncio
async def test_export_all_route_does_not_leak_managed_source_or_entities(store, tmp_path):
    # The export closure: a hidden managed item's SOURCE, entities and relations
    # must not ride along in the bundle after item filtering.
    managed_src = store.add_source("SF-Deal", SERVICE, "salesforce://deal-src")
    managed = store.add_item("Deal", "managed deal content", "record", source_id=managed_src)
    store.set_item_acl(
        managed,
        ["sf-owner"],
        tenant="t-A",
        managed=True,
        fresh_as_of=0.0,
        resource_ref=ProviderResourceRef(
            provider=SERVICE,
            account=ACCOUNT,
            resource_id="Deal",
            locator={"instanceUrl": INSTANCE_URL, "sobjectType": "Account", "recordId": "Deal"},
        ),
    )
    # Give the managed item an entity + mention so the bundle would carry them.
    eid = store.add_entity(name="AcmeCorp", entity_type="org")
    store.add_mention(managed, eid, context="ctx")
    tok = token_auth.generate_token(OWNER_UID)
    empty = BindingStore(path=tmp_path / "empty.json")
    async with TestClient(TestServer(_app(store, bstore=empty))) as client:
        resp = await client.get("/api/knowledge/export", params={"token": tok})
        assert resp.status == 200
        bundle = await resp.json()
        assert managed_src not in {s.get("id") for s in bundle.get("sources", [])}
        assert eid not in {e.get("id") for e in bundle.get("entities", [])}


@pytest.mark.asyncio
async def test_source_creation_absent_connector_refused_not_500(store, tmp_path):
    # A source-creation request for a lazy connector whose vendor module is
    # absent must be REFUSED (validate_config -> False), never a 500 from an
    # uncaught ImportError.
    from kiro_crew.dashboard.handlers.knowledge import _LazyConnector

    lc = _LazyConnector("github", "kiro_crew.knowledge.connectors.__absent__", "X")
    ok, msg = lc.validate_config({"repo": "x"})
    assert ok is False
    assert "not available" in msg


def test_install_binding_resolver_wires_real_control_plane():
    # With the control-plane package present (this runs only in the merged
    # candidate -- the module importorskips otherwise), _install_binding_resolver
    # constructs and installs a REAL ControlPlaneBindingResolver, not a stub.
    from aiohttp import web

    from kiro_crew.dashboard.handlers.knowledge import _install_binding_resolver

    app = web.Application()
    _install_binding_resolver(app)
    resolver = app.get("knowledge_binding_resolver")
    assert isinstance(resolver, ControlPlaneBindingResolver)
    # It exposes the new signature the bridge prefers.
    assert hasattr(resolver, "resolve_ref")


@pytest.mark.asyncio
async def test_browse_list_filters_before_pagination(store, tmp_path):
    # The browse listing must ACL-filter the COMPLETE candidate set BEFORE
    # computing total/pagination: total counts ONLY visible items, and a visible
    # item is never pushed off page 1 by hidden rows ordered ahead of it.
    local_src = store.add_source("Vault", "local_folder", "file:///v")
    # 3 managed (hidden to an unbound owner) + 2 local (visible).
    for i in range(3):
        _managed_item(store, f"Managed{i}", f"managed body {i}")
    store.add_item("LocalA", "local a", "doc", source_id=local_src)
    store.add_item("LocalB", "local b", "doc", source_id=local_src)
    tok = token_auth.generate_token(OWNER_UID)
    empty = BindingStore(path=tmp_path / "empty.json")
    async with TestClient(TestServer(_app(store, bstore=empty))) as client:
        resp = await client.get("/api/knowledge/items", params={"token": tok, "limit": "20"})
        assert resp.status == 200
        body = await resp.json()
        titles = {it.get("title") for it in body["items"]}
        # total counts ONLY the 2 visible local items, not the hidden managed.
        assert body["total"] == 2
        assert titles == {"LocalA", "LocalB"}
        assert not any(t.startswith("Managed") for t in titles)


@pytest.mark.asyncio
async def test_export_all_empty_visible_set_still_strips_bundle(store, tmp_path):
    # When NOTHING is visible (all items managed + unbound owner), export must
    # still strip sources/entities/relations, not fall through and leak them.
    managed_src = store.add_source("SF", SERVICE, "salesforce://s")
    managed = store.add_item("Deal", "deal", "record", source_id=managed_src)
    store.set_item_acl(
        managed,
        ["sf-owner"],
        tenant="t-A",
        managed=True,
        fresh_as_of=0.0,
        resource_ref=ProviderResourceRef(
            provider=SERVICE,
            account=ACCOUNT,
            resource_id="Deal",
            locator={"instanceUrl": INSTANCE_URL, "sobjectType": "Account", "recordId": "Deal"},
        ),
    )
    eid = store.add_entity(name="AcmeCorp", entity_type="org")
    store.add_mention(managed, eid, context="ctx")
    tok = token_auth.generate_token(OWNER_UID)
    empty = BindingStore(path=tmp_path / "empty.json")
    async with TestClient(TestServer(_app(store, bstore=empty))) as client:
        resp = await client.get("/api/knowledge/export", params={"token": tok})
        assert resp.status == 200
        bundle = await resp.json()
        assert bundle.get("items") == []
        assert bundle.get("sources") == []
        assert bundle.get("entities") == []
        assert bundle.get("relations") == []


@pytest.mark.asyncio
async def test_export_all_drops_locations_of_excluded_sources(store, tmp_path):
    # A visible local item that ALSO has a source_location under a managed
    # (excluded) source must not keep that location in the export -- otherwise the
    # imported bundle would fail its source foreign key.
    local_src = store.add_source("Vault", "local_folder", "file:///v")
    item = store.add_item("Shared", "alpha shared body", "doc", source_id=local_src)
    managed_src = store.add_source("SF", SERVICE, "salesforce://s")
    # A second location for the same item, under the managed source.
    store.add_source_location(item_id=item, source_id=managed_src, chunk_range="0-1")
    tok = token_auth.generate_token(OWNER_UID)
    empty = BindingStore(path=tmp_path / "empty.json")
    async with TestClient(TestServer(_app(store, bstore=empty))) as client:
        resp = await client.get("/api/knowledge/export", params={"token": tok})
        assert resp.status == 200
        bundle = await resp.json()
        kept_src_ids = {s.get("id") for s in bundle.get("sources", [])}
        # Every retained location's source is present in the exported sources.
        for loc in bundle.get("source_locations", []):
            assert loc.get("source_id") in kept_src_ids
        # The managed source is excluded, so its location is dropped.
        assert managed_src not in kept_src_ids
