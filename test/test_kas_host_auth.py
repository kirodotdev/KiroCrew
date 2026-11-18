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
from kiro_crew.platform.context import reset_context
from kiro_crew.platform.defaults import DefaultAgentIdentityProvider
from kiro_crew.workload_identity import SpiffeAgentIdentityProvider


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

    def test_a_lapsed_identity_is_not_signed_in_and_never_falls_back(self, data_home, caplog):
        """A stored entry is the sign-in even when it cannot answer a callback:
        the spawn stays Crew-owned, so it reports "not signed in" instead of
        running as kiro-cli's account. The WARNING says how to sign in again and
        that removing the entry hands over to kiro-cli; it never shows the token."""
        TokenStore(data_home).save(_token(expires_in=-60, refresh_token=None))
        assert vault_holds_identity() is True
        assert "kas login --provider idc" in caplog.text
        assert "kas logout" in caplog.text and "kiro-cli's sign-in" in caplog.text
        assert "at-value" not in caplog.text

    def test_warn_false_keeps_the_verdict_and_logs_at_debug(self, data_home, caplog):
        """A timer-driven caller asks the same question without repeating the
        spawn-time WARNING: same verdicts, diagnostics at DEBUG."""
        TokenStore(data_home).save(_token(expires_in=-60, refresh_token=None))
        with caplog.at_level("DEBUG", logger="kiro_crew.auth.bridge"):
            assert vault_holds_identity(warn=False) is True
            with patch(
                "kiro_crew.auth.store.TokenStore.resolve",
                side_effect=TokenStoreError("refusing linked token-store directory"),
            ):
                assert vault_holds_identity(warn=False) is True
        bridge_records = [r for r in caplog.records if r.name == "kiro_crew.auth.bridge"]
        assert [r.levelname for r in bridge_records] == ["DEBUG", "DEBUG"]
        assert "at-value" not in caplog.text
        caplog.clear()
        # The default still warns, for the spawn and doctor.
        with caplog.at_level("DEBUG", logger="kiro_crew.auth.bridge"):
            assert vault_holds_identity() is True
        assert [r.levelname for r in caplog.records if r.name == "kiro_crew.auth.bridge"] == [
            "WARNING"
        ]
        TokenStore(data_home).save(_token())
        assert vault_holds_identity(warn=False) is True

    def test_describe_is_token_free_and_names_the_usability_fields(self, data_home):
        assert describe_vault_identity() is None
        TokenStore(data_home).save(_token())
        line = describe_vault_identity()
        assert line is not None
        assert line.startswith("social/Google")
        assert "refresh token present" in line and "usable" in line
        # The EXACT suffix, not just the word: doctor's vault-owned auth row keys
        # its health glyph on endswith("-> usable") (cli_doctor._doctor_agent_auth),
        # so a reword here would silently flip healthy rows to a warning there.
        assert line.endswith("-> usable")
        assert "at-value" not in line and "rt-value" not in line
        TokenStore(data_home).save(_token(expires_in=-60, refresh_token=None))
        line = describe_vault_identity()
        assert line is not None
        assert "access token expired" in line and "NOT usable" in line
        assert not line.endswith("-> usable")

    def test_describe_dates_a_lapsed_client_registration(self, data_home):
        """SSO-OIDC refuses a refresh past the client registration's expiry with a
        bare invalid_client, so doctor names the date and does not call it usable,
        before the refusal and after it; a registration still in date changes nothing."""
        store = TokenStore(data_home)
        tok = _token(identity="builder_id", provider="BuilderId", client_id="c", client_secret="s")
        tok.client_secret_expires_at = int(
            datetime(2026, 1, 31, 12, tzinfo=timezone.utc).timestamp()
        )
        store.save(tok)
        line = describe_vault_identity()
        assert line is not None
        assert "client registration expired on 2026-01-31" in line
        assert line.endswith("-> cannot renew -- sign in again from the dashboard or sign out")
        store.mark_refresh_rejected("builder_id")
        line = describe_vault_identity()
        assert line is not None and "client registration expired on 2026-01-31" in line
        assert line.endswith("-> sign-in expired -- sign in again from the dashboard or sign out")
        tok.client_secret_expires_at = int(
            (datetime.now(timezone.utc) + timedelta(days=30)).timestamp()
        )
        store.save(tok)
        line = describe_vault_identity()
        assert line is not None and line.endswith("-> usable")
        assert "client registration" not in line

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
        # Doctor keys its vault-owned glyph on endswith("-> usable"): this verdict
        # must never carry that suffix, or a rejected sign-in renders green there.
        assert not line.endswith("-> usable")
        assert "at-value" not in line and "rt-value" not in line
        assert vault_holds_identity() is True

    def test_an_unreadable_vault_entry_keeps_the_spawn_crew_owned(self, data_home, caplog):
        """A stored entry that cannot be read is not signed in, never a silent
        switch to kiro-cli's account: the spawn stays Crew-owned and fails there."""
        TokenStore(data_home).save(_token())
        with patch(
            "kiro_crew.auth.store.TokenStore.resolve",
            side_effect=TokenStoreError("refusing linked token-store directory"),
        ):
            assert vault_holds_identity() is True
        assert "Crew-owned" in caplog.text
        assert "kas logout" in caplog.text

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

    def test_a_leftover_entry_labels_no_catalog_under_the_workload_switch(
        self, data_home, monkeypatch
    ):
        from kiro_crew.agent_sdk import kas_model_catalog

        # Off first, whatever the host sets: the first half asserts the vault's catalog identity.
        monkeypatch.delenv("KIROCREW_WORKLOAD_IDENTITY", raising=False)
        TokenStore(data_home).save(_token())
        fingerprint = vault_identity_fingerprint()
        assert kas_model_catalog.vault_identity() == fingerprint != ""
        monkeypatch.setenv("KIROCREW_WORKLOAD_IDENTITY", "spiffe")
        reset_context()  # the switch is read when the context is composed
        # No catalog is recorded or served under the leftover account; the vault's own
        # fingerprint (identity-change detection) is unchanged.
        assert kas_model_catalog.vault_identity() == ""
        assert kas_model_catalog.recorded_catalog_snapshot("") is None
        assert vault_identity_fingerprint() == fingerprint

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


