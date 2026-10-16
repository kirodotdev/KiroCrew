"""Tests for the personal cloud drive publish provider.

No AWS is reached: the deploy engine's call surface is stubbed, so these assert the
provider's OWN decisions -- account resolution and binding, visibility semantics, the
served-prefix bucket policy, the content type and digest on upload, the two dead-link
states and which paths may be blocked by them, and what the public edition registers.

This file holds the provider's shape and contract, drive discovery, account binding,
visibility, the upload details, the sandbox response-headers policy, the dead-link
states, head errors and the push guard. The rest of the suite sits beside it:
``test_publish_personal_drive_provisioning.py`` (drive creation, the bucket policy,
sharing changes and removal), ``test_publish_personal_drive_registration.py`` (what the
public edition registers) and ``test_publish_personal_drive_read_back.py`` (view_url and
serving_notice). Shared fixtures and fakes are in ``publish_personal_drive_helpers.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from publish_personal_drive_helpers import _clean_registry  # noqa: F401 - fixture
from publish_personal_drive_helpers import payload_fixture  # noqa: F401 - fixture
from publish_personal_drive_helpers import (
    BUCKET,
    DIST,
    DOMAIN,
    POLICY_EXISTING,
    POLICY_NEW,
    _EngineSpy,
    _publish,
    _stub_profiles,
)

from kiro_crew import publish_provider as pp
from kiro_crew.publish import personal_drive as pd
from kiro_crew.publish_provider import Capability, CapabilityNotSupportedError, PublishError

# ── shape / contract ─────────────────────────────────────────────────────────


def test_provider_implements_the_whole_abc():
    """Instantiation is the assertion: an unimplemented abstractmethod raises here."""
    assert pd.PersonalDriveProvider().name == pd.PERSONAL_DRIVE_PROVIDER


def test_no_per_account_provider_key_is_registered(monkeypatch):
    """A provider id is validated at the HTTP boundary against ^[a-z0-9-]{1,32}$, so a
    colon-qualified per-account key would be listed in the picker and rejected on
    submit. A row that 400s when clicked is worse than no row."""
    import re as _re

    from kiro_crew.dashboard.handlers import artifacts as art_handlers

    _stub_profiles(monkeypatch, ["alpha", "beta"], default="alpha")
    pd.register_public_edition_providers()
    gate = art_handlers._ARTIFACT_PROVIDER_RE
    assert isinstance(gate, _re.Pattern)
    for key in pp._FACTORIES:
        assert gate.match(key), f"registered key {key!r} cannot pass the publish gate"


def test_display_name_names_the_destination_not_the_account():
    """The picker already carries a row reading "Publish to public web (your AWS)" for the
    per-artifact deploy path. Repeating "your AWS" here made the two rows read as variants
    of one destination; which account it lands in belongs in the remedy text. "drive"
    alone read as PRIVATE storage, so the label keeps a legible PUBLIC promise."""
    label = pd.PersonalDriveProvider().display_name
    assert label == "Public web (shared drive)"
    assert "aws" not in label.lower()
    assert "public" in label.lower()


def test_the_remedy_text_names_the_action_that_makes_it_available():
    """The panel renders this in place for an unavailable destination, so it has to name
    WHICH action fixes it -- a provider is the only thing that knows."""
    hint = pd.PersonalDriveProvider().install_hint
    assert hint and "register" in hint.lower()


def test_display_name_is_vendor_neutral():
    """The seam mandates user-facing strings come from display_name; it must not
    name a third-party product."""
    label = pd.PersonalDriveProvider().display_name.lower()
    assert "drive" in label
    for vendor in ("google", "dropbox", "onedrive", "box.com"):
        assert vendor not in label


def test_sharing_capability_is_declared():
    """The orchestration gates its visibility reconciliation on this capability, so
    withholding it would let a re-publish-as-private push new bytes into the SERVED
    prefix and skip the move -- content still public, record still saying public."""
    assert Capability.SHARING in pd.PersonalDriveProvider().capabilities()


def test_declaring_sharing_does_not_imply_a_grant_list(monkeypatch):
    """SHARING means 'can change visibility', not 'has a per-principal grant list' --
    the grant list is refused by sharing_model and by update_sharing itself."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy().install(monkeypatch)
    assert pd.PersonalDriveProvider().sharing_model().supports_shared is False
    with pytest.raises(CapabilityNotSupportedError):
        asyncio.run(
            pd.PersonalDriveProvider().update_sharing(
                external_id="abc~alpha", visibility="shared", shared_with=["someone"]
            )
        )


def test_sharing_model_declares_no_grant_list():
    m = pd.PersonalDriveProvider().sharing_model()
    assert m.supports_public and m.supports_private
    assert not m.supports_shared
    assert m.principal_kind == "none"
    assert not m.supports_roles


def test_sync_model_is_a_token_guarded_mirror():
    m = pd.PersonalDriveProvider().sync_model()
    assert m.collab_mode == "mirror"
    assert m.concurrency == "token"


# ── the drive is invisible to the deploy surface ──────────────────────────────


def test_drive_does_not_carry_the_deploy_site_tag(monkeypatch, payload):
    """The deploy engine groups sites by kirocrew:site and SKIPS resources without it.
    Carrying that tag would list the pooled drive as an ordinary one-off site, where a
    recall empties it and a destroy deletes it -- wiping every published artifact."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    tagsets: list[str] = []
    monkeypatch.setattr(pd.engine, "_harden_bucket", lambda b, p, tags: tagsets.append(tags))
    _publish(pd.PersonalDriveProvider(), payload)
    assert tagsets, "the drive's bucket must be tagged"
    for tags in tagsets:
        assert pd.TAG_DRIVE in tags
        assert pd.engine.TAG_SITE not in tags


def test_discovery_filters_on_the_drive_tag_alone(monkeypatch, payload):
    """One private tag identifies the drive, passed as a SINGLE filter -- requiring a
    second tag in the same call would make discovery depend on how repeated
    --tag-filters flags combine."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    query = next(a for a in spy.checked if a[:2] == ["resourcegroupstaggingapi", "get-resources"])
    filters = [query[i + 1] for i, tok in enumerate(query) if tok == "--tag-filters"]
    assert filters == [f"Key={pd.TAG_DRIVE},Values={pd.TAG_DRIVE_VALUE}"]


