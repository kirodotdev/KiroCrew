"""Encrypted vault for agent-inaccessible secret storage.

Storage layout:
    <config_dir>/.vault/secrets.enc   — JSON envelope with per-entry AES-256-GCM ciphertext
    <config_dir>/.vault/.vault_key     — 256-bit key file (mode 0600, O_CREAT|O_EXCL)

Agent isolation:
    The whole ``.vault`` directory is a keystone leaf in _CREW_SECRET_LEAVES
    (security.py), so the verb-independent sensitive-path backstop blocks every
    Kiro Crew-mediated read of these files — tool reads (is_sensitive_path) and
    shell commands (is_sensitive_bash_command), including a scripted
    ``python -c "open('~/.kiro/crew/.vault/...')"``. This is the same
    application-level trust model as ``.local_secret`` and SSH keys; direct
    OS-level UID isolation is out of scope.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import file_lock, restrict_to_owner


def _fsync_dir(path: Path) -> None:
    """Fsync a directory so a rename/create is durable across power loss.

    No-op where directory fds cannot be opened for fsync (e.g. Windows),
    where the atomic rename + file fsync already provide the guarantee.
    """
    try:
        dir_fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


class SecretValue:
    """Opaque wrapper that prevents accidental secret leakage in logs/repr."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the plaintext secret."""
        return self._value

    def __repr__(self) -> str:
        return "SecretValue(****)"

    def __str__(self) -> str:
        return "****"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretValue):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        raise TypeError("SecretValue is not hashable")


