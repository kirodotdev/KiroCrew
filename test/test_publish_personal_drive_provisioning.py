"""The personal drive's own lifecycle: building the pooled drive on a first publish
(rechecked under the lock), the served-prefix bucket policy, sharing changes and
removal across both prefixes, and the read-back facets that stay unimplemented on
purpose.
``test_publish_personal_drive.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from publish_personal_drive_helpers import _clean_registry  # noqa: F401 - fixture
from publish_personal_drive_helpers import payload_fixture  # noqa: F401 - fixture
from publish_personal_drive_helpers import BUCKET, DIST, _EngineSpy, _publish, _stub_profiles

from kiro_crew.publish import personal_drive as pd
from kiro_crew.publish_provider import CapabilityNotSupportedError, PublishError

# ── drive creation ───────────────────────────────────────────────────────────


def test_first_publish_builds_the_pooled_drive(monkeypatch, payload):
    """One bucket, one OAC, one distribution -- and the prefix-scoped policy written
    AFTER the distribution, because the policy pins that distribution's ARN."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert len(spy.created_buckets) == 1
    assert spy.created_buckets[0].startswith(pd.BUCKET_PREFIX)
    assert spy.created_distributions == spy.created_buckets
    allow = [s for s in spy.bucket_policy()["Statement"] if s["Effect"] == "Allow"][0]
    assert allow["Condition"]["StringEquals"]["AWS:SourceArn"].endswith("E999")


def test_drive_creation_is_not_repeated_when_it_exists(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=True)
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert spy.created_buckets == []
    assert spy.created_distributions == []


