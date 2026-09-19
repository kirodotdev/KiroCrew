"""WeCom outbound media: chunked temporary-material upload.

The mirror image of :mod:`kiro_crew.wecom.media` (which DOWNLOADS and decrypts an
inbound object). Sending a file, image, voice or video over the long connection
is a three-command handshake, and this module owns exactly the protocol-shaped
work — split the bytes, base64 each chunk, compute the whole-file md5, build the
three request frames — so the WS transport in :mod:`kiro_crew.wecom.client` only
has to send a frame and hand back its response.

**The handshake.** Upload is three commands correlated by a server-issued
``upload_id``:

1. ``aibot_upload_media_init`` — declares ``type``, ``filename``, ``total_size``,
   ``total_chunks`` and the whole-file ``md5``; the reply carries the
   ``upload_id`` every later frame quotes.
2. ``aibot_upload_media_chunk`` × N — each carries the ``upload_id``, a
   zero-based ``chunk_index`` and the chunk's bytes as ``base64_data``.
3. ``aibot_upload_media_finish`` — quotes the ``upload_id``; the reply carries the
   ``media_id`` a send frame then references. The id is valid for three days.

**Chunk size is measured on the RAW bytes, not the base64.** WeCom caps a chunk
at 512 KiB and a message at 100 chunks. base64 inflates by 4/3, so a raw 512 KiB
chunk travels as ~683 KiB of text — the cap is on what is read here, before
encoding, which is why the split runs on ``data`` and the encode happens
per-chunk afterwards.

**The bytes are the payload; a path is never opened here.** Like
:mod:`kiro_crew.messaging.outbound_files` and every channel's ``send_file``, this
takes ``bytes`` that a caller already validated, so nothing between validation and
upload can substitute different content.

**No cipher.** Unlike the inbound path, an uploaded object is NOT encrypted by us:
the long connection carries it, and confidentiality is the connection's. So this
module has no AES dependency and none of :mod:`kiro_crew.wecom.media`'s
per-object-key handling.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: WeCom's per-chunk ceiling, on the RAW bytes (before base64). 512 KiB.
CHUNK_SIZE_BYTES = 512 * 1024

#: WeCom's per-message chunk ceiling. total_size therefore cannot exceed
#: CHUNK_SIZE_BYTES * MAX_CHUNKS = 50 MiB by this limit; the file-type ceiling
#: (WECOM_MAX_PLAINTEXT_BYTES, 20 MiB) is stricter and is what the caller enforces.
MAX_CHUNKS = 100

#: Outbound media types WeCom accepts for temporary material. ``msgtype`` on the
#: eventual send frame is the same string.
MEDIA_TYPES = frozenset({"file", "image", "voice", "video"})

#: WS command names for the three-step handshake. Inlined at the frame like the
#: rest of this module's siblings do (client.py builds aibot_respond_msg etc.
#: inline), but named here because all three belong to this one protocol.
CMD_INIT = "aibot_upload_media_init"
CMD_CHUNK = "aibot_upload_media_chunk"
CMD_FINISH = "aibot_upload_media_finish"


class WeComUploadError(Exception):
    """A media object could not be uploaded."""


@dataclass(frozen=True)
class MediaUpload:
    """A file prepared for the chunked upload handshake.

    Built by :func:`prepare_upload` off the event loop (md5 + chunking are
    CPU-bound on a multi-megabyte body), then consumed by the WS driver, which
    reads :attr:`init_body`, sends a chunk frame per :meth:`chunk_bodies`, and
    finishes with :attr:`finish_body` once the driver knows the ``upload_id``.
    """

    media_type: str
    filename: str
    total_size: int
    md5: str
    #: The raw byte chunks, ≤ CHUNK_SIZE_BYTES each, in order. base64 is applied
    #: per chunk at frame-build time so the encoded copies are not all held at
    #: once.
    chunks: tuple[bytes, ...]

    @property
    def total_chunks(self) -> int:
        return len(self.chunks)

    def init_body(self) -> dict[str, object]:
        """The ``body`` of the ``aibot_upload_media_init`` frame."""
        return {
            "type": self.media_type,
            "filename": self.filename,
            "total_size": self.total_size,
            "total_chunks": self.total_chunks,
            "md5": self.md5,
        }

    def chunk_body(self, upload_id: str, index: int) -> dict[str, object]:
        """The ``body`` of one ``aibot_upload_media_chunk`` frame.

        ``index`` is zero-based, matching the protocol's ``chunk_index``. The
        chunk's raw bytes are base64-encoded here, one at a time, so N encoded
        copies never coexist.
        """
        chunk = self.chunks[index]
        return {
            "upload_id": upload_id,
            "chunk_index": index,
            "base64_data": base64.b64encode(chunk).decode("ascii"),
        }

    def finish_body(self, upload_id: str) -> dict[str, object]:
        """The ``body`` of the ``aibot_upload_media_finish`` frame."""
        return {"upload_id": upload_id}


def _split_chunks(data: bytes) -> tuple[bytes, ...]:
    """Split *data* into ≤ CHUNK_SIZE_BYTES raw pieces, in order."""
    return tuple(data[i : i + CHUNK_SIZE_BYTES] for i in range(0, len(data), CHUNK_SIZE_BYTES))


def prepare_upload(data: bytes, media_type: str, filename: str, *, max_bytes: int) -> MediaUpload:
    """Validate *data* and build the chunked-upload plan.

    CPU-bound on a large body (md5 + the split), so a WS caller MUST run it off
    the event loop (``asyncio.to_thread``), matching how :mod:`kiro_crew.wecom.media`
    keeps AES off the loop.

    Raises :class:`WeComUploadError` for an unknown type, an empty body, a body
    over *max_bytes* (the caller's file-type ceiling), or one needing more than
    :data:`MAX_CHUNKS` chunks — all refused before a frame is built rather than
    after the platform rejects the handshake mid-flight.
    """
    if media_type not in MEDIA_TYPES:
        raise WeComUploadError(f"unsupported media type {media_type!r}")
    if not data:
        raise WeComUploadError("cannot upload an empty file")
    total_size = len(data)
    if total_size > max_bytes:
        raise WeComUploadError(f"file is {total_size} bytes, over the {max_bytes}-byte limit")
    chunks = _split_chunks(data)
    if len(chunks) > MAX_CHUNKS:
        # Unreachable while max_bytes (20 MiB) < CHUNK_SIZE_BYTES * MAX_CHUNKS
        # (50 MiB), but checked so a future ceiling bump cannot silently produce
        # an over-limit handshake.
        raise WeComUploadError(f"file needs {len(chunks)} chunks, over the {MAX_CHUNKS} limit")
    # WeCom's upload protocol mandates an md5 integrity tag; it is a wire
    # checksum, not a security hash. usedforsecurity=False states that intent.
    md5 = hashlib.md5(  # nosemgrep: python.lang.security.insecure-hash-algorithms-md5.insecure-hash-algorithm-md5
        data, usedforsecurity=False
    ).hexdigest()  # noqa: S324 - protocol-mandated integrity tag, not security
    return MediaUpload(
        media_type=media_type,
        filename=filename,
        total_size=total_size,
        md5=md5,
        chunks=chunks,
    )
