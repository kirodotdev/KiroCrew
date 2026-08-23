"""Consent must precede any billable AWS request.

Three properties, one per step of the fix:

1. The voice-reply provider default is the LOCAL one, so enabling voice reply
   without naming a provider cannot reach a paid AWS service.
2. Every gated call site refuses when there is no matching grant, and the grant
   record lives on the keystone floor where the agent cannot mint it.
3. The credential source is named (an empty profile is NOT "no account"), and a
   grant recorded for one account does not survive the profile being repointed
   at another.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew import aws_consent


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolated data home so the keystone file never touches the real one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config.loader import config_dir

    config_dir().mkdir(parents=True, exist_ok=True)
    aws_consent._probe_cache.clear()
    yield tmp_path
    aws_consent._probe_cache.clear()


def _consent_request(
    *,
    app: str = "",
    user: str = "owner-1",
    owner: str = "owner-1",
    query: dict | None = None,
    body: dict | None = None,
):
    """A consent-endpoint request shaped like a real DASHBOARD OWNER call.

    ``is_owner_dashboard_request`` requires ``app`` present-and-empty AND the
    caller to equal the configured ``owner_id``, so a bare MagicMock (whose
    ``get`` returns truthy stubs) is refused. Cases that mean to be refused pass
    a non-empty ``app`` or a mismatched ``user``.
    """
    from unittest.mock import MagicMock

    req = MagicMock()
    req.path = "/api/aws/consent"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
    req.rel_url.query = query or {}
    req.json = AsyncMock(return_value=body or {})
    return req


def _grant(service=aws_consent.SERVICE_POLLY, *, profile="", region="us-east-1", account="1234"):
    return aws_consent.record_grant(
        service,
        profile=profile,
        region=region,
        account=account,
        arn=f"arn:aws:iam::{account}:user/x",
        granted_at="2026-08-21T00:00:00+00:00",
    )


# ── Step 1: the default provider is local ──


class TestProviderDefaultIsLocal:
    """Turning voice on without naming a provider must not reach AWS."""

    def test_dataclass_default_is_piper(self):
        from kiro_crew.slack.handler import _VoiceConfig
        from kiro_crew.voice_reply import DEFAULT_PROVIDER, PROVIDER_PIPER

        assert DEFAULT_PROVIDER == PROVIDER_PIPER
        assert _VoiceConfig().provider == PROVIDER_PIPER

    def test_absent_provider_key_loads_as_piper(self, home):
        """The regression: a config with voice ON but no provider named."""
        from kiro_crew.config.loader import config_path
        from kiro_crew.slack.handler import _vc, load_voice_reply_config
        from kiro_crew.voice_reply import PROVIDER_PIPER

        config_path().write_text(json.dumps({"voice_reply": {"enabled": True}}))
        load_voice_reply_config()
        assert _vc.provider == PROVIDER_PIPER

    def test_invalid_provider_falls_back_to_piper(self, home):
        from kiro_crew.config.loader import config_path
        from kiro_crew.slack.handler import _vc, load_voice_reply_config
        from kiro_crew.voice_reply import PROVIDER_PIPER

        config_path().write_text(json.dumps({"voice_reply": {"provider": "ploly"}}))
        load_voice_reply_config()
        assert _vc.provider == PROVIDER_PIPER


# ── Step 2: the gate, and where the grant lives ──


class TestGrantIsOnTheKeystoneFloor:
    """The agent must not be able to consent on the operator's behalf."""

    def test_leaf_is_fenced_for_read_and_write(self):
        from kiro_crew.config.loader import aws_consent_path
        from kiro_crew.security import (
            _CREW_SECRET_LEAVES,
            is_sensitive_bash_command,
            is_sensitive_path,
        )

        assert "aws_service_consent.json" in _CREW_SECRET_LEAVES
        assert aws_consent_path().name == "aws_service_consent.json"
        assert is_sensitive_path("~/.kiro/crew/aws_service_consent.json") is True
        for command in (
            "cat ~/.kiro/crew/aws_service_consent.json",
            "echo x > ~/.kiro/crew/aws_service_consent.json",
            "tee ~/.kiro/crew/aws_service_consent.json",
        ):
            assert is_sensitive_bash_command(command)

    def test_file_is_owner_only(self, home):
        import stat

        from kiro_crew import platform_compat
        from kiro_crew.config.loader import aws_consent_path

        _grant()
        if not platform_compat.IS_POSIX:
            # Windows has no POSIX mode bits; the owner-only DACL is applied by
            # ``platform_compat.restrict_to_owner`` in ``_write_all`` instead.
            # Same skip as ``test_computer_use_enable_state`` for the sibling
            # keystone file.
            pytest.skip("POSIX mode bits")
        mode = stat.S_IMODE(os.stat(aws_consent_path()).st_mode)
        assert mode == 0o600

    def test_write_lockdown_precedes_content(self, home, monkeypatch):
        """The authorization record must never exist in a file that has not
        been locked down yet.

        On Windows the POSIX mode bits are a no-op, so the owner-only DACL from
        ``restrict_to_owner`` is the only protection; applying it after the
        rename left the record readable under the inherited ACL for the write
        window (issue #5285). Asserted by measuring the file's SIZE at lockdown
        time — zero means no payload byte existed yet. A post-write stat passes
        on the buggy ordering too, so it would not be a regression test.
        """
        from kiro_crew import platform_compat

        sizes: list[int] = []
        real_restrict = platform_compat.restrict_to_owner

        def _measuring_restrict(target):
            sizes.append(os.stat(target).st_size)
            return real_restrict(target)

        monkeypatch.setattr(platform_compat, "restrict_to_owner", _measuring_restrict)

        _grant()

        assert sizes, "premise: the lockdown ran at all"
        assert (
            sizes[0] == 0
        ), f"the file already held payload bytes when it was locked down: {sizes[0]} bytes"

    def test_sidecar_preservation_lockdown_precedes_content(self, home, monkeypatch):
        """The corrupt-store sidecar carries whatever the old store held, so its
        write gets the same lockdown-before-content ordering (issue #5285)."""
        from kiro_crew import platform_compat
        from kiro_crew.config.loader import aws_consent_path

        aws_consent_path().write_text("not json{", encoding="utf-8")
        sizes: list[int] = []
        real_restrict = platform_compat.restrict_to_owner

        def _measuring_restrict(target):
            sizes.append(os.stat(target).st_size)
            return real_restrict(target)

        monkeypatch.setattr(platform_compat, "restrict_to_owner", _measuring_restrict)

        _grant()

        # One lockdown for the preserved sidecar, then one for the new store,
        # in that order — both applied while their file was still empty. Later
        # calls belong to the SEL audit trail ``record_grant`` appends to (an
        # already-converted, out-of-scope consumer), so only the first two are
        # this site's.
        assert len(sizes) >= 2, f"expected sidecar + store lockdowns: {sizes}"
        assert sizes[:2] == [
            0,
            0,
        ], f"a file already held payload bytes when it was locked down: {sizes[:2]}"

    def test_a_failed_lockdown_refuses_and_leaves_no_store(self, home, monkeypatch):
        """The fail-loud policy survives the conversion: a record that cannot be
        locked down is refused (the OSError propagates), and — starting from an
        empty home — no consent store at ANY permission exists afterwards, which
        the read side treats as "no consent"."""
        from kiro_crew import platform_compat
        from kiro_crew.config.loader import aws_consent_path

        def _refuse(_target):
            raise OSError("cannot resolve the invoking user's SID")

        monkeypatch.setattr(platform_compat, "restrict_to_owner", _refuse)

        with pytest.raises(OSError):
            _grant()

        assert not aws_consent_path().exists(), "an unprotectable store was left behind"
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None

    def test_a_failed_lockdown_preserves_the_previous_store(self, home, monkeypatch):
        """A failed NEW write must not destroy the PREVIOUS, healthy store.

        Every failure inside ``atomic_write`` happens before the rename, so the
        final path still holds the last successfully written (and locked-down)
        record. Both pre-push reviews flagged the alternative — an unlink on any
        OSError — as data loss: one transient failure would have wiped every
        recorded authorization. The empty-home test above cannot see that
        destruction, so this variant seeds a real grant first.
        """
        from kiro_crew import platform_compat
        from kiro_crew.config.loader import aws_consent_path

        _grant()  # a healthy, locked-down store exists
        before = aws_consent_path().read_bytes()

        def _refuse(_target):
            raise OSError("cannot resolve the invoking user's SID")

        monkeypatch.setattr(platform_compat, "restrict_to_owner", _refuse)

        with pytest.raises(OSError):
            _grant(aws_consent.SERVICE_TRANSCRIBE)

        assert aws_consent_path().read_bytes() == before, "the previous store was altered"
        assert (
            aws_consent.read_grant(aws_consent.SERVICE_POLLY) is not None
        ), "a failed new grant destroyed the previously recorded authorization"

    def test_a_failed_payload_write_preserves_the_previous_store(self, home, monkeypatch):
        """Same property for an ordinary write failure (disk full while creating
        the temp file), which never even reaches the lockdown: the OSError
        propagates and the previous store survives byte-identical."""
        import tempfile

        from kiro_crew.config.loader import aws_consent_path

        _grant()  # a healthy, locked-down store exists
        before = aws_consent_path().read_bytes()

        def _no_space(*_a, **_kw):
            raise OSError("no space left on device")

        monkeypatch.setattr(tempfile, "mkstemp", _no_space)

        with pytest.raises(OSError):
            _grant(aws_consent.SERVICE_TRANSCRIBE)

        # No undo needed: the assertions below only READ (Path.read_bytes /
        # read_grant), which never calls tempfile.mkstemp — and undo would also
        # revert the home fixture's KIROCREW_HOME (same monkeypatch instance).
        assert aws_consent_path().read_bytes() == before, "the previous store was altered"
        assert (
            aws_consent.read_grant(aws_consent.SERVICE_POLLY) is not None
        ), "a transient write failure destroyed the previously recorded authorization"


