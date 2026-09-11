"""Tests for Crew-owned KAS auth: the vault probe and the callback answer.

Both driven against a real :class:`TokenStore` rooted in a temp data home: the
spawn-time vault probe (``kiro_crew.auth.bridge.vault_holds_identity``, plus the
runtime's off-loop wrapper in :mod:`kiro_crew.acp.kas_host_auth`) and the
callback answer (``answer_get_access_token``). The provider's own resolve/refresh
behavior is covered in ``test_kas_auth_flows.py``; here the network is never
touched — a stored token that is still far from expiry is returned without a
refresh, and the failure mapping is exercised by construction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kiro_crew.acp.kas_host_auth import (
    HostAuthCallbackError,
    answer_get_access_token,
    vault_holds_identity_off_loop,
)
from kiro_crew.auth.bridge import (
    describe_vault_identity,
    vault_holds_identity,
    vault_identity_fingerprint,
)
from kiro_crew.auth.store import KasToken, TokenStore, TokenStoreError


def _token(identity: str = "social", *, expires_in: int = 3600, **overrides) -> KasToken:
    kwargs = dict(
        access_token="at-value",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        provider="Google",
        identity=identity,
        refresh_token="rt-value",
        profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/x",
    )
    kwargs.update(overrides)
    return KasToken(**kwargs)


@pytest.fixture
def data_home(tmp_path):
    """Point the seam's vault root (``bridge.data_home``) at a temp directory."""
    home = tmp_path / "data-home"
    home.mkdir()
    with patch("kiro_crew.auth.bridge.data_home", return_value=home):
        yield home


class TestVaultProbe:
    def test_empty_vault_is_no_identity(self, data_home):
        assert vault_holds_identity() is False

    def test_stored_identity_is_detected(self, data_home):
        TokenStore(data_home).save(_token())
        assert vault_holds_identity() is True

    def test_expired_but_refreshable_identity_still_counts(self, data_home):
        """The probe asks "is anyone signed in", not "is the token live": an
        expired access token with a refresh token is exactly what the provider
        refreshes on the first callback, so the spawn must be Crew-owned."""
        TokenStore(data_home).save(_token(expires_in=-60))
        assert vault_holds_identity() is True

    def test_expired_identity_with_nothing_to_renew_it_does_not_count(self, data_home, caplog):
        """An entry that cannot answer a single callback must not capture the
        spawn: it would only shadow a working kiro-cli login. Degrades to
        cli-owned with a WARNING naming the identity, never the token."""
        TokenStore(data_home).save(_token(expires_in=-60, refresh_token=None))
        assert vault_holds_identity() is False
        assert "expired social identity" in caplog.text
        assert "at-value" not in caplog.text

    def test_describe_is_token_free_and_names_the_usability_fields(self, data_home):
        assert describe_vault_identity() is None
        TokenStore(data_home).save(_token())
        line = describe_vault_identity()
        assert line is not None
        assert line.startswith("social/Google")
        assert "refresh token present" in line and "usable" in line
        assert "at-value" not in line and "rt-value" not in line
        TokenStore(data_home).save(_token(expires_in=-60, refresh_token=None))
        line = describe_vault_identity()
        assert line is not None
        assert "access token expired" in line and "NOT usable" in line

    def test_describe_reports_issuer_rejection_without_changing_the_spawn_verdict(self, data_home):
        """A refresh the issuer refused is a diagnosis for the user, not a demotion:
        doctor says so and names the remedy, while the spawn-time predicate still
        chooses the Crew identity (a refresh token is present). Nothing hands the
        spawn back to kiro-cli's login behind the user's back."""
        store = TokenStore(data_home)
        store.save(_token(expires_in=-60))
        store.mark_refresh_rejected("social")
        line = describe_vault_identity()
        assert line is not None
        assert "refresh REJECTED by issuer" in line
        assert "sign-in expired" in line and "sign in again" in line
        assert "at-value" not in line and "rt-value" not in line
        assert vault_holds_identity() is True

    def test_unreadable_vault_degrades_to_no_identity(self, data_home, caplog):
        """A vault error must not fail the spawn; it falls back to cli-owned."""
        with patch(
            "kiro_crew.auth.store.TokenStore.resolve",
            side_effect=TokenStoreError("refusing linked token-store directory"),
        ):
            assert vault_holds_identity() is False
        assert "cli-owned" in caplog.text

    def test_unexpected_error_degrades_to_no_identity(self, data_home):
        with patch("kiro_crew.auth.store.TokenStore.resolve", side_effect=RuntimeError("boom")):
            assert vault_holds_identity() is False

    @pytest.mark.asyncio
    async def test_off_loop_variant_agrees(self, data_home):
        assert await vault_holds_identity_off_loop() is False
        TokenStore(data_home).save(_token())
        assert await vault_holds_identity_off_loop() is True


