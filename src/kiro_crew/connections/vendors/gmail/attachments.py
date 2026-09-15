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
from dataclasses import dataclass, field

from kiro_crew.connections.vendors.gmail.errors import AttachmentTooLargeError

# Gmail rejects a total message (base64-expanded) above 25 MiB. base64 inflates
# by 4/3, so the pre-encode byte budget for a single attachment is bounded well
# under that; callers pass their own cap, and this default is a safe ceiling
# for a single part's raw bytes.
DEFAULT_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def sha256_hex(data: bytes) -> str:
    """Lowercase hex SHA-256 of ``data`` — the checksum used everywhere here."""
    return hashlib.sha256(data).hexdigest()


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
        size = len(content)
        if size > max_bytes:
            raise AttachmentTooLargeError(
                f"attachment {filename!r} is {size} bytes, exceeds cap {max_bytes}"
            )
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "content", bytes(content))
        object.__setattr__(self, "content_type", content_type)
        object.__setattr__(self, "checksum", sha256_hex(bytes(content)))

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
        size = len(content)
        if size > max_bytes:
            raise AttachmentTooLargeError(
                f"inline image {cid!r} is {size} bytes, exceeds cap {max_bytes}"
            )
        object.__setattr__(self, "content_id", cid)
        object.__setattr__(self, "content", bytes(content))
        object.__setattr__(self, "content_type", content_type)
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "checksum", sha256_hex(bytes(content)))

    def cid_reference(self) -> str:
        """The ``cid:`` URL an HTML ``src`` attribute uses to reach this part."""
        return f"cid:{self.content_id}"

    def verify(self, data: bytes) -> bool:
        """True iff ``data`` hashes to this image's stored checksum."""
        return sha256_hex(data) == self.checksum