def test_creation_rechecks_discovery_under_the_lock(monkeypatch, payload):
    """A concurrent first publish may have built the drive while this call waited;
    creating a second one would leave two tagged drives for discovery to reject."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    calls = {"n": 0}
    real_find = pd.PersonalDriveProvider._find_drive

    def _find(self, profile, region):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # pre-lock: nothing yet
        spy.drive = True  # a rival finished while we waited
        return real_find(self, profile, region)

    monkeypatch.setattr(pd.PersonalDriveProvider, "_find_drive", _find)
    _publish(pd.PersonalDriveProvider(), payload)
    assert calls["n"] >= 2, "discovery must be re-checked after taking the lock"
    assert spy.created_buckets == [], "the rival's drive must be reused, not duplicated"


def test_bucket_creation_failure_is_reported(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    monkeypatch.setattr(
        pd.engine, "run_aws", lambda args, profile: (1, "", "AccessDenied: s3:CreateBucket")
    )
    with pytest.raises(PublishError) as exc:
        _publish(pd.PersonalDriveProvider(), payload)
    assert "storage bucket" in str(exc.value)


# ── bucket policy ────────────────────────────────────────────────────────────


def test_bucket_policy_serves_only_the_public_prefix():
    """The bucket also holds private objects, so a whole-bucket grant would serve
    them."""
    policy = json.loads(
        pd.PersonalDriveProvider()._prefix_scoped_bucket_policy(
            BUCKET, "arn:aws:cloudfront::1:distribution/E1"
        )
    )
    allow = [s for s in policy["Statement"] if s["Effect"] == "Allow"]
    deny = [s for s in policy["Statement"] if s["Effect"] == "Deny"]
    assert len(allow) == 1
    assert allow[0]["Resource"] == f"arn:aws:s3:::{BUCKET}/public/*"
    assert allow[0]["Condition"]["StringEquals"]["AWS:SourceArn"].endswith("E1")
    assert deny and deny[0]["NotResource"] == f"arn:aws:s3:::{BUCKET}/public/*"


def test_bucket_policy_never_grants_the_private_prefix():
    policy = json.loads(pd.PersonalDriveProvider()._prefix_scoped_bucket_policy("b", "arn:x"))
    for stmt in policy["Statement"]:
        if stmt["Effect"] == "Allow":
            assert pd.PRIVATE_PREFIX not in stmt["Resource"]


# ── sharing changes and removal ──────────────────────────────────────────────


def test_making_public_moves_the_object_into_the_served_prefix(monkeypatch):
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PRIVATE_PREFIX}abc"})
    spy.install(monkeypatch)
    asyncio.run(
        pd.PersonalDriveProvider().update_sharing(
            external_id="abc~alpha", visibility="public", shared_with=[]
        )
    )
    assert spy.keys_copied_to() == [f"{pd.PUBLIC_PREFIX}abc"]
    assert spy.keys_deleted() == [f"{pd.PRIVATE_PREFIX}abc"]
    assert spy.invalidated == [DIST]


def test_sharing_change_to_the_current_state_is_a_no_op(monkeypatch):
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PUBLIC_PREFIX}abc"})
    spy.install(monkeypatch)
    asyncio.run(
        pd.PersonalDriveProvider().update_sharing(
            external_id="abc~alpha", visibility="public", shared_with=[]
        )
    )
    assert spy.keys_copied_to() == []
    assert spy.keys_deleted() == []


def test_sharing_change_on_a_missing_object_says_so(monkeypatch):
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(existing_keys=set()).install(monkeypatch)
    with pytest.raises(PublishError) as exc:
        asyncio.run(
            pd.PersonalDriveProvider().update_sharing(
                external_id="gone~alpha", visibility="public", shared_with=[]
            )
        )
    assert "no longer in the drive" in str(exc.value)


def test_sharing_change_to_a_grant_list_is_refused(monkeypatch):
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy().install(monkeypatch)
    with pytest.raises(CapabilityNotSupportedError):
        asyncio.run(
            pd.PersonalDriveProvider().update_sharing(
                external_id="abc~alpha", visibility="shared", shared_with=["someone"]
            )
        )


def test_a_discovery_miss_is_not_confirmed_absence(monkeypatch):
    """A tag lookup that finds nothing must NOT be reported as `DriveNotFound`.

    Discovery keys on the drive's own tag, so a miss says the LOOKUP failed -- a tag
    removed by hand or a transient answer from the tagging API both land here with the
    bucket and distribution still serving. The withdrawal paths treat `DriveNotFound` as
    "nothing left to withdraw" and release the local record, so raising it here would
    strand a public copy with no handle on evidence no stronger than the substring match
    the type exists to replace.

    The refusal still has to be a `PublishError` so the 502 behaviour is unchanged; what
    is pinned is the SUBTYPE, because that is what the callers branch on.
    """
    provider = pd.PersonalDriveProvider()
    monkeypatch.setattr(provider, "_find_drive", lambda profile, region: None)
    with pytest.raises(pd.PublishError) as caught:
        provider._require_drive("prof", "us-west-2", require_serving=False)
    # Imported from the DEFINING module: personal_drive does not reference the type at
    # all, which is itself the point -- it has nothing left that can prove absence.
    from kiro_crew.publish_provider import DriveNotFound

    assert not isinstance(caught.value, DriveNotFound)
    # And it still tells the user their copy may be live rather than implying it is gone.
    assert "may still be public" in str(caught.value)


def test_unpublish_covers_both_prefixes(monkeypatch):
    """An artifact may be in either prefix depending on its visibility, and a
    half-removed artifact would leave a live public copy behind."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PUBLIC_PREFIX}abc", f"{pd.PRIVATE_PREFIX}abc"})
    spy.install(monkeypatch)
    asyncio.run(pd.PersonalDriveProvider().unpublish(external_id="abc~alpha"))
    assert set(spy.keys_deleted()) == {
        f"{pd.PUBLIC_PREFIX}abc",
        f"{pd.PRIVATE_PREFIX}abc",
    }
    assert spy.invalidated == [DIST]


