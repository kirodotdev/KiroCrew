"""The personal Crew's lease credential, read from the Crew's own Kiro sign-in."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from kiro_crew.auth.provider import KasAuthProvider
from kiro_crew.auth.refresh import RefreshRejected
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.relay_client.lease import LeaseCredentialError
from kiro_crew.relay_client.lease_signin import read_lease_sign_in

pytestmark = pytest.mark.asyncio

_IDC_ARN = "arn:aws:codewhisperer:us-east-1:111122223333:profile/IDCPROFILE"
_EXT_ARN = "arn:aws:codewhisperer:us-east-1:111122223333:profile/EXTPROFILE"
_REFUSED = "can't host a personal Crew"


def _token(
    identity: str,
    *,
    profile: Optional[str] = _IDC_ARN,
    auth_method: Optional[str] = None,
    provider: str = "Enterprise",
) -> KasToken:
    return KasToken(
        access_token=f"at-{identity}-secret",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        provider=provider,
        identity=identity,
        refresh_token=f"rt-{identity}-secret",
        profile_arn=profile,
        auth_method=auth_method,
        client_id="cid",
        client_secret="csecret",
    )


def _store(tmp_path: Path, monkeypatch, *tokens: KasToken) -> KasAuthProvider:
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    store = TokenStore(tmp_path)
    for token in tokens:
        store.save(token)
    return KasAuthProvider(store, allow_env_api_key=False)


class _Fake:
    """A provider that answers one token, or raises, and records what it was asked."""

    def __init__(self, token: Optional[KasToken] = None, exc: Optional[Exception] = None):
        self.token, self.exc, self.rejected = token, exc, "unset"

    async def current(self, *, rejected_access_token=None):
        self.rejected = rejected_access_token
        if self.exc is not None:
            raise self.exc
        return self.token


def _no_secret(text: str) -> None:
    for value in ("-secret", _IDC_ARN, _EXT_ARN, "csecret"):
        assert value not in text


async def test_identity_center_sends_no_token_type_and_its_profile(tmp_path, monkeypatch):
    signin = await read_lease_sign_in(
        provider=_store(tmp_path, monkeypatch, _token("identity_center"))
    )
    assert signin.bearer == "at-identity_center-secret"
    assert signin.token_type is None
    assert signin.profile_arn == _IDC_ARN
    _no_secret(repr(signin))


async def test_external_idp_token_type_follows_auth_method(tmp_path, monkeypatch):
    token = _token(
        "external_idp", profile=_EXT_ARN, auth_method="external_idp", provider="ExternalIdp"
    )
    signin = await read_lease_sign_in(provider=_store(tmp_path, monkeypatch, token))
    assert signin.token_type == "EXTERNAL_IDP"
    assert signin.profile_arn == _EXT_ARN


@pytest.mark.parametrize(
    ("identity", "auth_method", "expected"),
    [
        # The slot and the auth method disagree: the header follows auth_method,
        # as the relay viewer's does.
        ("identity_center", "external_idp", "EXTERNAL_IDP"),
        ("external_idp", None, None),
    ],
)
async def test_disagreeing_slot_and_auth_method_follow_auth_method(identity, auth_method, expected):
    fake = _Fake(_token(identity, auth_method=auth_method))
    assert (await read_lease_sign_in(provider=fake)).token_type == expected


@pytest.mark.parametrize(
    ("identity", "provider"), [("builder_id", "BuilderId"), ("social", "Google")]
)
async def test_builder_id_and_social_are_refused(tmp_path, monkeypatch, identity, provider):
    token = _token(identity, provider=provider)
    with pytest.raises(LeaseCredentialError, match=_REFUSED) as caught:
        await read_lease_sign_in(provider=_store(tmp_path, monkeypatch, token))
    _no_secret(str(caught.value))


async def test_builder_id_chosen_over_a_stored_identity_center_is_refused(tmp_path, monkeypatch):
    # The store's priority picks Builder ID over IAM Identity Center; the reader
    # does not look past it.
    provider = _store(
        tmp_path,
        monkeypatch,
        _token("identity_center"),
        _token("builder_id", profile=None, provider="BuilderId"),
    )
    assert (await provider.current()).identity == "builder_id"
    with pytest.raises(LeaseCredentialError, match=_REFUSED) as caught:
        await read_lease_sign_in(provider=provider)
    assert str(caught.value) == (
        "the Crew's Kiro sign-in can't host a personal Crew; sign in with IAM Identity "
        "Center or your organization's identity provider, and sign out of Builder ID or "
        "any social sign-in on this Crew"
    )


async def test_a_sign_in_with_no_profile_is_refused():
    # The store resolves no IAM Identity Center entry without a profile, so the
    # case is a stored external IdP entry with none.
    token = _token("external_idp", profile=None, auth_method="external_idp")
    with pytest.raises(LeaseCredentialError, match="names no Kiro profile"):
        await read_lease_sign_in(provider=_Fake(token))


async def test_signed_out_is_a_credential_error(tmp_path, monkeypatch):
    with pytest.raises(LeaseCredentialError, match="not signed in"):
        await read_lease_sign_in(provider=_store(tmp_path, monkeypatch))
    with pytest.raises(LeaseCredentialError, match="not signed in"):
        await read_lease_sign_in(provider=_Fake(exc=RefreshRejected("rt-x-secret refused")))


async def test_a_transient_failure_names_only_its_type():
    fake = _Fake(exc=RuntimeError("at-identity_center-secret leaked"))
    with pytest.raises(LeaseCredentialError, match=r"\(RuntimeError\)") as caught:
        await read_lease_sign_in(provider=fake)
    _no_secret(str(caught.value))


async def test_rejected_bearer_is_passed_to_the_refresh():
    fake = _Fake(_token("identity_center"))
    await read_lease_sign_in("at-old", provider=fake)
    assert fake.rejected == "at-old"
    await read_lease_sign_in(provider=fake)
    assert fake.rejected is None


async def test_an_environment_api_key_never_stands_in(tmp_path, monkeypatch):
    import kiro_crew.auth.bridge as bridge

    monkeypatch.setattr(bridge, "default_token_store", lambda: TokenStore(tmp_path))
    monkeypatch.setenv("KIRO_API_KEY", "ksk-env-secret")
    with pytest.raises(LeaseCredentialError, match="not signed in"):
        await read_lease_sign_in()


async def test_no_secret_in_logs(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    await read_lease_sign_in(provider=_store(tmp_path, monkeypatch, _token("identity_center")))
    for exc in (RefreshRejected("x"), RuntimeError("y")):
        with pytest.raises(LeaseCredentialError):
            await read_lease_sign_in(provider=_Fake(exc=exc))
    with pytest.raises(LeaseCredentialError):
        await read_lease_sign_in(provider=_Fake(_token("builder_id", provider="BuilderId")))
    _no_secret(caplog.text)
