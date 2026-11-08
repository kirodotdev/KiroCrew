"""``auth/whoami.py``: the vault's identity in ``kiro-cli whoami``'s vocabulary.

The cloud sign-in reads a remote identity through ``parse_whoami_output`` whichever
program printed it, so the document the vault renders must parse there into the
same fields kiro-cli's does, carry no e-mail (the vault stores none), and never a
token. The OIDC sign-in records the start URL it was begun against, which is the
one field that tells one organisation's Identity Center sign-in from another's.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew.auth.login import builder_id, control_plane
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.auth.whoami import (
    START_URL_EXTRA_KEY,
    account_type_for,
    resolve_signed_in,
    vault_owns_sign_in,
    whoami_document,
)
from kiro_crew.cloud.login_target import (
    KiroLoginTarget,
    identity_matches_target,
    is_personal_account_type,
    parse_whoami_output,
    target_from_whoami,
)

_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _token(identity: str, provider: str, **extra) -> KasToken:
    return KasToken(
        access_token="atk-secret-value",
        expires_at=_FUTURE,
        provider=provider,
        identity=identity,
        refresh_token="rtk-secret-value",
        profile_arn="arn:aws:kiro:us-east-1:1:profile/p1",
        extra=dict(extra),
    )


@pytest.mark.parametrize(
    "identity, provider, expected",
    [
        ("builder_id", "BuilderId", "BuilderId"),
        ("identity_center", "Enterprise", "IamIdentityCenter"),
        ("external_idp", "ExternalIdp", "ExternalIdP"),
        ("social", "Google", "SocialGoogle"),
        ("social", "Github", "SocialGithub"),
        ("social", "", ""),
        ("not-a-slot", "BuilderId", ""),
    ],
)
def test_account_type_spells_each_slot_the_way_kiro_cli_does(identity, provider, expected):
    assert account_type_for(identity, provider) == expected


def test_the_document_parses_through_the_cloud_whoami_parser():
    doc = whoami_document(_token("builder_id", "BuilderId"))
    parsed = parse_whoami_output(json.dumps(doc))
    assert parsed == {"account_type": "BuilderId"}
    # A Builder ID vault sign-in satisfies the default login target, as kiro-cli's does.
    assert identity_matches_target(parsed, KiroLoginTarget())
    assert is_personal_account_type(parsed["account_type"])


def test_an_identity_center_document_carries_the_recorded_start_url():
    doc = whoami_document(
        _token("identity_center", "Enterprise", start_url="https://acme.awsapps.com/start")
    )
    parsed = parse_whoami_output(json.dumps(doc))
    assert parsed["account_type"] == "IamIdentityCenter"
    assert parsed["start_url"] == "https://acme.awsapps.com/start"
    target = KiroLoginTarget.from_fields(
        start_url="https://acme.awsapps.com/start", region="us-east-1"
    )
    assert identity_matches_target(parsed, target)
    other = KiroLoginTarget.from_fields(
        start_url="https://other.awsapps.com/start", region="us-east-1"
    )
    assert not identity_matches_target(parsed, other)
    assert target_from_whoami(parsed, region="us-east-1") == target


def test_an_identity_center_document_without_a_start_url_is_an_unknown_organisation():
    # A token stored before the start URL was recorded: the parser sees the type and
    # no URL, and the target derivation answers None (not Builder ID), as it does for
    # a kiro-cli whoami that omitted it.
    doc = whoami_document(_token("identity_center", "Enterprise"))
    assert "startUrl" not in doc
    assert target_from_whoami(parse_whoami_output(json.dumps(doc))) is None


def test_the_document_never_carries_a_secret_or_an_email():
    token = _token("social", "Google", start_url="https://ignored.example")
    doc = whoami_document(token)
    serialised = json.dumps(doc)
    assert token.access_token not in serialised
    assert token.refresh_token not in serialised
    assert token.profile_arn not in serialised
    assert "email" not in doc
    # The whoami parser must not read a blank e-mail as "known to be blank".
    assert "email" not in parse_whoami_output(serialised)


def test_a_non_string_start_url_extra_is_not_rendered():
    doc = whoami_document(_token("builder_id", "BuilderId", **{START_URL_EXTRA_KEY: 42}))
    assert "startUrl" not in doc


def test_resolve_signed_in_is_the_usable_predicate(tmp_path):
    store = TokenStore(tmp_path)
    assert resolve_signed_in(store) is None
    lapsed = KasToken(
        access_token="atk",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        provider="BuilderId",
        identity="builder_id",
        refresh_token=None,
    )
    store.save(lapsed)
    # Stored, but nothing can renew it: signed out for every reader that asks.
    assert resolve_signed_in(store) is None
    store.save(_token("builder_id", "BuilderId"))
    found = resolve_signed_in(store)
    assert found is not None and found.identity == "builder_id"


def test_resolve_signed_in_answers_none_for_an_unreadable_vault():
    class _Broken:
        def resolve(self):
            raise OSError("vault unreadable")

    assert resolve_signed_in(_Broken()) is None  # type: ignore[arg-type]


def test_agent_owns_sign_in_is_the_one_predicate():
    from kiro_crew.auth.whoami import agent_owns_sign_in

    class _A:
        def __init__(self, b, k):
            self.acp_backend, self.kas_bundle = b, k

    assert agent_owns_sign_in(_A("kas", True)) is True
    assert agent_owns_sign_in(_A("kas", False)) is False
    assert agent_owns_sign_in(_A("kiro", True)) is False
    assert agent_owns_sign_in(object()) is False


def test_a_degraded_config_load_is_refused_for_the_owner_decision(monkeypatch):
    from kiro_crew.auth.whoami import ConfigUnreadable, load_sign_in_owner_config
    from kiro_crew.config.loader import KiroCrewConfig

    class _Cfg:
        agent = object()
        _base_unreadable = True
        _overlay_unreadable = False

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _Cfg()))
    with pytest.raises(ConfigUnreadable, match="config.json"):
        load_sign_in_owner_config()
    # The in-process reader keeps its kiro-cli fallback over the same refusal.
    assert vault_owns_sign_in() is False


def test_vault_owns_sign_in_is_positive_on_both_halves(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig

    class _Agent:
        def __init__(self, backend, bundle):
            self.acp_backend = backend
            self.kas_bundle = bundle

    class _Cfg:
        def __init__(self, backend, bundle):
            self.agent = _Agent(backend, bundle)

    cases = {
        ("kas", True): True,
        ("kas", False): False,
        ("kiro", True): False,
        ("claude", True): False,
    }
    for (backend, bundle), expected in cases.items():
        monkeypatch.setattr(
            KiroCrewConfig, "load", classmethod(lambda cls, b=backend, k=bundle: _Cfg(b, k))
        )
        assert vault_owns_sign_in() is expected, (backend, bundle)

    def _boom(cls):
        raise OSError("config unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    assert vault_owns_sign_in() is False


# ---------------------------------------------------------------------------
# The service records the start URL an OIDC sign-in was begun against.
# ---------------------------------------------------------------------------


def _oidc_auth() -> builder_id.DeviceAuthorization:
    return builder_id.DeviceAuthorization(
        device_code="oidc-dc-1",
        user_code="WXYZ-1234",
        verification_uri="https://device.sso.us-east-1.amazonaws.com/",
        verification_uri_complete="https://device.sso.us-east-1.amazonaws.com/?user_code=WXYZ-1234",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        interval_secs=0.0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider, kwargs, expected_url",
    [
        ("builder_id", {}, "https://view.awsapps.com/start"),
        (
            "idc",
            {"start_url": "https://acme.awsapps.com/start", "region": "eu-west-1"},
            "https://acme.awsapps.com/start",
        ),
    ],
)
async def test_an_oidc_sign_in_records_its_start_url_on_the_stored_token(
    tmp_path, monkeypatch, provider, kwargs, expected_url
):
    from kiro_crew.auth.service import KasLoginService

    async def _fake_register(region, *, session):
        return builder_id.RegisteredClient(client_id="cid-1", client_secret="csec-1")

    async def _fake_start(client, *, region, start_url, session):
        return _oidc_auth()

    async def _fake_poll(client, auth, *, region, identity, provider, session):
        return KasToken(
            access_token="at-1",
            expires_at=_FUTURE,
            provider=provider,
            identity=identity,
            refresh_token="rt-1",
            region=region,
            client_id=client.client_id,
            client_secret=client.client_secret,
        )

    async def _fake_profiles(access_token, *, region, session):
        return [
            control_plane.KiroProfile(arn="arn:aws:kiro:eu-west-1:1:profile/p1", profile_name="P1")
        ]

    monkeypatch.setattr(builder_id, "register_client", _fake_register)
    monkeypatch.setattr(builder_id, "start_device_authorization", _fake_start)
    monkeypatch.setattr(builder_id, "poll_token_once", _fake_poll)
    monkeypatch.setattr(control_plane, "list_available_profiles", _fake_profiles)

    store = TokenStore(tmp_path)
    service = KasLoginService(store)
    try:
        begun = await service.begin_device(provider, **kwargs)
        result = await service.poll_device(begun["login_id"])
    finally:
        await service.close()
    assert result["status"] == "authorized"
    saved = store.resolve()
    assert saved is not None
    assert saved.extra[START_URL_EXTRA_KEY] == expected_url
    doc = whoami_document(saved)
    assert doc["startUrl"] == expected_url
