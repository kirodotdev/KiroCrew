"""The base64url ``raw`` wire form Gmail ``messages.send`` uses.

Gmail's ``users.messages.send`` accepts the entire RFC 5322 message as a single
``raw`` field: the message bytes, base64url-encoded (RFC 4648 §5, the URL- and
filename-safe alphabet using ``-`` and ``_`` in place of ``+`` and ``/``).
Gmail accepts the value with or without ``=`` padding; this module encodes
WITHOUT padding (Gmail's own client libraries do) and decodes tolerantly
(padding present or absent), so an encode→decode round-trip is exact.

These are the only two functions here on purpose: the ``raw`` form is a pure
transport encoding of already-assembled message bytes, with no MIME semantics
of its own.
"""

from __future__ import annotations

import base64
import binascii

from kiro_crew.connections.vendors.gmail.errors import MalformedMimeError


def encode_raw(message_bytes: bytes) -> str:
    """Encode assembled RFC 5322 message bytes to Gmail's ``raw`` string.

    Uses the URL-safe base64 alphabet and strips ``=`` padding, matching what
    Gmail's official client libraries emit. The result is ASCII text suitable
    for the JSON ``raw`` field.
    """
    if not isinstance(message_bytes, (bytes, bytearray)):
        raise TypeError("encode_raw expects bytes")
    encoded = base64.urlsafe_b64encode(bytes(message_bytes))
    return encoded.rstrip(b"=").decode("ascii")


def decode_raw(raw: str) -> bytes:
    """Decode a Gmail ``raw`` string back to the original message bytes.

    Tolerates missing ``=`` padding (Gmail omits it) by restoring the padding
    before decoding. Raises :class:`MalformedMimeError` if the value is not
    valid base64url — a caller handed something that never came from
    :func:`encode_raw` or an equivalent encoder. Validation is STRICT: a
    non-ASCII character, or any byte outside the base64url alphabet, is a
    decode failure, not silently ignored — silent tolerance would let a
    corrupted ``raw`` decode to the wrong bytes and be treated as success.
    """
    if not isinstance(raw, str):
        raise TypeError("decode_raw expects str")
    try:
        ascii_bytes = raw.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MalformedMimeError(f"raw contains non-ASCII characters: {exc}") from exc
    # Strict base64url: the standard-alphabet characters '+' and '/' are NOT
    # part of the URL-safe alphabet. Reject them explicitly before the
    # translate step below (which would otherwise map nothing and let them
    # through as valid standard base64), so decode_raw honors its base64url-only
    # contract rather than silently accepting a standard-base64 string.
    if b"+" in ascii_bytes or b"/" in ascii_bytes:
        raise MalformedMimeError(
            "raw contains standard-base64 characters ('+' or '/'); "
            "expected URL-safe base64url ('-'/'_')"
        )
    # Restore padding to a multiple of 4.
    padding = (-len(ascii_bytes)) % 4
    padded = ascii_bytes + (b"=" * padding)
    # Translate the URL-safe alphabet to the standard one, then decode with
    # validate=True so any remaining character outside the alphabet is REJECTED
    # rather than silently discarded (urlsafe_b64decode alone tolerates junk).
    standard = padded.translate(bytes.maketrans(b"-_", b"+/"))
    try:
        return base64.b64decode(standard, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MalformedMimeError(f"raw is not valid base64url: {exc}") from exc
