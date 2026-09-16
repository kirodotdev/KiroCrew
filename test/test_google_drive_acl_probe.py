"""Query-time Google Drive ACL probe tests (no account, scripted subject runner).

Proves the brief's ACL requirements by construction:

* the querying identity is verified LIVE and probed AS itself -- a subject with
  no binding yields UNVERIFIABLE, never an impersonated probe;
* a revocation at the source is observed on the NEXT query (fresh probe every
  query; the grant is rewritten to deny);
* fail-closed on every uncertain shape (no ref, wrong provider, unverifiable).
"""

from __future__ import annotations

from kiro_crew.connections.vendors.google_drive.acl_probe import (
    GRANTED,
    REVOKED,
    UNVERIFIABLE,
    GoogleDriveRevalidationHook,
)
from kiro_crew.knowledge.acl import (
    AccessContext,
    ItemGrant,
    ProviderResourceRef,
    RevalidationOutcome,
)


class FakeStore:
    def __init__(self, resource_ref_json):
        self._ref = resource_ref_json
        self.revoked = []
        self.revalidated = []

    def get_item_grants(self, ids):
        return {i: {"resource_ref": self._ref} for i in ids}

    def revoke_item_acl(self, item_id):
        self.revoked.append(item_id)
        return 99

    def mark_item_acl_revalidated(self, item_id, subjects, *, fresh_as_of, tenant=None):
        self.revalidated.append((item_id, tuple(subjects), fresh_as_of, tenant))
        return 100


class ScriptedRunner:
    """A SubjectOperationRunner: returns queued ProbeOutcomes in order, and
    records the (subject, account, file_id) it was asked to probe -- so a test
    proves it was called AS the querying subject, never someone else."""

    def __init__(self, outcomes, allowed_subjects=None):
        self._outcomes = list(outcomes)
        self._allowed = allowed_subjects  # None = all allowed
        self.calls = []

    def probe_file(self, subject, account, file_id):
        self.calls.append((subject, account, file_id))
        if self._allowed is not None and subject not in self._allowed:
            # No binding for this subject: the host runner returns unverifiable
            # rather than borrowing another identity.
            return UNVERIFIABLE
        return self._outcomes.pop(0)


def _ref(file_id="FID", account="0AShared"):
    return ProviderResourceRef(
        provider="google_drive",
        account=account,
        resource_id=file_id,
        locator={"fileId": file_id, "driveId": account},
    ).to_json()


def _grant(acl_version=1, subjects=("alice@corp.com",)):
    return ItemGrant(
        subjects=frozenset(subjects), tenant="acme", acl_version=acl_version, managed=True
    )


def _ctx(subject="alice@corp.com", tenant="acme"):
    return AccessContext(subject=subject, tenant=tenant)


# --- FRESH -----------------------------------------------------------------


def test_fresh_when_subject_still_has_access():
    store = FakeStore(_ref())
    runner = ScriptedRunner([GRANTED])
    hook = GoogleDriveRevalidationHook(store, runner)
    out = hook.revalidate(_ctx(), "item1", _grant())
    assert out == RevalidationOutcome.FRESH
    # Probed AS alice, on the right file.
    assert runner.calls == [("alice@corp.com", "0AShared", "FID")]
    assert store.revalidated and store.revalidated[0][0] == "item1"


# --- REVOKE effective on the NEXT query ------------------------------------


def test_revocation_denies_on_next_query():
    store = FakeStore(_ref())
    runner = ScriptedRunner([GRANTED, REVOKED])
    hook = GoogleDriveRevalidationHook(store, runner, cache_ttl_secs=0.0)
    assert hook.revalidate(_ctx(), "item1", _grant(acl_version=1)) == RevalidationOutcome.FRESH
    # A later query (version bumped) sees the revocation on the very next probe.
    assert hook.revalidate(_ctx(), "item1", _grant(acl_version=2)) == RevalidationOutcome.REVOKED
    assert "item1" in store.revoked


