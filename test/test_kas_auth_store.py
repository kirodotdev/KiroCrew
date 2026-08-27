"""Tests for the vault-backed KAS token store."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew.auth.store import (
    REFRESH_MARGIN_SECS,
    KasToken,
    TokenStore,
    TokenStoreError,
)


def _token(identity: str = "social", *, expires_in: int = 3600, **overrides) -> KasToken:
    kwargs = dict(
        access_token="at-value",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        provider="Google",
        identity=identity,
        refresh_token="rt-value",
        profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/x",
    )
    kwargs.update(overrides)
    return KasToken(**kwargs)


# ── KasToken model ──


def test_token_json_roundtrip_preserves_fields():
    tok = _token(region="us-east-1", auth_method="social")
    back = KasToken.from_json(tok.to_json())
    assert back == tok


def test_from_json_naive_datetime_treated_as_utc():
    tok = _token()
    d = json.loads(tok.to_json())
    d["expires_at"] = "2030-01-01T00:00:00"  # naive
    back = KasToken.from_json(json.dumps(d))
    assert back.expires_at.tzinfo is not None
    assert back.expires_at.utcoffset().total_seconds() == 0


def test_from_json_drops_unknown_keys():
    d = json.loads(_token().to_json())
    d["future_field"] = "ignored"
    back = KasToken.from_json(json.dumps(d))
    assert not hasattr(back, "future_field")


def test_is_expired_respects_refresh_margin():
    live = _token(expires_in=REFRESH_MARGIN_SECS + 60)
    inside_margin = _token(expires_in=REFRESH_MARGIN_SECS - 60)
    assert not live.is_expired()
    assert inside_margin.is_expired()


# ── TokenStore on the vault ──


def test_save_load_roundtrip(tmp_path: Path):
    store = TokenStore(tmp_path)
    tok = _token()
    store.save(tok)
    assert store.load("social") == tok


def test_load_missing_returns_none(tmp_path: Path):
    assert TokenStore(tmp_path).load("social") is None


def test_unknown_identity_raises_value_error(tmp_path: Path):
    store = TokenStore(tmp_path)
    with pytest.raises(ValueError):
        store.load("nope")
    with pytest.raises(ValueError):
        store.delete("nope")
    with pytest.raises(ValueError):
        store.save(_token(identity="nope"))


def test_token_is_encrypted_at_rest(tmp_path: Path):
    """The plaintext access token must not appear anywhere under the store dir."""
    store = TokenStore(tmp_path)
    store.save(_token(access_token="hunter2-super-secret"))
    hits = []
    for p in (tmp_path / "kas").rglob("*"):
        if p.is_file() and b"hunter2-super-secret" in p.read_bytes():
            hits.append(p)
    assert hits == []


def test_delete_removes_token(tmp_path: Path):
    store = TokenStore(tmp_path)
    store.save(_token())
    store.delete("social")
    assert store.load("social") is None


def test_delete_missing_is_noop(tmp_path: Path):
    TokenStore(tmp_path).delete("social")  # no store yet — must not raise


def test_delete_propagates_store_failure(tmp_path: Path, monkeypatch):
    """Logout must not report success when the vault cannot be written."""
    store = TokenStore(tmp_path)
    store.save(_token())
    monkeypatch.setattr(
        store._vault, "delete_sync", lambda name: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(TokenStoreError):
        store.delete("social")


def test_save_wraps_vault_failure(tmp_path: Path, monkeypatch):
    store = TokenStore(tmp_path)
    monkeypatch.setattr(
        store._vault, "set_sync", lambda n, v: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(TokenStoreError):
        store.save(_token())


def test_load_corrupt_entry_returns_none(tmp_path: Path):
    """A tampered/undecryptable entry is treated as absent, not a crash."""
    store = TokenStore(tmp_path)
    store.save(_token())
    enc = tmp_path / "kas" / ".vault" / "secrets.enc"
    envelope = json.loads(enc.read_text())
    envelope["entries"]["social"]["ct"] = "00" * 16  # garbage ciphertext
    enc.write_text(json.dumps(envelope))
    assert store.load("social") is None


def test_load_malformed_envelope_returns_none(tmp_path: Path):
    store = TokenStore(tmp_path)
    store.save(_token())
    (tmp_path / "kas" / ".vault" / "secrets.enc").write_text("{not json")
    assert store.load("social") is None


def test_load_non_object_envelope_returns_none(tmp_path: Path):
    # Valid JSON that is NOT an object (.get on a list raises AttributeError)
    # must be treated as a corrupt store, not crash status with a 500.
    store = TokenStore(tmp_path)
    store.save(_token())
    (tmp_path / "kas" / ".vault" / "secrets.enc").write_text("[1, 2, 3]")
    assert store.load("social") is None
    with pytest.raises(TokenStoreError):
        store.save(_token())
    with pytest.raises(TokenStoreError):
        store.delete("social")


def test_load_malformed_token_json_returns_none(tmp_path: Path):
    """A vault entry that decrypts to non-token JSON is dropped."""
    store = TokenStore(tmp_path)
    store._vault.set_sync("social", '{"expires_at": null}')
    assert store.load("social") is None


def test_load_social_without_profile_arn_returns_none(tmp_path: Path):
    store = TokenStore(tmp_path)
    store.save(_token(profile_arn=None))
    assert store.load("social") is None


def test_resolve_priority_external_wins(tmp_path: Path):
    store = TokenStore(tmp_path)
    store.save(_token(identity="social"))
    store.save(_token(identity="builder_id", provider="BuilderId", profile_arn=None))
    store.save(_token(identity="external_idp", provider="ExternalIdp"))
    resolved = store.resolve()
    assert resolved is not None
    assert resolved.identity == "external_idp"


def test_resolve_empty_returns_none(tmp_path: Path):
    assert TokenStore(tmp_path).resolve() is None


def test_lock_path_is_owner_only_dir(tmp_path: Path):
    store = TokenStore(tmp_path)
    lock = store.lock_path("social")
    assert lock.parent == tmp_path / "kas"
    assert lock.parent.is_dir()
    with pytest.raises(ValueError):
        store.lock_path("nope")


def test_linked_kas_dir_is_refused(tmp_path: Path):
    # A pre-planted symlink at <data_home>/kas would redirect the vault's key
    # file and ciphertext into an attacker-readable target; every store entry
    # point must refuse to operate through it.
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "kas").symlink_to(target, target_is_directory=True)
    store = TokenStore(tmp_path)
    with pytest.raises(TokenStoreError):
        store.save(_token())
    with pytest.raises(TokenStoreError):
        store.load("social")
    with pytest.raises(TokenStoreError):
        store.delete("social")
    with pytest.raises(TokenStoreError):
        store.lock_path("social")


def test_linked_vault_subdir_is_refused(tmp_path: Path):
    # Same class one level down: a link planted at kas/.vault itself.
    kas = tmp_path / "kas"
    kas.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (kas / ".vault").symlink_to(target, target_is_directory=True)
    store = TokenStore(tmp_path)
    with pytest.raises(TokenStoreError):
        store.save(_token())
