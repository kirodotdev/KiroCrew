"""The dashboard's signed-in identity from Crew's vault, in whoami's field vocabulary.

Covers ``kiro_crew.dashboard.handlers.kiro_vault_identity`` (the slot/provider ->
``account_type`` mapping, the one-read snapshot and the identity door's
fields / ``{}`` / ``None`` answer) and the auth bridge's
``vault_identity_claims`` it reads, driven over a real ``TokenStore``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kiro_crew.auth.bridge import vault_identity_claims, vault_identity_fingerprint
from kiro_crew.auth.store import KNOWN_IDENTITIES, KasToken, TokenStore
from kiro_crew.auth.whoami import START_URL_EXTRA_KEY, whoami_document
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


#: The provider classes the vault records beside each store slot. The social slot
#: is tried once per provider it can hold and once provider-less.
_PROVIDERS_BY_SLOT = {
    "builder_id": ("BuilderId",),
    "identity_center": ("Enterprise",),
    "external_idp": ("ExternalIdp",),
    "social": ("Google", "Github", ""),
}
#: Every store slot with every provider class above. Built from
#: ``KNOWN_IDENTITIES`` so a slot added to the store fails here until it is listed.
_EVERY_SLOT = tuple(
    (identity, provider)
    for identity in KNOWN_IDENTITIES
    for provider in _PROVIDERS_BY_SLOT[identity]
)


class TestOneMappingWithWhoami:
    """The dashboard and ``kirocrew kas whoami`` spell every slot from ONE table.

    The account modal's badge (``identity_fields``) and the remote sign-in's
    identity match (``whoami_document``) read the same vault; if their
    ``accountType`` spellings ever differed, ``identity_matches_target`` would
    refuse an organisation the dashboard labels correctly.
    """

    @pytest.mark.parametrize(("identity", "provider"), _EVERY_SLOT)
    def test_every_slot_renders_the_same_account_type(self, identity, provider):
        doc = whoami_document(_token(identity, provider=provider, profile_arn=None))
        fields = kvi.identity_fields({"identity": identity, "provider": provider})
        assert fields.get("account_type", "") == doc["accountType"]
        if identity == "social" and not provider:
            assert fields == {} and doc["accountType"] == ""
        else:
            assert fields["account_type"]

    def test_the_dashboard_spells_from_the_auth_table_not_a_copy(self):
        # Respell one slot in auth/whoami's table: the dashboard reading MUST follow,
        # which it cannot if it keeps a table of its own that happens to agree today.
        with patch.dict(
            "kiro_crew.auth.whoami._ACCOUNT_TYPE_BY_SLOT", {"identity_center": "Respelled"}
        ):
            doc = whoami_document(
                _token("identity_center", provider="Enterprise", profile_arn=None)
            )
            assert doc["accountType"] == "Respelled"
            assert kvi.account_type_from_claims("identity_center", "Enterprise") == "Respelled"
            assert kvi.identity_fields(
                {"identity": "identity_center", "provider": "Enterprise"}
            ) == {"account_type": "Respelled"}
        assert not hasattr(kvi, "_ACCOUNT_TYPE_BY_SLOT"), "the handler grew a second table back"


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


_PORTAL = "https://acme.awsapps.com/start"


def _idc_token(**extra) -> KasToken:
    return _token("identity_center", provider="Enterprise", extra=dict(extra))


class TestStartUrlParity:
    """The account modal shows the Identity Center portal ``kirocrew kas whoami`` prints.

    The sign-in records the start URL on the token (``START_URL_EXTRA_KEY``);
    the dashboard's vault reading must carry it as ``start_url`` -- the field
    the modal's provider badge reads for its portal host -- exactly as whoami's
    ``startUrl``, so the two readers of one sign-in cannot disagree.
    """

    def test_recorded_start_url_reaches_the_snapshot(self, data_home):
        TokenStore(data_home).save(_idc_token(**{START_URL_EXTRA_KEY: _PORTAL}))
        fingerprint, fields = kvi.vault_identity_snapshot_sync()
        assert fingerprint
        assert fields == {"account_type": "IamIdentityCenter", "start_url": _PORTAL}
        assert kvi.vault_identity_sync() == fields

    def test_snapshot_matches_the_whoami_document(self, data_home):
        token = _idc_token(**{START_URL_EXTRA_KEY: _PORTAL})
        TokenStore(data_home).save(token)
        doc = whoami_document(token)
        _, fields = kvi.vault_identity_snapshot_sync()
        assert fields == {"account_type": doc["accountType"], "start_url": doc["startUrl"]}

    def test_start_url_does_not_move_the_fingerprint(self, data_home):
        # A token minted before the URL was recorded is the same account.
        TokenStore(data_home).save(_idc_token())
        before = vault_identity_fingerprint()
        TokenStore(data_home).save(_idc_token(**{START_URL_EXTRA_KEY: _PORTAL}))
        assert vault_identity_fingerprint() == before

    @pytest.mark.parametrize("value", [None, "", 42])
    def test_no_recorded_start_url_publishes_no_key(self, data_home, value):
        extra = {} if value is None else {START_URL_EXTRA_KEY: value}
        TokenStore(data_home).save(_idc_token(**extra))
        _, fields = kvi.vault_identity_snapshot_sync()
        assert fields == {"account_type": "IamIdentityCenter"}

    def test_start_url_without_a_labelable_kind_is_nothing(self):
        assert kvi.identity_fields({"identity": "social", "start_url": _PORTAL}) == {}

    def test_start_url_is_bounded_like_parse_whoami_output(self):
        long_url = _PORTAL + "/" + "x" * 400
        fields = kvi.identity_fields(
            {"identity": "identity_center", "provider": "Enterprise", "start_url": long_url}
        )
        assert fields["start_url"] == long_url[:200]


class TestIdentityDoorAnswer:
    """``vault_identity_sync`` keeps the identity door's ``{}`` / ``None`` distinction.

    ``{}`` is a READ answer the Remote Crew launch form turns into the default
    Builder ID target, so only a vault proven empty may give it; a vault that did
    not read, or holds an entry that will not decrypt, is unknown (``None``).
    """

    def test_stored_identity_is_its_fields(self, data_home):
        TokenStore(data_home).save(_token())
        assert kvi.vault_identity_sync() == {"account_type": "SocialGoogle"}

    def test_proven_empty_vault_is_nothing_to_inherit(self, data_home):
        assert kvi.vault_identity_sync() == {}

    def test_undecryptable_entry_is_unknown_not_empty(self, data_home):
        TokenStore(data_home).save(_token())
        enc = data_home / "kas" / ".vault" / "secrets.enc"
        envelope = json.loads(enc.read_text())
        envelope["entries"]["social"]["ct"] = "00" * 16  # garbage ciphertext
        enc.write_text(json.dumps(envelope))
        # resolve() reads it as no token, but the entry map still names it.
        assert kvi.vault_identity_snapshot_sync() == ("", {})
        assert kvi.vault_identity_sync() is None

    def test_unreadable_vault_is_unknown_not_empty(self, data_home):
        TokenStore(data_home).save(_token())
        (data_home / "kas" / ".vault" / "secrets.enc").write_text("{not json")
        assert kvi.vault_identity_snapshot_sync() == ("", {})
        assert kvi.vault_identity_sync() is None

    def test_failed_emptiness_probe_is_unknown(self, data_home):
        with patch(
            "kiro_crew.auth.store.TokenStore.holds_no_identity", side_effect=RuntimeError("boom")
        ):
            assert kvi.vault_identity_sync() is None

    @pytest.mark.asyncio
    async def test_off_loop_answer_agrees(self, data_home):
        assert await kvi.vault_identity() == {}
        TokenStore(data_home).save(_token("builder_id", provider="BuilderId", profile_arn=None))
        assert await kvi.vault_identity() == {"account_type": "BuilderId"}
