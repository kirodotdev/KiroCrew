"""Query-time ACL enforcement in the shared Knowledge Library retriever.

Every leg -- keyword, vector, the deliberately-unfiltered graph leg, RRF fusion,
the protected keyword rescue, and the citation/location enrichment passes --
must only ever emit items the querying identity is allowed to see. The tests
below drive the REAL ``HybridRetriever.search`` over a REAL ``KnowledgeStore``
(the legs are stubbed only where an exact ranking is needed; the ACL gate,
fusion, store reads and grant table are all production code), covering the
negative cases the connector production stack's shared contracts require:

* two distinct identities (A sees, B does not) -- ACL-PUBLISH-MATRIX
* revoke-then-query denies immediately, no re-crawl -- ACL-02
* a cached-then-revoked item is not re-served (acl_version bump) -- ACL-03
* same bare subject id in a DIFFERENT tenant is a different identity -- ACL-06
* same bare subject id owning a different source's item is still gated
* an unreadable/absent grant fails CLOSED, never open -- ACL-04
* the unfiltered graph leg cannot leak an unauthorised item
* backward-compat: no access_context => local single-user library sees all
"""

from __future__ import annotations

import pytest

from kiro_crew.knowledge.acl import (
    ALLOW_ALL,
    PUBLIC_SUBJECT,
    PUBLIC_TENANT,
    AccessContext,
    ItemGrant,
    SubjectTenantAclPolicy,
    UNREADABLE_GRANT,
)
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore

_MIN = 0.012  # mirrors the tool-surface confidence floor


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "acl.db"))
    yield s
    s.close()


def _retriever(store, kw=None, gr=None, vec=None) -> HybridRetriever:
    """A retriever whose three legs return fixed ``[(item_id, rank)]`` lists.

    Only the ranking is stubbed; the ACL gate, store grant reads and fusion are
    the real code under test.
    """
    r = HybridRetriever(store)
    r._keyword_search = lambda *a, **k: list(kw or [])  # type: ignore[method-assign]
    r._graph_search = lambda *a, **k: list(gr or [])  # type: ignore[method-assign]
    r._vector_search = lambda *a, **k: (None if vec is None else list(vec))  # type: ignore[method-assign]
    return r


def _ids(results):
    return {r["id"] for r in results}


# --------------------------------------------------------------------------
# Policy unit tests (pure decision, no store)
# --------------------------------------------------------------------------

def test_policy_denies_missing_and_unreadable_grant():
    pol = SubjectTenantAclPolicy()
    ctx = AccessContext(subject="alice", tenant="t1")
    assert pol.allows(ctx, UNREADABLE_GRANT) is False
    # A grant with an empty subject set (revoked) denies too.
    assert pol.allows(ctx, ItemGrant(subjects=frozenset(), tenant="t1")) is False


def test_policy_tenant_mismatch_denies_even_with_matching_subject():
    pol = SubjectTenantAclPolicy()
    ctx = AccessContext(subject="alice", tenant="t1")
    grant = ItemGrant(subjects=frozenset({"alice"}), tenant="t2")
    assert pol.allows(ctx, grant) is False


def test_policy_public_subject_and_public_tenant():
    pol = SubjectTenantAclPolicy()
    ctx = AccessContext(subject="alice", tenant="t1")
    # public subject within same tenant
    assert pol.allows(ctx, ItemGrant(subjects=frozenset({PUBLIC_SUBJECT}), tenant="t1"))
    # public tenant + public subject: visible cross-tenant
    assert pol.allows(
        ctx, ItemGrant(subjects=frozenset({PUBLIC_SUBJECT}), tenant=PUBLIC_TENANT)
    )


def test_policy_group_membership_satisfies_subject_test():
    pol = SubjectTenantAclPolicy()
    ctx = AccessContext(subject="alice", tenant="t1", groups=frozenset({"team-x"}))
    assert pol.allows(ctx, ItemGrant(subjects=frozenset({"team-x"}), tenant="t1"))


def test_bypass_context_allows_everything():
    pol = SubjectTenantAclPolicy()
    assert pol.allows(ALLOW_ALL, UNREADABLE_GRANT) is True


def test_enforcing_context_requires_subject():
    with pytest.raises(ValueError):
        AccessContext(subject="", tenant="t1")


# --------------------------------------------------------------------------
# End-to-end retrieval-chain tests (real store + real gate)
# --------------------------------------------------------------------------

def test_two_identities_A_sees_B_does_not(store):
    """A's private item is invisible to B across title/snippet/content/citation."""
    item = store.add_item("Budget Plan", "Q3 budget figures for planning", "doc")
    store.set_item_acl(item, ["alice"], tenant="t1")
    r = _retriever(store, kw=[(item, 1)])

    alice = AccessContext(subject="alice", tenant="t1")
    bob = AccessContext(subject="bob", tenant="t1")

    a_res = r.search("budget planning", limit=5, access_context=alice)
    b_res = r.search("budget planning", limit=5, access_context=bob)

    assert item in _ids(a_res)
    assert item not in _ids(b_res)
    # B gets nothing at all -- not a redacted row, not a title.
    assert b_res == []


def test_missing_grant_fails_closed(store):
    """An item that was ingested without any ACL grant is denied to everyone."""
    item = store.add_item("Orphan", "no grant written for this one", "doc")
    r = _retriever(store, kw=[(item, 1)])
    ctx = AccessContext(subject="alice", tenant="t1")
    assert r.search("orphan", limit=5, access_context=ctx) == []
    # ...but the local single-user library (bypass) still sees it.
    assert item in _ids(r.search("orphan", limit=5, access_context=ALLOW_ALL))


