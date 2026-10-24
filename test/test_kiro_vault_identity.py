"""The dashboard's signed-in identity from Crew's vault, in whoami's field vocabulary.

Covers ``kiro_crew.dashboard.handlers.kiro_vault_identity`` (the slot/provider ->
``account_type`` mapping and the one-read snapshot) and the auth bridge's
``vault_identity_claims`` it reads, driven over a real ``TokenStore``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kiro_crew.auth.bridge import vault_identity_claims, vault_identity_fingerprint
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.dashboard.handlers import kiro_vault_identity as kvi


def _token(identity: str = "social", **overrides) -> KasToken:
    kwargs = dict(
        access_token="at-value",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        provider="Google",
        identity=identity,
        refresh_token="rt-value",
        profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/x",
    )
    kwargs.update(overrides)
    return KasToken(**kwargs)


@pytest.fixture
def data_home(tmp_path):
    """Point the bridge's vault root at a temp directory."""
    home = tmp_path / "data-home"
    home.mkdir()
    with patch("kiro_crew.auth.bridge.data_home", return_value=home):
        yield home


class TestAccountTypeVocabulary:
    """Every store slot maps onto the ``accountType`` the frontend already labels."""

    @pytest.mark.parametrize(
        ("identity", "provider", "expected"),
        [
            ("builder_id", "BuilderId", "BuilderId"),
            ("identity_center", "Enterprise", "IamIdentityCenter"),
            ("external_idp", "ExternalIdp", "ExternalIdP"),
            ("social", "Google", "SocialGoogle"),
            ("social", "Github", "SocialGithub"),
        ],
    )
    def test_known_slots(self, identity, provider, expected):
        assert kvi.account_type_from_claims(identity, provider) == expected

    def test_social_keeps_the_vault_spelling_of_the_provider(self):
        # The account modal's badge splits a humanized name at an inner capital
        # ("GitHub" -> "Git Hub"), so the vault's one-capital spelling is kept.
        assert kvi.account_type_from_claims("social", "Github") == "SocialGithub"
        assert kvi.account_type_from_claims("social", "Example") == "SocialExample"

    def test_nothing_labelable_is_empty(self):
        assert kvi.account_type_from_claims("", "") == ""
        assert kvi.account_type_from_claims("social", "") == ""

    def test_fields_carry_only_the_account_type(self):
        # Never an empty ``email`` or ``start_url``: on the kiro-cli path an
        # empty string there reads as "known blank" to the agreement checks.
        assert kvi.identity_fields({"identity": "builder_id", "provider": "BuilderId"}) == {
            "account_type": "BuilderId"
        }
        assert kvi.identity_fields({}) == {}


class TestVaultClaims:
    """One vault read yields the fingerprint and the public claims together."""

    def test_empty_vault(self, data_home):
        assert vault_identity_claims() == ("", {})
        assert kvi.vault_identity_snapshot_sync() == ("", {})

    def test_stored_identity(self, data_home):
        TokenStore(data_home).save(_token())
        fingerprint, claims = vault_identity_claims()
        assert fingerprint == vault_identity_fingerprint()
        assert claims == {"identity": "social", "provider": "Google"}
        # Token-free: no secret, ARN or registration detail in the clear.
        assert "at-value" not in str(claims) and "rt-value" not in str(claims)
        assert "arn:" not in str(claims)
        assert kvi.vault_identity_snapshot_sync() == (fingerprint, {"account_type": "SocialGoogle"})

    def test_unreadable_vault_is_absent(self, data_home):
        TokenStore(data_home).save(_token())
        with patch("kiro_crew.auth.store.TokenStore.resolve", side_effect=RuntimeError("boom")):
            assert vault_identity_claims() == ("", {})
            assert kvi.vault_identity_snapshot_sync() == ("", {})

    @pytest.mark.asyncio
    async def test_off_loop_snapshot_agrees(self, data_home):
        TokenStore(data_home).save(_token("builder_id", provider="BuilderId", profile_arn=None))
        fingerprint, fields = await kvi.vault_identity_snapshot()
        assert fingerprint == vault_identity_fingerprint()
        assert fields == {"account_type": "BuilderId"}