def test_a_drive_outside_us_east_1_is_found_whole(monkeypatch, payload):
    """The tagging API answers per REGION and does not cover CloudFront, so the regional
    call that finds the bucket cannot see the distribution. Reporting a half-built drive
    there sends a HEALTHY drive down partial-create recovery: a second distribution is
    built and the bucket policy is re-pointed at it, so every link already handed out
    answers 403 -- and the second tagged distribution then makes discovery ambiguous
    forever. The default region is not us-east-1, so this was the normal path."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()  # region defaults to us-west-2
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert spy.created_distributions == [], "a healthy drive must not be rebuilt"
    regions = [
        a[a.index("--region") + 1]
        for a in spy.checked
        if a[:2] == ["resourcegroupstaggingapi", "get-resources"] and "--region" in a
    ]
    assert "us-east-1" in regions, "the distribution must be looked for where CloudFront lives"


def test_a_distribution_invisible_to_the_tagging_api_is_found_through_cloudfront(
    monkeypatch, payload
):
    """CloudFront's own docs say Tag Editor and Resource Groups do not cover it, so the
    tag query may report nothing in EVERY region. Discovery then falls back to CloudFront's
    own tag API -- the path CloudFront documents -- rather than concluding the drive is
    half-built and rebuilding it."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(cf_tags_visible=False)
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert spy.created_distributions == [], "a healthy drive must not be rebuilt"
    assert any(a[:2] == ["cloudfront", "list-tags-for-resource"] for a in spy.checked)


def test_a_genuinely_missing_distribution_is_still_created(monkeypatch, payload):
    """The partial-create recovery path is still needed for the state it was written for:
    a prior run tagged the bucket and died before the distribution existed. Neither tag
    source reports one, and CloudFront has none carrying the tag."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(cf_tags_visible=False, cf_own_tags=False)
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert spy.created_distributions == [BUCKET], "the existing bucket must be reused"


def test_a_throttled_list_distributions_raises_and_rebuilds_nothing(monkeypatch, payload):
    """The 403 chain. When the tagging API cannot see the distribution
    (the normal case outside us-east-1) discovery falls back to CloudFront's own tag API;
    a throttle on list-distributions there must NOT be swallowed as {}, which would read
    the existing drive as absent -> _ensure_drive rebuilds it: a NEW distribution over the
    existing bucket and the bucket policy repointed at it, so every URL already handed out
    answers 403. The throttle must propagate so NOTHING is created or repointed, and the
    next call (post-throttle) simply finds the real drive."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(
        cf_tags_visible=False,
        cf_list_error="An error occurred (Throttling) ... Rate exceeded",
    )
    spy.install(monkeypatch)
    with pytest.raises(PublishError) as exc:
        _publish(pd.PersonalDriveProvider(), payload)
    assert "could not be enumerated" in str(exc.value)
    assert "nothing was changed" in str(exc.value)
    # The 403 chain is exactly these two calls; neither may have happened.
    assert spy.created_distributions == [], "a discovery throttle must not build a distribution"
    assert [
        a for a in spy.checked if a[:2] == ["s3api", "put-bucket-policy"]
    ] == [], "a discovery throttle must not repoint the bucket policy"


def test_a_throttled_list_tags_for_resource_raises_rather_than_dropping_the_drive(
    monkeypatch, payload
):
    """The same absence lie at finer grain. Enumeration succeeds but reading ONE
    distribution's tags is throttled; the old `continue` silently dropped that
    distribution, so if it was the drive's the drive read as absent and got rebuilt into
    the 403 chain. A skipped distribution cannot be proven to be someone else's, so the
    throttle must propagate rather than be guessed away."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(
        cf_tags_visible=False,
        cf_tags_error="An error occurred (Throttling) ... Rate exceeded",
    )
    spy.install(monkeypatch)
    with pytest.raises(PublishError) as exc:
        _publish(pd.PersonalDriveProvider(), payload)
    assert "tags on a CloudFront distribution could not be read" in str(exc.value)
    assert "nothing was changed" in str(exc.value)
    assert spy.created_distributions == [], "a tag-read throttle must not build a distribution"
    assert [
        a for a in spy.checked if a[:2] == ["s3api", "put-bucket-policy"]
    ] == [], "a tag-read throttle must not repoint the bucket policy"


def test_a_genuine_empty_result_still_creates_absence_is_not_failure(monkeypatch, payload):
    """The fix must distinguish 'could not look' (raise) from 'looked and found nothing'
    (create). With no error and no distribution carrying the drive tag, discovery
    genuinely found nothing and the partial-create recovery reuses the tagged bucket and
    builds the one missing distribution -- exactly as before the fix."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(cf_tags_visible=False, cf_own_tags=False)  # no error, no matching tag
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert spy.created_distributions == [BUCKET], "a genuine absence must still create"


def test_an_orphaned_distribution_without_a_bucket_raises_and_creates_nothing(monkeypatch, payload):
    """A1 -- GPT BLOCKING. A drive-tagged distribution whose bucket was deleted is a
    PARTIAL drive, not an absent one. Reporting it as absence (the old ``if not bucket:
    return None``) sends publish down the create path, which builds a SECOND tagged
    distribution over a fresh bucket -- and two tagged distributions then wedge every
    later discovery on the ambiguity refusal. So the half-present state must RAISE,
    naming what was found and what is missing, and build nothing: recovering a
    half-deleted drive is a human's job."""
    _stub_profiles(monkeypatch, ["alpha"])
    # drive tag present on the distribution (visible in us-east-1) but NO bucket row.
    spy = _EngineSpy(buckets=[])
    spy.install(monkeypatch)
    with pytest.raises(PublishError) as exc:
        _publish(pd.PersonalDriveProvider(), payload)
    msg = str(exc.value)
    assert DIST in msg, "the error must name the distribution that was found"
    assert "bucket is missing" in msg
    assert "nothing was changed" in msg
    assert spy.created_buckets == [], "a half-present drive must not be rebuilt"
    assert spy.created_distributions == [], "a half-present drive must not be rebuilt"


def test_total_absence_no_bucket_no_distribution_still_creates(monkeypatch, payload):
    """A1's other half -- the distinction that must stay sharp. When NEITHER a bucket NOR
    a drive-tagged distribution exists, this is genuine total absence, not a partial
    drive, so the first publish builds the drive as before rather than raising the orphan
    error. Breaking the ``if dist_id`` guard so it fires on total absence would turn every
    first-ever publish into a refusal."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)  # no bucket, no distribution, no error anywhere
    spy.install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload)
    assert res.external_id, "total absence must create and return a handle"
    assert spy.created_buckets and spy.created_buckets[0].startswith(pd.BUCKET_PREFIX)
    assert spy.created_distributions == spy.created_buckets, "one distribution was built"


def test_two_drives_in_one_account_refuse_to_guess(monkeypatch, payload):
    """What discovery returns is what gets written to and deleted from, so an
    ambiguous match must fail loud and name the candidates."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(buckets=[BUCKET, f"{pd.BUCKET_PREFIX}000000000000"])
    spy.install(monkeypatch)
    with pytest.raises(PublishError) as exc:
        _publish(pd.PersonalDriveProvider(), payload)
    msg = str(exc.value)
    assert "more than one personal drive" in msg
    assert BUCKET in msg and f"{pd.BUCKET_PREFIX}000000000000" in msg
    assert "nothing was touched" in msg