class TestGate:
    def test_no_grant_refuses(self, home):
        granted, reason = aws_consent.is_granted(
            aws_consent.SERVICE_POLLY, profile="", region="us-east-1"
        )
        assert granted is False
        assert "has not been confirmed" in reason
        assert "Nothing was sent to AWS" in reason

    def test_matching_grant_allows(self, home):
        _grant(profile="voice", region="us-east-1")
        granted, reason = aws_consent.is_granted(
            aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1"
        )
        assert granted is True
        assert reason == ""

    @pytest.mark.parametrize(
        "profile,region",
        [
            ("other", "us-east-1"),  # profile changed
            ("voice", "eu-west-1"),  # region changed
            ("", "us-east-1"),  # dropped to the ambient chain
        ],
    )
    def test_grant_does_not_transfer_to_other_settings(self, home, profile, region):
        _grant(profile="voice", region="us-east-1")
        granted, _reason = aws_consent.is_granted(
            aws_consent.SERVICE_POLLY, profile=profile, region=region
        )
        assert granted is False

    def test_grant_is_per_service(self, home):
        _grant(aws_consent.SERVICE_POLLY, region="us-east-1")
        granted, _r = aws_consent.is_granted(
            aws_consent.SERVICE_TRANSCRIBE, profile="", region="us-east-1"
        )
        assert granted is False

    def test_malformed_record_is_no_consent(self, home):
        from kiro_crew.config.loader import aws_consent_path

        aws_consent_path().write_text('{"polly": "not-a-dict"}')
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None

    def test_unparseable_file_is_no_consent(self, home):
        from kiro_crew.config.loader import aws_consent_path

        aws_consent_path().write_text("{ this is not json")
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None

    def test_an_unreadable_store_is_preserved_before_a_new_grant(self, home):
        """A write must not silently discard bytes it could not parse.

        What is lost is not a working authorization -- an unreadable store grants
        nothing -- but it may hold the operator's other service grant. Preserved
        rather than refused, because refusing would leave a corrupt file
        unrecoverable from the dashboard. Found in review.
        """
        from kiro_crew.config.loader import aws_consent_path

        path = aws_consent_path()
        path.write_text('{"transcribe": {"service": "transcribe", TRUNCATED')

        _grant(aws_consent.SERVICE_POLLY, region="us-east-1")

        sidecars = list(path.parent.glob(f"{path.name}.corrupt-*"))
        assert len(sidecars) == 1, sidecars
        assert "TRUNCATED" in sidecars[0].read_text()
        # The new grant landed, so the operator is not locked out.
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is not None

    def test_a_readable_store_is_not_copied_aside(self, home):
        from kiro_crew.config.loader import aws_consent_path

        path = aws_consent_path()
        _grant(aws_consent.SERVICE_TRANSCRIBE, region="us-east-1")
        _grant(aws_consent.SERVICE_POLLY, region="us-east-1")
        assert list(path.parent.glob(f"{path.name}.corrupt-*")) == []
        # Both survive a normal read-modify-write.
        assert aws_consent.read_grant(aws_consent.SERVICE_TRANSCRIBE) is not None
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is not None

    def test_revoke_removes_only_that_service(self, home):
        _grant(aws_consent.SERVICE_POLLY, region="us-east-1")
        _grant(aws_consent.SERVICE_TRANSCRIBE, region="us-east-1")
        assert aws_consent.revoke(aws_consent.SERVICE_POLLY) is True
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None
        assert aws_consent.read_grant(aws_consent.SERVICE_TRANSCRIBE) is not None

    def test_unknown_service_cannot_be_granted(self, home):
        with pytest.raises(ValueError):
            aws_consent.record_grant(
                "bedrock", profile="", region="", account="1", arn="", granted_at="now"
            )