def test_a_failing_delete_still_purges_and_still_tries_the_other_key(monkeypatch, payload):
    """The record is cleared whether or not this call succeeded, so nothing skipped here
    is ever retried. A throttle on the private key must not leave the public copy alive
    in the edge cache with no local handle left to purge it."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    inner = pd.engine._checked

    def _boom(args, profile, action="", timeout=0):
        if args[:2] == ["s3api", "delete-object"] and pd.PRIVATE_PREFIX in args[-1]:
            inner(args, profile, action=action)  # keep it in the recorded call log
            raise pd.engine.AWSError("Throttling: rate exceeded")
        return inner(args, profile, action=action)

    monkeypatch.setattr(pd.engine, "_checked", _boom)
    with pytest.raises(pd.PublishError):
        asyncio.run(pd.PersonalDriveProvider().unpublish(external_id="abc~alpha"))
    deleted = [a[a.index("--key") + 1] for a in spy.checked if a[:2] == ["s3api", "delete-object"]]
    assert deleted == [f"{pd.PUBLIC_PREFIX}abc", f"{pd.PRIVATE_PREFIX}abc"]
    assert spy.invalidated == [DIST], "the purge must run even though a delete failed"


def test_unpublish_without_an_account_reports_the_remedy(monkeypatch):
    _stub_profiles(monkeypatch, [])
    with pytest.raises(pd.PublishUnavailableError):
        asyncio.run(pd.PersonalDriveProvider().unpublish(external_id="abc~alpha"))


# ── optional read-back facets stay unimplemented on purpose ─────────────────


def test_read_back_facets_report_unavailable():
    """The drive cannot read sharing state or content back, so it inherits the
    seam's None defaults rather than pretending."""
    p = pd.PersonalDriveProvider()
    assert asyncio.run(p.fetch_state(external_id="abc")) is None
    assert asyncio.run(p.fetch_content(external_id="abc")) is None


def test_discovery_is_declared_off():
    m = pd.PersonalDriveProvider().discovery_model()
    assert not m.list_mine and not m.list_public and not m.pull_by_id


def test_installable_so_an_account_with_no_drive_is_still_offered():
    assert pd.PersonalDriveProvider().installable() is True


def test_ensure_ready_tracks_availability(monkeypatch):
    _stub_profiles(monkeypatch, [])
    assert asyncio.run(pd.PersonalDriveProvider().ensure_ready()) is False
    _stub_profiles(monkeypatch, ["alpha"])
    assert asyncio.run(pd.PersonalDriveProvider().ensure_ready()) is True


def test_registered_names_survives_an_unreadable_registry(monkeypatch):
    def _boom():
        raise OSError("nope")

    monkeypatch.setattr(pd.profiles, "load_registry", _boom)
    assert pd.PersonalDriveProvider()._registered_names() == []


def test_engine_failure_is_wrapped_not_leaked(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    real = pd.engine._checked

    def _checked(args, profile, action="", timeout=0):
        if args[:2] == ["s3api", "put-object"]:
            raise pd.engine.AWSError("throttled")
        return real(args, profile, action=action, timeout=timeout)

    monkeypatch.setattr(pd.engine, "_checked", _checked)
    with pytest.raises(PublishError) as exc:
        _publish(pd.PersonalDriveProvider(), payload)
    assert "rejected this publish" in str(exc.value)


def test_a_retried_take_private_still_purges_the_cache(monkeypatch):
    """Regression: if a previous attempt moved the object and then failed on the
    invalidation, the edge cache still holds the public copy. Returning early because
    the object is 'already private' would make the retry a no-op and leave those bytes
    served while the record says private."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PRIVATE_PREFIX}abc"})
    spy.install(monkeypatch)
    asyncio.run(
        pd.PersonalDriveProvider().update_sharing(
            external_id="abc~alpha", visibility="private", shared_with=[]
        )
    )
    assert spy.keys_copied_to() == [], "nothing to move -- it is already private"
    assert spy.invalidated == [DIST], "but the purge must still be retried"