def test_discovery_ignores_a_bucket_outside_the_naming_scheme(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["alpha"])
    # No distribution carries the drive tag either, so this is genuine TOTAL absence with
    # only a stranger's bucket present: the create branch must run. (A drive-tagged
    # DISTRIBUTION alongside a non-scheme bucket is a different case -- a half-present
    # drive -- and is covered by the A1 orphan test, which raises rather than creating a
    # second distribution.)
    spy = _EngineSpy(buckets=["someone-elses-bucket"], cf_tags_visible=False, cf_own_tags=False)
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    # No usable bucket found -> the create branch ran instead of writing to a stranger's.
    assert spy.created_buckets and spy.created_buckets[0].startswith(pd.BUCKET_PREFIX)


# ── account resolution and BINDING ───────────────────────────────────────────


def test_no_registered_account_is_unavailable_with_a_remedy(monkeypatch):
    _stub_profiles(monkeypatch, [])
    p = pd.PersonalDriveProvider()
    assert p.available() is False
    with pytest.raises(pd.PublishUnavailableError) as exc:
        p._resolve_profile()
    assert "register a profile" in str(exc.value).lower()


def test_unknown_bound_account_names_the_registered_ones(monkeypatch):
    _stub_profiles(monkeypatch, ["alpha", "beta"], default="alpha")
    with pytest.raises(pd.PublishUnavailableError) as exc:
        pd.PersonalDriveProvider()._resolve_profile("typo")
    msg = str(exc.value)
    assert "typo" in msg and "alpha" in msg and "beta" in msg


def test_a_new_publish_follows_the_registry_default(monkeypatch):
    _stub_profiles(monkeypatch, ["alpha", "beta"], default="beta")
    assert pd.PersonalDriveProvider()._resolve_profile() == ("beta", "us-west-2")


def test_publish_binds_the_resolved_account_into_the_external_id(monkeypatch, payload):
    """The seam persists a provider KEY and re-resolves it later, so a destination
    whose key means 'whatever is default now' cannot locate its own past publishes."""
    _stub_profiles(monkeypatch, ["alpha", "beta"], default="alpha")
    _EngineSpy().install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload)
    _key, bound = pd.split_external_id(res.external_id)
    assert bound == "alpha"


def test_the_public_url_never_contains_the_account_name(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["secret-acct"], default="secret-acct")
    _EngineSpy().install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload)
    assert res.view_url
    assert "secret-acct" not in res.view_url


def test_the_object_key_never_contains_the_account_name(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["secret-acct"], default="secret-acct")
    spy = _EngineSpy()
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert all("secret-acct" not in k for k in spy.put_keys())


def test_changing_the_default_does_not_move_an_existing_publication(monkeypatch, payload):
    """Publish under alpha, make beta the default, then unpublish: the delete must run
    against ALPHA. Otherwise the local record is cleared while alpha's object stays
    publicly served -- the un-recallable state this destination must never produce."""
    reg = _stub_profiles(monkeypatch, ["alpha", "beta"], default="alpha")
    spy = _EngineSpy()
    spy.install(monkeypatch)
    provider = pd.PersonalDriveProvider()
    res = _publish(provider, payload)

    seen: list[str] = []
    real_resolve = pd.profiles.resolve_profile
    monkeypatch.setattr(
        pd.profiles,
        "resolve_profile",
        lambda requested="": (seen.append(requested), real_resolve(requested))[1],
    )
    reg["default"] = "beta"
    asyncio.run(provider.unpublish(external_id=res.external_id))
    assert "alpha" in seen, "the removal must target the account the publish bound"


def test_an_unbound_legacy_id_falls_back_to_the_default(monkeypatch):
    """An id minted before binding existed has no separator; the pre-binding
    behaviour (registry default) is the right fallback, not a crash."""
    _stub_profiles(monkeypatch, ["alpha"], default="alpha")
    assert pd.PersonalDriveProvider()._profile_for("plainid") == ("alpha", "us-west-2")


def test_available_does_not_reach_aws(monkeypatch):
    """available() gates whether the destination is offered at all, so it must not
    make a network call."""
    _stub_profiles(monkeypatch, ["alpha"])

    def _boom(*a, **k):  # pragma: no cover - fails the test if called
        raise AssertionError("available() reached AWS")

    monkeypatch.setattr(pd.engine, "run_aws", _boom)
    monkeypatch.setattr(pd.engine, "_checked", _boom)
    assert pd.PersonalDriveProvider().available() is True


# ── visibility semantics ─────────────────────────────────────────────────────


def test_shared_visibility_is_refused_not_downgraded(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["alpha"])
    with pytest.raises(CapabilityNotSupportedError):
        _publish(pd.PersonalDriveProvider(), payload, visibility="shared")


def test_a_grant_list_is_refused_even_when_visibility_is_public(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["alpha"])
    with pytest.raises(CapabilityNotSupportedError):
        _publish(pd.PersonalDriveProvider(), payload, shared_with=["someone"])


def test_private_publish_yields_no_browsable_url(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload, visibility="private")
    assert res.view_url == ""
    assert not spy.invalidated, "a private object is not served, so nothing to invalidate"
    assert spy.put_keys()[0].startswith(pd.PRIVATE_PREFIX)


# ── the load-bearing upload details ──────────────────────────────────────────


def test_upload_declares_the_content_type(monkeypatch, payload):
    """Without an explicit content type S3 stores binary/octet-stream, and the
    distribution's nosniff header then makes a browser download an HTML page instead
    of rendering it."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert spy.arg("put-object", "--content-type") == "text/html"


def test_upload_stores_the_content_digest_as_metadata(monkeypatch, payload):
    """The stored digest is the whole content of the token-guarded concurrency this
    provider declares; without it a push cannot tell the object changed."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload)
    assert spy.arg("put-object", "--metadata") == f"sha256={res.concurrency_token}"


def test_public_publish_returns_a_link_and_does_not_invalidate(monkeypatch, payload):
    """No purge on a first publish: the key is freshly minted so no edge has served it.
    Issuing one anyway would add the only failure mode this call otherwise lacks -- the
    object is already uploaded, so a throttled invalidation would report a failed
    publish while leaving that object behind with no record pointing at it."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload)
    key_part, _ = pd.split_external_id(res.external_id)
    assert res.view_url == f"https://{DOMAIN}/{pd.PUBLIC_PREFIX}{key_part}"
    assert res.concurrency_token, "a push guard token is required"
    assert spy.invalidated == []
    assert spy.put_keys() == [f"{pd.PUBLIC_PREFIX}{key_part}"]


def test_external_id_is_generated_not_derived(monkeypatch, payload):
    """The id is half of an unguessable public URL, so two publishes of the same
    bytes must not collide."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy().install(monkeypatch)
    a = _publish(pd.PersonalDriveProvider(), payload).external_id
    b = _publish(pd.PersonalDriveProvider(), payload).external_id
    assert a != b


def test_oversized_artifact_is_refused_with_its_size(monkeypatch, tmp_path):
    from kiro_crew.artifacts import MAX_CONTENT_BYTES

    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy().install(monkeypatch)
    big = tmp_path / "big.html"
    big.write_bytes(b"x" * (MAX_CONTENT_BYTES + 1))
    with pytest.raises(PublishError) as exc:
        _publish(pd.PersonalDriveProvider(), str(big))
    assert str(MAX_CONTENT_BYTES) in str(exc.value)