class TestPollySynthesisRefuses:
    """The billable request must not be issued without a grant."""

    def test_no_grant_means_no_subprocess(self, home):
        from kiro_crew import voice_reply

        with (
            patch("asyncio.create_subprocess_exec") as spawn,
            patch("kiro_crew.sandbox.create_subprocess_limited") as limited,
        ):
            result = asyncio.run(
                voice_reply._synthesize_polly("hello", aws_profile="", region="us-east-1")
            )
        assert result is None
        assert spawn.call_count == 0
        assert limited.call_count == 0

    def test_grant_for_a_different_profile_means_no_subprocess(self, home):
        from kiro_crew import voice_reply

        _grant(profile="voice", region="us-east-1")
        with (
            patch("asyncio.create_subprocess_exec") as spawn,
            patch("kiro_crew.sandbox.create_subprocess_limited") as limited,
        ):
            result = asyncio.run(
                voice_reply._synthesize_polly("hello", aws_profile="", region="us-east-1")
            )
        assert result is None
        assert spawn.call_count == 0
        assert limited.call_count == 0


class TestAccountIsVerifiedNotAssumed:
    """A profile NAME is not an account: the live account is checked too.

    ``aws configure set credential_process ... --profile <name>`` repoints an
    existing profile at a different account without touching the credential
    files, and that command is NOT on the shell denylist -- measured, not
    assumed. So a grant keyed only on the profile name would keep authorizing
    calls after the account under it changed. Found in review.
    """

    def test_same_account_authorizes(self, home):
        _grant(profile="voice", region="us-east-1", account="111122223333")
        same = aws_consent.Identity(ok=True, account="111122223333")
        with patch.object(aws_consent, "probe_identity", AsyncMock(return_value=same)):
            granted, reason = asyncio.run(
                aws_consent.authorize(
                    aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1"
                )
            )
        assert granted is True
        assert reason == ""

    def test_repointed_profile_is_refused_and_revoked(self, home):
        _grant(profile="voice", region="us-east-1", account="111122223333")
        moved = aws_consent.Identity(ok=True, account="999988887777")
        with patch.object(aws_consent, "probe_identity", AsyncMock(return_value=moved)):
            granted, reason = asyncio.run(
                aws_consent.authorize(
                    aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1"
                )
            )
        assert granted is False
        assert "999988887777" in reason
        assert "withdrawn" in reason
        # Revoked, so the next call refuses on the local check alone.
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None

    def test_unresolvable_account_is_refused_not_assumed(self, home):
        """Fail CLOSED when the account cannot be re-checked.

        An earlier revision allowed this so a transient STS fault would not stop
        voice output, but that let a repointed profile bill an unconfirmed
        account -- and on a host with boto3 but no ``aws`` CLI the account was
        never verified at all. Refusing costs little, because a grant can only
        exist where the probe once succeeded.
        """
        _grant(profile="voice", region="us-east-1", account="111122223333")
        unknown = aws_consent.Identity(ok=False, detail="network down")
        with patch.object(aws_consent, "probe_identity", AsyncMock(return_value=unknown)):
            granted, reason = asyncio.run(
                aws_consent.authorize(
                    aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1"
                )
            )
        assert granted is False
        assert "could not be re-checked" in reason
        # The grant is NOT revoked: an unresolvable probe proves nothing, so the
        # operator does not have to re-confirm once the outage clears.
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is not None

    def test_synthesis_refuses_when_the_account_moved(self, home):
        from kiro_crew import voice_reply

        _grant(profile="", region="us-east-1", account="111122223333")
        moved = aws_consent.Identity(ok=True, account="999988887777")
        with (
            patch.object(aws_consent, "probe_identity", AsyncMock(return_value=moved)),
            patch("asyncio.create_subprocess_exec") as spawn,
            patch("kiro_crew.sandbox.create_subprocess_limited") as limited,
        ):
            result = asyncio.run(
                voice_reply._synthesize_polly("hello", aws_profile="", region="us-east-1")
            )
        assert result is None
        assert spawn.call_count == 0
        assert limited.call_count == 0

    def test_authorization_never_uses_a_cached_identity(self, home):
        """A cached account is a window where a repointed profile still passes.

        The probe is free and goes to the account the operator already consented
        to, so paying it per call costs latency, not money -- and the window it
        removes is exactly what this check exists for. Found in review.
        """
        _grant(profile="voice", region="us-east-1", account="111122223333")
        same = aws_consent.Identity(ok=True, account="111122223333")
        probe = AsyncMock(return_value=same)
        with patch.object(aws_consent, "probe_identity", probe):
            asyncio.run(
                aws_consent.authorize(
                    aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1"
                )
            )
        assert probe.await_args.kwargs.get("use_cache") is False

    def test_a_stale_cached_account_cannot_authorize(self, home):
        """End to end: the cache holds account A while the profile resolves to B."""
        _grant(profile="voice", region="us-east-1", account="111122223333")
        # Seed the cache with the confirmed account, then make the CLI report a
        # different one. An authorization that consulted the cache would pass.
        aws_consent._probe_cache[("voice", "us-east-1")] = (
            1e9,
            aws_consent.Identity(ok=True, account="111122223333"),
        )
        moved = '{"Account": "999988887777", "Arn": "arn:aws:iam::9:user/x"}'
        with (
            patch("shutil.which", return_value="/usr/bin/aws"),
            patch.object(aws_consent, "_run_aws", return_value=(0, moved, "")),
        ):
            granted, reason = asyncio.run(
                aws_consent.authorize(
                    aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1"
                )
            )
        assert granted is False
        assert "999988887777" in reason


