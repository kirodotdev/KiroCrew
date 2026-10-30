"""Image sniffing for images pasted into a meeting note.

The one job here is deciding what a pasted blob actually IS, from its bytes, and
refusing everything else.

The magic table itself is NOT here. :mod:`kiro_crew.messaging.raster` owns it, and
its module docstring gives the reason a second copy must not exist: "a second copy
of the magic table is how one direction ends up accepting a type the other
rejects." What belongs to a note is the ALLOWLIST on top of that shared answer,
which is exactly the per-consumer narrowing ``raster`` reserves for its callers.

Two properties are deliberate and worth not "simplifying":

**The client's filename never reaches a path.** The extension is derived from the
sniffed signature, not from what the browser said the file was called, so the only
strings that can appear in a note-image path are ones this module produced. Compare
``dashboard/handlers/files.py::api_upload_file``, which sanitizes a client filename
and then verifies the magic bytes MATCH the claimed extension — a sound design for
a general-purpose uploader that must preserve names, but for a pasted screenshot
the name is worthless, and not accepting one removes a whole class of question.

**Refusal is the default.** A type missing from the allowlist returns ``None``.
That is what keeps SVG and BMP out — SVG because it has no binary signature to
sniff at all (the core uploader's ``_content_matches_ext`` fails OPEN for ``.svg``,
and an SVG is a document that can carry ``<script>`` and ``on*`` handlers, which is
why ``pptx_maker`` classifies ``image/svg+xml`` as script-capable), and BMP because
nothing produces BMP screenshots. Neither is named below, and omission is the
whole mechanism: a type added to the shared table does not silently widen a note.
"""

from __future__ import annotations

from typing import Optional

from kiro_crew.messaging.raster import SNIFF_BYTES, sniff_raster_mime

#: Sniffed type -> the canonical extension a note stores it under.
#:
#: Deliberately narrower than the shared table: ``image/bmp`` is absent (its ``BM``
#: signature is two bytes, which is weak, and nothing produces BMP screenshots),
#: and every vector or document format is absent from the shared table already.
_EXT_BY_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def sniff_image_ext(data: bytes) -> Optional[str]:
    """The canonical extension for *data*, or ``None`` if it is not an image we take.

    ``None`` is the answer for an empty body, a truncated header, a text file
    renamed to ``.png``, an SVG, a BMP, a PDF, and anything else outside
    :data:`_EXT_BY_MIME` — the refusal is the default rather than a special case.
    """
    if not data:
        return None
    mime = sniff_raster_mime(data[:SNIFF_BYTES])
    if mime is None:
        return None
    return _EXT_BY_MIME.get(mime)


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
