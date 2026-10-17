"""What the public edition registers for the personal drive -- boot never imports the
publish stack, the drive is one destination and never the default -- and the order a
drive is built in: the bucket policy before discovery, an opaque serving origin, a
reused headers policy and distribution.
``test_publish_personal_drive.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from publish_personal_drive_helpers import _clean_registry  # noqa: F401 - fixture
from publish_personal_drive_helpers import payload_fixture  # noqa: F401 - fixture
from publish_personal_drive_helpers import (
    DIST,
    POLICY_EXISTING,
    POLICY_NEW,
    _EngineSpy,
    _publish,
    _stub_profiles,
)

from kiro_crew import publish_provider as pp
from kiro_crew.publish import personal_drive as pd

# ── what the public edition registers ────────────────────────────────────────


def test_boot_does_not_import_the_publish_stack(monkeypatch):
    """Platform bootstrap must register the drive WITHOUT importing the provider module.

    `register_publish_providers` runs inside platform bootstrap, before the gateway binds
    its socket, so anything imported there is added to every install's time-to-ready --
    including the installs that never select this destination, which is most of them now
    that the drive is opt-in. Importing it eagerly cost ~0.5s of cumulative import,
    because the module reaches the deploy engine's profile registry, the artifact store
    and the validation stack. The registry was already factory-based and instantiates
    lazily, so only the import needed moving into the closure.

    Asserted by absence from ``sys.modules``, which is the only thing that actually
    demonstrates the import did not happen. The key the factory registers under is
    duplicated in `defaults.py` precisely so bootstrap does not need the module to learn
    it, and the second assertion pins that the copy has not drifted.

    Note the setup: the parent package's ATTRIBUTE has to be deleted as well as the
    ``sys.modules`` entry. `kiro_crew.publish` stays imported and keeps `personal_drive`
    bound on it, so `from kiro_crew.publish import personal_drive` would resolve that
    attribute and never enter the import system -- which made an earlier version of this
    test pass even with the eager import restored.

    And the teardown puts the SAME module object back rather than re-importing it. A
    re-import produces a second `PersonalDriveProvider` class, and every later test that
    holds the first one fails `isinstance` against instances built from the second -- an
    earlier version of this test broke `test_public_edition_registry_wires_the_drive`
    exactly that way.
    """
    import sys

    import kiro_crew.publish as publish_pkg
    from kiro_crew.platform.defaults import DefaultPublishRegistry

    original = sys.modules.pop("kiro_crew.publish.personal_drive", None)
    had_attr = hasattr(publish_pkg, "personal_drive")
    if had_attr:
        delattr(publish_pkg, "personal_drive")
    try:
        DefaultPublishRegistry().register_publish_providers()
        assert (
            "kiro_crew.publish.personal_drive" not in sys.modules
        ), "bootstrap imported the provider module; the import must live inside the factory"
    finally:
        if original is not None:
            sys.modules["kiro_crew.publish.personal_drive"] = original
        if had_attr:
            setattr(publish_pkg, "personal_drive", original)

    # The duplicated key must still name the same destination the module registers.
    assert DefaultPublishRegistry._PERSONAL_DRIVE_KEY == pd.PERSONAL_DRIVE_PROVIDER

    # And the factory must still produce the real provider when it is finally called.
    assert isinstance(pp.get_provider(pd.PERSONAL_DRIVE_PROVIDER), pd.PersonalDriveProvider)


def test_registration_does_not_make_the_drive_the_default_destination(monkeypatch):
    """Registering the drive must NOT capture the unnamed destination.

    ``publish_sync`` resolves a publish that names no provider through
    ``DEFAULT_PROVIDER``. The drive registers under its own key instead, so it is
    reachable only by asking for it, and an unnamed publish gets the same 503 it got
    before this module existed. Holding the default back is what keeps every window
    that depends on the unfinished publication-existence contract behind an explicit
    opt-in rather than on every user's default path.
    """
    _stub_profiles(monkeypatch, ["alpha", "beta"], default="alpha")
    pd.register_public_edition_providers()
    # Asked for BY NAME: available.
    assert isinstance(pp.get_provider(pd.PERSONAL_DRIVE_PROVIDER), pd.PersonalDriveProvider)
    # Asked for as the unnamed default: refused, exactly as with an empty registry.
    with pytest.raises(pp.PublishUnavailableError):
        pp.get_provider()


def test_registration_exposes_exactly_one_destination(monkeypatch):
    """No per-account keys: they cannot pass the publish gate, so listing them would
    put unusable rows in the picker."""
    _stub_profiles(monkeypatch, ["alpha", "beta"], default="alpha")
    pd.register_public_edition_providers()
    ids = {p.name for p in pp.list_providers()}
    assert ids == {pd.PERSONAL_DRIVE_PROVIDER}


def test_provider_list_has_no_duplicate_rows(monkeypatch):
    """One row per destination in the picker.

    ``list_providers`` collapses registry keys that name the same factory, so an
    alias cannot add a row. This pins the other half: the public edition's
    registration adds no distinct-but-redundant destination either."""
    _stub_profiles(monkeypatch, ["alpha", "beta"], default="alpha")
    pd.register_public_edition_providers()
    listed = pp.list_providers()
    assert len(listed) == len({p.name for p in listed})
    assert len(listed) == len({p.display_name for p in listed})


def test_registry_key_and_provider_name_agree(monkeypatch):
    """The seam documents ``name`` as the registry key; a mismatch would break any
    caller that round-trips a listed provider's name back through get_provider."""
    _stub_profiles(monkeypatch, ["alpha"], default="alpha")
    pd.register_public_edition_providers()
    for key in list(pp._FACTORIES):
        assert pp.get_provider(key).name == key


