"""The personal drive's read-back half: ``view_url``, withdrawals and publishes bound to
the publication's own account and its external id, and ``serving_notice``, which the
dashboard's re-probe depends on.
``test_publish_personal_drive.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from publish_personal_drive_helpers import _clean_registry  # noqa: F401 - fixture
from publish_personal_drive_helpers import payload_fixture  # noqa: F401 - fixture
from publish_personal_drive_helpers import (
    ACCOUNT_ID,
    BUCKET,
    DIST,
    DOMAIN,
    _EngineSpy,
    _publish,
    _stub_profiles,
)

from kiro_crew import publish_provider as pp
from kiro_crew.publish import personal_drive as pd
from kiro_crew.publish_provider import PublishError

# ── view_url ─────────────────────────────────────────────────────────────────


def test_view_url_is_empty_until_the_domain_is_known():
    """The seam treats an empty url as 'published, no link' rather than failure."""
    assert pd.PersonalDriveProvider().view_url_for("abc~alpha") == ""


def test_view_url_uses_the_served_prefix_and_the_random_half_only():
    p = pd.PersonalDriveProvider()
    p._domains["alpha"] = "d9.cloudfront.net"
    assert p.view_url_for("abc~alpha") == "https://d9.cloudfront.net/public/abc"


def test_a_repointed_profile_refuses_to_withdraw_from_the_wrong_account(monkeypatch):
    """The defect this binding exists to close, and it needs no race at all.

    A profile name is a local alias. Repoint it at another account, delete the artifact,
    and the withdrawal must NOT resolve the NAME to the new account, delete an absent key
    there, report success, and let the record be cleared -- leaving the original copy
    public with the only handle able to remove it erased. A config edit plus an ordinary
    delete was enough; no concurrency, no failure, first try.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    # Published under ACCOUNT_ID; the profile now resolves somewhere else.
    _EngineSpy(account="999988887777").install(monkeypatch)
    ident = f"abc{pd._ID_SEP}{ACCOUNT_ID}{pd._ID_SEP}alpha"
    with pytest.raises(PublishError) as exc:
        asyncio.run(pd.PersonalDriveProvider().unpublish(external_id=ident))
    msg = str(exc.value)
    assert "different AWS account" in msg, msg
    assert "nothing was changed" in msg


def test_an_unconfirmable_account_refuses_rather_than_guessing(monkeypatch):
    """Unproven is treated as unsafe here, unlike the reads that stay lenient.

    ``sts:GetCallerIdentity`` is not gated by any IAM policy, so a failure is credentials
    not resolving rather than a tier being too narrow -- which is why this one refuses
    where the distribution-config reads deliberately do not. Acting on an account we cannot
    identify risks removing nothing and clearing the record; refusing is retryable.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(account="").install(monkeypatch)
    ident = f"abc{pd._ID_SEP}{ACCOUNT_ID}{pd._ID_SEP}alpha"
    with pytest.raises(PublishError) as exc:
        asyncio.run(pd.PersonalDriveProvider().unpublish(external_id=ident))
    assert "could not be confirmed" in str(exc.value)


def test_a_matching_account_withdraws_normally(monkeypatch):
    """The guard must not make the ordinary case refuse."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy().install(monkeypatch)
    ident = f"abc{pd._ID_SEP}{ACCOUNT_ID}{pd._ID_SEP}alpha"
    asyncio.run(pd.PersonalDriveProvider().unpublish(external_id=ident))


