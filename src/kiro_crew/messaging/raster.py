"""What counts as a raster image, decided by leading bytes.

Shared by both directions of the file path: inbound ingestion
(:mod:`kiro_crew.messaging.attachments`) sniffs a downloaded temp file, and
outbound extraction (:mod:`kiro_crew.messaging.outbound_files`) sniffs a local
file the agent referenced. Both need the same answer to the same question, and a
second copy of the magic table is how one direction ends up accepting a type the
other rejects.

Deliberately dependency-free: it imports nothing from ``kiro_crew``, so either
direction can use it without dragging the other's dependencies (transcription,
document parsing, the file-read gate) onto its import path.

Bytes rather than metadata or filename, because both of those are
attacker-controlled and the leading bytes are not: a script named ``chart.png``
and declared ``image/png`` is still a script (CWE-434).

The set of types here is the SNIFFABLE set, not the set any one consumer
accepts. Each direction keeps its own allowlist on top -- what the ACP encoder
can inline is a different question from what a chat channel will accept as an
upload -- so a type added here does not silently widen either of them.
"""

from __future__ import annotations

#: Leading bytes per raster type. SVG is deliberately absent -- it is scriptable
#: markup, not a raster, and carries no fixed signature to sniff.
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),  # RIFF....WEBP
    "image/bmp": (b"BM",),
}

#: Leading bytes a caller must read for :func:`sniff_raster_mime` to decide. The
#: longest signature is 8 bytes, but WebP's form tag sits at offset 8-12.
SNIFF_BYTES = 16


def sniff_raster_mime(head: bytes) -> str | None:
    """The raster type *head* starts with, or ``None`` if it is not a raster.

    *head* must be at least :data:`SNIFF_BYTES` leading bytes of the file; a
    shorter read simply matches fewer types. Returning the DETECTED type rather
    than merely accepting or rejecting a declared one does double duty: a file
    matching no known signature is not an image at all, and a genuine JPEG
    mislabelled ``image/png`` still works with truthful metadata attached.
    """
    for mime, prefixes in _MAGIC.items():
        if any(head.startswith(p) for p in prefixes):
            # WebP and other RIFF containers share the "RIFF" prefix; confirm the
            # form tag so a RIFF/WAVE file is not mistaken for an image.
            if mime == "image/webp" and head[8:12] != b"WEBP":
                continue
            return mime
    return None
