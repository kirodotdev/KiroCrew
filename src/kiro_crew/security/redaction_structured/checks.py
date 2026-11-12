"""Format checks a row names by id, and the placeholder / allow checks (REQ-F3)."""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from typing import Any

from .charsets import ALPHABETS

_BASE62 = ALPHABETS["base62"]
_GITHUB_PREFIX_LEN = 4
_GITHUB_BODY_LEN = 30
_GITHUB_CHECK_LEN = 6
_DECODE_CAP = 8192


def base62_crc32(body: str) -> str:
    """The six-character base62 CRC32 a GitHub token appends to its body."""
    crc = zlib.crc32(body.encode("ascii"))
    digits = ""
    while crc:
        crc, rem = divmod(crc, 62)
        digits = _BASE62[rem] + digits
    return digits.rjust(_GITHUB_CHECK_LEN, "0")


def github_crc32_ok(token: str) -> bool:
    body = token[_GITHUB_PREFIX_LEN : _GITHUB_PREFIX_LEN + _GITHUB_BODY_LEN]
    check = token[_GITHUB_PREFIX_LEN + _GITHUB_BODY_LEN :]
    if len(body) != _GITHUB_BODY_LEN or len(check) != _GITHUB_CHECK_LEN:
        return False
    return base62_crc32(body) == check


CHECKSUMS = {"github_crc32": github_crc32_ok}


def b64url_json_object(segment: str) -> dict[str, Any] | None:
    """Decode a base64url segment (size-capped) to a JSON object, else ``None``."""
    if len(segment) > _DECODE_CAP:
        return None
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        decoded = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, RecursionError):
        return None
    return decoded if isinstance(decoded, dict) else None


# ── Placeholders and allow entries ──

ALLOW_WORDS = frozenset(
    {
        "changeme",
        "placeholder",
        "example",
        "true",
        "false",
        "null",
        "none",
        "required",
        "optional",
        "redacted",
        "unset",
        "empty",
        "string",
    }
)
_EXAMPLE_SUFFIXES = ("EXAMPLEKEY", "EXAMPLE")
_MASK_CHARS = frozenset("*xX.\u2026")
_ENV_HEAD = frozenset(ALPHABETS["upper_digit"].replace("0123456789", "") + "_")
_ENV_TAIL = frozenset(ALPHABETS["upper_digit"] + "_")
_IDENT = frozenset(ALPHABETS["b64url"])


def _braced(value: str) -> bool:
    if value.startswith("${") and value.endswith("}") and len(value) > 3:
        inner = value[2:-1]
        return "{" not in inner and "}" not in inner and "\n" not in inner
    body = value[1:] if value.startswith("$") else value
    if body.startswith("{{") and body.endswith("}}") and len(body) > 4:
        inner = body[2:-2]
        return "}}" not in inner and "\n" not in inner
    return False


def is_placeholder(value: str) -> bool:
    """A documented example value or placeholder, recognised without any pattern."""
    v = value.strip()
    if len(v) < 4:
        return True
    folded = v.casefold()
    if folded in ALLOW_WORDS:
        return True
    if v.endswith(_EXAMPLE_SUFFIXES):
        return True
    if folded.startswith(("your-", "your_")):
        return True
    if v[0] == "<" and v[-1] == ">" and not (set(v[1:-1]) & {"<", ">", "\n", "\r"}):
        return True
    if _braced(v):
        return True
    if v[0] == "$" and len(v) > 1 and v[1] in _ENV_HEAD and set(v[2:]) <= _ENV_TAIL:
        return True
    if v[0] == "%" == v[-1] and len(v) > 2 and set(v[1:-1]) <= _IDENT:
        return True
    return len(v) >= 3 and len(set(v)) == 1 and v[0] in _MASK_CHARS
