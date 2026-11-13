"""A Builder ID or Identity Center sign-in reuses the client registration its slot stores.

The two SSO-OIDC calls are faked at the ``builder_id`` seams, with the service and
sign-in helpers of ``test_kas_login_service.py``, so no test touches AWS.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from test_kas_login_service import _begin_oidc, _oidc_auth, _service

from kiro_crew.auth.login import builder_id, endpoints
from kiro_crew.auth.store import KasToken, TokenStore


def _store_oidc_sign_in(tmp_path, identity: str, *, region: str, lapses_in: int | None) -> None:
    """A stored SSO-OIDC sign-in whose client registration lapses ``lapses_in`` seconds
    from now (``None``: an entry from before the expiry was kept)."""
    now = datetime.now(timezone.utc)
    TokenStore(tmp_path).save(
        KasToken(
            access_token="at-old",
            expires_at=now + timedelta(hours=1),
            provider="BuilderId" if identity == "builder_id" else "Enterprise",
            identity=identity,
            refresh_token="rt-old",
            profile_arn=None if identity == "builder_id" else "arn:aws:kiro:us-east-1:1:profile/p",
            region=region,
            client_id="cid-stored",
            client_secret="csec-stored",
            client_secret_expires_at=(
                None if lapses_in is None else int(now.timestamp()) + lapses_in
            ),
        )
    )


@pytest.mark.parametrize(
    ("provider", "identity", "kwargs"),
    [
        ("builder_id", "builder_id", {}),
        ("idc", "identity_center", {"start_url": "https://acme.awsapps.com/start"}),
    ],
)
@pytest.mark.asyncio
async def test_a_sign_in_reuses_the_slots_registration_while_it_is_in_date(
    tmp_path, monkeypatch, provider, identity, kwargs
):
    _store_oidc_sign_in(tmp_path, identity, region="us-east-1", lapses_in=30 * 86400)
    service, _ = _service(tmp_path)

    async def _never(*_a, **_k):
        raise AssertionError("an in-date registration must not be replaced")

    async def _fake_start(client, *, region, start_url, session):
        return _oidc_auth()

    monkeypatch.setattr(builder_id, "register_client", _never)
    monkeypatch.setattr(builder_id, "start_device_authorization", _fake_start)
    begin = await service.begin_device(provider, **kwargs)
    client = service._pending[begin["login_id"]].client
    assert (client.client_id, client.client_secret) == ("cid-stored", "csec-stored")


@pytest.mark.parametrize(
    ("region", "lapses_in"),
    [
        ("us-east-1", -3600),  # lapsed
        ("us-east-1", 2 * 86400),  # would lapse during the sign-in it starts
        ("us-east-1", None),  # expiry never recorded
        ("eu-west-1", 30 * 86400),  # another region's registration
    ],
)
@pytest.mark.asyncio
async def test_a_sign_in_registers_a_new_client_when_the_stored_one_cannot_serve(
    tmp_path, monkeypatch, region, lapses_in
):
    _store_oidc_sign_in(tmp_path, "builder_id", region=region, lapses_in=lapses_in)
    service, _ = _service(tmp_path)
    begin = await _begin_oidc(service, monkeypatch, "builder_id")
    assert service._pending[begin["login_id"]].client.client_id == "cid-1"


@pytest.mark.asyncio
async def test_a_refused_stored_registration_is_replaced_but_a_refused_start_url_is_not(
    tmp_path, monkeypatch
):
    """AWS refusing the stored client must not block signing in again, while a start
    URL AWS refuses is the user's to fix and keeps its own error."""
    _store_oidc_sign_in(tmp_path, "identity_center", region="us-east-1", lapses_in=30 * 86400)
    service, _ = _service(tmp_path)
    acme = "https://acme.awsapps.com/start"
    registered: list[str] = []
    refusal: builder_id.BuilderIdAuthError = builder_id.BuilderIdAuthError(
        "StartDeviceAuthorization failed: HTTP 401 invalid_client"
    )

    async def _register(region, *, session, scopes=builder_id.OIDC_SCOPES):
        registered.append(region)
        return builder_id.RegisteredClient(client_id="cid-1", client_secret="csec-1")

    async def _start(client, *, region, start_url, session):
        if client.client_id == "cid-stored":
            raise refusal
        return _oidc_auth()

    monkeypatch.setattr(builder_id, "register_client", _register)
    monkeypatch.setattr(builder_id, "start_device_authorization", _start)
    begin = await service.begin_device("idc", start_url=acme)
    assert service._pending[begin["login_id"]].client.client_id == "cid-1"
    assert registered == ["us-east-1"]

    refusal = builder_id.StartUrlRejectedError("AWS did not accept the start URL")
    with pytest.raises(builder_id.StartUrlRejectedError):
        await service.begin_device("idc", start_url=acme)
    assert registered == ["us-east-1"]