class SecretVault:
    """Encrypted per-entry vault backed by a local key file.

    Thread-safe for concurrent async callers via an asyncio.Lock around
    mutating operations. Agent isolation is enforced by the ``.vault``
    keystone leaf in security.py, not by any in-process guard.
    """

    _ENVELOPE_VERSION = 1
    _BACKEND = "file"
    _SCOPE = "kiro_crew"

    def __init__(self, config_dir: str | Path) -> None:
        self._config_dir = Path(config_dir) / ".vault"
        self._store_path = self._config_dir / "secrets.enc"
        self._key_path = self._config_dir / ".vault_key"
        self._lock = asyncio.Lock()

    # ── Public API ──

    def get(self, name: str) -> Optional[SecretValue]:
        """Retrieve a secret by name, or None if not stored."""
        entries = self._load_entries()
        if name not in entries:
            return None
        plaintext = self._decrypt_entry(name, entries[name])
        return SecretValue(plaintext.decode("utf-8"))

    async def set(self, name: str, value: str) -> None:
        """Store or overwrite a secret."""
        async with self._lock:
            await asyncio.to_thread(self._set_sync, name, value)

    def _set_sync(self, name: str, value: str) -> None:
        self._write_store(
            lambda entries: {**entries, name: self._encrypt_entry(name, value.encode("utf-8"))}
        )

    async def delete(self, name: str) -> None:
        """Remove a secret. No-op if it does not exist."""
        async with self._lock:
            await asyncio.to_thread(self._delete_sync, name)

    def _delete_sync(self, name: str) -> None:
        # Early return when no store exists — prevents creating an empty
        # store (and hitting the mixed-key guard on a fresh vault).
        if not self._store_path.exists():
            return
        self._write_store(lambda entries: {k: v for k, v in entries.items() if k != name})

    def list_names(self) -> list[str]:
        """Return all stored secret names."""
        return list(self._load_entries().keys())

    # ── Key management ──

    def _get_or_create_key(self) -> bytes:
        """Load (or create) the 256-bit vault key.

        The key file is protected from agent reads by the ``.vault`` keystone
        leaf. On POSIX, mode 0600 is set atomically at creation. On Windows,
        restrict_to_owner applies the SID-based dual-grant lockdown.

        Refuses to create a new key when secrets.enc already exists (prevents
        mixed-key vault from a restored backup without its key).
        """
        if self._key_path.exists():
            # Enforce restrictive permissions on every read (catches restored
            # backups or manual copies with wrong mode).
            restrict_to_owner(self._key_path)
            return self._key_path.read_bytes()

        # Refuse to create a new key if a store already exists — that would
        # make existing entries undecryptable.
        if self._store_path.exists():
            raise ValueError(
                f"Vault store exists at {self._store_path} but key is missing at "
                f"{self._key_path}. Cannot create a new key without losing existing "
                f"secrets. Restore the original .vault_key file."
            )

        self._config_dir.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        # Atomic exclusive create with restrictive permissions.
        # O_BINARY prevents Windows newline translation corrupting the key.
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(self._key_path), flags, 0o600)
        try:
            # Lock down ACL before writing key material — on Windows the
            # 0o600 mode is a no-op, so restrict_to_owner must run first
            # to prevent another local account from reading the key between
            # create and write.
            restrict_to_owner(self._key_path)
            written = os.write(fd, key)
            if written != len(key):
                raise OSError(f"Short write: {written}/{len(key)} bytes")
            # Durably persist the key before returning — a power loss after
            # set() returns must never leave the store undecryptable.
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            # Remove incomplete key file to avoid corrupted state.
            try:
                os.unlink(str(self._key_path))
            except OSError:
                pass
            raise
        os.close(fd)
        _fsync_dir(self._config_dir)
        return key

    # ── Crypto helpers ──

    def _aad_for(self, name: str) -> bytes:
        """Per-entry AAD = b'v1' + scope + NUL + name (prevents transplant)."""
        return b"v1" + self._SCOPE.encode() + b"\x00" + name.encode()

    def _encrypt_entry(self, name: str, plaintext: bytes) -> dict[str, str]:
        key = self._get_or_create_key()
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        ct = aesgcm.encrypt(nonce, plaintext, self._aad_for(name))
        return {"nonce": nonce.hex(), "ct": ct.hex()}

    def _decrypt_entry(self, name: str, entry: dict[str, str]) -> bytes:
        key = self._get_or_create_key()
        aesgcm = AESGCM(key)
        nonce = bytes.fromhex(entry["nonce"])
        ct = bytes.fromhex(entry["ct"])
        return aesgcm.decrypt(nonce, ct, self._aad_for(name))

    # ── Store I/O ──

    def _load_entries(self) -> dict[str, dict[str, str]]:
        """Load and decode the on-disk entry map (empty if no store yet)."""
        if not self._store_path.exists():
            return {}

        raw = self._store_path.read_text(encoding="utf-8")
        envelope = json.loads(raw)

        if envelope.get("backend") != self._BACKEND:
            raise ValueError(
                f"Vault backend mismatch: expected {self._BACKEND!r}, "
                f"got {envelope.get('backend')!r}"
            )

        return dict(envelope.get("entries", {}))

    @contextmanager
    def _cross_process_lock(self) -> Iterator[None]:
        """Acquire a cross-process lock via platform_compat.file_lock."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._store_path.with_name(".secrets.enc.lock")
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            with file_lock(lock_fd, exclusive=True):
                yield
        finally:
            os.close(lock_fd)

    def _write_store(self, mutate) -> None:
        """Atomically read-modify-write the store under cross-process lock.

        mutate: callable(entries_dict) -> new_entries_dict

        Uses platform_compat.file_lock (fails closed on all platforms) and
        atomic_write (temp + os.replace) from the shared helpers. Re-reads
        under the lock so a concurrent writer's entries are never clobbered.
        """
        with self._cross_process_lock():
            entries = mutate(self._load_entries())

            envelope = {
                "version": self._ENVELOPE_VERSION,
                "backend": self._BACKEND,
                "entries": entries,
            }

            content = json.dumps(envelope, indent=2)
            atomic_write(
                self._store_path,
                content,
                mode=0o600,
                fsync=True,
                restrict_to_owner=True,
            )
            _fsync_dir(self._config_dir)
