"""Query-time ACL enforcement in the shared Knowledge Library retriever.

These tests drive the REAL ``HybridRetriever.search`` over a REAL
``KnowledgeStore`` (only the ranking legs are stubbed where an exact fusion
order matters; the ACL gate, source-type classification, grant table, freshness
check and revalidation hook are all production code). They cover the two
root-cause fixes Root required after re-reading an earlier version:

ROOT CAUSE 1 -- the bypass is ITEM-SCOPED, not call-surface-scoped. One store
mixes trusted-local material with MANAGED cloud/structured items. The local
single-user library sees trusted-local items without a grant but is DENIED
managed items (it has no verifiable provider-mapped subject). A managed item is
only ever visible to a real subject whose grant matches AND is fresh.

ROOT CAUSE 2 -- a static ingest grant is not proof the provider still grants
access. A managed grant must be revalidated: with no revalidation hook the
outcome is UNVERIFIABLE and a stale/never-revalidated managed grant is denied;
a hook returning REVOKED denies even a subject-matching grant; a hook returning
FRESH (or a within-window ``fresh_as_of``) allows.

Contracts referenced: KB-01, ACL-02, ACL-03, ACL-04, ACL-05, ACL-06, ACL-09.
"""

from __future__ import annotations

import time

import pytest

from kiro_crew.knowledge.acl import (
    DEFAULT_STALENESS_SECS,
    LOCAL_LIBRARY,
    PUBLIC_SUBJECT,
    PUBLIC_TENANT,
    AccessContext,
    ItemGrant,
    RevalidationOutcome,
    SubjectTenantAclPolicy,
    UNREADABLE_GRANT,
    is_managed_source_type,
)
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "acl.db"))
    yield s
    s.close()


def _retriever(store, kw=None, gr=None, vec=None, revalidator=None) -> HybridRetriever:
    r = HybridRetriever(store, revalidator=revalidator)
    r._keyword_search = lambda *a, **k: list(kw or [])  # type: ignore[method-assign]
    r._graph_search = lambda *a, **k: list(gr or [])  # type: ignore[method-assign]
    r._vector_search = lambda *a, **k: (None if vec is None else list(vec))  # type: ignore[method-assign]
    return r


def _ids(results):
    return {r["id"] for r in results}


def _local_source(store, name="Vault", uri="file:///vault"):
    """A trusted-local (local_folder) source id."""
    return store.add_source(name, "local_folder", uri)


def _managed_source(store, name="SP", uri="sharepoint://site", stype="sharepoint"):
    """A managed cloud/structured source id."""
    return store.add_source(name, stype, uri)


def _fresh(offset=0.0):
    return time.time() + offset


class _StubRevalidator:
    """A revalidation hook returning a fixed outcome (or per-item mapping)."""

    def __init__(self, outcome=RevalidationOutcome.FRESH, per_item=None, raises=False):
        self.outcome = outcome
        self.per_item = per_item or {}
        self.raises = raises
        self.calls: list[str] = []

    def revalidate(self, ctx, item_id, grant):
        self.calls.append(item_id)
        if self.raises:
            raise RuntimeError("provider probe failed")
        return self.per_item.get(item_id, self.outcome)


# --------------------------------------------------------------------------
# source-type classifier
# --------------------------------------------------------------------------

def test_trusted_local_types_are_not_managed():
    for t in ("local_folder", "obsidian_vault", "quip", "agent", "artifact",
              "local_file", "url", "doc"):
        assert is_managed_source_type(t) is False


def test_cloud_types_are_managed():
    for t in ("sharepoint", "onedrive", "salesforce", "github_structured",
              "gmail", "google_drive", "zoom", "slack", "asana", "teams"):
        assert is_managed_source_type(t) is True


def test_unknown_and_none_type_not_managed_by_type_alone():
    # An unknown/None TYPE is not managed by type -- the grant's managed flag and
    # the retriever's dangling-source handling (has_source + missing row) carry
    # the fail-closed cases, not the type predicate in isolation.
    assert is_managed_source_type(None) is False
    assert is_managed_source_type("???") is False


# --------------------------------------------------------------------------
# policy unit tests
# --------------------------------------------------------------------------

