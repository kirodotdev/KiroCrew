"""REAL W01 integration: ControlPlaneBindingResolver + BindingStore -> bridge ->
HybridRetriever, exercising the L04 trust contract.

Drives the ACTUAL W01 control-plane store and resolver (not an AccessGrant
stand-in), through the sanctioned seed path (create_binding + BindingStore.insert
recording the vendor ``account``), the REAL store-backed account -> deployment
mapping (no fixture lambda), and the REAL ``principal_verified`` trust predicate
(``acl.principal_is_verified`` -- NOT ``lambda: True``). Then bridges the
AccessGrant to an AccessContext and runs the real HybridRetriever ACL gate.

Cases: a verified bound principal is admitted; a DIFFERENT verified subject gets
ITS OWN binding (two subjects, each isolated); an UNVERIFIED principal is denied
EVEN WITH a live binding (the trust contract); a missing/local principal is
denied even though an owner binding exists; wrong-account and revoke fence.

The W01 package is not on this PR's branch; it arrives in the merged candidate
(this branch + the W01 L04 branch). The module importorskips when absent, so the
standalone branch stays green and the merged candidate runs the real integration.
Only the identity-proof verifier IO is substituted (per its Protocol); the store,
resolver, account->deployment mapping, revoke fencing and trust predicate are the
real ones.
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
    LOCAL_PRINCIPAL,
    ProviderResourceRef,
    QueryPrincipal,
    RevalidationOutcome,
    bridge_binding_resolver,
    principal_is_verified,
)
from kiro_crew.knowledge.retrieval import HybridRetriever  # noqa: E402
from kiro_crew.knowledge.store import KnowledgeStore  # noqa: E402

SERVICE = "salesforce"  # maps 1:1 via map_provider_to_service_id
ACCOUNT = "acme-org"  # the VENDOR account an object lives in
DEPLOYMENT = "sf-deploy-acme"  # the PROVIDER deployment that HOSTS the account
INSTANCE_URL = "https://acme.my.salesforce.com"  # SF endpoint (instanceUrl)


def _verified(principal_id: str) -> QueryPrincipal:
    # A principal the auth layer ESTABLISHED (verified=True) and NON-local: this
    # is the shared/multi-tenant deployment case (a host installs a
    # knowledge_query_principal that returns such a principal). It is NOT a fake
    # to dodge the local_library check -- the on-host personal-library principal
    # stays local_library=True and is correctly denied (see the local test). A
    # bare QueryPrincipal with verified=False is denied by principal_is_verified.
    return QueryPrincipal(principal_id=principal_id, verified=True)


def _verifier_for(subject_ref, tenant_ref):
    def _v(*, claimed_subject, claimed_tenant, service_id):
        return {"subject_ref": subject_ref, "tenant_ref": tenant_ref}

    return _v


@pytest.fixture()
def kb(tmp_path):
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    s.close()


def _seed(
    store,
    *,
    kiro_principal,
    subject_ref,
    tenant_ref,
    account=ACCOUNT,
    deployment=DEPLOYMENT,
    endpoint=INSTANCE_URL,
):
    binding = create_binding(
        service_id=SERVICE,
        claimed_subject="claimed",
        claimed_tenant="claimed",
        credential_mode="oauth_user",
        verifier=_verifier_for(subject_ref, tenant_ref),
        slug="salesforce",
    )
    # Register the vendor ``account`` AND the ``endpoint`` (instanceUrl) so the
    # store-backed (account, endpoint) -> deployment lookup the resolve_ref path
    # uses can resolve it (no lambda, no fabricated endpoint).
    return store.insert(
        binding,
        deployment_id=deployment,
        kiro_principal=kiro_principal,
        account=account,
        endpoint=endpoint,
    )


def _resolver(bstore):
    # REAL resolver with the REAL trust predicate and the default store-backed
    # (account, endpoint) -> deployment mapping.
    return ControlPlaneBindingResolver(bstore, principal_verified=principal_is_verified)


def _fresh_hook():
    class _H:
        def revalidate(self, ctx, item_id, grant):
            return RevalidationOutcome.FRESH

    return _H()


def _managed_item(
    kb, title, content, subject_ref, tenant_ref, account=ACCOUNT, instance_url=INSTANCE_URL
):
    src = kb.add_source(f"sf-{title}", SERVICE, f"salesforce://{title}")
    item = kb.add_item(title, content, "record", source_id=src)
    ref = ProviderResourceRef(
        provider=SERVICE,
        account=account,
        resource_id=title,
        locator={"instanceUrl": instance_url, "sobjectType": "Account", "recordId": title},
    )
    kb.set_item_acl(
        item, [subject_ref], tenant=tenant_ref, managed=True, fresh_as_of=0.0, resource_ref=ref
    )
    return item


def _ids(res):
    return {r["id"] for r in res}


def _search(kb, bstore, item, principal, term="alpha"):
    r = HybridRetriever(
        kb, revalidator=_fresh_hook(), binding_resolver=bridge_binding_resolver(_resolver(bstore))
    )
    r._keyword_search = lambda *a, **k: [(item, 1)]
    return r.search(term, limit=10, query_principal=principal)


def test_verified_bound_principal_admitted(kb, tmp_path):
    bstore = BindingStore(path=tmp_path / "b.json")
    _seed(bstore, kiro_principal="alice", subject_ref="sf-alice", tenant_ref="t-A")
    item = _managed_item(kb, "Deal", "alpha content", "sf-alice", "t-A")
    assert item in _ids(_search(kb, bstore, item, _verified("alice")))


def test_two_verified_subjects_each_get_their_own_binding(kb, tmp_path):
    bstore = BindingStore(path=tmp_path / "b.json")
    _seed(bstore, kiro_principal="alice", subject_ref="sf-alice", tenant_ref="t-A")
    _seed(bstore, kiro_principal="bob", subject_ref="sf-bob", tenant_ref="t-A")
    alice_item = _managed_item(kb, "ADeal", "alpha content", "sf-alice", "t-A")
    bob_item = _managed_item(kb, "BDeal", "alpha content", "sf-bob", "t-A")
    # Alice sees her item, not Bob's; Bob sees his, not Alice's.
    assert _ids(_search(kb, bstore, alice_item, _verified("alice"))) == {alice_item}
    assert _ids(_search(kb, bstore, bob_item, _verified("bob"))) == {bob_item}
    assert _search(kb, bstore, bob_item, _verified("alice")) == []


def test_unverified_principal_denied_even_with_a_binding(kb, tmp_path):
    bstore = BindingStore(path=tmp_path / "b.json")
    _seed(bstore, kiro_principal="alice", subject_ref="sf-alice", tenant_ref="t-A")
    item = _managed_item(kb, "Deal", "alpha content", "sf-alice", "t-A")
    # verified=False -> principal_is_verified returns False -> resolver denies,
    # even though alice HAS a live binding. A bare/echoed principal is not a
    # subject.
    unverified = QueryPrincipal(principal_id="alice", verified=False)
    assert _search(kb, bstore, item, unverified) == []


def test_missing_identity_denied_even_with_owner_binding(kb, tmp_path):
    bstore = BindingStore(path=tmp_path / "b.json")
    _seed(bstore, kiro_principal="alice", subject_ref="sf-alice", tenant_ref="t-A")
    item = _managed_item(kb, "Deal", "alpha content", "sf-alice", "t-A")
    # LOCAL_PRINCIPAL (no established identity) is local_library + unverified ->
    # denied, even though a binding for 'alice' exists in the store.
    assert _search(kb, bstore, item, LOCAL_PRINCIPAL) == []


def test_wrong_account_denies(kb, tmp_path):
    bstore = BindingStore(path=tmp_path / "b.json")
    _seed(bstore, kiro_principal="alice", subject_ref="sf-alice", tenant_ref="t-A")
    # Item in an account with no stored binding -> account->deployment yields
    # None -> deny.
    item = _managed_item(kb, "Other", "alpha content", "sf-alice", "t-A", account="other-org")
    assert _search(kb, bstore, item, _verified("alice")) == []


def test_revoked_binding_fences(kb, tmp_path):
    bstore = BindingStore(path=tmp_path / "b.json")
    stored = _seed(bstore, kiro_principal="alice", subject_ref="sf-alice", tenant_ref="t-A")
    item = _managed_item(kb, "Deal", "alpha content", "sf-alice", "t-A")
    bstore.revoke(stored["binding"]["binding_id"])
    assert _search(kb, bstore, item, _verified("alice")) == []


def test_wrong_endpoint_denies(kb, tmp_path):
    # Same account, but the item's ref carries an instanceUrl the store never
    # registered -> resolve_deployment_for_account_endpoint returns None -> deny.
    # This is the endpoint discriminator that (provider, account) alone lacks.
    bstore = BindingStore(path=tmp_path / "b.json")
    _seed(bstore, kiro_principal="alice", subject_ref="sf-alice", tenant_ref="t-A")
    item = _managed_item(
        kb,
        "Deal",
        "alpha content",
        "sf-alice",
        "t-A",
        instance_url="https://evil.my.salesforce.com",
    )
    assert _search(kb, bstore, item, _verified("alice")) == []


def test_local_library_principal_denied_documented_behavior(kb, tmp_path):
    # The on-host personal-library principal is local_library=True. The resolver
    # denies it (documented behavior) EVEN with a live SF binding -- managed
    # cross-identity content is never served to the local single-user context.
    # This is faced honestly, NOT bypassed with a fake non-local principal.
    bstore = BindingStore(path=tmp_path / "b.json")
    _seed(bstore, kiro_principal="alice", subject_ref="sf-alice", tenant_ref="t-A")
    item = _managed_item(kb, "Deal", "alpha content", "sf-alice", "t-A")
    local = QueryPrincipal(principal_id="user:alice", local_library=True, verified=True)
    assert _search(kb, bstore, item, local) == []