class TestConcurrentWithdrawalFailsClosed:
    """A consent that disappears mid-check must not authorize the call.

    The probe spawns a subprocess, so ``authorize`` has a real suspension point
    in the middle -- long enough for the operator to press Withdraw or for
    another request's drift check to revoke. Removing the probe cache in the
    previous round widened that window, so the grant is re-asserted immediately
    before the allow. Found in review.
    """

    def test_grant_withdrawn_before_the_probe_is_denied(self, home):
        _grant(profile="voice", region="us-east-1", account="111122223333")
        real_read = aws_consent.read_grant
        calls = {"n": 0}

        def _vanishing(service):
            calls["n"] += 1
            # First read (inside is_granted) sees it; the next does not.
            return real_read(service) if calls["n"] == 1 else None

        with patch.object(aws_consent, "read_grant", side_effect=_vanishing):
            granted, reason = asyncio.run(
                aws_consent.authorize(
                    aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1"
                )
            )
        assert granted is False
        assert "withdrawn" in reason
        assert "Nothing was sent to AWS" in reason

    def test_grant_withdrawn_during_the_probe_is_denied(self, home):
        """The window the uncached probe opened."""
        _grant(profile="voice", region="us-east-1", account="111122223333")
        real_read = aws_consent.read_grant
        state = {"revoked": False}

        def _reads(service):
            return None if state["revoked"] else real_read(service)

        async def _probe(_profile, _region, *, use_cache=True):
            # Stands in for the operator pressing Withdraw while the CLI runs.
            state["revoked"] = True
            return aws_consent.Identity(ok=True, account="111122223333")

        with (
            patch.object(aws_consent, "read_grant", side_effect=_reads),
            patch.object(aws_consent, "probe_identity", _probe),
        ):
            granted, reason = asyncio.run(
                aws_consent.authorize(
                    aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1"
                )
            )
        assert granted is False
        assert "Nothing was sent to AWS" in reason

    def test_grant_repointed_during_the_probe_is_denied(self, home):
        """Changed, not just absent: the re-assert compares the whole record."""
        _grant(profile="voice", region="us-east-1", account="111122223333")
        swapped = aws_consent.Grant(
            service=aws_consent.SERVICE_POLLY,
            profile="voice",
            region="us-east-1",
            account="555566667777",
            arn="arn:aws:iam::5:user/x",
            granted_at="2026-08-21T01:00:00+00:00",
        )
        original = aws_consent.read_grant(aws_consent.SERVICE_POLLY)
        seq = [original, original, swapped]

        def _reads(_service):
            return seq.pop(0) if seq else swapped

        async def _probe(_profile, _region, *, use_cache=True):
            return aws_consent.Identity(ok=True, account="111122223333")

        with (
            patch.object(aws_consent, "read_grant", side_effect=_reads),
            patch.object(aws_consent, "probe_identity", _probe),
        ):
            granted, reason = asyncio.run(
                aws_consent.authorize(
                    aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1"
                )
            )
        assert granted is False
        assert "changed" in reason

    def test_a_grant_naming_no_account_is_denied(self, home):
        """Only a hand-edited file produces one, and it cannot be verified."""
        from kiro_crew.config.loader import aws_consent_path

        aws_consent_path().write_text(
            json.dumps(
                {
                    "polly": {
                        "service": "polly",
                        "profile": "voice",
                        "region": "us-east-1",
                        "account": "",
                        "arn": "",
                        "granted_at": "2026-08-21T00:00:00+00:00",
                    }
                }
            )
        )
        with patch.object(aws_consent, "probe_identity") as probe:
            granted, reason = asyncio.run(
                aws_consent.authorize(
                    aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1"
                )
            )
        assert granted is False
        assert "names no AWS account" in reason
        # Denied before the probe: there is nothing to compare against.
        assert probe.call_count == 0