def test_policy_denies_unreadable_grant():
    pol = SubjectTenantAclPolicy()
    assert pol.allows(AccessContext(subject="a", tenant="t"), UNREADABLE_GRANT) is False


def test_policy_local_item_allowed_for_bypass():
    pol = SubjectTenantAclPolicy()
    g = ItemGrant(subjects=frozenset(), tenant="t", managed=False)
    assert pol.allows(LOCAL_LIBRARY, g) is True


def test_policy_managed_item_denied_for_bypass():
    """A managed item is never visible to the no-identity local context."""
    pol = SubjectTenantAclPolicy()
    g = ItemGrant(subjects=frozenset({PUBLIC_SUBJECT}), tenant="t", managed=True,
                  fresh_as_of=_fresh())
    assert pol.allows(LOCAL_LIBRARY, g) is False


def test_policy_managed_stale_denied_without_revalidation():
    """A managed grant older than the window with no fresh hook answer is denied."""
    pol = SubjectTenantAclPolicy()
    ctx = AccessContext(subject="alice", tenant="t")
    stale = ItemGrant(subjects=frozenset({"alice"}), tenant="t", managed=True,
                      fresh_as_of=_fresh(-DEFAULT_STALENESS_SECS - 10))
    assert pol.allows(ctx, stale, revalidation=RevalidationOutcome.UNVERIFIABLE) is False
    # ...but within the window it is allowed.
    fresh = ItemGrant(subjects=frozenset({"alice"}), tenant="t", managed=True,
                      fresh_as_of=_fresh(-10))
    assert pol.allows(ctx, fresh, revalidation=RevalidationOutcome.UNVERIFIABLE) is True


def test_policy_managed_revoked_by_hook_denied_even_if_subject_matches():
    pol = SubjectTenantAclPolicy()
    ctx = AccessContext(subject="alice", tenant="t")
    g = ItemGrant(subjects=frozenset({"alice"}), tenant="t", managed=True, fresh_as_of=_fresh())
    assert pol.allows(ctx, g, revalidation=RevalidationOutcome.REVOKED) is False
    assert pol.allows(ctx, g, revalidation=RevalidationOutcome.FRESH) is True


def test_policy_never_revalidated_managed_grant_denied():
    """fresh_as_of == 0 (ingest-time only) is stale for a managed item."""
    pol = SubjectTenantAclPolicy()
    ctx = AccessContext(subject="alice", tenant="t")
    g = ItemGrant(subjects=frozenset({"alice"}), tenant="t", managed=True, fresh_as_of=0.0)
    assert pol.allows(ctx, g, revalidation=RevalidationOutcome.UNVERIFIABLE) is False


def test_policy_tenant_mismatch_denies():
    pol = SubjectTenantAclPolicy()
    ctx = AccessContext(subject="alice", tenant="t1")
    g = ItemGrant(subjects=frozenset({"alice"}), tenant="t2", managed=False)
    assert pol.allows(ctx, g) is False


def test_enforcing_context_requires_subject():
    with pytest.raises(ValueError):
        AccessContext(subject="", tenant="t1")


# --------------------------------------------------------------------------
# MIXED-LIBRARY end-to-end: trusted-local + managed in ONE store
# --------------------------------------------------------------------------

def test_mixed_library_local_readable_managed_gated(store):
    """The regression Root required: one store, a local doc + a managed cloud doc.

    Local doc: readable by the local library (no grant needed) AND by any
    enforcing subject via its own grant. Managed cloud doc: invisible to the
    local library and to a non-granted subject; visible only to its granted
    subject when fresh."""
    local_src = _local_source(store)
    cloud_src = _managed_source(store)
    local_item = store.add_item("Vault Note", "alpha local content", "doc",
                                source_id=local_src)
    cloud_item = store.add_item("SP Doc", "alpha cloud content", "doc",
                                source_id=cloud_src)
    # cloud item's grant, freshly revalidated, for alice@acme
    store.set_item_acl(cloud_item, ["alice"], tenant="acme",
                       managed=True, fresh_as_of=_fresh())

    kw = [(local_item, 1), (cloud_item, 2)]

    # 1) Local single-user library: sees the local doc, NOT the managed cloud doc.
    r = _retriever(store, kw=kw)
    local_lib = r.search("alpha content", limit=10, access_context=LOCAL_LIBRARY)
    assert local_item in _ids(local_lib)
    assert cloud_item not in _ids(local_lib)

    # 2) alice@acme (granted on the cloud doc): sees the cloud doc. The local doc
    #    has no grant, so an enforcing subject does NOT see it (only the local
    #    library sees ungranted local material) -- managed enforcement does not
    #    accidentally widen ungranted local items either.
    r2 = _retriever(store, kw=kw)
    alice = AccessContext(subject="alice", tenant="acme")
    a_res = r2.search("alpha content", limit=10, access_context=alice)
    assert cloud_item in _ids(a_res)
    assert local_item not in _ids(a_res)

    # 3) bob@acme (not granted): sees neither.
    r3 = _retriever(store, kw=kw)
    bob = AccessContext(subject="bob", tenant="acme")
    assert r3.search("alpha content", limit=10, access_context=bob) == []


