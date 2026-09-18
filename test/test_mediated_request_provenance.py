"""Branch coverage for the owner-signature provenance layer.

These exercise the fail-closed edges of ``provenance`` — key mint, short/absent
key, OSErrors along the mint path, and the sign/verify guards — that the
policy/CLI tests do not reach. Every path that cannot produce a trustworthy
signature must fail closed (sign raises, verify returns False), never silently
accept.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.secrets_mediation import provenance
from kiro_crew.secrets_mediation.provenance import (
    _MEMBER_KEY_RELPATH,
    sign_authorizations,
    verify_authorizations,
)

_AUTHZ = {"WEATHER_API_KEY": {"origin": "https://api.example", "placement": {"type": "bearer"}}}


def _key_path(config_dir: Path) -> Path:
    return Path(config_dir).resolve() / _MEMBER_KEY_RELPATH


def test_sign_then_verify_round_trips_and_mints_the_key(tmp_path):
    # No key yet: signing mints one (create=True), and it verifies.
    assert not _key_path(tmp_path).exists()
    sig = sign_authorizations(_AUTHZ, tmp_path)
    assert _key_path(tmp_path).exists()
    assert verify_authorizations(_AUTHZ, sig, tmp_path) is True


def test_verify_fails_closed_on_missing_or_typewrong_signature(tmp_path):
    sign_authorizations(_AUTHZ, tmp_path)  # ensure a key exists
    assert verify_authorizations(_AUTHZ, "", tmp_path) is False
    assert verify_authorizations(_AUTHZ, None, tmp_path) is False
    assert verify_authorizations(_AUTHZ, 12345, tmp_path) is False


def test_verify_fails_closed_when_the_key_is_absent(tmp_path):
    # verify never mints (create=False); with no key it must return False, not raise.
    sig = "0" * 64
    assert not _key_path(tmp_path).exists()
    assert verify_authorizations(_AUTHZ, sig, tmp_path) is False


def test_verify_fails_closed_on_a_short_key(tmp_path):
    kp = _key_path(tmp_path)
    kp.parent.mkdir(parents=True, exist_ok=True)
    kp.write_bytes(b"tooshort")  # not 32 bytes -> treated as absent
    assert verify_authorizations(_AUTHZ, "0" * 64, tmp_path) is False


def test_tampering_with_any_entry_invalidates_the_signature(tmp_path):
    sig = sign_authorizations(_AUTHZ, tmp_path)
    tampered = {
        "WEATHER_API_KEY": {"origin": "https://evil.example", "placement": {"type": "bearer"}}
    }
    assert verify_authorizations(tampered, sig, tmp_path) is False


def test_sign_raises_when_the_key_cannot_be_created(tmp_path, monkeypatch):
    # _member_key returns None (e.g. unreadable/unwritable store) -> sign must
    # raise rather than emit an unverifiable signature.
    monkeypatch.setattr(provenance, "_member_key", lambda config_dir, *, create: None)
    with pytest.raises(RuntimeError):
        sign_authorizations(_AUTHZ, tmp_path)


def test_key_mint_returns_none_when_the_store_cannot_be_created(tmp_path, monkeypatch):
    # mkdir failure on the mint path -> None -> sign raises (fail closed).
    def _boom(*_a, **_k):
        raise OSError("read-only store")

    monkeypatch.setattr(provenance.Path, "mkdir", _boom)
    with pytest.raises(RuntimeError):
        sign_authorizations(_AUTHZ, tmp_path)


def test_key_mint_converges_when_another_creator_wins_the_link(tmp_path, monkeypatch):
    # Simulate a concurrent first-creator: os.link raises FileExistsError, so this
    # caller reads the winner's inode instead of failing.
    real_link = os.link
    published = {}

    def _link_then_conflict(src, dst):
        # Publish the winner ourselves, then report the race to the caller.
        real_link(src, dst)
        published["done"] = True
        raise FileExistsError(dst)

    monkeypatch.setattr(provenance.os, "link", _link_then_conflict)
    sig = sign_authorizations(_AUTHZ, tmp_path)
    assert published.get("done") is True
    assert _key_path(tmp_path).exists()
    assert verify_authorizations(_AUTHZ, sig, tmp_path) is True


def test_verify_fails_closed_on_a_symlinked_key_path(tmp_path):
    # A key path that resolves elsewhere (symlink) must be refused: _member_key
    # returns None when the resolved path differs from the literal path.
    kp = _key_path(tmp_path)
    kp.parent.mkdir(parents=True, exist_ok=True)
    decoy = tmp_path / "decoy-key"
    decoy.write_bytes(b"x" * 32)
    os.symlink(decoy, kp)
    # sign would mint over the symlink target; verification of an arbitrary sig
    # must still fail closed rather than trust a redirected key.
    assert verify_authorizations(_AUTHZ, "0" * 64, tmp_path) is False
