"""The IdC portal URL, from the login that knew it to the panel that shows it.

The account panel renders "Signed in with IAM Identity Center · <host>", and the
host is the reading's `start_url`. On the kiro-cli path whoami supplies it; on
Crew's own path nothing did, so a user signed in to one organization could not
tell from the UI which one -- the same information loss commit 8c4c079cb fixed
once already, when that host was being truncated rather than dropped.

It cannot be recovered after the fact. `ListAvailableProfiles` answers with an
ARN, a display name, and `SsoIdentityDetails { instanceArn, oidcClientId,
ssoRegion }`; an SSO instance ARN is not a portal host. So the only correct
place to capture it is the login call, and the only way to keep it is to carry
it through every refresh -- which is what these cases pin.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew.auth.bridge import _VAULT_IDENTITY_CLAIMS
from kiro_crew.auth.login import builder_id
from kiro_crew.auth.store import KasToken

_SOON = datetime.now(timezone.utc) + timedelta(hours=1)
_START_URL = "https://d-906679cc0e.awsapps.com/start"


def _created(start_url: str = _START_URL) -> KasToken:
    """A credential as the SSO-OIDC CreateToken response produces one."""
    return builder_id._token_from_create(
        {"accessToken": "a", "expiresIn": 3600, "refreshToken": "r"},
        builder_id.RegisteredClient(client_id="cid", client_secret="secret"),
        "us-east-1",
        "identity_center",
        "Enterprise",
        start_url,
    )


class TestCapturedAtLogin:
    def test_the_portal_url_lands_on_the_credential(self):
        assert _created().start_url == _START_URL

    def test_an_identity_with_no_portal_stores_none(self):
        """Social and external-IdP sign-ins have no portal, and whoami emits none.

        Empty string would render as a present-but-blank host; None is the
        absence the panel already handles.
        """
        assert _created("").start_url is None

    def test_both_poll_variants_carry_it(self):
        """A field only one of two sibling functions carries is a trap.

        Production drives `poll_token_once`, but `poll_token` reaches the same
        constructor, and a caller that reaches for it must not silently lose the
        URL.
        """
        import inspect

        for fn in (builder_id.poll_token_once, builder_id.poll_token):
            assert "start_url" in inspect.signature(fn).parameters, fn.__name__


class TestItSurvivesStorage:
    def test_the_round_trip_preserves_it(self):
        restored = KasToken.from_json(_created().to_json())
        assert restored.start_url == _START_URL

    def test_an_entry_written_before_the_field_existed_still_loads(self):
        """Backward compatibility, and the honest consequence of it.

        `from_json` drops unknown keys and fills defaults, so an older entry
        loads with `start_url=None` -- it shows the account kind without a host
        until the user signs in again. That is the price of a field no API can
        backfill, and it must not be a load failure.
        """
        legacy = (
            '{"access_token": "a", "expires_at": "2099-01-01T00:00:00+00:00",'
            ' "provider": "Enterprise", "identity": "identity_center",'
            ' "profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/P"}'
        )
        token = KasToken.from_json(legacy)
        assert token.start_url is None
        assert token.profile_arn.endswith("profile/P")

    def test_it_names_the_account_for_fingerprint_purposes(self):
        """Two organizations must not read as one signed-in account.

        The claim allowlist refuses new fields by default, so this is the
        assertion that the refusal was overridden deliberately rather than by
        accident.
        """
        assert "start_url" in _VAULT_IDENTITY_CLAIMS


class TestItSurvivesRefresh:
    """A field the refresher drops is worse than one never stored.

    It would show the host for an hour and then lose it, which reads as a bug in
    the panel rather than in the credential.
    """

    @pytest.mark.parametrize("builder", ["_refresh_sso_oidc", "_refresh_external_idp"])
    def test_every_refresh_path_reassigns_it(self, builder):
        import inspect

        from kiro_crew.auth import refresh

        fn = getattr(refresh, builder, None)
        if fn is None:
            pytest.skip(f"{builder} is not part of this build")
        src = inspect.getsource(fn)
        assert "start_url=token.start_url" in src, (
            f"{builder} rebuilds the credential without carrying start_url forward"
        )

    def test_the_social_path_carries_it_too(self):
        """Social has no portal today, but the rule is about the shape.

        Every constructor that rebuilds a credential from an older one copies the
        identity metadata across; an exception here is how a field starts leaking
        on one path only.
        """
        import inspect

        from kiro_crew.auth import refresh

        src = inspect.getsource(refresh)
        rebuilds = src.count("return KasToken(")
        carries = src.count("start_url=token.start_url")
        assert carries == rebuilds, (
            f"{rebuilds} credential rebuilds but only {carries} carry start_url"
        )
