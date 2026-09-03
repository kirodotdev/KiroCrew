"""Image sniffing for images pasted into a meeting note.

The one job here is deciding what a pasted blob actually IS, from its bytes, and
refusing everything else. That decision is the whole security boundary for note
images, so it is a separate module with its own tests rather than a few lines
inside a request handler.

Two properties are deliberate and worth not "simplifying":

**The client's filename never reaches a path.** The extension is derived from the
sniffed signature, not from what the browser said the file was called, so the only
strings that can appear in a note-image path are ones this module produced. Compare
``dashboard/handlers/files.py::api_upload_file``, which sanitizes a client filename
and then verifies the magic bytes MATCH the claimed extension — a sound design for
a general-purpose uploader that must preserve names, but for a pasted screenshot
the name is worthless, and not accepting one removes a whole class of question.

**Refusal is the default.** An unrecognised signature returns ``None``. That is
what keeps SVG out: an SVG has no binary signature, so the core uploader's
``_content_matches_ext`` fails OPEN for ``.svg`` (its docstring says so), and an
SVG is not really an image — it is a document that can carry ``<script>`` and
``on*`` handlers, which is why ``pptx_maker`` classifies ``image/svg+xml`` as
script-capable and ``files.py`` refuses to serve one inline. A note never needs
one, so the cheapest correct answer is to not accept it at all.
"""

from __future__ import annotations

from typing import Optional

#: ``(magic prefix, canonical extension)`` for the raster formats a note may embed.
#:
#: Deliberately narrow. BMP is omitted (its ``BM`` signature is two bytes, which is
#: weak, and nothing produces BMP screenshots), and every vector or document format
#: is omitted because a note image is a screenshot, not an attachment.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)

#: WebP is a RIFF container: ``RIFF`` + 4 little-endian size bytes + ``WEBP``. The
#: split check is why it cannot live in the flat prefix table above.
_RIFF_PREFIX = b"RIFF"
_WEBP_TAG = b"WEBP"
_WEBP_TAG_OFFSET = 8

#: Longest prefix any check needs, so a caller can sniff without buffering a file.
MIN_SNIFF_BYTES = _WEBP_TAG_OFFSET + len(_WEBP_TAG)

#: Extension -> the content type ``/api/file-raw`` will independently re-derive when
#: it serves the file back. Kept here only so the upload response can name the type
#: it accepted; nothing trusts it later.
CONTENT_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def sniff_image_ext(data: bytes) -> Optional[str]:
    """The canonical extension for *data*, or ``None`` if it is not an image we take.

    ``None`` is the answer for an empty body, a truncated header, a text file
    renamed to ``.png``, an SVG, a PDF, and anything else unrecognised — the
    refusal is the default rather than a special case.
    """
    if not data:
        return None
    for prefix, ext in _IMAGE_SIGNATURES:
        if data.startswith(prefix):
            return ext
    if (
        data.startswith(_RIFF_PREFIX)
        and data[_WEBP_TAG_OFFSET : _WEBP_TAG_OFFSET + len(_WEBP_TAG)] == _WEBP_TAG
    ):
        return ".webp"
    return None


def format_elapsed(seconds: float) -> str:
    """``mm:ss``, or ``h:mm:ss`` past an hour — the alt text for a pasted image.

    The elapsed time is what makes a pasted screenshot useful later: it is how a
    reader lines the image up against the transcript. Negative or non-finite input
    (a clock that moved backwards) collapses to ``0:00`` rather than rendering a
    minus sign into the note.
    """
    try:
        total = int(seconds)
    except (TypeError, ValueError, OverflowError):
        return "0:00"
    if total < 0:
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