def test_the_bucket_policy_lands_before_the_drive_becomes_discoverable(monkeypatch, payload):
    """The drive tag is the only thing _find_drive keys on, so it must not go on until
    the bucket policy is installed. Reversed, ONE PutBucketPolicy failure leaves a drive
    that discovery reports as complete and that serves 403 for the life of the account:
    the next publish finds bucket + distribution, hands out a link, and nothing ever
    goes back to install the policy."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    verbs = [tuple(a[:2]) for a in spy.checked]
    policy = verbs.index(("s3api", "put-bucket-policy"))
    assert policy >= 0, "the served prefix must be granted before a link is handed out"


def test_the_drive_serves_every_artifact_from_an_opaque_origin(monkeypatch, payload):
    """Every artifact on the drive shares ONE domain, so without this they share a browser
    origin and one published document can read what another wrote to localStorage. The
    documents are mutually untrusted authored content, so the CSP sandbox directive gives
    each an opaque origin -- scripts run, storage is unreachable. It has to be a HEADER:
    the sandbox directive is ignored in a <meta> CSP by specification."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    created = [a for a in spy.checked if a[:2] == ["cloudfront", "create-response-headers-policy"]]
    assert created, "the drive must define its own response headers"
    cfg = json.loads(created[0][created[0].index("--response-headers-policy-config") + 1])
    csp = cfg["SecurityHeadersConfig"]["ContentSecurityPolicy"]
    assert csp["ContentSecurityPolicy"].startswith("sandbox ")
    assert csp["Override"] is True
    assert spy.policy_ids == [POLICY_NEW], "the distribution must actually carry it"


def test_a_second_drive_reuses_the_existing_headers_policy(monkeypatch, payload):
    """The policy is account-global, so a second drive or a retried create must find it
    rather than fail on the name collision."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False, existing_policy=True)
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert [
        a for a in spy.checked if a[:2] == ["cloudfront", "create-response-headers-policy"]
    ] == []
    assert spy.policy_ids == [POLICY_EXISTING]


def test_a_first_publish_uploads_before_it_reports_the_rollout(monkeypatch, payload):
    """A distribution is always InProgress in the minutes after it is created, so
    asserting first made the very first publish on a fresh account fail with nothing
    written -- leaving a tagged distribution over an empty origin, the dangling-origin
    shape this destination exists to avoid. The object goes up, then the state is
    reported, so the same link works the moment the rollout finishes.

    And the report is a NOTICE, not a raise. The orchestration records the publication
    only when publish returns, so raising after the upload stranded a public object with
    no withdrawal handle -- while the message claimed "nothing was lost"."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False, status="InProgress")
    spy.install(monkeypatch)
    res = _publish(pd.PersonalDriveProvider(), payload)
    assert spy.put_keys(), "the origin must not be left empty"
    assert any(k.startswith(pd.PUBLIC_PREFIX) for k in spy.put_keys()), spy.put_keys()
    assert "still rolling out" in res.notice
    assert res.external_id, "the handle for the uploaded object must come back"


def test_a_failed_purge_after_a_push_still_returns_the_new_token(monkeypatch, payload):
    """The bytes are already uploaded, so reporting failure would leave the caller holding
    the previous token while the object carries the new digest -- and a stored digest
    matching neither the token nor the next payload is a conflict, so every later push
    would conflict forever with nothing able to reconcile it."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(existing_keys={f"{pd.PUBLIC_PREFIX}abc"})
    spy.install(monkeypatch)
    monkeypatch.setattr(
        pd.engine, "invalidate", lambda d, p: (_ for _ in ()).throw(pd.engine.AWSError("throttled"))
    )
    out = asyncio.run(
        pd.PersonalDriveProvider().push_version(
            external_id="abc~alpha", file_path=payload, expected_token=""
        )
    )
    assert not out.error, "a failed purge must not discard the token"
    assert out.concurrency_token


def test_a_denied_policy_reassert_blocks_a_link_but_not_a_withdrawal(monkeypatch, payload):
    """The two objections to this call are both real and they point opposite ways, so the
    rule is the one the serving assertion already uses. Handing out a link while the bucket
    is unconfirmed promises a URL that may 403; raising on a withdrawal instead turns an
    optional permission into one every call needs, so a profile without
    s3:PutBucketPolicy would lose the ability to withdraw from a drive that works."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    inner = pd.engine._checked

    def _boom(args, profile, action="", timeout=0):
        if args[:2] == ["s3api", "put-bucket-policy"]:
            raise pd.engine.AWSError("AccessDenied: s3:PutBucketPolicy")
        return inner(args, profile, action=action)

    monkeypatch.setattr(pd.engine, "_checked", _boom)
    with pytest.raises(pd.PublishError):
        _publish(pd.PersonalDriveProvider(), payload)
    asyncio.run(pd.PersonalDriveProvider().unpublish(external_id="abc~alpha"))
    assert spy.invalidated == [DIST], "the withdrawal must still complete"


