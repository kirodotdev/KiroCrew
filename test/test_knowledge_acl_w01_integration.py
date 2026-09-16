"""REAL W01 integration: ControlPlaneBindingResolver + BindingStore -> bridge ->
HybridRetriever.

Unlike test_knowledge_query_acl_binding.py (which drives an AccessGrant-shaped
stand-in), this test imports the ACTUAL W01 control-plane store and resolver,
seeds a VERIFIED binding through the sanctioned create_binding + BindingStore
.insert path, resolves it through the real ControlPlaneBindingResolver, bridges
the returned AccessGrant into an AccessContext with acl.bridge_binding_resolver,
and runs the real HybridRetriever ACL gate over it. It also drives revoke
fencing and a wrong-account miss.

The W01 control-plane package is not on this PR's own branch; it arrives only in
the merged candidate (my branch + the W01 L04 branch). So the whole module is
skipped when it is absent -- the standalone branch stays green and the merged
candidate exercises the real integration. No fake store or fake verifier for the
identity itself: create_binding runs a real verifier and the store persists only
its VerifiedIdentity. A local test verifier stands in for the provider-IO
verifier that later leaves supply (per its Protocol: it returns the verified
refs), which is the sanctioned test seam, not a bypass of the store/resolver.
"""

from __future__ import annotations

import pytest

pytest.importorskip("kiro_crew.connections.control_plane.acl_binding_resolver")

from kiro_crew.connections.control_plane.acl_binding_resolver import (  # noqa: E402
    ControlPlaneBindingResolver,
)
from kiro_crew.connections.control_plane.binding import create_binding  # noqa: E402
from kiro_crew.connections.control_plane.lifecycle import BindingStore  # noqa: E402
from kiro_crew.knowledge.acl import (  # noqa: E402
    ProviderResourceRef,
    QueryPrincipal,
    RevalidationOutcome,
    bridge_binding_resolver,
)
from kiro_crew.knowledge.retrieval import HybridRetriever  # noqa: E402
from kiro_crew.knowledge.store import KnowledgeStore  # noqa: E402

SERVICE = "salesforce"        # a provider that map_provider_to_service_id maps 1:1
DEPLOYMENT = "org-acme"       # the vendor-side account/deployment id
KIRO_PRINCIPAL = "alice"      # the authenticated KiroCrew caller id
SUBJECT_REF = "sf-alice"      # the verified provider subject
TENANT_REF = "sf-tenant-A"    # the verified provider tenant


def _verifier(*, claimed_subject, claimed_tenant, service_id):
    # A SubjectTenantVerifier stand-in for the provider-IO verifier later leaves
    # supply: it returns the VerifiedIdentity the store then persists. The store
    # + resolver under test are the REAL ones; only the identity-proof IO is
    # substituted, exactly as the Protocol intends for a test.
    return {"subject_ref": SUBJECT_REF, "tenant_ref": TENANT_REF}


@pytest.fixture()
def kb(tmp_path):
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    s.close()


def _seed_binding(store: BindingStore):
    binding = create_binding(
        service_id=SERVICE,
        claimed_subject="whatever-claimed",
        claimed_tenant="whatever-claimed",
        credential_mode="oauth_user",
        verifier=_verifier,
        slug="salesforce",
    )
    stored = store.insert(binding, deployment_id=DEPLOYMENT, kiro_principal=KIRO_PRINCIPAL)
    return stored


def _fresh_hook():
    class _H:
        def revalidate(self, ctx, item_id, grant):
            return RevalidationOutcome.FRESH
    return _H()


def _managed_item(kb, title, content):
    src = kb.add_source(f"sf-{title}", SERVICE, f"salesforce://{title}")
    item = kb.add_item(title, content, "record", source_id=src)
    ref = ProviderResourceRef(provider=SERVICE, account=DEPLOYMENT, resource_id=title)
    kb.set_item_acl(item, [SUBJECT_REF], tenant=TENANT_REF, managed=True,
                    fresh_as_of=0.0, resource_ref=ref)
    return item


def _ids(res):
    return {r["id"] for r in res}


def test_real_resolver_bridge_admits_bound_principal(kb, tmp_path):
    bstore = BindingStore(path=tmp_path / "bindings.json")
    _seed_binding(bstore)
    item = _managed_item(kb, "Deal", "alpha content")

    resolver = ControlPlaneBindingResolver(bstore)      # REAL resolver
    bridged = bridge_binding_resolver(resolver)          # AccessGrant -> AccessContext
    r = HybridRetriever(kb, revalidator=_fresh_hook(), binding_resolver=bridged)
    r._keyword_search = lambda *a, **k: [(item, 1)]
    res = r.search("alpha", limit=10, query_principal=QueryPrincipal(KIRO_PRINCIPAL))
    assert item in _ids(res)


def test_real_resolver_wrong_account_denies(kb, tmp_path):
    bstore = BindingStore(path=tmp_path / "bindings.json")
    _seed_binding(bstore)                                # bound on org-acme
    # Item lives in a DIFFERENT account: the principal holds no binding there.
    src = kb.add_source("sf-Other", SERVICE, "salesforce://Other")
    item = kb.add_item("Other", "alpha content", "record", source_id=src)
    ref = ProviderResourceRef(provider=SERVICE, account="org-other", resource_id="Other")
    kb.set_item_acl(item, [SUBJECT_REF], tenant=TENANT_REF, managed=True,
                    fresh_as_of=0.0, resource_ref=ref)

    resolver = ControlPlaneBindingResolver(bstore)
    bridged = bridge_binding_resolver(resolver)
    r = HybridRetriever(kb, revalidator=_fresh_hook(), binding_resolver=bridged)
    r._keyword_search = lambda *a, **k: [(item, 1)]
    assert r.search("alpha", limit=10, query_principal=QueryPrincipal(KIRO_PRINCIPAL)) == []


def test_real_resolver_revoked_binding_fences(kb, tmp_path):
    bstore = BindingStore(path=tmp_path / "bindings.json")
    stored = _seed_binding(bstore)
    item = _managed_item(kb, "Deal", "alpha content")

    bstore.revoke(stored["binding"]["binding_id"])       # fence the binding

    resolver = ControlPlaneBindingResolver(bstore)
    bridged = bridge_binding_resolver(resolver)
    r = HybridRetriever(kb, revalidator=_fresh_hook(), binding_resolver=bridged)
    r._keyword_search = lambda *a, **k: [(item, 1)]
    # Revoked -> resolve_for_acl returns None -> gate denies (fail-closed).
    assert r.search("alpha", limit=10, query_principal=QueryPrincipal(KIRO_PRINCIPAL)) == []


def test_real_resolver_unbound_principal_denies(kb, tmp_path):
    bstore = BindingStore(path=tmp_path / "bindings.json")
    _seed_binding(bstore)                                # bound for "alice"
    item = _managed_item(kb, "Deal", "alpha content")

    resolver = ControlPlaneBindingResolver(bstore)
    bridged = bridge_binding_resolver(resolver)
    r = HybridRetriever(kb, revalidator=_fresh_hook(), binding_resolver=bridged)
    r._keyword_search = lambda *a, **k: [(item, 1)]
    # A different principal holds no binding -> denied.
    assert r.search("alpha", limit=10, query_principal=QueryPrincipal("mallory")) == []