# ---- KIRO_OIDC_SCOPE_PREFIX ------------------------------------------------------

_PREFIXED = ("acme_test:completions", "acme_test:analysis", "acme_test:conversations")


def test_scope_prefix_unset_or_blank_keeps_the_public_scopes(monkeypatch):
    monkeypatch.delenv(endpoints.OIDC_SCOPE_PREFIX_ENV, raising=False)
    assert endpoints.oidc_scopes() == endpoints.OIDC_SCOPES
    monkeypatch.setenv(endpoints.OIDC_SCOPE_PREFIX_ENV, "  ")
    assert endpoints.oidc_scopes() == endpoints.OIDC_SCOPES
    assert endpoints.OIDC_SCOPES == (
        "codewhisperer:completions",
        "codewhisperer:analysis",
        "codewhisperer:conversations",
    )


def test_scope_prefix_replaces_the_public_prefix(monkeypatch):
    monkeypatch.setenv(endpoints.OIDC_SCOPE_PREFIX_ENV, "acme_test")
    assert endpoints.oidc_scopes() == _PREFIXED


@pytest.mark.parametrize("bad", ["acme:x", "acme test", "Acme", "acme1", "acme-test", "x" * 65])
def test_scope_prefix_that_is_not_a_token_is_ignored_with_a_warning(monkeypatch, caplog, bad):
    monkeypatch.setenv(endpoints.OIDC_SCOPE_PREFIX_ENV, bad)
    with caplog.at_level("WARNING", logger=endpoints.__name__):
        assert endpoints.oidc_scopes() == endpoints.OIDC_SCOPES
    assert endpoints.OIDC_SCOPE_PREFIX_ENV in caplog.text


@pytest.mark.parametrize(
    ("provider", "identity", "kwargs"),
    [
        ("builder_id", "builder_id", {}),
        ("idc", "identity_center", {"start_url": "https://acme.awsapps.com/start"}),
    ],
)
@pytest.mark.parametrize(("prefix", "want"), [(None, None), ("acme_test", _PREFIXED)])
@pytest.mark.asyncio
async def test_a_sign_in_registers_for_the_configured_scopes(
    tmp_path, monkeypatch, provider, identity, kwargs, prefix, want
):
    """Unset, every SSO-OIDC sign-in registers the public scopes; with a prefix, an
    in-date public-scope registration is not reused and the prefixed ones are asked."""
    want = want or endpoints.OIDC_SCOPES
    if prefix is None:
        monkeypatch.delenv(endpoints.OIDC_SCOPE_PREFIX_ENV, raising=False)
    else:
        monkeypatch.setenv(endpoints.OIDC_SCOPE_PREFIX_ENV, prefix)
    _store_oidc_sign_in(tmp_path, identity, region="us-east-1", lapses_in=30 * 86400)
    service, _ = _service(tmp_path)
    registered: list[tuple[str, ...]] = []

    async def _register(region, *, session, scopes=builder_id.OIDC_SCOPES):
        registered.append(scopes)
        return builder_id.RegisteredClient("cid-1", "csec-1", scopes=scopes)

    async def _start(client, *, region, start_url, session):
        return _oidc_auth()

    monkeypatch.setattr(builder_id, "register_client", _register)
    monkeypatch.setattr(builder_id, "start_device_authorization", _start)
    begin = await service.begin_device(provider, **kwargs)
    client = service._pending[begin["login_id"]].client
    assert client.scopes == want
    assert registered == ([] if prefix is None else [want])
