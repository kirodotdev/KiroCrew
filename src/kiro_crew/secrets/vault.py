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
import hashlib
import hmac
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.platform_compat import file_lock, restrict_to_owner


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
        key = self._get_or_create_key()
        plaintext = self._decrypt_entry(name, entries[name], key)
        return SecretValue(plaintext.decode("utf-8"))

    def get_many(self, names: list[str]) -> dict[str, Optional[SecretValue]]:
        """Batch-retrieve secrets, loading the store and key exactly once.

        Returns a mapping from each requested name to its :class:`SecretValue`,
        or ``None`` for names not present in the store — the same
        per-name found/missing semantics as :meth:`get`, but without re-reading
        ``secrets.enc`` (``_load_entries``) and the key file
        (``_get_or_create_key``) once per name. On the spawn path K secret
        references would otherwise cost K full store loads plus K key reads.

        Like :meth:`get`, this is lock-free by design: ``_load_entries`` reads
        the store atomically (writers commit via ``os.replace``, so a torn read
        is impossible), and the key file is immutable once created. All names
        resolve against the SAME on-disk snapshot, which is the intended
        behaviour for a single spawn.

        The vault key is only loaded when at least one requested name is
        present, so a call for names none of which exist on a fresh vault does
        not create a key file (matching :meth:`get`, which returns before
        ``_get_or_create_key`` for a missing name).
        """
        entries = self._load_entries()
        result: dict[str, Optional[SecretValue]] = {}
        key: Optional[bytes] = None
        for name in names:
            # Membership, then index: a corrupt store can hold a JSON ``null``
            # entry, and ``entries.get(name)`` would misclassify it as MISSING
            # (wrong remediation: "store the secret") instead of MALFORMED
            # (the store is corrupt) — ``_decrypt_entry`` raises the
            # descriptive fail-closed ValueError for it.
            if name not in entries:
                result[name] = None
                continue
            entry = entries[name]
            if key is None:
                key = self._get_or_create_key()
            plaintext = self._decrypt_entry(name, entry, key)
            result[name] = SecretValue(plaintext.decode("utf-8"))
        return result

    async def set(self, name: str, value: str) -> None:
        """Store or overwrite a secret."""
        async with self._lock:
            await asyncio.to_thread(self._set_sync, name, value)

    def set_sync(self, name: str, value: str) -> None:
        """Store or overwrite a secret, synchronously.

        For callers already off the event loop (worker threads, sync storage
        layers). Safe without the asyncio lock: every mutation serializes on the
        cross-process flock inside ``_write_store``, which also covers threads
        in this process (each acquisition uses its own fd).
        """
        self._set_sync(name, value)

    def delete_sync(self, name: str) -> None:
        """Remove a secret synchronously. No-op if it does not exist.

        Same locking rationale as ``set_sync``. Failures (e.g. an unwritable
        store) propagate to the caller.
        """
        self._delete_sync(name)

    async def set_if_absent(self, name: str, value: str) -> Optional[dict[str, str]]:
        """Store ``value`` under ``name`` ONLY if the key is not already present.

        Returns the exact encrypted entry dict that was written on a fresh write,
        or ``None`` if the key was already present (left untouched). A non-None
        return means "this call wrote the entry"; ``None`` means "a value was
        already present". The presence check happens INSIDE the cross-process
        store lock (see ``_write_store``), so a concurrent writer that stored the
        key between a caller's earlier ``list_names()`` and this call always wins
        — this call becomes a no-op rather than clobbering the newer value. Used
        by the .env→vault importer, which must never overwrite a credential a
        dashboard/other writer saved to the vault in the meantime.

        The returned entry dict (when non-None) is the unique ciphertext
        fingerprint for what this call stored — because ``_encrypt_entry`` uses a
        random nonce per call, two encryptions of the same plaintext produce
        different dicts. The importer's rollback path passes this dict to
        ``_compare_and_delete_sync`` so it can identify and delete the exact entry
        this run wrote, without risking deletion of a concurrent writer's entry
        that may coincidentally hold the same plaintext.
        """
        async with self._lock:
            return await asyncio.to_thread(self._set_if_absent_sync, name, value)

    def _set_if_absent_sync(self, name: str, value: str) -> Optional[dict[str, str]]:
        written_entry: Optional[dict[str, str]] = None

        def _mutate(entries: dict) -> dict:
            nonlocal written_entry
            if name in entries:
                # Concurrent writer already populated it under the lock — do not
                # overwrite. Return entries unchanged so the write is a no-op.
                return entries
            entry = self._encrypt_entry(name, value.encode("utf-8"))
            written_entry = entry
            return {**entries, name: entry}

        self._write_store(_mutate)
        return written_entry

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

    def _compare_and_delete_sync(self, name: str, expected_entry: dict[str, str]) -> bool:
        """Delete ``name`` ONLY if its stored encrypted entry equals ``expected_entry``.

        The comparison is by exact ciphertext identity: ``entries[name] ==
        expected_entry``. Because ``_encrypt_entry`` uses a random nonce per call,
        two encryptions of the same plaintext produce different dicts, so this
        comparison uniquely fingerprints the exact entry written by a particular
        ``set_if_absent`` call. A concurrent writer that stored a new value (even
        the same plaintext) will have a different ciphertext and a different dict,
        so its entry is never deleted.

        The comparison and deletion happen inside a single ``_write_store`` call,
        i.e. under one cross-process flock acquisition, so no concurrent writer
        can replace the value between the check and the delete.

        Returns ``True`` if the entry was deleted, ``False`` if it was absent or
        the stored entry differed from ``expected_entry`` (concurrent overwrite —
        leave it alone).

        Used by the rollback path in the .env importer: ``expected_entry`` is the
        exact dict returned by ``set_if_absent``, so only the entry this run
        wrote can be deleted — a concurrent writer's entry is always safe.
        """
        deleted = False

        def _mutate(entries: dict) -> dict:
            nonlocal deleted
            if name not in entries:
                return entries
            if entries[name] != expected_entry:
                # Stored entry differs (different ciphertext) — a concurrent
                # writer replaced our entry. Do not delete.
                return entries
            deleted = True
            return {k: v for k, v in entries.items() if k != name}

        self._write_store(_mutate)
        return deleted

    def list_names(self) -> list[str]:
        """Return all stored secret names."""
        return list(self._load_entries().keys())

    def derive_subkey(self, purpose: str) -> bytes:
        """Derive a purpose-scoped 32-byte key from the vault key (HMAC-SHA256).

        Lets other agent-fenced mechanisms (e.g. the cron grant-pin HMAC) key
        themselves without a second key file and its own birth/corruption
        handling — the vault key's exclusive-create birth, fsync durability,
        and owner-only ACL are inherited. Distinct purposes yield independent
        keys, and the vault key itself is never handed out.
        """

        return hmac.new(self._get_or_create_key(), purpose.encode(), hashlib.sha256).digest()

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
        # best_effort: the key file is created, written and fsynced by this point, and
        # the failure cleanup above has already been left behind. Raising here would
        # report a key that IS on disk as never created, and the caller's recovery for
        # that is to mint a second one over it.
        fsync_dir(self._config_dir, best_effort=True)
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

    def _decrypt_entry(self, name: str, entry: dict[str, str], key: bytes) -> bytes:
        aesgcm = AESGCM(key)
        # A corrupt / hand-edited store can make ``entry`` any shape (a string,
        # a list, a dict missing ``nonce``/``ct``, or one whose values are not
        # valid hex). Guard the extraction so callers see the module's
        # fail-closed descriptive ValueError instead of a raw TypeError /
        # KeyError / non-hex ValueError. The message names the entry but NEVER
        # includes ciphertext or plaintext material.
        try:
            nonce = bytes.fromhex(entry["nonce"])
            ct = bytes.fromhex(entry["ct"])
        except (TypeError, KeyError, ValueError, AttributeError) as exc:
            # No entry name in the message: names are caller-supplied strings
            # that may contain anything, and this error propagates into spawn
            # logs. The resolution caller already names the env-var key.
            raise ValueError(
                f"Vault store corrupt: an entry is malformed ({type(exc).__name__})."
            ) from None
        return aesgcm.decrypt(nonce, ct, self._aad_for(name))

    # ── Store I/O ──

    def _load_entries(self) -> dict[str, dict[str, str]]:
        """Load and decode the on-disk entry map (empty if no store yet)."""
        if not self._store_path.exists():
            return {}

        raw = self._store_path.read_text(encoding="utf-8")
        envelope = json.loads(raw)

        # A corrupt / hand-edited store can carry a non-object top-level value
        # (a list, a string, null, a number). Guard before ``.get`` so it fails
        # closed with a descriptive ValueError rather than a raw AttributeError,
        # matching the ``entries`` guard below. No store content is echoed.
        if not isinstance(envelope, dict):
            raise ValueError(
                f"Vault store corrupt: top-level value must be an object, "
                f"got {type(envelope).__name__}."
            )

        if envelope.get("backend") != self._BACKEND:
            raise ValueError(
                f"Vault backend mismatch: expected {self._BACKEND!r}, "
                f"got {envelope.get('backend')!r}"
            )

        entries = envelope.get("entries", {})
        # A corrupt / hand-edited store can carry a non-mapping ``entries``
        # (a list, a string, null). Fail closed with a descriptive ValueError
        # rather than letting ``dict(entries)`` raise a raw ValueError/TypeError
        # deep in an unrelated caller. No store content is echoed.
        if not isinstance(entries, dict):
            raise ValueError(
                f"Vault store corrupt: 'entries' must be an object, "
                f"got {type(entries).__name__}."
            )
        return dict(entries)

    @contextmanager
    def hold_cross_process_lock(self) -> Iterator[None]:
        """Acquire and hold the vault's cross-process flock (.secrets.enc.lock).

        Public context manager so callers that need to keep the vault immutable
        across multiple operations (e.g. a re-verify + atomic .env rewrite that
        must not be interrupted by a concurrent vault DELETE) can hold the lock
        for the entire critical section.

        The lock is the same advisory flock used internally by every vault write
        (``_write_store``), so holding it here prevents any concurrent
        ``set`` / ``set_if_absent`` / ``delete`` from mutating the store until
        the caller's ``with`` block exits.

        Deadlock prevention: callers MUST NOT call any mutating vault method
        (``set``, ``set_if_absent``, ``delete``) while holding this lock,
        because those methods call ``_write_store`` → ``_cross_process_lock``
        which re-acquires the same flock — causing a deadlock on POSIX (flock is
        not re-entrant across the same fd on most kernels). Read-only access
        (``get``, ``list_names``) is safe because those methods do NOT acquire
        the flock.
        """
        self._config_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._store_path.with_name(".secrets.enc.lock")
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            with file_lock(lock_fd, exclusive=True):
                yield
        finally:
            os.close(lock_fd)

    @contextmanager
    def _cross_process_lock(self) -> Iterator[None]:
        """Acquire a cross-process lock via platform_compat.file_lock.

        Delegates to :meth:`hold_cross_process_lock` so there is exactly one
        flock-acquire implementation.
        """
        with self.hold_cross_process_lock():
            yield

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
            # best_effort, and it is this call site's own decision, not a weakening of
            # the helper: atomic_write has already COMMITTED the entry. A raise here
            # would abort set_if_absent before it returns its fingerprint, so a
            # migration would omit an entry that is on disk from its rollback set and
            # let it shadow the plaintext it was migrating away from. The directory
            # entry is the least of what is at stake once the value is stored.
            fsync_dir(self._config_dir, best_effort=True)
