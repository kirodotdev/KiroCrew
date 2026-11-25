"""A KAS sign-in vault whose key is gone recovers on the next sign-in.

``kas/.vault/secrets.enc`` present with ``.vault_key`` missing (or not a 32-byte key,
or an envelope that is not JSON) can never be opened again, and the vault refuses to
mint a new key over an existing store. Every sign-in then failed to save, so the card
kept saying it could not check the stored sign-in. A sign-in now moves the dead file
aside under a timestamped name and starts a new vault; it never deletes it.
"""

from __future__ import annotations

import errno
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew.auth.store import KasToken, TokenStore, TokenStoreError


def _token(identity: str = "builder_id", access_token: str = "at-new") -> KasToken:
    return KasToken(
        access_token=access_token,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        provider="BuilderId",
        identity=identity,
        refresh_token="rt-value",
    )


def _vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "kas" / ".vault"


def _set_aside(vault: Path, stem: str) -> list[Path]:
    return sorted(vault.glob(f"{stem}.corrupt-*"))


def _sign_in(store: TokenStore, token: KasToken) -> None:
    try:
        store.save(token)
    except TokenStoreError as err:
        pytest.fail(f"a new sign-in must replace a vault no key opens: {err}")


def _signed_in_store(tmp_path: Path) -> tuple[TokenStore, bytes]:
    store = TokenStore(tmp_path)
    store.save(_token(access_token="at-old"))
    return store, (_vault_dir(tmp_path) / "secrets.enc").read_bytes()


def test_sign_in_recovers_a_vault_whose_key_is_missing(tmp_path: Path):
    store, old = _signed_in_store(tmp_path)
    vault = _vault_dir(tmp_path)
    (vault / ".vault_key").unlink()
    assert store.resolve() is None  # the state the card reports as "could not check"

    _sign_in(store, _token())

    assert store.load("builder_id").access_token == "at-new"
    backups = _set_aside(vault, "secrets.enc")
    assert [b.read_bytes() for b in backups] == [old]  # kept byte for byte, never deleted
    assert (vault / ".vault_key").stat().st_size == 32


def test_sign_in_recovers_a_vault_whose_key_has_the_wrong_length(tmp_path: Path):
    store, old = _signed_in_store(tmp_path)
    vault = _vault_dir(tmp_path)
    (vault / ".vault_key").write_bytes(b"short")

    _sign_in(store, _token())

    assert store.load("builder_id").access_token == "at-new"
    assert [b.read_bytes() for b in _set_aside(vault, "secrets.enc")] == [old]
    assert [b.read_bytes() for b in _set_aside(vault, ".vault_key")] == [b"short"]


def test_sign_in_recovers_a_vault_whose_envelope_is_not_json(tmp_path: Path):
    store, _ = _signed_in_store(tmp_path)
    vault = _vault_dir(tmp_path)
    key = (vault / ".vault_key").read_bytes()
    (vault / "secrets.enc").write_text("{not json", encoding="utf-8")

    _sign_in(store, _token())

    assert store.load("builder_id").access_token == "at-new"
    assert [b.read_text() for b in _set_aside(vault, "secrets.enc")] == ["{not json"]
    assert (vault / ".vault_key").read_bytes() == key  # a good key is kept in place


def test_two_recoveries_keep_both_old_files(tmp_path: Path):
    store, _ = _signed_in_store(tmp_path)
    vault = _vault_dir(tmp_path)
    for _ in range(2):
        (vault / ".vault_key").unlink()
        _sign_in(store, _token())
    assert len(_set_aside(vault, "secrets.enc")) == 2


def test_a_readable_vault_is_never_set_aside(tmp_path: Path):
    store, _ = _signed_in_store(tmp_path)
    vault = _vault_dir(tmp_path)
    key = (vault / ".vault_key").read_bytes()

    store.save(_token(identity="external_idp", access_token="at-ext"))

    assert store.load("builder_id").access_token == "at-old"
    assert store.load("external_idp").access_token == "at-ext"
    assert list(vault.glob("*.corrupt-*")) == []
    assert (vault / ".vault_key").read_bytes() == key


def test_a_key_that_cannot_be_read_is_not_a_lost_key(tmp_path: Path, monkeypatch):
    """A key file that is there but locked (antivirus, macOS privacy) is left alone."""
    store, old = _signed_in_store(tmp_path)
    vault = _vault_dir(tmp_path)
    real = Path.read_bytes

    def read_bytes(self):
        if self.name == ".vault_key":
            raise PermissionError(errno.EPERM, "Operation not permitted", str(self))
        return real(self)

    with monkeypatch.context() as m:
        m.setattr(Path, "read_bytes", read_bytes)
        with pytest.raises(TokenStoreError):
            store.save(_token())
    assert (vault / "secrets.enc").read_bytes() == old
    assert list(vault.glob("*.corrupt-*")) == []


def test_a_key_rename_that_fails_leaves_a_recoverable_vault(tmp_path: Path, monkeypatch):
    """The bad key moves first, so a failed set-aside leaves both files in place."""
    import kiro_crew.auth.store as store_mod

    store, old = _signed_in_store(tmp_path)
    vault = _vault_dir(tmp_path)
    (vault / ".vault_key").write_bytes(b"short")
    real = store_mod.os.replace

    def replace(src, dst):
        if Path(src).name == ".vault_key":
            raise OSError(errno.EBUSY, "held open", str(src))
        return real(src, dst)

    with monkeypatch.context() as m:
        m.setattr(store_mod.os, "replace", replace)
        with pytest.raises(TokenStoreError):
            store.save(_token())
    assert (vault / "secrets.enc").exists(), "the store must not move before its bad key"
    assert (vault / "secrets.enc").read_bytes() == old
    assert list(vault.glob("*.corrupt-*")) == []  # nothing moved, no placeholder left

    _sign_in(store, _token())

    assert store.load("builder_id").access_token == "at-new"
    assert [b.read_bytes() for b in _set_aside(vault, "secrets.enc")] == [old]
    assert [b.read_bytes() for b in _set_aside(vault, ".vault_key")] == [b"short"]
