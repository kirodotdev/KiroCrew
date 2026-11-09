"""Device identity: a stable id derived from the host name, made unique only on collision.

The local machine proposes a *base* id, the slug of its host name, and proves
which install it is with a *fingerprint*: the SHA-256 of a random install key
that never leaves the local machine's vault. The remote gateway keeps the id it
assigned per fingerprint, so a reconnect gets the same id back; a second
computer with the same host name gets the base plus a short differentiator taken
from its own fingerprint, so two computers never share an id (B09).

The human label is the computer's own name. Labels may repeat; the id is what
tells two computers apart, and the picker shows it when labels collide.
"""

from __future__ import annotations

import hashlib
import platform
import re
import socket
from collections.abc import Mapping
from typing import Final

#: Longest device id. The id is a Session Environments choice value, whose
#: grammar allows 64 characters; the differentiator needs room after the base.
DEVICE_ID_MAX: Final[int] = 48
#: Longest base (host-name) part of an id.
BASE_MAX: Final[int] = 36
#: The differentiator lengths tried, shortest first.
_DIFFERENTIATOR_LENGTHS: Final[tuple[int, ...]] = (4, 6, 8, 12)
#: Longest human label kept.
LABEL_MAX: Final[int] = 64
#: Install keys are 32 random bytes.
INSTALL_KEY_BYTES: Final[int] = 32

DEVICE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
FINGERPRINT_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_FALLBACK_BASE: Final[str] = "computer"


class IdentityError(ValueError):
    """A device id, fingerprint or label is malformed."""


def hostname_slug(hostname: str) -> str:
    """The base device id for ``hostname``: lower-case ``[a-z0-9-]``, no domain part.

    ``Zezhen's MacBook-Pro.local`` -> ``zezhen-s-macbook-pro``. A name with no
    usable character becomes ``computer``.
    """
    name = (hostname or "").strip().split(".", 1)[0].lower()
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    slug = slug[:BASE_MAX].strip("-")
    return slug or _FALLBACK_BASE


def install_fingerprint(install_key: bytes) -> str:
    """The fingerprint the remote gateway records for an install key."""
    if not isinstance(install_key, (bytes, bytearray)) or len(install_key) != INSTALL_KEY_BYTES:
        raise IdentityError("install key must be 32 bytes")
    return hashlib.sha256(b"kirocrew-connected-computer\x00" + bytes(install_key)).hexdigest()


def validate_device_id(device_id: str) -> str:
    if not isinstance(device_id, str) or not DEVICE_ID_RE.match(device_id):
        raise IdentityError("device id is invalid")
    return device_id


def validate_fingerprint(fingerprint: str) -> str:
    if not isinstance(fingerprint, str) or not FINGERPRINT_RE.match(fingerprint):
        raise IdentityError("device fingerprint is invalid")
    return fingerprint


def clean_label(label: str) -> str:
    """A printable, bounded label; the base id when nothing printable is left."""
    text = "".join(ch for ch in str(label or "") if ch.isprintable()).strip()
    return text[:LABEL_MAX]


def assign_device_id(base: str, fingerprint: str, existing: Mapping[str, str]) -> str:
    """The id for the install ``fingerprint``, given ``existing`` (id -> fingerprint).

    An install that already has an id keeps it, whatever its base now is (a
    renamed host keeps its id, so its chats stay bound). Otherwise the base is
    used when free, else the base plus the first characters of the fingerprint,
    lengthened until free. Never returns an id another fingerprint holds.
    """
    validate_fingerprint(fingerprint)
    for device_id, fp in existing.items():
        if fp == fingerprint:
            return device_id
    base = hostname_slug(base)
    if base not in existing:
        return base
    for length in _DIFFERENTIATOR_LENGTHS:
        candidate = f"{base[: DEVICE_ID_MAX - length - 1].rstrip('-')}-{fingerprint[:length]}"
        if candidate not in existing:
            return candidate
    # 12 hex characters collided for a different fingerprint: refuse rather than share.
    raise IdentityError("could not give this computer an id that no other computer holds")


def local_computer_name() -> str:
    """This machine's own name for the human label (not unique; never an id)."""
    name = ""
    try:
        name = socket.gethostname()
    except OSError:
        name = ""
    return clean_label(name or platform.node() or _FALLBACK_BASE)