class _ForcedSeam(SpiffeAgentIdentityProvider):
    """The SPIFFE provider with ``enabled()`` fixed, whatever the switch says."""

    def __init__(self, on: bool) -> None:
        self._on = on

    def enabled(self) -> bool:
        return self._on


class _EditionSeam(DefaultAgentIdentityProvider):
    """An edition's own agent-identity provider: enabled, but not the SPIFFE one."""

    def enabled(self) -> bool:
        return True


async def _the_five_sites_say(data_home, monkeypatch) -> dict:
    """What each of the five workload sites decides, read through their own code."""
    from unittest.mock import AsyncMock

    from kiro_crew import workload_identity
    from kiro_crew.acp import kas_host_auth
    from kiro_crew.agent_sdk import kas_model_catalog
    from kiro_crew.dashboard import kiro_readiness
    from kiro_crew.dashboard.handlers import sessions

    TokenStore(data_home).save(_token())  # a vault entry, so "off" has an answer
    answer = AsyncMock(return_value={"accessToken": "workload"})
    monkeypatch.setattr(kas_host_auth, "_answer_from_workload_identity", answer)
    try:
        callback = await kas_host_auth.answer_get_access_token()
    except HostAuthCallbackError:
        callback = None
    ready_answer = AsyncMock(return_value={})
    monkeypatch.setattr(workload_identity, "kas_access_token_answer", ready_answer)
    monkeypatch.setattr(
        workload_identity.WorkloadIdentitySettings,
        "from_env",
        classmethod(lambda cls, env=None: object()),
    )
    monkeypatch.setattr(kiro_readiness, "_kas_bundle_turn_sync", lambda *_a: True)
    await kiro_readiness.kas_crew_signin_ready(None)
    return {
        "kas_host_auth": answer.await_count == 1 and callback == {"accessToken": "workload"},
        "bridge": _bridge_says_crew_owned(monkeypatch),
        "kas_model_catalog": kas_model_catalog.vault_identity() == "",
        "sessions": sessions._workload_sign_in(),
        "kiro_readiness": ready_answer.await_count == 1,
    }


