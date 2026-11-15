"""A Builder ID or Identity Center sign-in reuses the client registration its slot stores.

The two SSO-OIDC calls are faked at the ``builder_id`` seams, with the service and
sign-in helpers of ``test_kas_login_service.py``, so no test touches AWS.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from test_kas_login_service import _begin_oidc, _oidc_auth, _service, _viewer_config

from kiro_crew.auth.login import builder_id
from kiro_crew.auth.login.endpoints import (
    OIDC_SCOPES,
    OIDC_SCOPES_EXTRA_KEY,
    oidc_scopes,
)
from kiro_crew.auth.store import KasToken, TokenStore

# A made-up prefix: the tests never name a real non-prod one.
_TEST_PREFIX = "test_prefix"
_TEST_SCOPES = oidc_scopes(_TEST_PREFIX)


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

    async def _register(region, *, session, **_k):
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


@pytest.mark.parametrize(
    "stored_scopes, prefixed, reused",
    [
        (None, True, False),  # a prod registration is not reused under a prefix
        (list(_TEST_SCOPES), False, False),  # nor a prefixed one for prod
        (list(_TEST_SCOPES), True, True),
        (None, False, True),  # prod reuse unchanged
    ],
    ids=["public-to-prefixed", "prefixed-to-public", "prefixed-kept", "public-kept"],
)
@pytest.mark.asyncio
async def test_a_registration_is_reused_only_for_the_scopes_it_was_made_with(
    tmp_path, monkeypatch, stored_scopes, prefixed, reused
):
    _store_oidc_sign_in(tmp_path, "identity_center", region="us-east-1", lapses_in=30 * 86400)
    if stored_scopes is not None:
        store = TokenStore(tmp_path)
        entry = store.load("identity_center")
        assert entry is not None
        entry.extra[OIDC_SCOPES_EXTRA_KEY] = stored_scopes
        store.save(entry)
    _viewer_config(
        monkeypatch,
        enabled=True,
        url="https://management.gamma.kiro.example",
        prefix=_TEST_PREFIX if prefixed else "",
    )
    service, _ = _service(tmp_path)
    registered: list[tuple[str, ...]] = []

    async def _register(region, *, session, scopes):
        registered.append(scopes)
        return builder_id.RegisteredClient("cid-1", "csec-1", scopes=scopes)

    async def _start(client, *, region, start_url, session):
        return _oidc_auth()

    monkeypatch.setattr(builder_id, "register_client", _register)
    monkeypatch.setattr(builder_id, "start_device_authorization", _start)
    begin = await service.begin_device("idc", start_url="https://acme.awsapps.com/start")
    client = service._pending[begin["login_id"]].client
    want = _TEST_SCOPES if prefixed else OIDC_SCOPES
    assert client.client_id == ("cid-stored" if reused else "cid-1")
    assert registered == ([] if reused else [want])
