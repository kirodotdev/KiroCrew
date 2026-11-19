"""The tamper-evident JSON file the dashboard's signed stores keep their records in.

Each record is written with an HMAC-SHA256 of its canonical JSON under a key the
secret vault derives for that store's purpose, and is trusted on load only when the
MAC matches. The file is written under an exclusive lock beside it, replaced
atomically and readable by its owner only.

Shared by the change-card store (:mod:`kiro_crew.dashboard.change_cards`) and the
waiting-on-you store (:mod:`kiro_crew.dashboard.waiting_on_you`), so a fix to the
signing or the write lands in both. Each store keeps its own record checks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

#: The record field that carries its MAC.
MAC_FIELD = "mac"
#: The only shape a MAC this module writes can take: a lowercase hex SHA-256 digest.
MAC_RE = re.compile(r"[0-9a-f]{64}")
#: The shortest key a store accepts.
KEY_MIN_BYTES = 16


def canonical(value: Any) -> str:
    """The one serialization a record is signed and compared through."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def record_mac(key: bytes, rec: dict[str, Any]) -> str:
    """The MAC of *rec*, which must not hold its own :data:`MAC_FIELD`."""
    return hmac.new(key, canonical(rec).encode("utf-8"), hashlib.sha256).hexdigest()


def mac_matches(key: bytes, rec: dict[str, Any], mac: Any) -> bool:
    """Whether *mac* is the MAC this module would write for *rec*. Never raises.

    Only a lowercase hex digest can be one; anything else (a non-ASCII string makes
    ``compare_digest`` raise) is one unverifiable record, never a reason to stop
    loading the rest.
    """
    if not isinstance(mac, str) or not MAC_RE.fullmatch(mac):
        return False
    try:
        return hmac.compare_digest(mac, record_mac(key, rec))
    except Exception:
        return False


def store_key(
    path: Path | None, purpose: str, key_source: Callable[[], bytes] | None, what: str
) -> bytes:
    """The MAC key for a store at *path*: *key_source*'s, else the vault's *purpose* subkey.

    Raises ``ValueError`` naming *what* when the key is missing or too short.
    """
    if key_source is not None:
        key = key_source()
    else:
        from kiro_crew.secrets import SecretVault

        assert path is not None
        key = SecretVault(path.parent).derive_subkey(purpose)
    if not isinstance(key, bytes) or len(key) < KEY_MIN_BYTES:
        raise ValueError(f"{what} key unavailable")
    return key


def write_locked(path: Path, payload: str) -> None:
    """Replace *path* with *payload* under an exclusive lock on its ``.lock`` sibling."""
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.platform_compat import file_lock, open_create_or_existing

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = open_create_or_existing(str(lock_path), os.O_RDWR, 0o600)
    try:
        with file_lock(fd, exclusive=True):
            atomic_write(path, payload, restrict_to_owner=True)
    finally:
        os.close(fd)