def test_a_publish_refuses_when_the_account_cannot_be_read(monkeypatch, payload):
    """Read the account BEFORE minting the handle, or the handle is unverifiable forever.

    Minting without an account would create exactly the publication whose withdrawal
    cannot be checked later, so this fails before anything is uploaded.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy(account="").install(monkeypatch)
    with pytest.raises(PublishError) as exc:
        _publish(pd.PersonalDriveProvider(), payload)
    assert "could not be confirmed" in str(exc.value)


def test_a_published_handle_carries_the_live_account(monkeypatch, payload):
    """End to end: the handle a real publish hands back is verifiable."""
    _stub_profiles(monkeypatch, ["alpha"])
    _EngineSpy().install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload)
    assert pd.bound_account(res.external_id) == ACCOUNT_ID
    key_part, bound = pd.split_external_id(res.external_id)
    assert bound == "alpha" and key_part


def test_external_id_round_trips():
    ident = pd.make_external_id("alpha", ACCOUNT_ID)
    key_part, bound = pd.split_external_id(ident)
    assert bound == "alpha"
    assert pd.bound_account(ident) == ACCOUNT_ID
    assert key_part and pd._ID_SEP not in key_part


def test_a_profile_containing_the_separator_cannot_corrupt_the_key():
    """The random half comes FIRST precisely so the key survives any profile name.

    With the account field in the middle this matters more than it did: the parser has to
    tell ``key~account~we~ird`` apart from a two-field handle, and it does so on the
    account's SHAPE rather than on how many separators it finds.
    """
    ident = pd.make_external_id("we~ird", ACCOUNT_ID)
    key_part, bound = pd.split_external_id(ident)
    assert bound == "we~ird", "the profile is everything after the account, separators included"
    assert pd.bound_account(ident) == ACCOUNT_ID
    assert pd._ID_SEP not in key_part


def test_a_pre_binding_handle_is_read_as_unbound_not_as_an_account():
    """The one parse error that would break every existing publication.

    A handle minted before account binding is ``key~profile``. Reading that middle field
    as an account would make every withdrawal compare a profile NAME against a live
    account id, mismatch every time, and refuse every operation on every publication that
    predates this change. So the field counts as an account only if it has an account's
    shape, and a two-field handle reports no account -- which is what makes it keep the
    older, weaker behaviour instead of being refused.
    """
    key_part, bound = pd.split_external_id("abc~alpha")
    assert bound == "alpha"
    assert key_part == "abc"
    assert pd.bound_account("abc~alpha") == ""

    # THE case the shape guard exists for, and the only one that can actually go wrong: a
    # pre-binding handle whose PROFILE contains the separator. Counting separators would
    # read `we` as the account and `ird` as the profile -- corrupting both, and then
    # refusing every operation on it because `we` never matches a live account id.
    key_part, bound = pd.split_external_id("abc~we~ird")
    assert bound == "we~ird", "a non-account middle field belongs to the profile"
    assert key_part == "abc"
    assert pd.bound_account("abc~we~ird") == ""

    # A profile that happens to be twelve digits is the ambiguous case, and it resolves
    # toward treating the handle as BOUND. That is the safe direction: the account check
    # then runs and can only refuse, where the other reading would silently skip it.
    assert pd.bound_account(f"abc~{ACCOUNT_ID}~beta") == ACCOUNT_ID

    # No separator at all: both fields unknown, caller falls back to the registry default.
    assert pd.split_external_id("abc") == ("abc", "")
    assert pd.bound_account("abc") == ""


# ── serving_notice: the read-back half the dashboard's re-probe depends on ──────
#
# Without this override the seam's default returns None for every provider, which
# makes the re-probe a silent no-op: the "Check again" control would call it and the
# stale banner would never clear. The caller distinguishes three outcomes, and the
# difference between them is the whole safety property -- an EMPTY pair means
# "checked, condition cleared, clear the record", while None means "could not
# establish anything, leave the record alone". Guessing the former from a failed
# look would drop a warning that is still true.


def test_serving_notice_returns_an_empty_pair_once_the_drive_is_healthy(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    provider = pd.PersonalDriveProvider()
    res = _publish(provider, payload)
    probed = asyncio.run(provider.serving_notice(external_id=res.external_id))
    # Deployed + enabled -> nothing to say, which is what lets the re-probe clear.
    assert probed == ("", "")


def test_serving_notice_reports_the_live_condition_when_one_remains(monkeypatch, payload):
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(status="InProgress")
    spy.install(monkeypatch)
    provider = pd.PersonalDriveProvider()
    res = _publish(provider, payload, visibility="private")
    probed = asyncio.run(provider.serving_notice(external_id=res.external_id))
    assert probed is not None
    text, code = probed
    assert code == "rolling_out"
    assert text
    # NOT an empty pair: a live condition must refresh the notice, never clear it.
    assert probed != ("", "")


def test_serving_notice_returns_none_when_the_drive_cannot_be_found(monkeypatch, payload):
    """A missing drive is 'could not establish', NOT 'cleared'."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    provider = pd.PersonalDriveProvider()
    res = _publish(provider, payload)
    # Re-probe against an account whose drive is not discoverable.
    gone = _EngineSpy(drive=False, cf_tags_visible=False, cf_own_tags=False)
    gone.install(monkeypatch)
    assert asyncio.run(provider.serving_notice(external_id=res.external_id)) is None


