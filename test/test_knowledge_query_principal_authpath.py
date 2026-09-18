"""_knowledge_query_principal derives ONLY from the identity the real auth
middleware establishes on the request -- not from any caller-supplied header.

Root's correction: the earlier fallback minted session:<X-Session-Key> from any
header and ignored the app-token identity. This drives the ACTUAL
token_auth.token_auth_middleware over a TestClient with real signed tokens (not a
hand-filled _slots + direct helper), and asserts:

* a dashboard-user token (no app) -> LOCAL_PRINCIPAL (the on-host single user);
* an app token -> QueryPrincipal("app:<name>", local_library=True);
* a request that carries only a bogus X-Session-Key header and NO valid token is
  REJECTED by the middleware (401/403) -- it never reaches the handler, so a raw
  header can never become a principal.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.handlers.knowledge import (
    _knowledge_binding_resolver,
    _knowledge_query_principal,
)
from kiro_crew.knowledge.acl import (
    LOCAL_PRINCIPAL,
    AccessContext,
    ProviderResourceRef,
    RevalidationOutcome,
)
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore


async def _probe(request: web.Request) -> web.Response:
    # Runs AFTER the auth middleware, so request["app"]/["user"]/
    # ["is_dashboard_user"] are the validated identity (or absent). A POST body
    # is read and DISCARDED here only to prove the derivation ignores it.
    if request.method == "POST":
        try:
            await request.json()
        except Exception:
            pass
    p = _knowledge_query_principal(request)
    return web.json_response({
        "principal_id": p.principal_id,
        "local_library": p.local_library,
        "verified": p.verified,
        "is_local_principal": p is LOCAL_PRINCIPAL,
        "authed_app": request.get("app"),
        "is_dashboard_user": request.get("is_dashboard_user"),
    })


class _GrantResolver:
    """A W01-shaped resolver returning an AccessGrant-like record (no subject_ids)."""

    def __init__(self, grants):
        self.grants = grants  # {(provider, account): (subject, tenant)}

    def resolve(self, principal, provider, account):
        pair = self.grants.get((provider, account))
        if pair is None:
            return None
        subject, tenant = pair
        # AccessContext IS accepted by the bridge (isinstance pass-through); this
        # keeps the test's resolver dependency-free while exercising the real
        # bridge + gate.
        return AccessContext(subject=subject, tenant=tenant)


class _Fresh:
    def revalidate(self, ctx, item_id, grant):
        return RevalidationOutcome.FRESH


async def _gate_probe(request: web.Request) -> web.Response:
    """REAL route -> middleware -> query principal -> binding resolver -> gate.

    Builds the query principal from the authenticated request, pulls the
    installed binding resolver through the handler seam, and runs the actual
    HybridRetriever ACL gate over one managed item. Returns whether the item is
    visible -- the end-to-end route->resolver closure (A.4)."""
    store = request.app["_kb"]
    item = request.app["_item"]
    principal = _knowledge_query_principal(request)
    resolver = _knowledge_binding_resolver(request)
    r = HybridRetriever(store, revalidator=_Fresh(), binding_resolver=resolver)
    r._keyword_search = lambda *a, **k: [(item, 1)]
    res = r.search("alpha", limit=10, query_principal=principal)
    return web.json_response({"visible": item in {row["id"] for row in res}})


def _app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/knowledge/_probe_principal", _probe)
    app.router.add_post("/api/knowledge/_probe_principal", _probe)
    # An app token is confined to its own namespace by the real allowlist
    # (deny-by-default), so give the app-token case a path it can reach.
    app.router.add_get("/api/apps/mochi/_probe_principal", _probe)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    return app


@pytest.mark.asyncio
async def test_dashboard_user_token_yields_verified_owner_principal():
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get(
            "/api/knowledge/_probe_principal",
            params={"token": token_auth.generate_token("local-app")},
        )
        assert resp.status == 200
        body = await resp.json()
        # No app -> the dashboard owner: a STABLE, VERIFIED principal, distinct
        # from the anonymous LOCAL_PRINCIPAL so a future owner binding resolves.
        assert body["authed_app"] in ("", None)
        assert body["is_dashboard_user"] is True
        assert body["is_local_principal"] is False
        assert body["principal_id"].startswith("user:")
        assert body["verified"] is True
        assert body["local_library"] is True


@pytest.mark.asyncio
async def test_app_token_yields_verified_app_principal():
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get(
            "/api/apps/mochi/_probe_principal",
            params={"token": token_auth.generate_token("local-app", app="mochi")},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["authed_app"] == "mochi"
        assert body["is_dashboard_user"] is False
        assert body["principal_id"] == "app:mochi"
        assert body["verified"] is True
        assert body["local_library"] is True
        assert body["is_local_principal"] is False


@pytest.mark.asyncio
async def test_arbitrary_session_header_without_a_token_is_rejected():
    async with TestClient(TestServer(_app())) as client:
        # A caller-supplied X-Session-Key and NO valid token: the middleware
        # denies before the handler runs, so the header can never mint an
        # identity. (This is the security property Root flagged.)
        resp = await client.get(
            "/api/knowledge/_probe_principal",
            headers={"X-Session-Key": "session:attacker-controlled"},
        )
        assert resp.status in (401, 403)


@pytest.mark.asyncio
async def test_post_body_identity_claims_are_inert():
    # A POST body carrying verified/app/user claims must not change the derived
    # principal: the derivation reads the middleware-validated request keys, never
    # the body. Dashboard-user token + injected body -> still the owner principal.
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post(
            "/api/knowledge/_probe_principal",
            params={"token": token_auth.generate_token("local-app")},
            json={"verified": True, "app": "evil-app", "user": "root",
                  "is_dashboard_user": True, "principal_id": "app:evil"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["authed_app"] in ("", None)      # body 'app' ignored
        assert body["principal_id"].startswith("user:")
        assert "evil" not in body["principal_id"]     # body value never echoed
        assert "root" not in body["principal_id"]


@pytest.mark.asyncio
async def test_route_to_resolver_gate_seam_wiring():
    """Handler-seam wiring over a real authenticated route (NOT a production
    closure): real middleware -> _knowledge_query_principal -> the installed
    _knowledge_binding_resolver seam -> the real HybridRetriever gate.

    The resolver here is a STAND-IN (_GrantResolver), and the non-local principal
    is supplied by an installed knowledge_query_principal resolver -- so this
    proves the request path THREADS principal + binding resolver into the gate,
    it does NOT prove the production path with the real W01 store/resolver. That
    end-to-end closure is test_knowledge_acl_w01_integration.py (real
    ControlPlaneBindingResolver + BindingStore), which runs in the merged
    candidate."""
    # Seed a managed item in a real KB.
    store = KnowledgeStore(":memory:")
    src = store.add_source("sp-src", "sharepoint", "sharepoint://Doc")
    item = store.add_item("Doc", "alpha content", "doc", source_id=src)
    store.set_item_acl(
        item, ["sp-mochi"], tenant="ms-A", managed=True, fresh_as_of=0.0,
        resource_ref=ProviderResourceRef(
            provider="sharepoint", account="tenA", resource_id="Doc"),
    )

    def _make_app(resolver, principal_resolver=None):
        app = web.Application()
        app["_kb"] = store
        app["_item"] = item
        if resolver is not None:
            app["knowledge_binding_resolver"] = resolver
        if principal_resolver is not None:
            app["knowledge_query_principal"] = principal_resolver
        app.router.add_get("/api/apps/mochi/gate", _gate_probe)
        app.middlewares.insert(0, token_auth.token_auth_middleware())
        return app

    tok = token_auth.generate_token("local-app", app="mochi")

    # A shared/multi-tenant deployment installs a query-principal resolver that
    # returns a NON-local_library verified principal (the on-host personal
    # library keeps local_library=True, under which managed items are always
    # denied -- so a managed-item closure models the shared case). verified=True
    # so the W01 trust predicate would vouch for it.
    from kiro_crew.knowledge.acl import QueryPrincipal
    shared_principal = QueryPrincipal(principal_id="user:mochi", verified=True)

    # (1) Resolver holds a binding for (sharepoint, tenA) -> visible.
    resolver = _GrantResolver({("sharepoint", "tenA"): ("sp-mochi", "ms-A")})
    app = _make_app(resolver, principal_resolver=lambda r: shared_principal)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/apps/mochi/gate", params={"token": tok})
        assert resp.status == 200
        assert (await resp.json())["visible"] is True

    # (2) No matching binding installed -> denied (fail-closed).
    app = _make_app(_GrantResolver({}), principal_resolver=lambda r: shared_principal)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/apps/mochi/gate", params={"token": tok})
        assert resp.status == 200
        assert (await resp.json())["visible"] is False

    store.close()
