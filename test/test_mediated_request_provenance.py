"""Branch coverage for mediated-request policy provenance.

The policy MAC key is derived from the independently hidden dashboard token-signing
secret under its own purpose label, so no file holds it and an agent cannot compute
it.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from kiro_crew.secrets_mediation import provenance
from kiro_crew.secrets_mediation.provenance import (
    sign_secret_authorizations,
    verify_secret_authorizations,
)

_AUTHZ = {"WEATHER_API_KEY": {"origin": "https://api.example", "placement": {"type": "bearer"}}}


@pytest.fixture
def token_root(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """The token-signing secret the key derives from; tests swap ``root[0]``."""
    root = [b"T" * 32]
    monkeypatch.setattr(provenance, "_token_root", lambda: root[0])
    return root


def test_sign_then_verify_round_trips(token_root):
    sig = sign_secret_authorizations(_AUTHZ)
    assert verify_secret_authorizations(_AUTHZ, sig) is True


def test_derived_key_is_stable_and_not_the_raw_token_secret(token_root):
    k1 = provenance._secret_policy_key()
    k2 = provenance._secret_policy_key()
    assert k1 == k2 and k1 is not None
    assert k1 != token_root[0]


def test_no_key_file_is_written(token_root, tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    sign_secret_authorizations(_AUTHZ)
    assert list(tmp_path.iterdir()) == []


def test_verify_fails_closed_on_missing_or_typewrong_signature(token_root):
    assert verify_secret_authorizations(_AUTHZ, "") is False
    assert verify_secret_authorizations(_AUTHZ, None) is False
    assert verify_secret_authorizations(_AUTHZ, 12345) is False


@pytest.mark.parametrize("signature", ["é", "é" * 64, "0" * 63, "0" * 65, "A" * 64, " " + "0" * 63])
def test_a_non_hex_signature_is_refused_without_raising(token_root, signature):
    """``hmac.compare_digest`` raises ``TypeError`` on non-ASCII text, so a planted
    or hand-edited ``sig`` would otherwise crash the CLI before its ``--reset``
    recovery branch. Anything but the 64-char lowercase hex this module writes is
    simply not a valid signature."""
    assert verify_secret_authorizations(_AUTHZ, signature) is False


def test_verify_fails_closed_when_the_token_secret_cannot_be_read(monkeypatch):
    def _unreadable() -> bytes:
        raise OSError("unreadable")

    monkeypatch.setattr(provenance, "_token_root", _unreadable)
    assert verify_secret_authorizations(_AUTHZ, "0" * 64) is False
    with pytest.raises(RuntimeError, match="token-signing secret"):
        sign_secret_authorizations(_AUTHZ)


def test_rotating_the_token_secret_invalidates_existing_signatures(token_root):
    sig = sign_secret_authorizations(_AUTHZ)
    token_root[0] = b"U" * 32
    assert verify_secret_authorizations(_AUTHZ, sig) is False


def test_tampering_with_any_entry_invalidates_the_signature(token_root):
    sig = sign_secret_authorizations(_AUTHZ)
    tampered = {
        "WEATHER_API_KEY": {"origin": "https://evil.example", "placement": {"type": "bearer"}}
    }
    assert verify_secret_authorizations(tampered, sig) is False


@pytest.mark.parametrize(
    "label",
    [
        b"kirocrew.secret_request_policy.sig.v1",
        b"kirocrew.secret_request_policy.sig.v2",
        b"kirocrew.some_other_purpose.v1\x00",
    ],
)
def test_a_signature_under_another_purpose_label_is_rejected(token_root, label):
    """The purpose label keeps this key apart from every other key derived from the
    same token secret, including the earlier policy-key labels."""
    other = hmac.new(token_root[0], label, hashlib.sha256).digest()
    forged = hmac.new(other, provenance._canonical_payload(_AUTHZ), hashlib.sha256).hexdigest()
    assert verify_secret_authorizations(_AUTHZ, forged) is False


def test_a_signature_from_a_different_secret_is_rejected(token_root):
    sel_root = b"S" * 32
    subkey = hmac.new(sel_root, provenance._POLICY_KEY_DOMAIN, hashlib.sha256).digest()
    forged = hmac.new(subkey, provenance._canonical_payload(_AUTHZ), hashlib.sha256).hexdigest()
    assert verify_secret_authorizations(_AUTHZ, forged) is False


def test_policy_directory_is_hidden_and_precreated():
    from kiro_crew import sandbox
    from kiro_crew.security import paths as security_paths

    leaf = provenance.REQUEST_POLICY_DIRNAME
    assert leaf in security_paths._CREW_SECRET_LEAVES
    assert leaf in sandbox._CREW_HIDDEN_LEAVES
    assert leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    assert leaf not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES


def test_the_gateway_boot_does_no_mediation_key_work():
    """With a derived key there is nothing to mint, so neither entrypoint runs a
    mediation-specific boot step."""
    import inspect

    from kiro_crew.dashboard import server as server_mod
    from kiro_crew.dashboard.server_runtime import api_entrypoint

    for module in (server_mod, api_entrypoint):
        source = inspect.getsource(module)
        assert "mediation" not in source and "secrets_mediation" not in source


def _force_loader(monkeypatch: pytest.MonkeyPatch, *, ephemeral: bool) -> bytes:
    """Make the shared token-secret loader return a fresh key, flagged ephemeral or not."""
    from kiro_crew.dashboard import token_secret

    key = b"E" * 32 if ephemeral else b"P" * 32

    def _load(outcome=None):
        if ephemeral and outcome is not None:
            outcome.append("ephemeral")
        return key

    monkeypatch.setattr(token_secret, "_SECRET", None)
    monkeypatch.setattr(token_secret, "_EPHEMERAL_SECRET", None)
    monkeypatch.setattr(token_secret, "_load_or_create_secret", _load)
    return key


def test_an_ephemeral_signing_root_never_signs_a_durable_policy(monkeypatch):
    """The loader degrades an unreadable ``token_signing.key`` to a process-local
    random key. A policy signed with it would publish as success yet never verify
    in the gateway, so signing must refuse instead (and verification fail closed).
    Mutation guard: reading the root through ``_get_secret`` again signs here."""
    _force_loader(monkeypatch, ephemeral=True)
    with pytest.raises(RuntimeError, match="persisted"):
        sign_secret_authorizations(_AUTHZ)
    assert verify_secret_authorizations(_AUTHZ, "0" * 64) is False


def test_an_ephemeral_root_memoized_by_an_earlier_caller_still_refuses(monkeypatch):
    """The CLI verifies the existing policy before it signs, so the memo is usually
    filled by the time signing runs; the ephemeral verdict must survive that."""
    from kiro_crew.dashboard import token_secret

    _force_loader(monkeypatch, ephemeral=True)
    token_secret._get_secret()
    with pytest.raises(RuntimeError, match="persisted"):
        sign_secret_authorizations(_AUTHZ)


def test_a_persisted_signing_root_signs_through_the_real_loader(monkeypatch):
    key = _force_loader(monkeypatch, ephemeral=False)
    sig = sign_secret_authorizations(_AUTHZ)
    derived = hmac.new(key, provenance._POLICY_KEY_DOMAIN, hashlib.sha256).digest()
    assert (
        sig == hmac.new(derived, provenance._canonical_payload(_AUTHZ), hashlib.sha256).hexdigest()
    )
    assert verify_secret_authorizations(_AUTHZ, sig) is True
