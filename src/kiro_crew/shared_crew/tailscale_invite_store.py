"""Where in-Crew Tailscale invites keep their key, records and avatars.

Everything lives under ``<data_home>/.vault/shared-crew-tailscale/``. The
``.vault`` subtree is masked from every sandbox mode and fenced from the
agent's file tools, so neither a tool nor a terminal can read the API key, plant
a fake accepted invite, or swap a teammate's avatar:

* the API key is an entry in a :class:`~kiro_crew.secrets.vault.SecretVault`
  rooted here (encrypted at rest, ``secrets.enc`` plus its key file). It is a
  separate vault from the user's own secrets, so a ``secret://`` reference in
  config cannot name it;
* ``invites.json`` holds the device id, the key's expiry, each invite Crew made
  and each person who accepted one, keyed by Tailscale's stable user id;
* ``avatars/`` holds uploaded avatar images, named by content digest.

The key is never written to ``invites.json``, never logged and never returned
by any route; :func:`credential_present` is all a status read learns.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.owner_only_files import mkdirs_owner_only

ROOT_LEAF = "shared-crew-tailscale"
SECRET_NAME = "tailscale_api_key"
STATE_FILE = "invites.json"
AVATAR_DIR = "avatars"
SCHEMA = 1

#: Avatar uploads: small, and only formats a browser renders as an image.
MAX_AVATAR_BYTES = 256 * 1024
_AVATAR_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)
_AVATAR_ID_RE = re.compile(r"\A[0-9a-f]{32}\.(png|jpg|gif|webp)\Z")
AVATAR_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}

MAX_ALIAS_CHARS = 64
_LOCK = threading.Lock()


def root() -> Path:
    from kiro_crew.config.paths import config_dir

    return config_dir() / ".vault" / ROOT_LEAF


def ensure_root() -> Path:
    """The store's directory, creating it and any missing parent owner-only."""
    here = root()
    mkdirs_owner_only(here)
    return here


def _vault() -> Any:
    from kiro_crew.secrets.vault import SecretVault

    return SecretVault(root())


# ── credential ──


def save_credential(key: str) -> None:
    ensure_root()
    _vault().set_sync(SECRET_NAME, key)


def load_credential() -> Optional[str]:
    if not (root() / ".vault").exists():
        return None
    value = _vault().get(SECRET_NAME)
    return value.reveal() if value is not None else None


def credential_present() -> bool:
    try:
        return load_credential() is not None
    except Exception:  # noqa: BLE001 -- an unreadable vault reads as absent
        return False


def delete_credential() -> None:
    if (root() / ".vault").exists():
        _vault().delete_sync(SECRET_NAME)


# ── state ──


def _empty() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "device_id": "",
        "tailnet": "",
        "key_expires": "",
        "connected_at": 0.0,
        "invites": {},
        "members": {},
        "backoff_until": 0.0,
        "backoff_secs": 0.0,
        "last_sync_at": 0.0,
    }


def load_state() -> dict[str, Any]:
    path = root() / STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return _empty()
    out = _empty()
    out.update({k: v for k, v in data.items() if k in out})
    for key in ("invites", "members"):
        if not isinstance(out[key], dict):
            out[key] = {}
    return out


def save_state(state: dict[str, Any]) -> None:
    ensure_root()
    payload = json.dumps(state, indent=2, sort_keys=True)
    atomic_write(root() / STATE_FILE, payload, restrict_to_owner=True)


def update_state(mutate: Any) -> dict[str, Any]:
    """Read, apply *mutate* in place, write; one writer at a time in this process."""
    with _LOCK:
        state = load_state()
        mutate(state)
        save_state(state)
        return state


# ── display fields ──


def clean_alias(raw: object, limit: int = MAX_ALIAS_CHARS) -> str:
    """Display text: printable, single-line, bounded to *limit*. Never an identity."""
    if not isinstance(raw, str):
        return ""
    text = "".join(ch for ch in raw if ch.isprintable()).strip()
    return text[:limit]


def email_hash(email: str) -> str:
    """The audit form of an email: a short digest, never the address."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


# ── avatars ──


def _sniff(data: bytes) -> tuple[str, str] | None:
    for magic, ext, ctype in _AVATAR_MAGIC:
        if data.startswith(magic):
            return ext, ctype
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def save_avatar(data: bytes) -> str:
    """Store *data* and return its id. Raises ``ValueError`` for a bad upload."""
    if not data or len(data) > MAX_AVATAR_BYTES:
        raise ValueError("avatar_too_large" if data else "avatar_empty")
    sniffed = _sniff(data)
    if sniffed is None:
        raise ValueError("avatar_type")
    ext, _ctype = sniffed
    avatar_id = f"{hashlib.sha256(data).hexdigest()[:32]}.{ext}"
    folder = ensure_root() / AVATAR_DIR
    mkdirs_owner_only(folder)
    target = folder / avatar_id
    if not target.exists():
        atomic_write(target, data)
    return avatar_id


def valid_avatar_id(avatar_id: object) -> bool:
    return isinstance(avatar_id, str) and bool(_AVATAR_ID_RE.match(avatar_id))


def read_avatar(avatar_id: str) -> tuple[bytes, str] | None:
    if not valid_avatar_id(avatar_id):
        return None
    try:
        data = (root() / AVATAR_DIR / avatar_id).read_bytes()
    except OSError:
        return None
    return data, AVATAR_TYPES[avatar_id.rsplit(".", 1)[1]]


def now() -> float:
    return time.time()
