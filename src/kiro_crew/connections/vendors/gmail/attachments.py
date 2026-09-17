"""Attachment and inline-image models: bytes, filenames, checksums, size caps.

An :class:`Attachment` is a regular file part (``Content-Disposition:
attachment``); an :class:`InlineImage` is a body-referenced part
(``Content-Disposition: inline`` plus a ``Content-ID`` a ``cid:`` URL in the
HTML body points at). Both carry their raw bytes and compute a SHA-256
checksum over those bytes so a caller can verify an extracted attachment
byte-for-byte against what was constructed.

Non-ASCII filenames are handled per RFC 2231 (the ``filename*=UTF-8''…`` form)
on the way out, which the builder applies; the model here just holds the
Unicode filename verbatim.

Size caps are enforced HERE, at model construction, before any base64
expansion — an oversize payload raises :class:`AttachmentTooLargeError` without
ever being encoded into a larger in-memory string.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from kiro_crew.connections.vendors.gmail.errors import (
    AttachmentTooLargeError,
    MalformedMimeError,
)

# An RFC 2045 media type: maintype/subtype, each a non-empty run of token
# characters (no CTLs, no whitespace, none of the tspecials
# ()<>@,;:\"/[]?= ). This rejects a value with a stray delimiter on either side
# of the '/' (e.g. "a/;b", "a/b;c", "text/") that a bare count("/")==1 check
# would let through and silently discard the tail of.
_MEDIA_TYPE_RE = re.compile(r"^[^\x00-\x20()<>@,;:\\\"/\[\]?=]+/[^\x00-\x20()<>@,;:\\\"/\[\]?=]+$")


def _is_valid_media_type(ct: str) -> bool:
    """Whether ``ct`` is a single well-formed ``maintype/subtype`` MIME token."""
    return _MEDIA_TYPE_RE.match(ct) is not None


# Gmail rejects a total message (base64-expanded) above 25 MiB. This default is
# a coarse RAW-byte ceiling for a single attachment part, NOT a base64-safe
# budget — 25 MiB of raw bytes expand to ~33 MiB base64, so a caller that needs
# to stay under Gmail's post-encode limit must pass its own (smaller) cap. The
# default only guards against an obviously-oversize single part.
DEFAULT_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def sha256_hex(data: bytes) -> str:
    """Lowercase hex SHA-256 of ``data`` — the checksum used everywhere here."""
    return hashlib.sha256(data).hexdigest()


def _normalize_crlf(data: bytes) -> bytes:
    """Normalize all line endings in ``data`` to CRLF (RFC 5322 wire form).

    Handles bare LF and bare CR as well as existing CRLF, idempotently: a
    message already in CRLF is unchanged. Canonicalizes an embedded
    ``message/*`` attachment so its stored bytes and checksum match the form
    that ships on the wire and is later extracted — the email serializer
    normalizes line endings on both construction and extraction, so a checksum
    over LF-only or mixed source bytes would otherwise not survive the
    round-trip.
    """
    lf = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return lf.replace(b"\n", b"\r\n")


@dataclass(frozen=True)
class Attachment:
    """A file attachment part.

    ``content`` is the raw (decoded) bytes. ``checksum`` is computed once at
    construction over those bytes; :meth:`verify` re-checks arbitrary bytes
    against it. ``filename`` may be non-ASCII.
    """

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"
    checksum: str = field(init=False)

    def __init__(
        self,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        *,
        max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    ) -> None:
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("attachment content must be bytes")
        # Reject a CR/LF in the filename and a malformed media type at
        # construction: unchecked, they reach EmailMessage.add_attachment /
        # header serialization and raise an uncaught ValueError (the header-
        # injection class). A media type must be a single ``maintype/subtype``
        # token with no whitespace or line break.
        if "\r" in filename or "\n" in filename:
            raise MalformedMimeError(f"attachment filename contains a line break: {filename!r}")
        ct = content_type.strip()
        if "\r" in content_type or "\n" in content_type or not _is_valid_media_type(ct):
            raise MalformedMimeError(f"attachment has a malformed media type: {content_type!r}")
        size = len(content)
        if size > max_bytes:
            raise AttachmentTooLargeError(
                f"attachment {filename!r} is {size} bytes, exceeds cap {max_bytes}"
            )
        stored = bytes(content)
        # A message/* attachment is an embedded RFC 5322 message whose wire form
        # is CRLF-terminated. The email serializer normalizes line endings on
        # both construction and extraction, so a checksum taken over LF-only or
        # mixed source bytes would not survive the round-trip. Canonicalize
        # message/* content to CRLF here so the stored bytes and checksum match
        # the form that actually ships and is later extracted — one consistent
        # rule end to end, instead of trying to preserve arbitrary source line
        # endings verbatim through a library that normalizes them.
        if ct.lower().startswith("message/"):
            stored = _normalize_crlf(stored)
            # CRLF normalization can GROW the byte count (LF -> CRLF), so
            # re-enforce the cap on the stored form: the pre-normalization check
            # above is a lower bound and must not be the only gate.
            size = len(stored)
            if size > max_bytes:
                raise AttachmentTooLargeError(
                    f"attachment {filename!r} is {size} bytes after CRLF "
                    f"normalization, exceeds cap {max_bytes}"
                )
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "content", stored)
        object.__setattr__(self, "content_type", ct)
        object.__setattr__(self, "checksum", sha256_hex(stored))

    def verify(self, data: bytes) -> bool:
        """True iff ``data`` hashes to this attachment's stored checksum."""
        return sha256_hex(data) == self.checksum


@dataclass(frozen=True)
class InlineImage:
    """An inline image part referenced from the HTML body via ``cid:``.

    ``content_id`` is the bare token (no angle brackets) that the HTML body
    references as ``cid:<content_id>`` and that the part carries as
    ``Content-ID: <<content_id>>``. The builder adds the angle brackets; the
    body author writes ``cid:<content_id>``.
    """

    content_id: str
    content: bytes
    content_type: str = "image/png"
    filename: str = ""
    checksum: str = field(init=False)

    def __init__(
        self,
        content_id: str,
        content: bytes,
        content_type: str = "image/png",
        filename: str = "",
        *,
        max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    ) -> None:
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("inline image content must be bytes")
        cid = content_id.strip().strip("<>").strip()
        if not cid:
            raise ValueError("inline image content_id must be non-empty")
        # Reject CR/LF in every header-bound field (content_id, content_type,
        # filename): unchecked, a newline reaches add_related's cid / filename
        # param and header construction, raising an uncaught (non-MimeError)
        # ValueError — the header-injection class Attachment already guards.
        for _field_name, _value in (
            ("content_id", content_id),
            ("content_type", content_type),
            ("filename", filename),
        ):
            if "\r" in _value or "\n" in _value:
                raise MalformedMimeError(
                    f"inline image {_field_name} contains a line break: {_value!r}"
                )
        # An inline image MUST have an image/* content type. A non-image inline
        # part (e.g. text/plain) round-trips as a body leaf on parse and vanishes
        # from inline_images — silent structural corruption. Reject it at
        # construction so the type mismatch surfaces loudly instead. The type
        # must also be a single well-formed maintype/subtype token.
        ct = content_type.strip()
        if not _is_valid_media_type(ct) or not ct.lower().startswith("image/"):
            raise MalformedMimeError(
                f"inline image content_type must be a well-formed image/* type, got {content_type!r}"
            )
        size = len(content)
        if size > max_bytes:
            raise AttachmentTooLargeError(
                f"inline image {cid!r} is {size} bytes, exceeds cap {max_bytes}"
            )
        object.__setattr__(self, "content_id", cid)
        object.__setattr__(self, "content", bytes(content))
        object.__setattr__(self, "content_type", ct)
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "checksum", sha256_hex(bytes(content)))

    def cid_reference(self) -> str:
        """The ``cid:`` URL an HTML ``src`` attribute uses to reach this part."""
        return f"cid:{self.content_id}"

    def verify(self, data: bytes) -> bool:
        """True iff ``data`` hashes to this image's stored checksum."""
        return sha256_hex(data) == self.checksum