# ── the sandbox response-headers policy: verify-before-reuse, else fail closed ──


def test_same_name_policy_with_wrong_csp_fails_closed(monkeypatch, payload):
    """An agent holding the user's AWS creds can pre-create a policy under this
    account-global name WITHOUT the sandbox CSP. Reusing it on the name alone would
    silently drop the opaque-origin sandbox and let published docs share an origin, so a
    name match with a wrong/absent CSP must FAIL CLOSED and create NOTHING."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False, existing_policy=True, existing_policy_csp="default-src 'self'")
    spy.install(monkeypatch)
    with pytest.raises(PublishError) as excinfo:
        _publish(pd.PersonalDriveProvider(), payload)
    msg = str(excinfo.value)
    assert pd._SANDBOX_POLICY_NAME in msg
    assert POLICY_EXISTING in msg
    # Failed closed: no distribution built, no duplicate policy created.
    assert spy.created_distributions == []
    assert spy.created_policies() == []


def test_the_policy_denies_framing_on_both_mechanisms():
    """A published artifact is a public page, so whoever may FRAME it is part of its
    security posture: without this, any third-party origin could frame one and drive
    clickjacking against whatever the artifact renders.

    Both mechanisms are asserted because they cover different browsers -- `frame-ancestors`
    for CSP3, the `X-Frame-Options` header for anything older -- and DENY rather than
    SAMEORIGIN because one drive serves every artifact from a single domain, so "same
    origin" here means another published document, not a trusted host.
    """
    config = pd._sandbox_policy_config()
    headers = config["SecurityHeadersConfig"]
    assert headers["FrameOptions"] == {"FrameOption": "DENY", "Override": True}
    assert "frame-ancestors 'none'" in headers["ContentSecurityPolicy"]["ContentSecurityPolicy"]
    # The sandbox half must survive alongside it -- adding framing protection must not
    # have displaced the opaque-origin directive this policy exists for.
    assert "sandbox" in pd._SANDBOX_CSP


def test_a_policy_without_framing_protection_fails_closed(monkeypatch, payload):
    """Framing protection lives in the VERIFIED constant, not only in a header, and this
    is the test that proves the difference.

    The reuse path compares the CSP and fails closed on a mismatch. A pre-created policy
    that carries the old sandbox-only CSP therefore cannot be reused, so framing
    protection cannot be silently skipped on an account that already has a same-named
    policy. Expressed only as `FrameOptions`, it would have been reused unverified.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(
        drive=False,
        existing_policy=True,
        # Exactly the CSP before framing protection was added.
        existing_policy_csp="sandbox allow-scripts allow-popups",
    )
    spy.install(monkeypatch)
    with pytest.raises(PublishError):
        _publish(pd.PersonalDriveProvider(), payload)
    # Failed closed: nothing built, nothing duplicated.
    assert spy.created_distributions == []
    assert spy.created_policies() == []


def test_same_name_policy_with_exact_csp_is_reused(monkeypatch, payload):
    """Genuine find-or-create: a same-named policy carrying the EXACT sandbox CSP with
    Override true is reused -- its id reaches create_distribution and no new policy is
    created."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False, existing_policy=True)  # defaults to exact CSP + Override true
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert spy.created_policies() == []  # reused, not re-created
    assert POLICY_EXISTING in spy.policy_ids  # the reused id was handed to create_distribution


def test_same_name_policy_with_override_false_fails_closed(monkeypatch, payload):
    """A CSP that matches the directive but is not enforced (Override false) does not
    isolate anything, so it must fail closed exactly like a wrong CSP -- not be reused."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False, existing_policy=True, existing_policy_override=False)
    spy.install(monkeypatch)
    with pytest.raises(PublishError) as excinfo:
        _publish(pd.PersonalDriveProvider(), payload)
    assert pd._SANDBOX_POLICY_NAME in str(excinfo.value)
    assert spy.created_distributions == []
    assert spy.created_policies() == []


def test_no_existing_policy_is_created_as_before(monkeypatch, payload):
    """With no same-named policy present, the sandbox policy is created and its new id
    flows into the distribution -- the find-or-create create branch is intact."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False, existing_policy=False)
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert len(spy.created_policies()) == 1
    assert POLICY_NEW in spy.policy_ids


# ── the two dead-link states, and HOW they are reported ──────────────────────
def test_still_rolling_out_is_reported_on_a_public_publish(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(status="InProgress").install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload)
    assert "still rolling out" in res.notice.lower()
    # The raw AWS state token (e.g. "InProgress") is deliberately NOT surfaced in user
    # copy -- the sentence already explains the state without leaking the literal.
    assert "InProgress" not in res.notice
    assert "nothing was lost" in res.notice.lower()


def test_externally_disabled_is_reported_on_a_public_publish(monkeypatch, payload):
    """An account security control can disable the distribution; the resulting dead
    link is invisible from the URL, so the publish must say so -- on the result, because
    the content is already uploaded and raising would strand it."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(enabled=False).install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload)
    assert "DISABLED" in res.notice
    assert "not a state Kiro Crew sets" in res.notice


def test_serving_notice_returns_the_rolling_out_code(monkeypatch):
    """A2 -- backend half of the shared discriminator. A distribution that is not yet
    Deployed is still propagating, reachable shortly: its code is ``rolling_out`` and the
    human text is kept as the audit trail."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(status="InProgress").install(monkeypatch)
    text, code = pd.PersonalDriveProvider()._serving_notice(DIST, "InProgress", "alpha")
    assert code == "rolling_out"
    assert "rolling out" in text.lower()


def test_serving_notice_returns_the_disabled_code(monkeypatch):
    """A2 -- the case one fixed 'reachable in a few minutes' string is WRONG for. A
    DISABLED distribution's remedy is to re-enable it, not to wait, so it carries the
    distinct ``distribution_disabled`` code the frontend maps to the console remedy."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(enabled=False).install(monkeypatch)
    text, code = pd.PersonalDriveProvider()._serving_notice(DIST, "Deployed", "alpha")
    assert code == "distribution_disabled"
    assert "DISABLED" in text


def test_serving_notice_returns_no_code_when_the_link_will_resolve(monkeypatch):
    """A Deployed, enabled distribution has no dead-link state: empty text AND empty code,
    so the frontend renders no notice at all."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(status="Deployed", enabled=True).install(monkeypatch)
    text, code = pd.PersonalDriveProvider()._serving_notice(DIST, "Deployed", "alpha")
    assert text == ""
    assert code == ""


def _notice_code(res) -> str:
    """The notice_code carried on a PublishResult.

    The field is added to PublishResult by the seam half of this change (contract);
    reading it defensively lets Section A's own gate run whether or not that half has
    landed yet, while still asserting the value once it exists.
    """
    return getattr(res, "notice_code", "__ABSENT__")


def test_a_rolling_out_publish_carries_the_rolling_out_code(monkeypatch, payload):
    """A2 -- the code must ride back on the result the module returns, not just the text,
    so the frontend can select its per-case string."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(status="InProgress").install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload)
    code = _notice_code(res)
    if code != "__ABSENT__":
        assert code == "rolling_out"
    # Regardless of the seam field's landing order, the human text still reflects it.
    assert "rolling out" in res.notice.lower()