def test_mixed_library_shared_local_grant_seen_by_subject(store):
    """A local item that DOES carry a (non-managed) grant is honoured for a
    matching enforcing subject -- local material can be shared too."""
    local_src = _local_source(store)
    item = store.add_item("Shared Vault", "beta content", "doc", source_id=local_src)
    store.set_item_acl(item, ["alice"], tenant="acme", managed=False)
    r = _retriever(store, kw=[(item, 1)])
    alice = AccessContext(subject="alice", tenant="acme")
    bob = AccessContext(subject="bob", tenant="acme")
    assert item in _ids(r.search("beta content", limit=10, access_context=alice))
    assert item not in _ids(r.search("beta content", limit=10, access_context=bob))
    # and the local library still sees it (trusted-local)
    assert item in _ids(r.search("beta content", limit=10, access_context=LOCAL_LIBRARY))


def test_managed_item_no_grant_denied_to_everyone(store):
    """A managed cloud item ingested without a grant is denied to all (fail-closed)."""
    cloud_src = _managed_source(store)
    item = store.add_item("Orphan Cloud", "gamma content", "doc", source_id=cloud_src)
    r = _retriever(store, kw=[(item, 1)])
    assert r.search("gamma", limit=10, access_context=LOCAL_LIBRARY) == []
    assert r.search("gamma", limit=10,
                    access_context=AccessContext(subject="alice", tenant="acme")) == []


def test_dangling_source_item_fails_closed(store):
    """An item whose source row has vanished (dangling source_id) is managed
    (fail-closed): we cannot prove it is trusted-local, so the local library
    does not see it without a grant."""
    src = _local_source(store, name="Gone", uri="file:///gone")
    item = store.add_item("Dangling", "delta content", "doc", source_id=src)
    # Drop the source row directly, leaving the item pointing at a missing source.
    store.db.execute("PRAGMA foreign_keys = OFF")
    store.db.execute("DELETE FROM sources WHERE id = ?", (src,))
    store.db.commit()
    r = _retriever(store, kw=[(item, 1)])
    assert r.search("delta", limit=10, access_context=LOCAL_LIBRARY) == []


def test_sourceless_item_is_trusted_local(store):
    """An item with NO source_id is trusted-local: the local library sees it."""
    item = store.add_item("Pasted", "epsilon content", "doc")  # source_id=None
    r = _retriever(store, kw=[(item, 1)])
    assert item in _ids(r.search("epsilon", limit=10, access_context=LOCAL_LIBRARY))


# --------------------------------------------------------------------------
# Two-identity / tenant / source (real chain)
# --------------------------------------------------------------------------

def test_two_identities_cloud_A_sees_B_does_not(store):
    cloud_src = _managed_source(store)
    item = store.add_item("Budget", "q3 budget alpha", "doc", source_id=cloud_src)
    store.set_item_acl(item, ["alice"], tenant="acme", managed=True, fresh_as_of=_fresh())
    r = _retriever(store, kw=[(item, 1)])
    assert item in _ids(r.search("budget alpha", limit=5,
                                 access_context=AccessContext(subject="alice", tenant="acme")))
    assert r.search("budget alpha", limit=5,
                    access_context=AccessContext(subject="bob", tenant="acme")) == []