class TestConsentEndpointRequiresTheOwner:
    """Confirming a charge spends the OWNER's money, so it is an owner action.

    Narrower than "authenticated" and narrower than "not an app". Two caller
    classes had to be shut out and only the first is obvious: an app token (an
    app declaring the endpoint's permission would mint a grant with no human in
    the loop) and an allowed MESSAGING user (a Slack allow-listed non-owner who
    runs ``!dashboard`` authenticates with ``app == ""``, so an app-only check let
    them authorize spending in the owner's account). Reads are refused too: the
    GET names the account id and caller ARN a keystone read is fenced from. Both
    found in review.
    """

    def _req(
        self,
        *,
        app: str = "",
        user: str = "owner-1",
        owner: str = "owner-1",
        query: dict | None = None,
        body: dict | None = None,
    ):
        return _consent_request(app=app, user=user, owner=owner, query=query, body=body)

    def test_get_refuses_a_non_owner_messaging_user(self, home):
        """The finding an app-only check missed: app is empty, user is not owner."""
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resp = asyncio.run(
            handler.api_aws_consent_get(
                self._req(app="", user="slack-guest", owner="owner-1", query={"service": "polly"})
            )
        )
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "dashboard_owner_required"

    def test_post_refuses_a_non_owner_messaging_user(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        req = self._req(app="", user="slack-guest", owner="owner-1", body={"service": "polly"})
        resp = asyncio.run(handler.api_aws_consent_post(req))
        assert resp.status == 403
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None
        assert req.json.await_count == 0

    def test_delete_refuses_a_non_owner_messaging_user(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        _grant(region="us-east-1")
        resp = asyncio.run(
            handler.api_aws_consent_delete(
                self._req(app="", user="slack-guest", owner="owner-1", query={"service": "polly"})
            )
        )
        assert resp.status == 403
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is not None

    def test_an_unauthenticated_caller_is_refused(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resp = asyncio.run(
            handler.api_aws_consent_get(
                self._req(app="", user="", owner="owner-1", query={"service": "polly"})
            )
        )
        assert resp.status == 403

    def test_get_refuses_an_app_token(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resp = asyncio.run(
            handler.api_aws_consent_get(self._req(app="notes", query={"service": "polly"}))
        )
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "dashboard_owner_required"

    def test_post_refuses_an_app_token_before_reading_the_body(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        req = self._req(app="notes", body={"service": "polly"})
        resp = asyncio.run(handler.api_aws_consent_post(req))
        assert resp.status == 403
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None
        assert req.json.await_count == 0

    def test_delete_refuses_an_app_token(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        _grant(region="us-east-1")
        resp = asyncio.run(
            handler.api_aws_consent_delete(self._req(app="notes", query={"service": "polly"}))
        )
        assert resp.status == 403
        # The grant survives: an app cannot withdraw the operator's consent either.
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is not None

    def test_the_owner_is_not_refused(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        assert handler._deny_non_owner(self._req(), "aws_consent.read") is None

    def test_the_denial_is_audited(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        with patch.object(handler.aws_consent, "audit_decision") as audit:
            handler._deny_non_owner(self._req(app="notes"), "aws_consent.grant")
        assert [c.kwargs.get("outcome") for c in audit.call_args_list] == ["denied"]

    def test_the_denial_never_logs_the_caller_credential(self, home):
        """The log line names the calling app, never a token value.

        Pinned because the SAST rule reads "token" in a logger literal as a
        possible secret, and because the accurate statement is that the value is
        an app NAME.
        """
        import inspect

        from kiro_crew.dashboard.handlers import aws_consent as handler

        src = inspect.getsource(handler._deny_non_owner)
        body = src.split('"""')[-1]
        logger_lines = [ln for ln in body.splitlines() if "logger." in ln or "%s" in ln]
        assert logger_lines, "expected a logger call to inspect"
        assert not any("token" in ln for ln in logger_lines)


class TestConsentEndpointReads:
    """The GET is the surface that shows what would be billed."""

    def _req(self, query: dict):
        return _consent_request(query=query)

    def test_unknown_service_is_rejected(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resp = asyncio.run(handler.api_aws_consent_get(self._req({"service": "bedrock"})))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "unknown_aws_service"

    def test_missing_service_is_rejected(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resp = asyncio.run(handler.api_aws_consent_get(self._req({})))
        assert resp.status == 400

    def test_reports_what_would_be_billed(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resolved = aws_consent.Identity(ok=True, account="111122223333", arn="arn:aws:iam::1:u/x")
        with (
            patch.object(handler.aws_consent, "probe_identity", AsyncMock(return_value=resolved)),
            patch.object(handler, "_effective_target", AsyncMock(return_value=("", "us-east-1"))),
        ):
            resp = asyncio.run(handler.api_aws_consent_get(self._req({"service": "polly"})))
        body = json.loads(resp.text)
        assert resp.status == 200
        assert body["serviceLabel"] == "Amazon Polly"
        assert body["account"] == "111122223333"
        assert body["credentialSource"] == "AWS CLI default credential provider chain"
        assert body["granted"] is False
        assert body["grant"] is None

    def test_reports_a_grant_and_its_account(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        _grant(profile="voice", region="us-east-1", account="111122223333")
        resolved = aws_consent.Identity(ok=True, account="111122223333", arn="arn:aws:iam::1:u/x")
        with (
            patch.object(handler.aws_consent, "probe_identity", AsyncMock(return_value=resolved)),
            patch.object(
                handler, "_effective_target", AsyncMock(return_value=("voice", "us-east-1"))
            ),
        ):
            resp = asyncio.run(handler.api_aws_consent_get(self._req({"service": "polly"})))
        body = json.loads(resp.text)
        assert body["granted"] is True
        assert body["grant"]["account"] == "111122223333"
        assert body["revokedOnAccountChange"] is False

    def test_a_drifted_account_is_revoked_in_the_same_response(self, home):
        """The panel must not report a grant this request just invalidated."""
        from kiro_crew.dashboard.handlers import aws_consent as handler

        _grant(profile="voice", region="us-east-1", account="111122223333")
        moved = aws_consent.Identity(ok=True, account="999988887777", arn="arn:aws:iam::9:u/x")
        with (
            patch.object(handler.aws_consent, "probe_identity", AsyncMock(return_value=moved)),
            patch.object(
                handler, "_effective_target", AsyncMock(return_value=("voice", "us-east-1"))
            ),
        ):
            resp = asyncio.run(handler.api_aws_consent_get(self._req({"service": "polly"})))
        body = json.loads(resp.text)
        assert body["revokedOnAccountChange"] is True
        assert body["granted"] is False
        assert body["grant"] is None

    def test_an_unresolved_identity_is_reported_not_fatal(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        unknown = aws_consent.Identity(ok=False, detail="creds did not resolve")
        with (
            patch.object(handler.aws_consent, "probe_identity", AsyncMock(return_value=unknown)),
            patch.object(handler, "_effective_target", AsyncMock(return_value=("", "us-east-1"))),
        ):
            resp = asyncio.run(handler.api_aws_consent_get(self._req({"service": "transcribe"})))
        body = json.loads(resp.text)
        assert resp.status == 200
        assert body["identityResolved"] is False
        assert body["identityDetail"] == "creds did not resolve"


class TestConsentEndpointDelete:
    def _req(self, query: dict):
        return _consent_request(query=query)

    def test_withdraws_a_grant(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        _grant(region="us-east-1")
        resp = asyncio.run(handler.api_aws_consent_delete(self._req({"service": "polly"})))
        assert json.loads(resp.text) == {"ok": True, "removed": True}
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None

    def test_withdrawing_nothing_is_not_an_error(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resp = asyncio.run(handler.api_aws_consent_delete(self._req({"service": "polly"})))
        assert json.loads(resp.text) == {"ok": True, "removed": False}

    def test_unknown_service_is_rejected(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resp = asyncio.run(handler.api_aws_consent_delete(self._req({"service": "bedrock"})))
        assert resp.status == 400


class TestEffectiveTargetReadsLiveConfig:
    """A confirmation is recorded against what the code will really use.

    Taking profile/region from the request would let the confirmation and the
    request disagree -- the operator shown one account and billed another.
    """

    def test_polly_reads_the_live_voice_state(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler
        from kiro_crew.slack.handler import _vc

        with (
            patch.object(_vc, "aws_profile", "voice"),
            patch.object(_vc, "region", "eu-west-1"),
        ):
            assert asyncio.run(handler._effective_target("polly")) == ("voice", "eu-west-1")

    def test_transcribe_reads_the_live_stt_config(self, home):
        from kiro_crew.config.loader import config_path
        from kiro_crew.dashboard.handlers import aws_consent as handler

        config_path().write_text(
            json.dumps(
                {
                    "stt": {
                        "provider": "transcribe",
                        "transcribe_profile": "dictation",
                        "transcribe_region": "ap-southeast-2",
                    }
                }
            )
        )
        assert asyncio.run(handler._effective_target("transcribe")) == (
            "dictation",
            "ap-southeast-2",
        )


class TestThereIsNoCliGrantSurface:
    """The agent must not be able to grant itself permission to spend.

    An earlier revision shipped a ``kirocrew aws-consent grant`` verb behind an
    env-var guard. Review found that single layer bypassable (an in-process agent
    can unset the variable, and unlike ``kirocrew cloud`` the verb was not also
    shell-denied), so the surface was removed rather than hardened: the
    authenticated dashboard is available on every install. This pins that it
    stays removed -- re-adding a terminal grant re-opens the hole.
    """

    def test_cli_exposes_no_consent_command(self):
        import pathlib as _pathlib

        import kiro_crew.cli as cli_mod

        source = _pathlib.Path(cli_mod.__file__).read_text(encoding="utf-8")
        assert "aws-consent" not in source
        assert "aws_consent_yes" not in source

    def test_cli_commands_has_no_consent_helpers(self):
        import pathlib as _pathlib

        import kiro_crew.cli_commands as cc

        source = _pathlib.Path(cc.__file__).read_text(encoding="utf-8")
        for dead in ("_aws_consent", "_consent_target", "_print_consent_status"):
            assert dead not in source, dead

    def test_refusals_point_at_the_dashboard_only(self, home):
        _granted, reason = aws_consent.is_granted(
            aws_consent.SERVICE_POLLY, profile="", region="us-east-1"
        )
        assert "Settings -> Voice" in reason
        assert "aws-consent" not in reason


class TestConsentDecisionsAreAudited:
    """Authorization changes and denials belong in the tamper-evident log."""

    def test_grant_and_revoke_are_audited(self, home):
        with patch.object(aws_consent, "audit_decision") as audit:
            _grant(profile="voice", region="us-east-1")
            aws_consent.revoke(aws_consent.SERVICE_POLLY)
        outcomes = [c.kwargs.get("outcome") for c in audit.call_args_list]
        assert "granted" in outcomes
        assert "revoked" in outcomes

    def test_a_denial_is_audited(self, home):
        with patch.object(aws_consent, "audit_decision") as audit:
            allowed = asyncio.run(
                aws_consent.refuse_and_log(
                    aws_consent.SERVICE_POLLY, profile="", region="us-east-1"
                )
            )
        assert allowed is False
        assert [c.kwargs.get("outcome") for c in audit.call_args_list] == ["denied"]

    def test_a_verified_allow_is_audited_once_per_probe(self, home):
        """Audited on VERIFICATION, which the probe cache bounds.

        Auditing every allow would emit one event per synthesis and bury the
        events that matter; auditing each verification keeps the trail complete
        without that noise.
        """
        _grant(profile="voice", region="us-east-1", account="111122223333")
        same = aws_consent.Identity(ok=True, account="111122223333")
        with (
            patch.object(aws_consent, "probe_identity", AsyncMock(return_value=same)),
            patch.object(aws_consent, "audit_decision") as audit,
        ):
            allowed = asyncio.run(
                aws_consent.refuse_and_log(
                    aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1"
                )
            )
        assert allowed is True
        assert [c.kwargs.get("outcome") for c in audit.call_args_list] == ["verified"]

    def test_an_audit_failure_does_not_break_the_gate(self, home):
        with patch("kiro_crew.sel.sel", side_effect=RuntimeError("sel down")):
            aws_consent.audit_decision("polly", outcome="denied", detail="x")


class TestTranscribeRefuses:
    def test_no_grant_means_no_client(self, home):
        from types import SimpleNamespace

        from kiro_crew import transcribe

        cfg = SimpleNamespace(
            transcribe_profile="", transcribe_region="us-east-1", language_code="en-US"
        )
        with patch.object(transcribe, "_load_aws_transcribe_components") as load:
            result = asyncio.run(transcribe._transcribe_aws("/tmp/x.ogg", cfg))
        assert result is None
        # Refused before the optional-dependency probe, so nothing was loaded.
        assert load.call_count == 0


class TestDescribeVoicesEndpoint:
    """The voice catalogue is a Polly call and needs both gates."""

    def _request(self):
        return _consent_request()

    def test_non_polly_provider_returns_empty_without_calling_aws(self, home):
        from kiro_crew.dashboard import chat_voice
        from kiro_crew.voice_reply import PROVIDER_PIPER

        chat_voice._voices_cache = None
        with (
            patch.object(chat_voice._vc, "provider", PROVIDER_PIPER),
            patch("asyncio.create_subprocess_exec") as spawn,
        ):
            resp = asyncio.run(chat_voice.api_voice_voices(self._request()))
        assert json.loads(resp.text) == {"voices": []}
        assert spawn.call_count == 0

    def test_polly_without_consent_returns_an_empty_list_without_calling_aws(self, home):
        """Empty list, not a bespoke refusal field.

        The operator-facing explanation is the consent card's job -- it has its
        own GET carrying the reason -- so a second copy here would be a response
        field with no reader. Review found the dead surface.
        """
        from kiro_crew.dashboard import chat_voice
        from kiro_crew.voice_reply import PROVIDER_POLLY

        chat_voice._voices_cache = None
        with (
            patch.object(chat_voice._vc, "provider", PROVIDER_POLLY),
            patch.object(chat_voice._vc, "aws_profile", ""),
            patch.object(chat_voice._vc, "region", "us-east-1"),
            patch("asyncio.create_subprocess_exec") as spawn,
            patch.object(aws_consent, "audit_decision") as audit,
        ):
            resp = asyncio.run(chat_voice.api_voice_voices(self._request()))
        body = json.loads(resp.text)
        assert body == {"voices": []}
        assert spawn.call_count == 0
        # Routed through refuse_and_log, so the denial is audited like every
        # other gated call site rather than only logged. Found in review.
        assert [c.kwargs.get("outcome") for c in audit.call_args_list] == ["denied"]


# ── Step 3: credential source, and account drift ──


class TestCredentialSource:
    def test_empty_profile_is_named_as_the_ambient_chain(self):
        assert aws_consent.credential_source("") == "AWS CLI default credential provider chain"

    def test_named_profile_is_reported(self):
        assert aws_consent.credential_source("voice") == "profile voice"

    def test_refusal_reason_names_both_sides(self, home):
        _grant(profile="voice", region="us-east-1", account="111122223333")
        _granted, reason = aws_consent.is_granted(
            aws_consent.SERVICE_POLLY, profile="", region="us-east-1"
        )
        assert "profile voice" in reason
        assert "AWS CLI default credential provider chain" in reason


class TestAccountDrift:
    def test_new_account_revokes_the_grant(self, home):
        _grant(profile="voice", region="us-east-1", account="111122223333")
        moved = aws_consent.Identity(ok=True, account="999988887777")
        assert aws_consent.reconcile_drift(aws_consent.SERVICE_POLLY, moved) is True
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None

    def test_same_account_keeps_the_grant(self, home):
        _grant(profile="voice", region="us-east-1", account="111122223333")
        same = aws_consent.Identity(ok=True, account="111122223333")
        assert aws_consent.reconcile_drift(aws_consent.SERVICE_POLLY, same) is False
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is not None

    def test_failed_probe_is_not_drift(self, home):
        """An unresolvable probe proves nothing, so it must not revoke."""
        _grant(profile="voice", region="us-east-1", account="111122223333")
        unknown = aws_consent.Identity(ok=False, detail="creds did not resolve")
        assert aws_consent.reconcile_drift(aws_consent.SERVICE_POLLY, unknown) is False
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is not None


class TestIdentityProbeInputs:
    """Profile and region reach an ``aws`` argv, and both come from config.json.

    Argv elements cannot be shell-injected, but a leading dash would be read as
    an OPTION rather than as the previous option's value -- and config.json is
    writable by an auto-approved agent shell.
    """

    def test_flag_shaped_profile_is_refused(self):
        assert aws_consent._inputs_are_safe("--debug", "") is False

    def test_flag_shaped_region_is_refused(self):
        assert aws_consent._inputs_are_safe("", "--endpoint-url=http://x") is False

    def test_ordinary_values_pass(self):
        assert aws_consent._inputs_are_safe("voice", "us-east-1") is True

    def test_empty_values_pass(self):
        assert aws_consent._inputs_are_safe("", "") is True

    def test_unsafe_inputs_never_reach_the_cli(self, home):
        with patch.object(aws_consent, "_run_aws") as run:
            identity = asyncio.run(aws_consent.probe_identity("--debug", "", use_cache=False))
        assert identity.ok is False
        assert run.call_count == 0

    def test_the_probe_goes_through_the_cloud_chokepoint(self, home):
        """Delegated rather than a fourth hand-rolled ``aws`` spawn.

        ``cloud.aws.run_aws`` supplies the sandbox wrap, the scrubbed env, the
        resource-limited spawn and the agent-session allowlist that already
        contains ``("sts", "get-caller-identity")``.
        """
        payload = '{"Account": "111122223333", "Arn": "arn:aws:iam::1:user/x"}'
        with (
            patch("shutil.which", return_value="/usr/bin/aws"),
            patch.object(aws_consent, "_run_aws", return_value=(0, payload, "")) as run,
        ):
            identity = asyncio.run(
                aws_consent.probe_identity("voice", "us-east-1", use_cache=False)
            )
        assert identity.ok is True
        assert identity.account == "111122223333"
        args, profile, region = run.call_args[0]
        assert args == ["sts", "get-caller-identity", "--output", "json"]
        assert (profile, region) == ("voice", "us-east-1")

    def test_a_chokepoint_refusal_is_reported_not_raised(self, home):
        """``run_aws`` raises for a refused call or an unbuildable sandbox."""
        with (
            patch("shutil.which", return_value="/usr/bin/aws"),
            patch.object(aws_consent, "_run_aws", side_effect=RuntimeError("refused")),
        ):
            identity = asyncio.run(aws_consent.probe_identity("", "", use_cache=False))
        assert identity.ok is False
        assert "could not be resolved" in identity.detail

    def test_a_nonzero_exit_is_reported(self, home):
        with (
            patch("shutil.which", return_value="/usr/bin/aws"),
            patch.object(aws_consent, "_run_aws", return_value=(255, "", "ExpiredToken")),
        ):
            identity = asyncio.run(aws_consent.probe_identity("", "", use_cache=False))
        assert identity.ok is False
        assert "ExpiredToken" in identity.detail

    def test_missing_cli_is_reported_not_raised(self, home):
        with patch("shutil.which", return_value=None):
            identity = asyncio.run(aws_consent.probe_identity("", "", use_cache=False))
        assert identity.ok is False
        assert "could not be found" in identity.detail

    def test_cli_probe_resolves_under_minimal_path(self, home, monkeypatch, tmp_path):
        """A GUI-launched gateway's minimal PATH must not fail the consent gate
        closed: the probe routes through the deploy engine's well-known-dirs
        resolver (#4770), agreeing with the resolved spawn below it."""
        import os as _os

        if _os.name == "nt":
            pytest.skip("fallback install dirs are POSIX literals; dead on Windows by design")
        from kiro_crew import github_runner
        from kiro_crew.deploy import engine

        fake_aws = tmp_path / "aws"
        fake_aws.write_text("#!/bin/sh\n")
        fake_aws.chmod(0o755)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(tmp_path),))
        monkeypatch.setattr(github_runner, "validate_provider_executable", lambda c: c)

        payload = '{"Account": "111122223333", "Arn": "arn:aws:iam::1:user/x"}'
        with patch.object(aws_consent, "_run_aws", return_value=(0, payload, "")) as run:
            identity = asyncio.run(aws_consent.probe_identity("", "", use_cache=False))
        assert identity.ok is True
        assert run.call_count == 1  # the gate passed; the probe reached the spawn

    def test_the_local_half_of_the_gate_never_probes(self, home):
        """``is_granted`` stays local; only ``authorize`` may probe.

        Keeping the split explicit is what lets the dashboard report the local
        decision without triggering an AWS call on every render.
        """
        _grant(profile="voice", region="us-east-1")
        with patch.object(aws_consent, "probe_identity") as probe:
            aws_consent.is_granted(aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1")
            aws_consent.is_granted(aws_consent.SERVICE_POLLY, profile="other", region="x")
        assert probe.call_count == 0


class TestConsentEndpoint:
    """The POST refuses to record a consent it cannot attach an account to."""

    def _post(self, body):
        # A bare MagicMock returns truthy stubs for request.get(...), which the
        # owner guard reads as a non-owner caller. These cases are about the
        # POST's own branches, so present a real dashboard owner.
        return _consent_request(body=body)

    def test_unresolved_account_is_not_recorded(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        unresolved = aws_consent.Identity(ok=False, detail="creds did not resolve")
        with (
            patch.object(handler.aws_consent, "probe_identity", AsyncMock(return_value=unresolved)),
            patch.object(handler, "_effective_target", AsyncMock(return_value=("", "us-east-1"))),
        ):
            resp = asyncio.run(handler.api_aws_consent_post(self._post({"service": "polly"})))
        assert resp.status == 409
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None

    def test_resolved_account_is_recorded_against_live_config(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resolved = aws_consent.Identity(ok=True, account="111122223333", arn="arn:aws:iam::1:u/x")
        with (
            patch.object(handler.aws_consent, "probe_identity", AsyncMock(return_value=resolved)),
            patch.object(
                handler, "_effective_target", AsyncMock(return_value=("voice", "us-east-1"))
            ),
        ):
            resp = asyncio.run(
                handler.api_aws_consent_post(
                    self._post(
                        {
                            "service": "polly",
                            "expectedProfile": "voice",
                            "expectedRegion": "us-east-1",
                            "expectedAccount": "111122223333",
                        }
                    )
                )
            )
        assert resp.status == 200
        grant = aws_consent.read_grant(aws_consent.SERVICE_POLLY)
        assert grant is not None
        assert grant.account == "111122223333"
        # Recorded from live config, NOT from the request body.
        assert grant.profile == "voice"
        assert grant.region == "us-east-1"

    def test_a_confirmation_for_a_different_account_is_refused(self, home):
        """Confirming what you were SHOWN is the point of the surface.

        The consent card and the provider fields are separate queries, so an
        operator could read account A, change the profile, then click Confirm.
        Without this the POST would record account B while A was on screen.
        Found in review.
        """
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resolved = aws_consent.Identity(ok=True, account="999988887777", arn="arn:aws:iam::9:u/x")
        with (
            patch.object(handler.aws_consent, "probe_identity", AsyncMock(return_value=resolved)),
            patch.object(
                handler, "_effective_target", AsyncMock(return_value=("other", "eu-west-1"))
            ),
        ):
            resp = asyncio.run(
                handler.api_aws_consent_post(
                    self._post(
                        {
                            "service": "polly",
                            "expectedProfile": "voice",
                            "expectedRegion": "us-east-1",
                            "expectedAccount": "111122223333",
                        }
                    )
                )
            )
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "aws_consent_stale_confirmation"
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None

    def test_a_confirmation_with_no_echoed_values_is_refused(self, home):
        """An empty echo must not pass as "everything matched"."""
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resolved = aws_consent.Identity(ok=True, account="111122223333", arn="arn:aws:iam::1:u/x")
        with (
            patch.object(handler.aws_consent, "probe_identity", AsyncMock(return_value=resolved)),
            patch.object(
                handler, "_effective_target", AsyncMock(return_value=("voice", "us-east-1"))
            ),
        ):
            resp = asyncio.run(handler.api_aws_consent_post(self._post({"service": "polly"})))
        assert resp.status == 409
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None

    def test_unknown_service_is_rejected(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resp = asyncio.run(handler.api_aws_consent_post(self._post({"service": "bedrock"})))
        assert resp.status == 400

    @pytest.mark.parametrize("bad", [[], {}, 7, None, True])
    def test_a_non_string_service_is_rejected_not_a_500(self, home, bad):
        """``{"service": []}`` reached ``list.strip()`` and 500ed. Found in review."""
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resp = asyncio.run(handler.api_aws_consent_post(self._post({"service": bad})))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "unknown_aws_service"

    def test_a_non_dict_body_is_rejected(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        resp = asyncio.run(handler.api_aws_consent_post(self._post(["not", "a", "dict"])))
        assert resp.status == 400