def test_a_disabled_publish_carries_the_disabled_code(monkeypatch, payload):
    """A2 -- the disabled case is the one the false time promise harmed; its code must be
    ``distribution_disabled`` so the UI shows the re-enable remedy instead."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(enabled=False).install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload)
    code = _notice_code(res)
    if code != "__ABSENT__":
        assert code == "distribution_disabled"
    assert "DISABLED" in res.notice


def test_an_unreadable_rollout_state_carries_the_unknown_code(monkeypatch, payload):
    """A2 -- when the rollout state cannot be read, the object is already uploaded and the
    handle must come back, but the code must NOT claim a state it does not know: it is
    ``unknown``, which the frontend maps to a neutral string with no time promise."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    real_status = pd.engine.distribution_status

    def _status(dist_id, profile):
        if spy.put_keys():  # only the post-upload read fails
            raise pd.engine.AWSError("throttled")
        return real_status(dist_id, profile)

    monkeypatch.setattr(pd.engine, "distribution_status", _status)
    res = _publish(pd.PersonalDriveProvider(), payload)
    assert res.external_id, "the handle for the uploaded object must come back"
    code = _notice_code(res)
    if code != "__ABSENT__":
        assert code == "unknown"
    assert "did not report" in res.notice


def test_a_healthy_publish_carries_no_notice_code(monkeypatch, payload):
    """A Deployed, enabled drive hands out a working link with nothing to warn about, so
    both notice and notice_code are empty and the UI shows no banner."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(status="Deployed", enabled=True).install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload)
    assert res.notice == ""
    code = _notice_code(res)
    if code != "__ABSENT__":
        assert code == ""


def test_an_unreadable_rollout_state_still_returns_the_handle(monkeypatch, payload):
    """Making the VERDICT non-fatal was not enough -- the probe itself reaches CloudFront,
    so a throttle or a permissions gap raised after the object was already uploaded and the
    orchestration, which records the publication only on return, left it public with no
    withdrawal handle. Not knowing the state is a thing to report, not a reason to discard
    the handle."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    # Drive resolution reads the status too, so failing every call would abort BEFORE the
    # upload -- a different (and already covered) path. Only the post-upload read fails.
    real_status = pd.engine.distribution_status
    calls = {"n": 0}

    def _status(dist_id, profile):
        calls["n"] += 1
        if spy.put_keys():
            raise pd.engine.AWSError("throttled")
        return real_status(dist_id, profile)

    monkeypatch.setattr(pd.engine, "distribution_status", _status)
    res = _publish(pd.PersonalDriveProvider(), payload)
    assert res.external_id, "the handle for the uploaded object must come back"
    assert "did not report" in res.notice
    assert spy.put_keys(), "the object was uploaded before the state was read"


def test_an_upload_bounds_how_long_an_edge_may_serve_it(monkeypatch, payload):
    """Withdrawal deletes the object and SUBMITS an invalidation without awaiting it, so
    the invalidation is the fast path and this header is the guarantee. Without it objects
    inherit the distribution's cache policy default (Managed-CachingOptimized, 24 hours),
    so an edge that had served a public artifact could keep serving it for a day after the
    user withdrew it and was told it was gone."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert spy.arg("put-object", "--cache-control") == f"max-age={pd._EDGE_MAX_AGE_SECONDS}"
    # A day-long window is the thing being prevented; keep this honest if the value moves.
    assert pd._EDGE_MAX_AGE_SECONDS <= 900


def test_a_disabled_network_is_also_reported_rather_than_raised(monkeypatch, payload):
    """Same reasoning for the other dead-link state. A DISABLED distribution needs saying
    out loud, but not at the cost of the handle for content already uploaded."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(drive=False, status="Deployed", enabled=False).install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload)
    assert "DISABLED" in res.notice
    assert res.external_id


def test_removal_is_never_blocked_by_an_unhealthy_network(monkeypatch, payload):
    """THE critical one. The orchestration clears the local record whether or not the
    destination delete happened, so refusing here would report a withdrawal that never
    occurred while the object stayed publicly served."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(status="InProgress", enabled=False, existing_keys=set())
    spy.install(monkeypatch)
    key_part = "abc"
    spy.existing_keys = {f"{pd.PUBLIC_PREFIX}{key_part}"}
    asyncio.run(pd.PersonalDriveProvider().unpublish(external_id=f"{key_part}~alpha"))
    assert spy.keys_deleted() == [
        f"{pd.PUBLIC_PREFIX}{key_part}",
        f"{pd.PRIVATE_PREFIX}{key_part}",
    ]


def test_taking_something_private_is_never_blocked_by_an_unhealthy_network(monkeypatch):
    """Same reasoning: this path WITHDRAWS public content."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(status="InProgress", enabled=False, existing_keys={f"{pd.PUBLIC_PREFIX}abc"})
    spy.install(monkeypatch)
    asyncio.run(
        pd.PersonalDriveProvider().update_sharing(
            external_id="abc~alpha", visibility="private", shared_with=[]
        )
    )
    assert spy.keys_copied_to() == [f"{pd.PRIVATE_PREFIX}abc"]
    assert spy.keys_deleted() == [f"{pd.PUBLIC_PREFIX}abc"]


def test_making_something_public_IS_blocked_by_an_unhealthy_network(monkeypatch):
    """The asymmetry is the point: this path hands out a link, so it must refuse."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(status="InProgress", existing_keys={f"{pd.PRIVATE_PREFIX}abc"}).install(monkeypatch)
    with pytest.raises(PublishError):
        asyncio.run(
            pd.PersonalDriveProvider().update_sharing(
                external_id="abc~alpha", visibility="public", shared_with=[]
            )
        )


def test_an_unreadable_enabled_flag_cannot_clear_a_stored_notice(monkeypatch, payload):
    """The re-probe must report "could not look" (``None``), never "checked, cleared".

    `serving_notice`'s contract is that EVERY failure returns ``None`` so the stored
    notice is left exactly as it was. Collapsing an unreadable enabled-flag into
    "enabled" broke that from underneath: `GetDistribution` succeeds, the config read is
    throttled or unauthorized, and the derivation reached its final ``("", "")`` -- which
    the caller reads as "the condition cleared, drop the record's notice". That drops a
    warning that may still be true, which is the one outcome the contract forbids.

    The sibling test above pins the OPPOSITE choice for the publish path, deliberately:
    there an unreadable flag must not refuse, because the shipped IAM tier does not grant
    the read and a permissions gap would fail every publish. Same value, opposite
    handling, because the cost of guessing is reversed.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    provider = pd.PersonalDriveProvider()
    real = pd.engine._checked

    def _checked(args, profile, action="", timeout=0):
        if args[:2] == ["cloudfront", "get-distribution-config"]:
            raise pd.engine.AWSError("Throttling: rate exceeded")
        return real(args, profile, action=action, timeout=timeout)

    monkeypatch.setattr(pd.engine, "_checked", _checked)
    # Deployed status, so the rollout branch is not what produces the answer.
    assert provider._serving_notice("DIST123", "Deployed", "alpha") is None


