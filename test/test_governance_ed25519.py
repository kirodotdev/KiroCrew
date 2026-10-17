"""Ed25519 verification of security-policy signatures (``trust_public_keys``).

The trust root holds only the PUBLIC half per issuer, so reading it does not let
anyone mint a ceiling that verifies.  An issuer with a public key is verified with
it alone and never falls back to its symmetric ``trust_keys`` secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kiro_crew.platform import governance
from kiro_crew.platform.admission import AdmissionPolicy, ed25519_verify
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import (
    SIGNATURE_UNVERIFIED,
    SIGNATURE_VERIFIED,
    assert_policy_signature_satisfied,
    load_security_policy,
    policy_signing_payload,
)

ISSUER = "fleet-control"


def _keypair():
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, base64.b64encode(raw).decode("ascii")


def _doc() -> dict:
    return {"version": 1, "boot": {"fail_closed": True}, "identity": {"issuer": ISSUER}}


def _ed25519_signed(private, doc: dict) -> dict:
    signed = json.loads(json.dumps(doc))
    sig = private.sign(policy_signing_payload(signed))
    signed["identity"]["signature"] = base64.b64encode(sig).decode("ascii")
    return signed


def _hmac_signed(secret: str, doc: dict) -> dict:
    signed = json.loads(json.dumps(doc))
    signed["identity"]["signature"] = hmac.new(
        secret.encode("utf-8"), policy_signing_payload(signed), hashlib.sha256
    ).hexdigest()
    return signed


class TestEd25519Verify:
    def test_valid_signature_verifies(self):
        private, public = _keypair()
        sig = base64.b64encode(private.sign(b"payload")).decode("ascii")
        assert ed25519_verify(public, b"payload", sig) is True

    def test_unpadded_and_wrapped_base64_still_verify(self):
        private, public = _keypair()
        sig = base64.b64encode(private.sign(b"payload")).decode("ascii")
        assert ed25519_verify(public.rstrip("="), b"payload", sig.rstrip("=")) is True
        assert ed25519_verify(public[:20] + "\n" + public[20:], b"payload", sig) is True

    def test_tampered_payload_and_wrong_key_are_refused(self):
        private, public = _keypair()
        _other, other_public = _keypair()
        sig = base64.b64encode(private.sign(b"payload")).decode("ascii")
        assert ed25519_verify(public, b"payload!", sig) is False
        assert ed25519_verify(other_public, b"payload", sig) is False

    @pytest.mark.parametrize(
        "key, sig",
        [
            ("", "AAAA"),
            ("not base64 !!", "AAAA"),
            (base64.b64encode(b"\x00" * 31).decode(), base64.b64encode(b"\x00" * 64).decode()),
            (base64.b64encode(b"\x00" * 32).decode(), base64.b64encode(b"\x00" * 63).decode()),
            ("00" * 32, "00" * 64),  # hex is not accepted: hex digits decode to the wrong length
            ("\udc80", "\udc80"),
            (None, None),
        ],
    )
    def test_malformed_material_is_false_never_an_exception(self, key, sig):
        assert ed25519_verify(key, b"payload", sig) is False


class TestPolicySignatureState:
    def test_issuer_with_public_key_verifies_an_ed25519_document(self):
        private, public = _keypair()
        doc = _ed25519_signed(private, _doc())
        state, detail = governance._policy_signature_state(doc, {}, {ISSUER: public})
        assert state == SIGNATURE_VERIFIED
        assert "ed25519" in detail

    def test_issuer_with_public_key_never_falls_back_to_its_hmac_secret(self):
        # Anyone who can read ``trust_keys`` can mint this document; once the issuer
        # has a public key it must stop verifying.
        _private, public = _keypair()
        doc = _hmac_signed("shared-secret", _doc())
        state, _detail = governance._policy_signature_state(
            doc, {ISSUER: "shared-secret"}, {ISSUER: public}
        )
        assert state == SIGNATURE_UNVERIFIED

    @pytest.mark.parametrize("placeholder", ["", "not-base64!!"])
    def test_unusable_public_key_still_blocks_the_hmac_fallback(self, placeholder):
        doc = _hmac_signed("shared-secret", _doc())
        state, _detail = governance._policy_signature_state(
            doc, {ISSUER: "shared-secret"}, {ISSUER: placeholder}
        )
        assert state == SIGNATURE_UNVERIFIED

    def test_issuer_without_public_key_keeps_hmac(self):
        _private, public = _keypair()
        doc = _hmac_signed("shared-secret", _doc())
        state, _detail = governance._policy_signature_state(
            doc, {ISSUER: "shared-secret"}, {"another-issuer": public}
        )
        assert state == SIGNATURE_VERIFIED

    def test_edited_document_no_longer_verifies(self):
        private, public = _keypair()
        doc = _ed25519_signed(private, _doc())
        doc["boot"]["fail_closed"] = False
        state, _detail = governance._policy_signature_state(doc, {}, {ISSUER: public})
        assert state == SIGNATURE_UNVERIFIED


class TestTrustRoot:
    def test_from_dict_keeps_every_named_issuer_even_with_an_unusable_key(self):
        # Dropping a placeholder would re-enable that issuer's HMAC fallback.
        policy = AdmissionPolicy.from_dict(
            {"trust_public_keys": {"a": "KEY", "b": None, "c": "", "d": 12}}
        )
        assert policy.trust_public_keys == {"a": "KEY", "b": "", "c": "", "d": ""}
        assert AdmissionPolicy.from_dict({}).trust_public_keys == {}
        assert AdmissionPolicy.from_dict({"trust_public_keys": ["x"]}).trust_public_keys == {}


class TestLoaderEndToEnd:
    """Real admission file + real policy file through load and the boot gate."""

    def _write(self, monkeypatch, tmp_path, *, doc: dict, admission: dict) -> None:
        policy = tmp_path / "security_policy.json"
        policy.write_text(json.dumps(doc))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(policy))
        adm = tmp_path / "admission_policy.json"
        adm.write_text(json.dumps(admission))
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))

    def test_required_signature_is_satisfied_by_ed25519(self, monkeypatch, tmp_path):
        private, public = _keypair()
        self._write(
            monkeypatch,
            tmp_path,
            doc=_ed25519_signed(private, _doc()),
            admission={"require_policy_signature": True, "trust_public_keys": {ISSUER: public}},
        )
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_VERIFIED
        assert_policy_signature_satisfied(ceiling)

    def test_hmac_document_fails_closed_once_the_issuer_has_a_public_key(
        self, monkeypatch, tmp_path
    ):
        _private, public = _keypair()
        self._write(
            monkeypatch,
            tmp_path,
            doc=_hmac_signed("shared-secret", _doc()),
            admission={
                "require_policy_signature": True,
                "trust_keys": {ISSUER: "shared-secret"},
                "trust_public_keys": {ISSUER: public},
            },
        )
        ceiling = load_security_policy()
        with pytest.raises(PlatformCompositionError):
            assert_policy_signature_satisfied(ceiling)

    @pytest.mark.parametrize("placeholder", [None, ""])
    def test_placeholder_public_key_fails_closed_with_hmac_secret_retained(
        self, monkeypatch, tmp_path, placeholder
    ):
        self._write(
            monkeypatch,
            tmp_path,
            doc=_hmac_signed("shared-secret", _doc()),
            admission={
                "require_policy_signature": True,
                "trust_keys": {ISSUER: "shared-secret"},
                "trust_public_keys": {ISSUER: placeholder},
            },
        )
        ceiling = load_security_policy()
        with pytest.raises(PlatformCompositionError):
            assert_policy_signature_satisfied(ceiling)
