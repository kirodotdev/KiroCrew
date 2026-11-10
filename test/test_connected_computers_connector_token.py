"""Connected computers: the connector credential's proofs and every refusal it names.

Complements ``test_connected_computers_identity.py`` (mint, verify, revocation):
here the peer key, the gateway's hello proof, the client's proof of possession,
the unverified generation hint, the SSH renewal check and each claim check on a
credential signed with this gateway's own key but carrying a wrong claim.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from kiro_crew.connected_computers import connector_token
from kiro_crew.connected_computers.connector_token import (
    AUDIENCE,
    MAX_CREDENTIAL_CHARS,
    CredentialRejected,
    connector_auth,
    credential_generation,
    gateway_proof,
    mint,
    peer_key_from_secret,
    proof_matches,
    verify,
    verify_auth,
    verify_for_renewal,
    verify_standing,
)
from kiro_crew.connected_computers.devices import STATUS_REVOKED, DeviceRegistry
from kiro_crew.connected_computers.identity import install_fingerprint
from kiro_crew.dashboard import token_auth_tokens as tokens
from kiro_crew.secrets.vault import SecretVault

GATEWAY = "gw-test-0001"
BOUND = (GATEWAY, "laptop", "b" * 64, "c" * 64)
#: A session expiry no test clock reaches.
FAR_FUTURE = float(1 << 40)


def _fp(seed: int) -> str:
    return install_fingerprint(bytes([seed]) * 32)


@pytest.fixture(autouse=True)
def _isolated_token_secret(tmp_path, _floor_monkeypatch):
    # The credential is signed with the gateway's token secret under the data home.
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))


@pytest.fixture
def registry(tmp_path):
    return DeviceRegistry(SecretVault(tmp_path / "vault-home"))


@pytest.fixture
def rec(registry):
    return registry.enroll(base="laptop", fingerprint=_fp(1), label="L", owner="me")


def _signed_raw(raw: bytes) -> str:
    return f"{tokens._b64url_encode(raw)}.{tokens._sign(raw)}"


def _claims(record: Any, **changes: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "sub": record.owner,
        "aud": AUDIENCE,
        "gw": GATEWAY,
        "device": record.device_id,
        "fp": record.fingerprint,
        "cgen": str(record.cgen),
        "session_exp": FAR_FUTURE,
    }
    claims.update(changes)
    return {k: v for k, v in claims.items() if v is not None}


def _signed(record: Any, **changes: Any) -> str:
    return _signed_raw(json.dumps(_claims(record, **changes)).encode())


def _code(call: Any) -> str:
    with pytest.raises(CredentialRejected) as exc:
        call()
    return exc.value.code


class TestMint:
    def test_an_inactive_record_gets_no_credential(self, rec):
        revoked = replace(rec, status=STATUS_REVOKED)
        assert _code(lambda: mint(revoked, GATEWAY)) == "device_inactive"

    def test_a_gateway_without_an_id_mints_nothing(self, rec):
        assert _code(lambda: mint(rec, "")) == "gateway_unknown"

    def test_the_rejection_message_defaults_to_the_code(self):
        assert str(CredentialRejected("malformed")) == "connector credential refused: malformed"
        assert str(CredentialRejected("x", "said")) == "said"


class TestPeerKeyAndHelloProof:
    def test_the_peer_key_is_stable_and_bound_to_the_enrollment(self, rec):
        key = peer_key_from_secret(rec, GATEWAY)
        assert len(key) == 64 and int(key, 16) >= 0
        assert peer_key_from_secret(rec, GATEWAY) == key
        assert peer_key_from_secret(replace(rec, cgen=rec.cgen + 1), GATEWAY) != key
        assert peer_key_from_secret(rec, "gw-other") != key

    def test_the_hello_proof_checks_only_under_its_key_and_nonces(self, rec):
        key = peer_key_from_secret(rec, GATEWAY)
        proof = gateway_proof(key, *BOUND)
        assert proof_matches(key, BOUND, proof) is True
        assert proof_matches(key, (*BOUND[:3], "d" * 64), proof) is False
        other = peer_key_from_secret(replace(rec, cgen=rec.cgen + 1), GATEWAY)
        assert proof_matches(other, BOUND, proof) is False

    def test_a_malformed_key_never_matches(self):
        assert proof_matches("not hex", BOUND, "0" * 64) is False


class TestProofOfPossession:
    def test_a_minted_credential_proves_its_device(self, registry, rec):
        token, _ = mint(rec, GATEWAY)
        auth = connector_auth(token, BOUND)
        assert auth["op"] == "auth" and "." not in auth["payload"]
        assert token.partition(".")[2] not in json.dumps(auth), "the signature never travels"
        assert verify_auth(auth["payload"], auth["proof"], BOUND, registry=registry) == (
            rec,
            token,
        )

    @pytest.mark.parametrize(
        "token",
        ["no-separator", ".signature", "payload.", "p." + "s" * MAX_CREDENTIAL_CHARS],
    )
    def test_connector_auth_refuses_a_malformed_credential(self, token):
        assert _code(lambda: connector_auth(token, BOUND)) == "malformed"

    @pytest.mark.parametrize(
        "payload", ["", "a.b", "x" * (MAX_CREDENTIAL_CHARS + 1), "a", "caf\u00e9"]
    )
    def test_a_malformed_payload_is_refused(self, registry, payload):
        assert _code(lambda: verify_auth(payload, "0" * 64, BOUND, registry=registry)) == (
            "malformed"
        )

    def test_a_proof_for_another_socket_is_refused(self, registry, rec):
        token, _ = mint(rec, GATEWAY)
        auth = connector_auth(token, (*BOUND[:3], "d" * 64))
        call = lambda: verify_auth(  # noqa: E731
            auth["payload"], auth["proof"], BOUND, registry=registry
        )
        assert _code(call) == "invalid_proof"

    def test_a_proof_bound_to_another_device_is_refused(self, registry, rec):
        token, _ = mint(rec, GATEWAY)
        bound = (GATEWAY, "desktop", *BOUND[2:])
        auth = connector_auth(token, bound)
        call = lambda: verify_auth(  # noqa: E731
            auth["payload"], auth["proof"], bound, registry=registry
        )
        assert _code(call) == "wrong_device"

    def test_the_credential_is_verified_in_full_after_the_proof(self, registry, rec):
        token, _ = mint(rec, GATEWAY)
        registry.revoke(rec.device_id)
        auth = connector_auth(token, BOUND)
        call = lambda: verify_auth(  # noqa: E731
            auth["payload"], auth["proof"], BOUND, registry=registry
        )
        assert _code(call) == "revoked"


class TestCredentialGeneration:
    def test_a_minted_credential_names_its_generation(self, rec):
        token, _ = mint(rec, GATEWAY)
        assert credential_generation(token) == str(rec.cgen)

    @pytest.mark.parametrize(
        "raw",
        [b"[1]", b'{"cgen": 1}', b'{"cgen": "x"}', b'{"cgen": "1234567890123456789"}', b"{}"],
    )
    def test_anything_but_a_short_decimal_claim_is_empty(self, raw):
        assert credential_generation(_signed_raw(raw)) == ""

    @pytest.mark.parametrize("token", ["a", "not json at all", "\u00e9.x"])
    def test_an_undecodable_credential_is_empty(self, token):
        assert credential_generation(token) == ""


class TestClaims:
    @pytest.mark.parametrize("token", [7, "", "x" * (MAX_CREDENTIAL_CHARS + 1)])
    def test_a_non_credential_is_malformed(self, registry, token):
        assert _code(lambda: verify(token, gateway_id=GATEWAY, registry=registry)) == "malformed"

    def test_a_forged_signature_is_invalid(self, registry, rec):
        payload = tokens._b64url_encode(json.dumps(_claims(rec)).encode())
        forged = f"{payload}.{tokens._b64url_encode(b'not the signature')}"
        assert _code(lambda: verify(forged, gateway_id=GATEWAY, registry=registry)) == "invalid"

    @pytest.mark.parametrize("raw", [b"not json", b"[1]", b'"text"'])
    def test_a_signed_non_object_is_invalid(self, registry, raw):
        token = _signed_raw(raw)
        assert _code(lambda: verify(token, gateway_id=GATEWAY, registry=registry)) == "invalid"

    @pytest.mark.parametrize(
        "changes,code",
        [
            ({"kind": "session"}, "wrong_audience"),
            ({"app": "notes"}, "wrong_audience"),
            ({"aud": None}, "wrong_audience"),
            ({"aud": 1}, "wrong_audience"),
            ({"gw": "gw-other"}, "wrong_gateway"),
            ({"device": "ghost"}, "unknown_device"),
            ({"device": None}, "unknown_device"),
            ({"fp": _fp(2)}, "wrong_device"),
            ({"sub": "someone-else"}, "wrong_owner"),
            ({"sub": None}, "wrong_owner"),
            ({"cgen": "9"}, "superseded"),
            ({"session_exp": None}, "invalid"),
            ({"session_exp": True}, "invalid"),
            ({"session_exp": "soon"}, "invalid"),
            ({"session_exp": 1.0}, "expired"),
        ],
    )
    def test_each_claim_is_checked(self, registry, rec, changes, code):
        token = _signed(rec, **changes)
        assert _code(lambda: verify(token, gateway_id=GATEWAY, registry=registry)) == code

    def test_a_gateway_without_an_id_accepts_nothing(self, registry, rec):
        token = _signed(rec)
        assert _code(lambda: verify(token, gateway_id="", registry=registry)) == "wrong_gateway"

    def test_a_removed_record_refuses_by_its_status(self, registry, rec):
        token = _signed(rec)
        registry.remove(rec.device_id)
        assert _code(lambda: verify(token, gateway_id=GATEWAY, registry=registry)) == "removed"

    def test_standing_ignores_expiry(self, registry, rec):
        token = _signed(rec, session_exp=1.0)
        assert verify_standing(token, gateway_id=GATEWAY, registry=registry) == rec


class TestRenewal:
    def test_an_expired_credential_of_an_active_record_renews(self, registry, rec):
        token = _signed(rec, session_exp=1.0)
        renewed = verify_for_renewal(
            token, gateway_id=GATEWAY, registry=registry, fingerprint=rec.fingerprint, owner="me"
        )
        assert renewed == rec

    def test_another_computer_cannot_renew_it(self, registry, rec):
        token, _ = mint(rec, GATEWAY)
        call = lambda: verify_for_renewal(  # noqa: E731
            token, gateway_id=GATEWAY, registry=registry, fingerprint=_fp(2), owner="me"
        )
        assert _code(call) == "wrong_device"

    def test_another_ssh_user_cannot_renew_it(self, registry, rec):
        token, _ = mint(rec, GATEWAY)
        call = lambda: verify_for_renewal(  # noqa: E731
            token,
            gateway_id=GATEWAY,
            registry=registry,
            fingerprint=rec.fingerprint,
            owner="someone-else",
        )
        assert _code(call) == "wrong_owner"

    def test_a_revoked_record_never_renews(self, registry, rec):
        token, _ = mint(rec, GATEWAY)
        registry.revoke(rec.device_id)
        call = lambda: verify_for_renewal(  # noqa: E731
            token, gateway_id=GATEWAY, registry=registry, fingerprint=rec.fingerprint, owner="me"
        )
        assert _code(call) == "revoked"


def test_the_credential_lifetime_matches_the_token_cap():
    assert connector_token.CREDENTIAL_TTL_SECS == tokens.MAX_SESSION_TTL_SECS
