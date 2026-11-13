"""Crew invites for people who reach this machine through AWS Systems Manager.

An SSM port forward arrives as a plain loopback connection, so the network
proves nothing about who is on the other end. The owner's invite is the whole
credential: ``<invite id>.<secret>``, shown once when it is made. Only the
SHA-256 of the secret is stored, so a copy of the file cannot admit anyone.

The store lives under ``<data_home>/.vault/shared-crew-ssm/``. The ``.vault``
subtree is masked from every sandbox mode, so neither an agent tool nor a
terminal can read it or plant an invite. Revoking removes the record, and the
next request that carries the credential is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.owner_only_files import mkdirs_owner_only
from kiro_crew.shared_crew.contracts import SsmGuest
from kiro_crew.shared_crew.tailscale_invite_store import MAX_ALIAS_CHARS, clean_alias

ROOT_LEAF = "shared-crew-ssm"
STATE_FILE = "invites.json"
SCHEMA = 1
#: At most this many live invites; a full store refuses a new one.
MAX_INVITES = 64
_ID_HEX_CHARS = 12

_LOCK = threading.Lock()
#: The parsed file, kept so a request never reads the disk. This process is the
#: only writer, so the copy stays current.
_cache: dict[str, dict[str, Any]] | None = None


class InviteLimitError(Exception):
    """The store already holds :data:`MAX_INVITES` invites."""


def root() -> Path:
    from kiro_crew.config.paths import config_dir

    return config_dir() / ".vault" / ROOT_LEAF


def _read() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads((root() / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("invites") if isinstance(data, dict) and data.get("schema") == SCHEMA else None
    if not isinstance(rows, dict):
        return {}
    return {
        key: row
        for key, row in rows.items()
        if isinstance(key, str)
        and isinstance(row, dict)
        and isinstance(row.get("secret_sha256"), str)
    }


def _invites() -> dict[str, dict[str, Any]]:
    global _cache
    if _cache is None:
        _cache = _read()
    return _cache


def warm() -> None:
    """Read the store now, so a request never reads the disk. Blocking: call off the event loop."""
    with _LOCK:
        _invites()


def _write(rows: dict[str, dict[str, Any]]) -> None:
    mkdirs_owner_only(root())
    payload = json.dumps({"schema": SCHEMA, "invites": rows}, indent=2, sort_keys=True)
    atomic_write(root() / STATE_FILE, payload, restrict_to_owner=True)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create(alias: object) -> tuple[str, str]:
    """Make an invite and return ``(invite_id, credential)``. The credential is not stored."""
    global _cache
    with _LOCK:
        rows = dict(_invites())
        if len(rows) >= MAX_INVITES:
            raise InviteLimitError
        invite_id = secrets.token_hex(_ID_HEX_CHARS // 2)
        while invite_id in rows:
            invite_id = secrets.token_hex(_ID_HEX_CHARS // 2)
        secret = secrets.token_urlsafe(32)
        rows[invite_id] = {
            "alias": clean_alias(alias, MAX_ALIAS_CHARS),
            "secret_sha256": _digest(secret),
            "created_at": time.time(),
        }
        _write(rows)
        _cache = rows
    return invite_id, f"{invite_id}.{secret}"


def revoke(invite_id: str) -> bool:
    """Delete an invite. ``False`` when there was none."""
    global _cache
    with _LOCK:
        rows = dict(_invites())
        if invite_id not in rows:
            return False
        del rows[invite_id]
        _write(rows)
        _cache = rows
    return True


def verify(credential: object) -> SsmGuest | None:
    """The guest *credential* names, or ``None`` for anything that does not match a live invite."""
    if not isinstance(credential, str):
        return None
    invite_id, dot, secret = credential.partition(".")
    if not dot or not secret:
        return None
    row = _invites().get(invite_id)
    if row is None:
        # Compare anyway, so a miss costs what a hit does.
        hmac.compare_digest(_digest(secret), _digest(""))
        return None
    if not hmac.compare_digest(_digest(secret), str(row["secret_sha256"])):
        return None
    return SsmGuest(invite_id=invite_id, alias=str(row.get("alias") or ""))


def mark_joined(invite_id: str) -> None:
    """Record, once, that someone opened the Crew with this invite."""
    global _cache
    with _LOCK:
        rows = dict(_invites())
        row = rows.get(invite_id)
        if row is None or row.get("joined_at"):
            return
        rows[invite_id] = {**row, "joined_at": time.time()}
        _write(rows)
        _cache = rows


def has_invites() -> bool:
    return bool(_invites())


def public_rows() -> list[dict[str, Any]]:
    """Each live invite as the owner's list shows it, oldest first. Never carries a secret.

    ``joined`` is whether anyone has opened the Crew with the invite;
    ``principal_id`` is the id that person's People row carries, so a joined
    invite and its person are one entry.
    """
    from kiro_crew.shared_crew.identity import ssm_principal

    rows = [
        {
            "id": invite_id,
            "alias": str(row.get("alias") or ""),
            "created_at": float(row.get("created_at") or 0.0),
            "joined": bool(row.get("joined_at")),
            "principal_id": ssm_principal(invite_id).id,
        }
        for invite_id, row in _invites().items()
    ]
    return sorted(rows, key=lambda r: (r["created_at"], r["id"]))


def _reset_for_tests() -> None:
    global _cache
    with _LOCK:
        _cache = None


__all__ = [
    "InviteLimitError",
    "MAX_INVITES",
    "SsmGuest",
    "create",
    "has_invites",
    "mark_joined",
    "public_rows",
    "revoke",
    "verify",
    "warm",
]