class TestVaultFingerprint:
    """The vault's contribution to identity-change detection."""

    def test_empty_vault_is_absent(self, data_home):
        assert vault_identity_fingerprint() == ""

    def test_stable_across_refresh_but_not_across_account(self, data_home):
        store = TokenStore(data_home)
        store.save(_token())
        before = vault_identity_fingerprint()
        assert before and "at-value" not in before and "rt-value" not in before
        # A refresh rotates the secrets and the expiry: same account, same digest.
        store.save(_token(access_token="at-2", refresh_token="rt-2", expires_in=7200))
        assert vault_identity_fingerprint() == before
        # A different account (provider / profile) is a different digest.
        store.save(
            _token(provider="Github", profile_arn="arn:aws:codewhisperer:us-east-1:2:profile/y")
        )
        assert vault_identity_fingerprint() != before
        # Sign-out returns to absent.
        store.delete("social")
        assert vault_identity_fingerprint() == ""

    def test_unreadable_vault_is_absent(self, data_home):
        TokenStore(data_home).save(_token())
        with patch("kiro_crew.auth.store.TokenStore.resolve", side_effect=RuntimeError("boom")):
            assert vault_identity_fingerprint() == ""

    @pytest.mark.asyncio
    async def test_a_crew_sign_out_is_an_identity_change_for_the_sweep(self, data_home, tmp_path):
        """What makes an incomplete post-logout retirement retry: the gateway's
        identity fingerprint moves when the vault empties, so the per-turn check
        re-sweeps until the sweep completes -- same path as a kiro-cli logout."""
        from kiro_crew import kiro_prerequisite as kp

        service = kp.KiroPrerequisiteService(
            home=tmp_path / "cli-home", environ={}, platform_name="linux"
        )
        TokenStore(data_home).save(_token())
        changed, live = await service.identity_changed_since_sessions()
        assert changed is True and live != ""  # unset baseline reports changed
        service.note_sessions_reconciled(live)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False

        TokenStore(data_home).delete("social")  # Crew sign-out
        changed, live_after = await service.identity_changed_since_sessions()
        assert changed is True
        assert live_after == ""  # no identity anywhere: never reconciled, re-swept each turn


class TestCallbackAnswer:
    @pytest.mark.asyncio
    async def test_renders_the_covenant_shape_without_the_refresh_token(self, data_home):
        TokenStore(data_home).save(_token())
        resp = await answer_get_access_token()
        assert resp["accessToken"] == "at-value"
        assert resp["provider"] == "Google"
        assert resp["profileArn"].startswith("arn:aws:codewhisperer:")
        assert "expiresAt" in resp
        assert "refreshToken" not in resp
        assert "rt-value" not in str(resp)

    @pytest.mark.asyncio
    async def test_no_identity_is_a_token_free_error(self, data_home):
        with pytest.raises(HostAuthCallbackError) as excinfo:
            await answer_get_access_token()
        assert str(excinfo.value) == "not signed in to Kiro Crew"

    @pytest.mark.asyncio
    async def test_an_ambient_api_key_never_answers_for_the_vault(self, data_home, monkeypatch):
        """Vault only: with the vault empty (signed out) and KIRO_API_KEY set in the
        gateway's environment, the callback still refuses -- the key is not the
        identity the operator signed out of, and it is not an OIDC bearer."""
        monkeypatch.setenv("KIRO_API_KEY", "ambient-key")
        with pytest.raises(HostAuthCallbackError) as excinfo:
            await answer_get_access_token()
        assert str(excinfo.value) == "not signed in to Kiro Crew"

    @pytest.mark.asyncio
    async def test_vault_error_is_categorized_not_echoed(self, data_home, caplog):
        with patch(
            "kiro_crew.auth.store.TokenStore.resolve",
            side_effect=TokenStoreError("refusing linked token-store directory: /secret/path"),
        ):
            with pytest.raises(HostAuthCallbackError) as excinfo:
                await answer_get_access_token()
        assert str(excinfo.value) == "sign-in vault error"
        assert "/secret/path" not in str(excinfo.value)
        assert "/secret/path" in caplog.text

    @pytest.mark.asyncio
    async def test_unexpected_failure_exposes_only_its_type(self, data_home, caplog):
        """A refresh endpoint's response body could ride on an exception
        message; neither the engine nor the log gets it."""
        TokenStore(data_home).save(_token())

        class Exploding(Exception):
            pass

        with patch(
            "kiro_crew.auth.bridge.handle_get_access_token",
            side_effect=Exploding("body: at-value rt-value"),
        ):
            with pytest.raises(HostAuthCallbackError) as excinfo:
                await answer_get_access_token()
        assert str(excinfo.value) == "refresh failed"
        assert "rt-value" not in caplog.text
        assert "Exploding" in caplog.text