def test_a_reused_drive_reinstalls_the_bucket_policy(monkeypatch, payload):
    """Both resources are tagged, so discovery finds the drive the moment it exists. If a
    first publish lost PutBucketPolicy to a denial or a throttle, the reuse path would
    otherwise hand out links to a bucket CloudFront cannot read -- 403 for the life of the
    account, reported as success. Re-installing makes that state self-healing, and also
    repairs a policy deleted by hand."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    policies = [a for a in spy.checked if a[:2] == ["s3api", "put-bucket-policy"]]
    assert policies, "a publish onto an existing drive must re-assert the policy"
    assert pd.PUBLIC_PREFIX in policies[0][policies[0].index("--policy") + 1]


def test_the_distribution_is_born_with_the_drive_tag(monkeypatch, payload):
    """Without the drive's own tag the distribution is invisible to this module's
    discovery, and because a missing distribution reads as 'no drive yet', every publish
    would build another one."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert spy.created_tags, "the distribution must be created with an explicit tag set"
    keys = {t["Key"] for t in spy.created_tags[0]}
    assert pd.TAG_DRIVE in keys


def test_the_distribution_is_never_born_with_the_deploy_site_tag(monkeypatch, payload):
    """Carrying kirocrew:site the pooled distribution shows on the deploy site surface,
    where a destroy deletes the drive every artifact lives on. Supplying the final tags on
    create is what removes the window a tag-then-untag left open, and with it the
    cloudfront:UntagResource permission the shipped least-privilege policy never grants."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    keys = {t["Key"] for t in spy.created_tags[0]}
    assert pd.engine.TAG_SITE not in keys
    assert [a for a in spy.checked if a[:2] == ["cloudfront", "untag-resource"]] == []
    assert [a for a in spy.checked if a[:2] == ["cloudfront", "tag-resource"]] == []


def test_a_second_publish_reuses_the_distribution(monkeypatch, payload):
    """The regression the re-tag prevents: discovery must find the distribution it just
    created, or each publish leaks another one and none of them is ever serving."""
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy(drive=False)
    spy.install(monkeypatch)
    _publish(pd.PersonalDriveProvider(), payload)
    assert len(spy.created_distributions) == 1
    spy.drive = True  # the drive now exists and carries the drive tag
    _publish(pd.PersonalDriveProvider(), payload)
    assert len(spy.created_distributions) == 1, "a second publish must not build another"


def test_registration_survives_an_unreadable_registry(monkeypatch):
    """Registration runs at boot; a broken registry must not take the gateway down."""

    def _boom():
        raise OSError("registry unreadable")

    monkeypatch.setattr(pd.profiles, "load_registry", _boom)
    pd.register_public_edition_providers()
    assert set(pp._FACTORIES) == {pd.PERSONAL_DRIVE_PROVIDER}


def test_the_artifact_store_is_never_imported_at_module_scope():
    """Ratchet, not decoration. ``kiro_crew.artifacts`` resolves config while it is
    still importing, which installs the platform context, which registers this provider,
    which imports this module. A module-scope ``from kiro_crew.artifacts import ...``
    therefore ran against a half-initialised module and every publish died on a
    circular-import ImportError -- it reds the sandbox shard, not this file, so only a
    check like this one stops it coming back as a tidy-up."""
    import ast

    source = Path(pd.__file__).with_suffix(".py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = [
        node.module
        for node in tree.body  # module scope ONLY -- nested imports are the fix
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("kiro_crew.artifacts")
    ]
    assert offenders == [], f"module-scope import of {offenders} recreates the cycle"


def test_public_edition_registry_wires_the_drive(monkeypatch):
    """The platform seam is the only place the edition names a concrete provider."""
    from kiro_crew.platform.defaults import DefaultPublishRegistry

    _stub_profiles(monkeypatch, ["alpha"])
    DefaultPublishRegistry().register_publish_providers()
    assert isinstance(pp.get_provider(pd.PERSONAL_DRIVE_PROVIDER), pd.PersonalDriveProvider)