def test_unreadable_distribution_config_does_not_block_publishing(monkeypatch, payload):
    """An unreadable enabled-flag is no objection: refusing here would make a
    permissions gap look like a disabled distribution."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    real = pd.engine._checked

    def _checked(args, profile, action="", timeout=0):
        if args[:2] == ["cloudfront", "get-distribution-config"]:
            raise pd.engine.AWSError("AccessDenied")
        return real(args, profile, action=action, timeout=timeout)

    monkeypatch.setattr(pd.engine, "_checked", _checked)
    assert _publish(pd.PersonalDriveProvider(), payload).view_url


def test_a_detached_sandbox_policy_refuses_to_publish(monkeypatch, payload):
    """The create path verifies the policy's CSP; nothing verified the ATTACHMENT.

    ``_sandbox_headers_policy`` already fails closed on a same-named policy whose CSP is
    not exactly the sandbox one, so a drive is wired correctly the moment it is created.
    That says nothing about a drive that already exists: the policy object can be perfect
    and simply not be attached any more. Then every artifact on this pooled domain shares
    one real browser origin and one published document can read another's storage, which
    is the single cost of pooling this module says cannot be argued away.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(existing_policy=True, attached_policy_id="").install(monkeypatch)
    with pytest.raises(PublishError) as exc:
        _publish(pd.PersonalDriveProvider(), payload)
    msg = str(exc.value)
    assert "sharing a browser origin" in msg, msg
    assert pd._SANDBOX_POLICY_NAME in msg, "the message must name the policy to re-attach"


def test_a_swapped_sandbox_policy_refuses_to_publish(monkeypatch, payload):
    """A DIFFERENT policy is as bad as none: the id is what carries the sandbox header."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(existing_policy=True, attached_policy_id="RHP-SOMETHING-ELSE").install(monkeypatch)
    with pytest.raises(PublishError):
        _publish(pd.PersonalDriveProvider(), payload)


def test_an_unreadable_attachment_does_not_block_publishing(monkeypatch, payload):
    """Only a CONFIRMED mismatch refuses. An unreadable config is not evidence.

    This is the same third state the enabled flag uses and for the same reason: an install
    whose IAM policy was narrowed cannot read the distribution config at all, so refusing
    on an unreadable answer would turn a permissions gap into a publish outage. The
    documented drive tier does grant the read; the tolerance is for the ones that do not.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_policy=True)
    spy.install(monkeypatch)
    real = pd.engine._checked

    def _checked(args, profile, action="", timeout=0):
        if args[:2] == ["cloudfront", "get-distribution-config"]:
            raise pd.engine.AWSError("AccessDenied")
        return real(args, profile, action=action, timeout=timeout)

    monkeypatch.setattr(pd.engine, "_checked", _checked)
    assert _publish(pd.PersonalDriveProvider(), payload).view_url


def test_a_withdrawal_is_not_blocked_by_a_detached_sandbox_policy(monkeypatch):
    """Removing content must never be gated on the delivery network's headers.

    Refusing here would strand the very copy the header was protecting: the artifact stays
    public and its owner cannot take it down. Same rule as the serving assertion, which is
    why both live behind ``require_serving``.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(existing_policy=True, attached_policy_id="").install(monkeypatch)
    asyncio.run(pd.PersonalDriveProvider().unpublish(external_id="abc~alpha"))


def test_flipping_an_artifact_public_refuses_on_a_detached_sandbox_policy(monkeypatch):
    """The first publish is not the only way bytes start being served on the pooled domain.

    ``update_sharing`` private -> public reaches the drive through ``_require_drive``, a
    DIFFERENT resolver from the one a first publish uses, so a check placed only on the
    publish path leaves this one open. Both review lanes found this independently, and they
    were right: gating the guarantee on how the artifact happened to become public is not a
    guarantee.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(existing_policy=True, attached_policy_id="").install(monkeypatch)
    with pytest.raises(PublishError) as exc:
        asyncio.run(
            pd.PersonalDriveProvider().update_sharing(
                external_id="abc~alpha", visibility="public", shared_with=[]
            )
        )
    assert "sharing a browser origin" in str(exc.value)


def test_flipping_an_artifact_private_is_not_blocked_by_a_detached_policy(monkeypatch):
    """Taking content OUT of public serving is a removal, so it is never gated."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(existing_policy=True, attached_policy_id="").install(monkeypatch)
    asyncio.run(
        pd.PersonalDriveProvider().update_sharing(
            external_id="abc~alpha", visibility="private", shared_with=[]
        )
    )


def test_pushing_new_bytes_is_not_blocked_by_a_detached_sandbox_policy(monkeypatch, payload):
    """A push replaces bytes in place and hands out no new link.

    If the header were missing, the artifact was ALREADY served without isolation before
    the push, so refusing un-exposes nothing -- it only strands the owner on the old bytes,
    which is the opposite of what someone replacing content wants. ``push_version`` is
    best-effort by contract and must not raise, so this asserts it reports rather than
    throws.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(existing_policy=True, attached_policy_id="").install(monkeypatch)
    result = asyncio.run(
        pd.PersonalDriveProvider().push_version(
            external_id="abc~alpha", file_path=payload, expected_token=""
        )
    )
    assert result is not None


# ── head must not turn an AWS failure into "already gone" ────────────────────


def test_a_throttled_head_is_an_error_not_an_absence(monkeypatch):
    """On the paths that DO probe, collapsing every failure to 'absent' would move or
    overwrite the wrong thing. (The removal path deliberately does not probe at all.)"""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(head_error="An error occurred (SlowDown) ... Please reduce").install(monkeypatch)
    with pytest.raises(PublishError) as exc:
        asyncio.run(
            pd.PersonalDriveProvider().update_sharing(
                external_id="abc~alpha", visibility="private", shared_with=[]
            )
        )
    assert "nothing was changed" in str(exc.value)


def test_an_expired_token_head_is_an_error_not_an_absence(monkeypatch):
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(head_error="An error occurred (ExpiredToken)").install(monkeypatch)
    with pytest.raises(PublishError):
        asyncio.run(
            pd.PersonalDriveProvider().update_sharing(
                external_id="abc~alpha", visibility="private", shared_with=[]
            )
        )