def test_reachable_for_follows_the_publications_own_account(monkeypatch, payload):
    """Two accounts registered, and the one this publication is bound to is removed.

    `available()` stays True the whole time -- an account IS registered -- which is
    exactly why the withdrawal paths cannot use it: they would attempt a call against
    a account that does not exist and report the failure as retryable.
    """
    _stub_profiles(monkeypatch, ["alpha", "work"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    provider = pd.PersonalDriveProvider()
    res = _publish(provider, payload)
    assert provider.reachable_for(external_id=res.external_id) is True
    # 'work' remains registered, so the destination is still offerable...
    _stub_profiles(monkeypatch, ["work"])
    assert provider.available() is True
    # ...but this publication's own account is gone, which is the narrow answer.
    assert provider.reachable_for(external_id=res.external_id) is False


def test_serving_notice_runs_its_aws_calls_off_the_event_loop(monkeypatch, payload):
    """The probe's body is blocking AWS CLI subprocesses; on the loop it freezes the
    gateway.

    The repo's `no-blocking-call-on-event-loop` ratchet cannot see this: the blocking
    call is four levels down (`_require_drive` -> `_find_drive` -> `engine._checked` ->
    `run_aws` -> `subprocess.run`), so the property is asserted directly here by
    recording which thread the AWS layer is entered on.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    provider = pd.PersonalDriveProvider()
    res = _publish(provider, payload)

    seen: list[str] = []
    real_status = pd.engine.distribution_status

    def _recording_status(dist_id, profile):
        seen.append(threading.current_thread().name)
        return real_status(dist_id, profile)

    monkeypatch.setattr(pd.engine, "distribution_status", _recording_status)

    async def _drive() -> object:
        loop_thread = threading.current_thread().name
        probed = await provider.serving_notice(external_id=res.external_id)
        return loop_thread, probed

    loop_thread, probed = asyncio.run(_drive())
    assert probed is not None, "the probe must still return its answer"
    assert seen, "the AWS layer was never reached, so this asserts nothing"
    # The blocking work must NOT have run on the thread carrying the event loop.
    assert all(
        t != loop_thread for t in seen
    ), f"AWS calls ran on the event-loop thread {loop_thread!r}: {seen!r}"


def test_the_in_lock_recheck_reasserts_the_bucket_policy_before_returning(monkeypatch, payload):
    """`_create_drive`'s in-lock recheck is a REUSE path and carries the reuse
    obligation.

    The read policy is installed after the distribution (it pins the distribution ARN),
    so it lands after the drive is discoverable. A concurrent first publish that lost
    `PutBucketPolicy` to a denial or a throttle therefore leaves a drive this recheck
    finds COMPLETE -- and returning without re-installing hands back a link to a bucket
    CloudFront cannot read: 403 for the life of the account, reported as success. The
    two other reuse paths already re-install; this one did not.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    # No drive at first call (so the create path is entered and takes the lock), then a
    # complete drive on the in-lock recheck -- the concurrent-creation race.
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    provider = pd.PersonalDriveProvider()

    calls = {"n": 0}
    complete = {
        "bucket": BUCKET,
        "distribution_id": DIST,
        "distribution_arn": f"arn:aws:cloudfront::1234:distribution/{DIST}",
        "domain": DOMAIN,
    }

    def _find(profile, region):
        calls["n"] += 1
        # 1st call: pre-lock discovery sees nothing. 2nd: the in-lock recheck sees a
        # drive a concurrent publish just finished building.
        return None if calls["n"] == 1 else dict(complete)

    monkeypatch.setattr(provider, "_find_drive", _find)
    _publish(provider, payload)

    assert calls["n"] >= 2, "the in-lock recheck must have run"
    # It must NOT have built a second distribution (that is the lock's job) ...
    assert spy.created_distributions == [], "the recheck must reuse, not create"
    # ... and it MUST have re-installed the read policy before handing back a link.
    policies = [a for a in spy.checked if a[:2] == ["s3api", "put-bucket-policy"]]
    assert policies, "the in-lock reuse path must reassert the bucket policy"


def test_a_racing_public_publish_refuses_rather_than_handing_out_a_403_link(monkeypatch, payload):
    """The in-lock reuse branch must fail CLOSED for a caller that hands out a URL.

    `publish` resolves the drive with `require_serving=False` -- it uploads before it
    checks the rollout -- while still being the one path that returns a link. So the
    fatality flag has to be `link_promised`, which is what `_reassert_bucket_policy`'s
    docstring says ("deliberately NOT require_serving"). Wired to `require_serving` this
    branch warned instead of raising, uploaded, and returned a view URL for a bucket the
    delivery network cannot read: a 403 for the life of the account, reported as success.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    provider = pd.PersonalDriveProvider()

    calls = {"n": 0}
    complete = {
        "bucket": BUCKET,
        "distribution_id": DIST,
        "distribution_arn": f"arn:aws:cloudfront::1234:distribution/{DIST}",
        "domain": DOMAIN,
    }

    def _find(profile, region):
        calls["n"] += 1
        # Pre-lock discovery misses; the in-lock recheck finds a drive a concurrent
        # first publish just finished building.
        return None if calls["n"] == 1 else dict(complete)

    monkeypatch.setattr(provider, "_find_drive", _find)

    inner = pd.engine._checked

    def _policy_denied(args, profile, action="", timeout=0, **kw):
        if args[:2] == ["s3api", "put-bucket-policy"]:
            raise pd.engine.AWSError("AccessDenied: s3:PutBucketPolicy")
        return inner(args, profile, action=action, **kw)

    monkeypatch.setattr(pd.engine, "_checked", _policy_denied)

    # A PUBLIC publish promises a link, so the unconfirmable read policy must refuse.
    with pytest.raises(pd.PublishError) as exc:
        _publish(provider, payload, visibility="public")
    assert "no link was handed out" in str(exc.value)


def test_a_racing_private_publish_still_proceeds_when_the_policy_cannot_be_reasserted(
    monkeypatch, payload
):
    """The other half of the same distinction: a private publish hands out no link, so
    an unconfirmable read policy must NOT block it -- raising there would turn an
    optional permission into one every call requires."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    provider = pd.PersonalDriveProvider()

    calls = {"n": 0}
    complete = {
        "bucket": BUCKET,
        "distribution_id": DIST,
        "distribution_arn": f"arn:aws:cloudfront::1234:distribution/{DIST}",
        "domain": DOMAIN,
    }

    def _find(profile, region):
        calls["n"] += 1
        return None if calls["n"] == 1 else dict(complete)

    monkeypatch.setattr(provider, "_find_drive", _find)
    inner = pd.engine._checked

    def _policy_denied(args, profile, action="", timeout=0, **kw):
        if args[:2] == ["s3api", "put-bucket-policy"]:
            raise pd.engine.AWSError("AccessDenied: s3:PutBucketPolicy")
        return inner(args, profile, action=action, **kw)

    monkeypatch.setattr(pd.engine, "_checked", _policy_denied)
    res = _publish(provider, payload, visibility="private")
    assert res.external_id, "a private publish must not be blocked by the read policy"


def test_kind_support_declines_exactly_the_kinds_the_seam_refuses():
    """The picker only offers a provider whose answer is not UNSUPPORTED, so declaring a
    kind hostable that the seam then rejects offers a publish that ends in a 400.

    The declaration and the refusal must read ONE set. Answering NATIVE for everything
    was true about the destination -- a blob store could hold the bytes -- and false
    about what a publish does, because the render path is str-typed and an image keeps
    its raster outside `content`.
    """
    provider = pd.PersonalDriveProvider()
    for kind in pp.NON_TEXT_KINDS:
        assert provider.kind_support(kind) is pp.KindSupport.UNSUPPORTED, kind
    # Everything the seam CAN carry stays first-class.
    for kind in ("markdown", "html", "widget", "text", "svg", "json"):
        assert provider.kind_support(kind) is pp.KindSupport.NATIVE, kind
    # The set is non-empty, or this test would assert nothing at all.
    assert pp.NON_TEXT_KINDS


def test_serving_notice_never_writes_a_bucket_policy(monkeypatch, payload):
    """The re-probe is a READ, and "Check again" is reachable from a state where
    publishing was DENIED -- so this path must not mutate the account.

    It must not resolve the drive through ``_require_drive``, which re-asserts the bucket
    policy whenever the distribution carries an ARN. That is an ``s3:PutBucketPolicy``
    write, so routing a read through it lets a caller who could not publish drive a policy
    write from the notice path.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(status="InProgress")
    spy.install(monkeypatch)
    provider = pd.PersonalDriveProvider()
    res = _publish(provider, payload, visibility="private")

    # Arm the tripwire only AFTER the publish, so the first publish's legitimate
    # policy assertion is not what this test catches.
    writes: list[object] = []
    monkeypatch.setattr(
        pd.PersonalDriveProvider,
        "_reassert_bucket_policy",
        lambda self, *a, **kw: writes.append(a),
    )

    probed = asyncio.run(provider.serving_notice(external_id=res.external_id))

    assert writes == [], f"the re-probe asserted a bucket policy: {writes!r}"
    # And it still does its job -- a test that broke the read would also pass the
    # no-write assertion, so pin the answer too.
    assert probed is not None
    assert probed[1] == "rolling_out"