def _bridge_says_crew_owned(monkeypatch) -> bool:
    # The shared resolver is made to pick kiro-cli, so only the seam can answer True.
    from types import SimpleNamespace

    from kiro_crew import lts_sign_in

    monkeypatch.setattr(lts_sign_in, "select", lambda **_k: SimpleNamespace(source=None))
    return vault_holds_identity() is True


def _install_seam(on: bool) -> None:
    _install_provider(_ForcedSeam(on))


def _install_provider(provider) -> None:
    import dataclasses

    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.context import set_context

    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, agent_identity=provider))


@pytest.mark.asyncio
async def test_a_seam_reporting_no_workload_identity_turns_every_site_off(data_home, monkeypatch):
    # The switch is set, but the composed seam says disabled: the seam decides.
    monkeypatch.setenv("KIROCREW_WORKLOAD_IDENTITY", "spiffe")
    _install_seam(False)
    said = await _the_five_sites_say(data_home, monkeypatch)
    assert said == dict.fromkeys(said, False), said


@pytest.mark.asyncio
async def test_a_seam_reporting_a_workload_identity_turns_every_site_on(data_home, monkeypatch):
    # The switch is unset, but the composed seam is the SPIFFE provider: the seam decides.
    monkeypatch.delenv("KIROCREW_WORKLOAD_IDENTITY", raising=False)
    _install_provider(SpiffeAgentIdentityProvider())
    said = await _the_five_sites_say(data_home, monkeypatch)
    assert said == dict.fromkeys(said, True), said


@pytest.mark.asyncio
async def test_an_edition_enabled_identity_leaves_every_site_on_the_vault(data_home, monkeypatch):
    # A companion edition's own enabled provider, switch unset: no SPIFFE reroute.
    # bridge False here means kiro-cli keeps its own sign-in, as today.
    monkeypatch.delenv("KIROCREW_WORKLOAD_IDENTITY", raising=False)
    _install_provider(_EditionSeam())
    said = await _the_five_sites_say(data_home, monkeypatch)
    assert said == dict.fromkeys(said, False), said
    assert (await answer_get_access_token())["accessToken"] == "at-value"


@pytest.mark.parametrize("value", [None, "", "  \t", "spiffe"])
def test_the_import_guard_agrees_with_the_switch_rule(monkeypatch, value):
    # The bootstrap's import guard must never disagree with workload_identity.enabled().
    from kiro_crew import workload_identity
    from kiro_crew.platform.bootstrap import _public_agent_identity_provider

    if value is None:
        monkeypatch.delenv("KIROCREW_WORKLOAD_IDENTITY", raising=False)
    else:
        monkeypatch.setenv("KIROCREW_WORKLOAD_IDENTITY", value)
    composed = _public_agent_identity_provider()
    spiffe = isinstance(composed, workload_identity.SpiffeAgentIdentityProvider)
    assert spiffe is workload_identity.enabled(), value


def test_the_switch_composes_the_spiffe_provider_and_its_absence_the_default(monkeypatch):
    from kiro_crew import workload_identity
    from kiro_crew.platform.bootstrap import (
        _WORKLOAD_IDENTITY_SWITCH,
        _public_agent_identity_provider,
    )
    from kiro_crew.platform.defaults import DefaultAgentIdentityProvider

    assert _WORKLOAD_IDENTITY_SWITCH == workload_identity.ENV_MODE
    monkeypatch.delenv("KIROCREW_WORKLOAD_IDENTITY", raising=False)
    assert isinstance(_public_agent_identity_provider(), DefaultAgentIdentityProvider)
    monkeypatch.setenv("KIROCREW_WORKLOAD_IDENTITY", "spiffe")
    provider = _public_agent_identity_provider()
    assert isinstance(provider, workload_identity.SpiffeAgentIdentityProvider)
    assert provider.enabled() is True
    assert provider.gateway_mcp_spec() is None and provider.status() == {}
