"""Per-candidate provider binding resolution + resource-ref persistence.

Root's decision: one query must NOT resolve a single provider identity for the
whole library. The gate resolves, PER managed candidate, the binding the
authenticated principal holds on THAT candidate's (provider, account); a
principal with a binding for one provider does not thereby see another
provider's items. The ProviderResourceRef that keys this is persisted on the
grant at ingest and survives read/round-trip.
"""

from __future__ import annotations

import time

import pytest

from kiro_crew.knowledge.acl import (
    AccessContext,
    LOCAL_PRINCIPAL,
    ProviderResourceRef,
    QueryPrincipal,
    RevalidationOutcome,
)
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "binding.db"))
    yield s
    s.close()


def _ids(res):
    return {r["id"] for r in res}


def _managed(store, title, content, *, provider, account, subjects, tenant, ref_locator=None):
    src = store.add_source(f"{provider}-src-{title}", provider, f"{provider}://{title}")
    item = store.add_item(title, content, "doc", source_id=src)
    ref = ProviderResourceRef(provider=provider, account=account,
                              resource_id=title, locator=ref_locator or {})
    store.set_item_acl(item, subjects, tenant=tenant, managed=True,
                       fresh_as_of=time.time(), resource_ref=ref)
    return item


class _Resolver:
    """A binding resolver: maps (principal, provider, account) -> AccessContext.

    ``bindings`` is {(provider, account): AccessContext}; a missing pair yields
    None (the principal holds no binding there -> deny)."""

    def __init__(self, bindings, raises=False):
        self.bindings = bindings
        self.raises = raises
        self.calls = []

    def resolve(self, principal, provider, account):
        self.calls.append((principal.principal_id, provider, account))
        if self.raises:
            raise RuntimeError("resolver down")
        return self.bindings.get((provider, account))


def _fresh_hook():
    class _H:
        def revalidate(self, ctx, item_id, grant):
            return RevalidationOutcome.FRESH
    return _H()


def test_resource_ref_persists_and_round_trips(store):
    item = _managed(store, "Doc", "content", provider="sharepoint",
                    account="tenant-A", subjects=["sp-alice"], tenant="acme",
                    ref_locator={"siteId": "s1", "driveItemId": "d1"})
    raw = store.get_item_grants([item])[item]["resource_ref"]
    ref = ProviderResourceRef.from_json(raw)
    assert ref.provider == "sharepoint"
    assert ref.account == "tenant-A"
    assert ref.locator["siteId"] == "s1"
    # export/import carries it
    bundle = store.export_all()
    acls = {a["item_id"]: a for a in bundle["item_acls"]}
    assert ProviderResourceRef.from_json(acls[item]["resource_ref"]).provider == "sharepoint"


def test_per_candidate_binding_only_matching_provider_visible(store):
    """A principal bound on sharepoint/tenant-A sees the SP item, not the
    salesforce item, even in one query -- no single identity for the whole lib."""
    sp = _managed(store, "SPDoc", "alpha shared", provider="sharepoint",
                  account="tenant-A", subjects=["sp-alice"], tenant="ms-tenant-A")
    sf = _managed(store, "SFDoc", "alpha shared", provider="salesforce",
                  account="org-9", subjects=["sf-alice"], tenant="sf-org-9")
    principal = QueryPrincipal(principal_id="alice")
    resolver = _Resolver({
        ("sharepoint", "tenant-A"): AccessContext(subject="sp-alice", tenant="ms-tenant-A"),
        # NOTE: no salesforce binding for this principal.
    })
    r = HybridRetriever(store, revalidator=_fresh_hook(), binding_resolver=resolver)
    r._keyword_search = lambda *a, **k: [(sp, 1), (sf, 2)]
    res = r.search("alpha", limit=10, query_principal=principal)
    assert sp in _ids(res)
    assert sf not in _ids(res)  # no binding for salesforce -> denied, not laundered


def test_binding_for_wrong_account_does_not_leak(store):
    """A binding on tenant-A must not authorise an item in tenant-B of the same
    provider."""
    a = _managed(store, "A", "beta content alpha-doc", provider="sharepoint",
                 account="tenant-A", subjects=["sp-alice"], tenant="ms-A")
    b = _managed(store, "B", "beta content beta-doc", provider="sharepoint",
                 account="tenant-B", subjects=["sp-alice"], tenant="ms-B")
    principal = QueryPrincipal(principal_id="alice")
    resolver = _Resolver({
        ("sharepoint", "tenant-A"): AccessContext(subject="sp-alice", tenant="ms-A"),
    })
    r = HybridRetriever(store, revalidator=_fresh_hook(), binding_resolver=resolver)
    r._keyword_search = lambda *_a, **_k: [(a, 1), (b, 2)]
    res = r.search("beta", limit=10, query_principal=principal)
    assert a in _ids(res)
    assert b not in _ids(res)


def test_managed_without_resource_ref_denied_under_resolver(store):
    """With a resolver wired, a managed item lacking a resource_ref cannot be
    located to resolve a binding -> denied."""
    src = store.add_source("SP", "sharepoint", "sharepoint://x")
    item = store.add_item("NoRef", "gamma content", "doc", source_id=src)
    store.set_item_acl(item, ["sp-alice"], tenant="ms-A", managed=True,
                       fresh_as_of=time.time())  # no resource_ref
    resolver = _Resolver({("sharepoint", ""): AccessContext(subject="sp-alice", tenant="ms-A")})
    r = HybridRetriever(store, revalidator=_fresh_hook(), binding_resolver=resolver)
    r._keyword_search = lambda *a, **k: [(item, 1)]
    assert r.search("gamma", limit=10, query_principal=QueryPrincipal("alice")) == []


def test_resolver_raises_denies_candidate(store):
    item = _managed(store, "Doc", "delta content", provider="sharepoint",
                    account="tenant-A", subjects=["sp-alice"], tenant="ms-A")
    resolver = _Resolver({}, raises=True)
    r = HybridRetriever(store, revalidator=_fresh_hook(), binding_resolver=resolver)
    r._keyword_search = lambda *a, **k: [(item, 1)]
    assert r.search("delta", limit=10, query_principal=QueryPrincipal("alice")) == []


def test_local_principal_denies_managed_but_sees_local(store):
    """The local-library principal sees trusted-local items and no managed item,
    even with a resolver present."""
    local_src = store.add_source("V", "local_folder", "file:///v")
    loc = store.add_item("Loc", "epsilon local", "doc", source_id=local_src)
    mg = _managed(store, "Mg", "epsilon cloud", provider="sharepoint",
                  account="tenant-A", subjects=["sp-alice"], tenant="ms-A")
    resolver = _Resolver({("sharepoint", "tenant-A"): AccessContext(subject="sp-alice", tenant="ms-A")})
    r = HybridRetriever(store, revalidator=_fresh_hook(), binding_resolver=resolver)
    r._keyword_search = lambda *a, **k: [(loc, 1), (mg, 2)]
    res = r.search("epsilon", limit=10, query_principal=LOCAL_PRINCIPAL)
    assert loc in _ids(res)
    assert mg not in _ids(res)