def test_same_email_different_tenant_is_different_identity(store):
    cloud_src = _managed_source(store)
    item = store.add_item("T1 Doc", "tenant content", "doc", source_id=cloud_src)
    store.set_item_acl(item, ["alice@x.com"], tenant="tenant-1",
                       managed=True, fresh_as_of=_fresh())
    r = _retriever(store, kw=[(item, 1)])
    assert item in _ids(r.search("tenant", limit=5,
                        access_context=AccessContext(subject="alice@x.com", tenant="tenant-1")))
    assert r.search("tenant", limit=5,
                    access_context=AccessContext(subject="alice@x.com", tenant="tenant-2")) == []


def test_graph_leg_cannot_leak_managed_item(store):
    """The unfiltered graph leg is still ACL-gated for a managed hit."""
    local_src = _local_source(store)
    cloud_src = _managed_source(store)
    visible = store.add_item("Visible", "authorised content", "doc", source_id=local_src)
    hidden = store.add_item("Hidden", "secret graph-only content", "doc", source_id=cloud_src)
    store.set_item_acl(hidden, ["carol"], tenant="acme", managed=True, fresh_as_of=_fresh())
    # hidden reaches fusion ONLY via the graph leg
    r = _retriever(store, kw=[(visible, 1)], gr=[(hidden, 1)])
    alice = AccessContext(subject="alice", tenant="acme")
    res = r.search("content", limit=5, access_context=alice)
    assert hidden not in _ids(res)


# --------------------------------------------------------------------------
# Revocation + revalidation chain (real chain)
# --------------------------------------------------------------------------

def test_local_revoke_then_query_denies_immediately(store):
    """revoke_item_acl on a managed item: next query denies, no re-crawl (ACL-02)."""
    cloud_src = _managed_source(store)
    item = store.add_item("Shared", "shared content", "doc", source_id=cloud_src)
    store.set_item_acl(item, ["alice"], tenant="acme", managed=True, fresh_as_of=_fresh())
    r = _retriever(store, kw=[(item, 1)])
    alice = AccessContext(subject="alice", tenant="acme")
    assert item in _ids(r.search("shared", limit=5, access_context=alice))
    store.revoke_item_acl(item)
    assert r.search("shared", limit=5, access_context=alice) == []


def test_provider_revocation_observed_via_hook(store):
    """A provider revocation the local grant has NOT yet seen is caught by the
    revalidation hook returning REVOKED -- the second root-cause fix.

    The stored grant still lists alice and is within its freshness window, so
    WITHOUT revalidation it would (wrongly) be served; the hook observing the
    provider's revocation is what denies it."""
    cloud_src = _managed_source(store)
    item = store.add_item("Cloud", "revalidate content", "doc", source_id=cloud_src)
    store.set_item_acl(item, ["alice"], tenant="acme", managed=True, fresh_as_of=_fresh())
    alice = AccessContext(subject="alice", tenant="acme")

    # No hook: within-window stored grant is served.
    r_nohook = _retriever(store, kw=[(item, 1)])
    assert item in _ids(r_nohook.search("revalidate", limit=5, access_context=alice))

    # Hook observes the provider revoked access -> denied despite fresh stored grant.
    revoker = _StubRevalidator(outcome=RevalidationOutcome.REVOKED)
    r_hook = _retriever(store, kw=[(item, 1)], revalidator=revoker)
    assert r_hook.search("revalidate", limit=5, access_context=alice) == []
    assert item in revoker.calls  # the managed item WAS revalidated


def test_stale_managed_grant_denied_without_hook(store):
    """A managed grant past the staleness window with no hook is denied
    (static grant never refreshed != live ACL)."""
    cloud_src = _managed_source(store)
    item = store.add_item("Old", "stale content", "doc", source_id=cloud_src)
    store.set_item_acl(item, ["alice"], tenant="acme", managed=True,
                       fresh_as_of=_fresh(-DEFAULT_STALENESS_SECS - 60))
    r = _retriever(store, kw=[(item, 1)])
    alice = AccessContext(subject="alice", tenant="acme")
    assert r.search("stale", limit=5, access_context=alice) == []
    # A hook returning FRESH rescues it.
    r2 = _retriever(store, kw=[(item, 1)], revalidator=_StubRevalidator(RevalidationOutcome.FRESH))
    assert item in _ids(r2.search("stale", limit=5, access_context=alice))


