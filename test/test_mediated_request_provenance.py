"""Branch coverage for the owner-signature provenance layer.

The policy signature is keyed by a subkey DERIVED from the SEL trust root
(``sel_hmac.key``) via a domain-separation label — not a stored key file, so a
pre-upgrade agent cannot preseed it. These exercise the fail-closed edges: sign
raises and verify returns False whenever the root (and thus the derived key) is
unavailable, and any tamper invalidates the signature.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.secrets_mediation import provenance
from kiro_crew.secrets_mediation.provenance import (
    sign_authorizations,
    verify_authorizations,
)

_AUTHZ = {"WEATHER_API_KEY": {"origin": "https://api.example", "placement": {"type": "bearer"}}}


def _install_sel_root(tmp_path: Path, monkeypatch, *, key: bytes = b"R" * 32) -> Path:
    """Point the SEL trust-root resolvers at a tmp key file. The provenance key
    is derived from these bytes, so every process that reads the same root
    derives the same signing subkey."""
    root = tmp_path / "sel_hmac.key"
    if key:
        root.write_bytes(key)
    monkeypatch.setattr("kiro_crew.sel.sel_hmac_key_path", lambda: root)
    monkeypatch.setattr("kiro_crew.sel._sel_hmac_key_bytes", lambda: key or None)
    return root


def test_sign_then_verify_round_trips_from_the_sel_root(tmp_path, monkeypatch):
    _install_sel_root(tmp_path, monkeypatch)
    sig = sign_authorizations(_AUTHZ, tmp_path)
    assert verify_authorizations(_AUTHZ, sig, tmp_path) is True


def test_derived_key_is_stable_and_not_the_raw_root(tmp_path, monkeypatch):
    root_bytes = b"R" * 32
    _install_sel_root(tmp_path, monkeypatch, key=root_bytes)
    k1 = provenance._member_key(tmp_path)
    k2 = provenance._member_key(tmp_path)
    assert k1 == k2 and k1 is not None
    # It is a DERIVED subkey, never the raw root itself.
    assert k1 != root_bytes


def test_verify_fails_closed_on_missing_or_typewrong_signature(tmp_path, monkeypatch):
    _install_sel_root(tmp_path, monkeypatch)
    sign_authorizations(_AUTHZ, tmp_path)
    assert verify_authorizations(_AUTHZ, "", tmp_path) is False
    assert verify_authorizations(_AUTHZ, None, tmp_path) is False
    assert verify_authorizations(_AUTHZ, 12345, tmp_path) is False


def test_verify_fails_closed_when_the_sel_root_is_absent(tmp_path, monkeypatch):
    # No root file, no cached bytes -> no derivable key -> verify returns False.
    missing = tmp_path / "sel_hmac.key"
    monkeypatch.setattr("kiro_crew.sel.sel_hmac_key_path", lambda: missing)
    monkeypatch.setattr("kiro_crew.sel._sel_hmac_key_bytes", lambda: None)
    assert verify_authorizations(_AUTHZ, "0" * 64, tmp_path) is False


def test_verify_fails_closed_on_a_short_sel_root(tmp_path, monkeypatch):
    _install_sel_root(tmp_path, monkeypatch, key=b"tooshort")
    assert verify_authorizations(_AUTHZ, "0" * 64, tmp_path) is False


def test_tampering_with_any_entry_invalidates_the_signature(tmp_path, monkeypatch):
    _install_sel_root(tmp_path, monkeypatch)
    sig = sign_authorizations(_AUTHZ, tmp_path)
    tampered = {
        "WEATHER_API_KEY": {"origin": "https://evil.example", "placement": {"type": "bearer"}}
    }
    assert verify_authorizations(tampered, sig, tmp_path) is False


def test_sign_raises_when_the_key_cannot_be_derived(tmp_path, monkeypatch):
    # _member_key returns None (no SEL root) -> sign must raise rather than emit
    # an unverifiable signature.
    monkeypatch.setattr(provenance, "_member_key", lambda config_dir, **_k: None)
    with pytest.raises(RuntimeError):
        sign_authorizations(_AUTHZ, tmp_path)


def test_falls_back_to_cached_sel_bytes_when_the_file_will_not_load(tmp_path, monkeypatch):
    # File unreadable, but the live SEL validated bytes are available: derivation
    # still succeeds from the cached copy (mirrors session_pid_sig).
    missing = tmp_path / "gone.key"
    monkeypatch.setattr("kiro_crew.sel.sel_hmac_key_path", lambda: missing)
    monkeypatch.setattr("kiro_crew.sel._sel_hmac_key_bytes", lambda: b"C" * 32)
    sig = sign_authorizations(_AUTHZ, tmp_path)
    assert verify_authorizations(_AUTHZ, sig, tmp_path) is True