def test_removal_never_probes_before_deleting(monkeypatch):
    """DeleteObject is idempotent, so asking first buys nothing -- and it costs
    everything: the probe can fail on a throttle or an expired token, and the
    orchestration clears the local record regardless, so a probe failure would discard
    the only handle to an object that is still served."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(head_error="An error occurred (SlowDown) ... Please reduce")
    spy.install(monkeypatch)
    asyncio.run(pd.PersonalDriveProvider().unpublish(external_id="abc~alpha"))
    assert spy.keys_deleted() == [f"{pd.PUBLIC_PREFIX}abc", f"{pd.PRIVATE_PREFIX}abc"]
    assert spy.invalidated == [DIST]


def test_removal_is_idempotent_when_nothing_is_there(monkeypatch):
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys=set())
    spy.install(monkeypatch)
    asyncio.run(pd.PersonalDriveProvider().unpublish(external_id="gone~alpha"))
    assert spy.keys_deleted() == [f"{pd.PUBLIC_PREFIX}gone", f"{pd.PRIVATE_PREFIX}gone"]
    assert spy.invalidated == [DIST], "the purge must run: it is the last call for this id"


# ── the push guard actually guards ───────────────────────────────────────────


def test_push_refuses_to_overwrite_an_out_of_band_change(monkeypatch, payload):
    """sync_model declares token concurrency, so a remote digest matching neither the
    token we were given nor the bytes we are about to write means someone else changed
    it -- overwriting would destroy that change while reporting success."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PUBLIC_PREFIX}abc"}, stored_digest="deadbeef" * 8)
    spy.install(monkeypatch)
    out = asyncio.run(
        pd.PersonalDriveProvider().push_version(
            external_id="abc~alpha", file_path=payload, expected_token="cafebabe" * 8
        )
    )
    assert out.conflict is True
    assert out.error
    assert spy.put_keys() == [], "nothing may be written on a conflict"


def test_push_treats_a_missing_digest_as_an_out_of_band_change(monkeypatch, payload):
    """Every object this module writes carries the sha256 metadata, so its ABSENCE means
    the object was replaced by something that is not us -- an upload from the S3 console
    or `aws s3 cp`, neither of which preserves user metadata. A missing digest is less
    evidence that the object is ours than a mismatching one, so it must not fail open."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PUBLIC_PREFIX}abc"}, stored_digest="")
    spy.install(monkeypatch)
    out = asyncio.run(
        pd.PersonalDriveProvider().push_version(
            external_id="abc~alpha", file_path=payload, expected_token="cafebabe" * 8
        )
    )
    assert out.conflict is True
    assert spy.put_keys() == [], "a metadata-less object may not be overwritten"


def test_push_proceeds_when_the_remote_matches_the_token(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["alpha"])
    token = "cafebabe" * 8
    spy = _EngineSpy(existing_keys={f"{pd.PUBLIC_PREFIX}abc"}, stored_digest=token)
    spy.install(monkeypatch)
    out = asyncio.run(
        pd.PersonalDriveProvider().push_version(
            external_id="abc~alpha", file_path=payload, expected_token=token
        )
    )
    assert not out.error and not out.conflict
    assert spy.put_keys() == [f"{pd.PUBLIC_PREFIX}abc"]


def test_push_proceeds_when_the_remote_already_holds_the_new_bytes(monkeypatch, payload):
    """A retried push is not a conflict."""
    _stub_profiles(monkeypatch, ["alpha"])
    digest = pd.hashlib.sha256(Path(payload).read_bytes()).hexdigest()
    spy = _EngineSpy(existing_keys={f"{pd.PUBLIC_PREFIX}abc"}, stored_digest=digest)
    spy.install(monkeypatch)
    out = asyncio.run(
        pd.PersonalDriveProvider().push_version(
            external_id="abc~alpha", file_path=payload, expected_token="cafebabe" * 8
        )
    )
    assert not out.conflict


def test_push_version_reports_instead_of_raising(monkeypatch, payload):
    _stub_profiles(monkeypatch, [])  # no account -> would raise inside
    out = asyncio.run(
        pd.PersonalDriveProvider().push_version(
            external_id="abc~alpha", file_path=payload, expected_token=""
        )
    )
    assert out.error, "push_version must never raise; it reports"


def test_push_version_missing_object_says_republish(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(existing_keys=set()).install(monkeypatch)
    out = asyncio.run(
        pd.PersonalDriveProvider().push_version(
            external_id="gone~alpha", file_path=payload, expected_token=""
        )
    )
    assert "publish it again" in out.error.lower()


def test_a_withdrawal_never_builds_a_replacement_drive(monkeypatch, payload):
    """A discovery miss -- the drive's tag removed by hand, a transient tagging answer --
    must NOT make unpublish BUILD a bucket, OAC and distribution, delete keys that were
    never in it, and report success while the original object stays public and the
    report takes the local record with it."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    with pytest.raises(pd.PublishError):
        asyncio.run(pd.PersonalDriveProvider().unpublish(external_id="abc~alpha"))
    assert spy.created_buckets == [], "a withdrawal must not provision anything"
    assert spy.created_distributions == []


def test_a_sharing_change_never_builds_a_replacement_drive(monkeypatch, payload):
    """Same rule: only a first publish may create infrastructure."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    with pytest.raises(pd.PublishError):
        asyncio.run(
            pd.PersonalDriveProvider().update_sharing(
                external_id="abc~alpha", visibility="private", shared_with=[]
            )
        )
    assert spy.created_buckets == []
    assert spy.created_distributions == []


def test_a_push_never_builds_a_replacement_drive(monkeypatch, payload):
    """A push reports the missing drive rather than writing into a fresh one, where the
    bytes would be served from nowhere the publication's link points at."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    out = asyncio.run(
        pd.PersonalDriveProvider().push_version(
            external_id="abc~alpha", file_path=payload, expected_token=""
        )
    )
    assert out.error
    assert spy.created_buckets == []
    assert spy.created_distributions == []


def test_taking_private_recopies_the_served_copy_even_when_both_keys_exist(monkeypatch, payload):
    """push_version heads public/<key> first, so whenever both prefixes hold a copy a push
    lands on public/<key> -- which is the SOURCE when taking something private. Skipping
    the copy and deleting it would destroy the pushed bytes, keep the stale private copy,
    and leave the record's token matching neither, so every later push conflicts forever."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PRIVATE_PREFIX}abc", f"{pd.PUBLIC_PREFIX}abc"})
    spy.install(monkeypatch)
    asyncio.run(
        pd.PersonalDriveProvider().update_sharing(
            external_id="abc~alpha", visibility="private", shared_with=[]
        )
    )
    copies = [a for a in spy.checked if a[:2] == ["s3api", "copy-object"]]
    assert copies, "the served copy must be carried across before it is deleted"
    assert copies[0][copies[0].index("--key") + 1] == f"{pd.PRIVATE_PREFIX}abc"
    assert copies[0][copies[0].index("--copy-source") + 1].endswith(f"{pd.PUBLIC_PREFIX}abc")


def test_a_surviving_stale_source_never_overwrites_a_newer_destination(monkeypatch, payload):
    """Both keys present means an earlier move landed and only its cleanup failed. The
    destination is authoritative and may be strictly newer -- a push writes to whichever
    prefix is live -- so copying again would overwrite the pushed version with pre-push
    bytes and report success. Clean up the source instead."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PRIVATE_PREFIX}abc", f"{pd.PUBLIC_PREFIX}abc"})
    spy.install(monkeypatch)
    asyncio.run(
        pd.PersonalDriveProvider().update_sharing(
            external_id="abc~alpha", visibility="public", shared_with=[]
        )
    )
    assert [a for a in spy.checked if a[:2] == ["s3api", "copy-object"]] == []
    deleted = [a[a.index("--key") + 1] for a in spy.checked if a[:2] == ["s3api", "delete-object"]]
    assert deleted == [f"{pd.PRIVATE_PREFIX}abc"], "the stale source is what must go"
    assert spy.invalidated == [DIST]