def test_revalidation_hook_error_fails_closed(store):
    """A hook that raises is treated as unverifiable -> managed item denied."""
    cloud_src = _managed_source(store)
    item = store.add_item("Cloud", "boom content", "doc", source_id=cloud_src)
    store.set_item_acl(item, ["alice"], tenant="acme", managed=True, fresh_as_of=_fresh())
    r = _retriever(store, kw=[(item, 1)], revalidator=_StubRevalidator(raises=True))
    alice = AccessContext(subject="alice", tenant="acme")
    assert r.search("boom", limit=5, access_context=alice) == []


def test_mark_revalidated_refreshes_and_bumps_version(store):
    """The store's revalidation-record method stamps freshness and bumps version."""
    cloud_src = _managed_source(store)
    item = store.add_item("Cloud", "content", "doc", source_id=cloud_src)
    v1 = store.set_item_acl(item, ["alice"], tenant="acme", managed=True, fresh_as_of=0.0)
    # Never-revalidated managed grant: denied.
    r = _retriever(store, kw=[(item, 1)])
    alice = AccessContext(subject="alice", tenant="acme")
    assert r.search("content", limit=5, access_context=alice) == []
    # Provider confirms current access -> record it.
    v2 = store.mark_item_acl_revalidated(item, ["alice"], fresh_as_of=_fresh())
    assert v2 > v1
    assert item in _ids(r.search("content", limit=5, access_context=alice))
    # Provider observes revocation -> record empty subjects.
    store.mark_item_acl_revalidated(item, [], fresh_as_of=_fresh())
    assert r.search("content", limit=5, access_context=alice) == []


# --------------------------------------------------------------------------
# public + backward-compat + deletion cascade
# --------------------------------------------------------------------------

def test_public_managed_item_visible_to_tenant_subjects_when_fresh(store):
    cloud_src = _managed_source(store)
    item = store.add_item("Public", "world content", "doc", source_id=cloud_src)
    store.set_item_acl(item, [PUBLIC_SUBJECT], tenant="acme", managed=True, fresh_as_of=_fresh())
    r = _retriever(store, kw=[(item, 1)])
    for who in ("alice", "bob"):
        assert item in _ids(r.search("content", limit=5,
                            access_context=AccessContext(subject=who, tenant="acme")))
    # cross-tenant public
    store.set_item_acl(item, [PUBLIC_SUBJECT], tenant=PUBLIC_TENANT,
                       managed=True, fresh_as_of=_fresh())
    r2 = _retriever(store, kw=[(item, 1)])
    assert item in _ids(r2.search("content", limit=5,
                        access_context=AccessContext(subject="z", tenant="other")))


def test_no_context_defaults_to_local_library(store):
    """Omitting access_context => local library: trusted-local visible, managed not."""
    local_src = _local_source(store)
    cloud_src = _managed_source(store)
    local_item = store.add_item("Local", "personal content", "doc", source_id=local_src)
    cloud_item = store.add_item("Cloud", "managed content", "doc", source_id=cloud_src)
    store.set_item_acl(cloud_item, ["someone"], tenant="t", managed=True, fresh_as_of=_fresh())
    r = _retriever(store, kw=[(local_item, 1), (cloud_item, 2)])
    res = r.search("content", limit=5)  # no access_context
    assert local_item in _ids(res)
    assert cloud_item not in _ids(res)


def test_unreadable_grant_row_fails_closed(store):
    cloud_src = _managed_source(store)
    item = store.add_item("Corrupt", "content", "doc", source_id=cloud_src)
    store.set_item_acl(item, ["alice"], tenant="acme", managed=True, fresh_as_of=_fresh())
    store.db.execute("UPDATE item_acl SET subjects = ? WHERE item_id = ?", ("{bad", item))
    store.db.commit()
    r = _retriever(store, kw=[(item, 1)])
    assert r.search("content", limit=5,
                    access_context=AccessContext(subject="alice", tenant="acme")) == []


def test_deleting_item_removes_its_grant(store):
    cloud_src = _managed_source(store)
    item = store.add_item("Doomed", "content", "doc", source_id=cloud_src)
    store.set_item_acl(item, ["alice"], tenant="acme", managed=True, fresh_as_of=_fresh())
    assert store.get_item_grants([item])
    store.delete_item(item)
    assert store.get_item_grants([item]) == {}