def test_revoked_outcome_records_deny():
    store = FakeStore(_ref())
    hook = GoogleDriveRevalidationHook(store, ScriptedRunner([REVOKED]))
    assert hook.revalidate(_ctx(), "item1", _grant()) == RevalidationOutcome.REVOKED
    assert "item1" in store.revoked


# --- anti-impersonation ----------------------------------------------------


def test_subject_with_no_binding_is_unverifiable():
    store = FakeStore(_ref())
    # alice holds no binding: the runner returns unverifiable and NEVER probes as
    # anyone else.
    runner = ScriptedRunner([], allowed_subjects={"someone-else@corp.com"})
    hook = GoogleDriveRevalidationHook(store, runner)
    out = hook.revalidate(_ctx(subject="alice@corp.com"), "item1", _grant())
    assert out == RevalidationOutcome.UNVERIFIABLE
    # It DID attempt to probe as alice (and got unverifiable) -- it never
    # substituted another subject.
    assert runner.calls == [("alice@corp.com", "0AShared", "FID")]
    assert store.revoked == [] and store.revalidated == []


def test_bypass_local_context_is_unverifiable_and_never_probed():
    store = FakeStore(_ref())
    runner = ScriptedRunner([])
    hook = GoogleDriveRevalidationHook(store, runner)
    from kiro_crew.knowledge.acl import LOCAL_LIBRARY

    assert hook.revalidate(LOCAL_LIBRARY, "item1", _grant()) == RevalidationOutcome.UNVERIFIABLE
    assert runner.calls == []


# --- fail-closed -----------------------------------------------------------


def test_ref_for_other_provider_is_unverifiable():
    store = FakeStore(ProviderResourceRef(provider="salesforce", resource_id="R").to_json())
    hook = GoogleDriveRevalidationHook(store, ScriptedRunner([]))
    assert hook.revalidate(_ctx(), "item1", _grant()) == RevalidationOutcome.UNVERIFIABLE


def test_missing_resource_ref_is_unverifiable():
    store = FakeStore(None)
    hook = GoogleDriveRevalidationHook(store, ScriptedRunner([]))
    assert hook.revalidate(_ctx(), "item1", _grant()) == RevalidationOutcome.UNVERIFIABLE


def test_unverifiable_does_not_rewrite_grant():
    store = FakeStore(_ref())
    hook = GoogleDriveRevalidationHook(store, ScriptedRunner([UNVERIFIABLE]))
    assert hook.revalidate(_ctx(), "item1", _grant()) == RevalidationOutcome.UNVERIFIABLE
    assert store.revoked == [] and store.revalidated == []


def test_runner_raising_is_unverifiable():
    store = FakeStore(_ref())

    class Boom:
        def probe_file(self, *a):
            raise RuntimeError("down")

    hook = GoogleDriveRevalidationHook(store, Boom())
    assert hook.revalidate(_ctx(), "item1", _grant()) == RevalidationOutcome.UNVERIFIABLE


# --- same-query cache pinned to acl_version --------------------------------


def test_cache_dedups_within_window_and_invalidates_on_version_bump():
    store = FakeStore(_ref())
    runner = ScriptedRunner([GRANTED, REVOKED])
    hook = GoogleDriveRevalidationHook(store, runner, cache_ttl_secs=1000.0, now=lambda: 100.0)
    assert hook.revalidate(_ctx(), "item1", _grant(acl_version=1)) == RevalidationOutcome.FRESH
    assert hook.revalidate(_ctx(), "item1", _grant(acl_version=1)) == RevalidationOutcome.FRESH
    assert len(runner.calls) == 1  # served from cache
    assert hook.revalidate(_ctx(), "item1", _grant(acl_version=2)) == RevalidationOutcome.REVOKED
    assert len(runner.calls) == 2  # version bump re-probed