def test_revoke_then_query_denies_immediately(store):
    """After revoke, the very next query denies -- no re-crawl (ACL-02)."""
    item = store.add_item("Shared Doc", "shared content here", "doc")
    store.set_item_acl(item, ["alice"], tenant="t1")
    r = _retriever(store, kw=[(item, 1)])
    ctx = AccessContext(subject="alice", tenant="t1")

    assert item in _ids(r.search("shared", limit=5, access_context=ctx))
    store.revoke_item_acl(item)
    assert r.search("shared", limit=5, access_context=ctx) == []


def test_revoke_bumps_acl_version(store):
    """Revocation bumps acl_version so any version-keyed cache is invalidated."""
    item = store.add_item("Doc", "content", "doc")
    v1 = store.set_item_acl(item, ["alice"], tenant="t1")
    v2 = store.revoke_item_acl(item)
    assert v2 > v1
    grants = store.get_item_grants([item])
    assert grants[item]["acl_version"] == v2
    assert grants[item]["subjects"] == "[]"


def test_same_email_different_tenant_is_different_identity(store):
    """Same bare subject id in another tenant does not match the grant (ACL-06)."""
    item = store.add_item("Tenant1 Doc", "tenant one content", "doc")
    store.set_item_acl(item, ["alice@x.com"], tenant="tenant-1")
    r = _retriever(store, kw=[(item, 1)])

    same_tenant = AccessContext(subject="alice@x.com", tenant="tenant-1")
    other_tenant = AccessContext(subject="alice@x.com", tenant="tenant-2")

    assert item in _ids(r.search("tenant", limit=5, access_context=same_tenant))
    assert r.search("tenant", limit=5, access_context=other_tenant) == []


def test_same_bare_id_different_source_still_gated(store):
    """Two items with the same subject id but different sources are each gated
    by their OWN grant, not merged."""
    src_a = store.add_source("A", "local_folder", "file:///a")
    src_b = store.add_source("B", "local_folder", "file:///b")
    item_a = store.add_item("DocA", "alpha content shared", "doc", source_id=src_a)
    item_b = store.add_item("DocB", "beta content shared", "doc", source_id=src_b)
    store.set_item_acl(item_a, ["alice"], tenant="t1")
    store.set_item_acl(item_b, ["bob"], tenant="t1")
    r = _retriever(store, kw=[(item_a, 1), (item_b, 2)])

    alice = AccessContext(subject="alice", tenant="t1")
    res = r.search("content shared", limit=5, access_context=alice)
    assert item_a in _ids(res)
    assert item_b not in _ids(res)


def test_graph_leg_cannot_leak_unauthorised_item(store):
    """The deliberately-unfiltered graph leg is still ACL-gated in fusion."""
    visible = store.add_item("Visible", "authorised content", "doc")
    hidden = store.add_item("Hidden", "secret graph-only content", "doc")
    store.set_item_acl(visible, ["alice"], tenant="t1")
    store.set_item_acl(hidden, ["carol"], tenant="t1")
    # hidden reaches fusion ONLY via the graph leg (not keyword/vector).
    r = _retriever(store, kw=[(visible, 1)], gr=[(hidden, 1)])
    alice = AccessContext(subject="alice", tenant="t1")
    res = r.search("content", limit=5, access_context=alice)
    assert visible in _ids(res)
    assert hidden not in _ids(res)


def test_keyword_rescue_respects_acl(store):
    """The protected top-keyword rescue never resurrects a denied item."""
    denied = store.add_item("Exact", "ORA-01555 rare token", "doc")
    store.set_item_acl(denied, ["carol"], tenant="t1")
    # The denied item is the keyword top hit that the rescue would normally
    # protect from truncation.
    r = _retriever(store, kw=[(denied, 1)])
    alice = AccessContext(subject="alice", tenant="t1")
    assert r.search("ORA-01555", limit=1, access_context=alice) == []


def test_public_item_visible_to_all_authenticated_subjects(store):
    """A grant with the public sentinel is visible to any subject in tenant."""
    item = store.add_item("Public", "world readable content", "doc")
    store.set_item_acl(item, [PUBLIC_SUBJECT], tenant="t1")
    r = _retriever(store, kw=[(item, 1)])
    for who in ("alice", "bob", "carol"):
        ctx = AccessContext(subject=who, tenant="t1")
        assert item in _ids(r.search("content", limit=5, access_context=ctx))


def test_no_context_defaults_to_bypass_for_local_library(store):
    """Backward compat: omitting access_context => single-user library sees all."""
    item = store.add_item("Local", "personal library content", "doc")
    store.set_item_acl(item, ["someone-else"], tenant="t-other")
    r = _retriever(store, kw=[(item, 1)])
    # No access_context passed at all.
    assert item in _ids(r.search("content", limit=5))


def test_unreadable_grant_row_fails_closed(store):
    """A grant row whose subjects JSON is corrupt is denied (fail-closed)."""
    item = store.add_item("Corrupt", "content with broken acl", "doc")
    store.set_item_acl(item, ["alice"], tenant="t1")
    # Corrupt the stored JSON directly, simulating a damaged/partial write.
    store.db.execute("UPDATE item_acl SET subjects = ? WHERE item_id = ?",
                     ("{not-json", item))
    store.db.commit()
    r = _retriever(store, kw=[(item, 1)])
    ctx = AccessContext(subject="alice", tenant="t1")
    assert r.search("content", limit=5, access_context=ctx) == []


def test_deleting_item_removes_its_grant(store):
    """An item's ACL row does not outlive the item (KB-10 consistency)."""
    item = store.add_item("Doomed", "content", "doc")
    store.set_item_acl(item, ["alice"], tenant="t1")
    assert store.get_item_grants([item])
    store.delete_item(item)
    assert store.get_item_grants([item]) == {}
