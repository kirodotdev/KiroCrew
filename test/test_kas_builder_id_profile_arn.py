"""A Builder ID sign-in gives the engine kiro-cli's fixed Builder ID profile ARN.

Builder ID has no profile of its own (``ListAvailableProfiles`` denies it), so the
vault stores none. Without an ARN in the ``_kiro/auth/getAccessToken`` reply the
Kiro runtime refuses every request with HTTP 400 "profileArn is required for
this request". kiro-cli answers the same callback with a fixed ARN for a Builder
ID token; these tests pin that Crew does the same, on every engine-facing read,
and that no other identity is touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew.auth.provider import BUILDER_ID_PROFILE_ARN, KasAuthProvider, kas_profile_arn
from kiro_crew.auth.store import KasToken, TokenStore

pytestmark = pytest.mark.asyncio

_KIRO_CLI_BUILDER_ID_ARN = "arn:aws:codewhisperer:us-east-1:638616132270:profile/AAAACCCCXXXX"


def _token(identity: str, provider: str, *, profile: str | None) -> KasToken:
    return KasToken(
        access_token=f"at-{identity}",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        provider=provider,
        identity=identity,
        refresh_token=f"rt-{identity}",
        profile_arn=profile,
        client_id="cid",
        client_secret="csecret",
    )


def _provider(tmp_path: Path, monkeypatch, token: KasToken) -> KasAuthProvider:
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    store = TokenStore(tmp_path)
    store.save(token)
    return KasAuthProvider(store, allow_env_api_key=False)


async def test_constant_is_kiro_clis_builder_id_arn():
    assert BUILDER_ID_PROFILE_ARN == _KIRO_CLI_BUILDER_ID_ARN


async def test_builder_id_callback_carries_the_fixed_arn(tmp_path: Path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch, _token("builder_id", "BuilderId", profile=None))
    resp = await provider.get_access_token_callback()
    assert resp["profileArn"] == BUILDER_ID_PROFILE_ARN
    assert resp["provider"] == "BuilderId"
    assert resp["accessToken"] == "at-builder_id"
    assert "refreshToken" not in resp


async def test_builder_id_profile_arn_and_request_credential(tmp_path: Path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch, _token("builder_id", "BuilderId", profile=None))
    assert await provider.get_profile_arn() == BUILDER_ID_PROFILE_ARN
    cred = await provider.resolve_request_credential()
    assert cred["profileArn"] == BUILDER_ID_PROFILE_ARN


async def test_builder_id_ignores_a_stored_arn_like_kiro_cli():
    token = _token(
        "builder_id", "BuilderId", profile="arn:aws:codewhisperer:eu-central-1:1:profile/X"
    )
    assert kas_profile_arn(token) == BUILDER_ID_PROFILE_ARN


@pytest.mark.parametrize(
    ("identity", "provider", "stored"),
    [
        ("identity_center", "Enterprise", "arn:aws:codewhisperer:eu-central-1:1:profile/ORG"),
        ("social", "Google", "arn:aws:codewhisperer:us-east-1:1:profile/SOCIAL"),
        ("external_idp", "ExternalIdp", None),
    ],
)
async def test_other_identities_send_what_their_sign_in_stored(identity, provider, stored):
    assert kas_profile_arn(_token(identity, provider, profile=stored)) == stored


async def test_external_idp_without_arn_still_omits_the_key(tmp_path: Path, monkeypatch):
    token = _token("external_idp", "ExternalIdp", profile=None)
    token.token_endpoint = "https://idp.example/token"
    provider = _provider(tmp_path, monkeypatch, token)
    resp = await provider.get_access_token_callback()
    assert "profileArn" not in resp