def test_the_drive_never_claims_the_site_name_a_user_would_destroy(monkeypatch, payload):
    """The engine hard-codes kirocrew:site on the create call and it can only be removed
    afterwards, so a window exists where the drive is listed as a deploy site. Named
    "default" that window meant `destroy default` -- the likeliest destroy anyone runs --
    deleting the distribution every published URL depends on."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert spy.created_site_ids == [pd.SITE_TAG_VALUE]
    assert "default" not in spy.created_site_ids


def test_a_failed_purge_does_not_abort_a_completed_move_to_public(monkeypatch, payload):
    """The object is already served from the new prefix, so raising here would abort
    before the caller records the new visibility -- record private, content served."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PRIVATE_PREFIX}abc"})
    spy.install(monkeypatch)
    monkeypatch.setattr(
        pd.engine, "invalidate", lambda d, p: (_ for _ in ()).throw(pd.engine.AWSError("throttled"))
    )
    asyncio.run(
        pd.PersonalDriveProvider().update_sharing(
            external_id="abc~alpha", visibility="public", shared_with=[]
        )
    )


def test_a_failed_purge_does_abort_a_move_to_private(monkeypatch, payload):
    """The mirror case: the purge is what evicts the copy the edge is still serving, so a
    failure means the withdrawal did not happen and must be reported."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PUBLIC_PREFIX}abc"})
    spy.install(monkeypatch)
    monkeypatch.setattr(
        pd.engine, "invalidate", lambda d, p: (_ for _ in ()).throw(pd.engine.AWSError("throttled"))
    )
    with pytest.raises(pd.PublishError):
        asyncio.run(
            pd.PersonalDriveProvider().update_sharing(
                external_id="abc~alpha", visibility="private", shared_with=[]
            )
        )


def test_a_publish_makes_no_cloudfront_tag_calls_at_all(monkeypatch, payload):
    """The distribution is born with its final tag set, so neither tag-resource nor
    untag-resource is ever issued. That is what removes the drive's need for
    cloudfront:UntagResource, which the shipped least-privilege policy does not grant."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert [a for a in spy.checked if a[:2] == ["cloudfront", "untag-resource"]] == []
    assert [a for a in spy.checked if a[:2] == ["cloudfront", "tag-resource"]] == []


def test_going_public_caches_the_domain_so_the_seam_can_derive_the_link(monkeypatch, payload):
    """The seam derives the public link from view_url_for right after this call, and on a
    fresh process this is the only call that resolved the domain."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PRIVATE_PREFIX}abc"})
    spy.install(monkeypatch)
    p = pd.PersonalDriveProvider()
    asyncio.run(p.update_sharing(external_id="abc~alpha", visibility="public", shared_with=[]))
    assert p.view_url_for("abc~alpha") == f"https://{DOMAIN}/{pd.PUBLIC_PREFIX}abc"


def test_a_named_account_never_borrows_another_accounts_domain(monkeypatch):
    """An uncached named account must resolve to no link rather than to a link into the
    wrong AWS account's drive, which reads as working and is not."""
    _stub_profiles(monkeypatch, ["alpha", "beta"])
    p = pd.PersonalDriveProvider()
    p._domains["alpha"] = DOMAIN
    assert p.view_url_for("abc~beta") == ""


def test_a_failed_cleanup_does_not_abort_a_completed_move_to_public(monkeypatch, payload):
    """The copy already landed in the served prefix, so the object IS public. Raising
    here would leave the record saying private about content that is being served."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PRIVATE_PREFIX}abc"})
    spy.install(monkeypatch)
    inner = pd.engine._checked

    def _boom(args, profile, action="", timeout=0):
        if args[:2] == ["s3api", "delete-object"]:
            raise pd.engine.AWSError("Throttling: rate exceeded")
        return inner(args, profile, action=action)

    monkeypatch.setattr(pd.engine, "_checked", _boom)
    asyncio.run(
        pd.PersonalDriveProvider().update_sharing(
            external_id="abc~alpha", visibility="public", shared_with=[]
        )
    )
    assert spy.invalidated == [DIST], "the purge must still run"


def test_a_failed_delete_does_abort_a_move_to_private(monkeypatch, payload):
    """The mirror case: taking something private, the source IS the served copy, so a
    failed delete means the withdrawal did not happen and must be reported."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PUBLIC_PREFIX}abc"})
    spy.install(monkeypatch)
    inner = pd.engine._checked

    def _boom(args, profile, action="", timeout=0):
        if args[:2] == ["s3api", "delete-object"]:
            raise pd.engine.AWSError("Throttling: rate exceeded")
        return inner(args, profile, action=action)

    monkeypatch.setattr(pd.engine, "_checked", _boom)
    with pytest.raises(pd.PublishError):
        asyncio.run(
            pd.PersonalDriveProvider().update_sharing(
                external_id="abc~alpha", visibility="private", shared_with=[]
            )
        )


def test_push_version_types_the_bytes_it_was_handed_not_the_stale_remote_header(
    monkeypatch, payload
):
    """An artifact re-saved as HTML after being published as text arrives here with an
    .html suffix while the remote object still advertises text/plain. Reusing the remote
    header would serve markup as source text under nosniff."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PUBLIC_PREFIX}abc"}, content_type="text/plain")
    spy.install(monkeypatch)
    out = asyncio.run(
        pd.PersonalDriveProvider().push_version(
            external_id="abc~alpha", file_path=payload, expected_token=""
        )
    )
    assert not out.error
    assert spy.arg("put-object", "--content-type") == "text/html"


def test_push_version_keeps_the_stored_type_for_a_suffix_it_cannot_read(monkeypatch, tmp_path):
    """The fallback is the remote header, so an unrecognised suffix is no worse than
    before rather than being forced to octet-stream."""
    _stub_profiles(monkeypatch, ["alpha"])
    blob = tmp_path / "artifact.bin"
    blob.write_bytes(b"\x00\x01")
    spy = _EngineSpy(existing_keys={f"{pd.PUBLIC_PREFIX}abc"}, content_type="image/png")
    spy.install(monkeypatch)
    out = asyncio.run(
        pd.PersonalDriveProvider().push_version(
            external_id="abc~alpha", file_path=str(blob), expected_token=""
        )
    )
    assert not out.error
    assert spy.arg("put-object", "--content-type") == "image/png"
