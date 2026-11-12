"""Protection masks: the Identifier VALUES a caller may hide from a later shape-based pass.

``protect(text)`` returns the redacted text, the resolved hits and the masks. A mask is
``(start, end, rule_id)`` over the input text. Masks are in text order, never overlap
each other, and never overlap a Secret-tier hit.

- The row's ``mask`` field decides how much of an Identifier hit is masked: ``value``
  the whole hit (a labelled value, a public parameter's value, a key ID, a
  placeholder); ``none`` nothing (a whole URL is a container, and an ARN is left to
  main's own ARN handling); ``sha_segment`` only the SHA segment of a commit
  permalink; ``image_payload`` a data URI's payload only when its media type is
  ``image/*``.
- A span that overlaps any Secret-tier hit a detector found is never masked. This is
  checked against the hits before resolve, so a Secret hit that resolve rule 1 dropped
  inside a structural Identifier still keeps its span unmasked.
- Parameter rows never list a credential parameter (``table.NEVER_MASK_PARAMS``), so
  ``client_secret``, ``code`` and the tokens are never masked.
"""

from __future__ import annotations

import bisect

from .charsets import cached
from .engine import render, resolve, scan
from .model import IDENTIFIER, SECRET, Hit
from .table import TABLE, Table

Mask = tuple[int, int, str]


def _modes(table: Table) -> dict[str, str]:
    return {row.id: row.mask for row in table.structures}


def _sha_segment(text: str, h: Hit) -> tuple[int, int] | None:
    """``scheme://host/owner/repo/(commit|blob|tree)/<sha>``: the ``<sha>`` segment."""
    q = text.find("://", h.start, h.end)
    if q == -1:
        return None
    pos = q + 3
    for _ in range(4):
        slash = text.find("/", pos, h.end)
        if slash == -1:
            return None
        pos = slash + 1
    end = text.find("/", pos, h.end)
    end = h.end if end == -1 else end
    return (pos, end) if end > pos else None


_IMAGE_TYPE = "image/"
#: Bitmap subtypes only. ``svg+xml`` (and any other image type) is a text document a
#: credential can sit in, so its payload is read by the later passes like any text.
_BITMAP_SUBTYPES = frozenset({"png", "jpeg", "jpg", "gif", "webp", "avif", "bmp"})
_DATA = "data:"
#: How far before a payload its ``data:`` header can start: a media type, up to eight
#: parameters and ``;base64,`` (the data URI row's own bounds).
_HEADER_REACH = 1024


def _image_payload(text: str, h: Hit) -> tuple[int, int] | None:
    """A data URI's payload when its media type is a bitmap image; ``None`` otherwise."""
    lo = max(0, h.start - _HEADER_REACH)
    window = text[lo : h.start].lower()
    head = window.rfind(_DATA)
    if head == -1:
        return None
    mime = head + len(_DATA)
    if window[mime : mime + len(_IMAGE_TYPE)] != _IMAGE_TYPE:
        return None
    sub = mime + len(_IMAGE_TYPE)
    end = sub
    while end < len(window) and window[end] not in ";,":
        end += 1
    if window[sub:end] not in _BITMAP_SUBTYPES:
        return None
    return h.start, h.end


#: Base64 characters decoded to read a bitmap's fixed header (24 bytes).
_HEAD_B64 = 32
#: JPEG markers that may follow ``FF D8 FF``: an APPn segment, a quantisation or
#: Huffman table, or a start of frame.
_JPEG_MARKERS = frozenset(range(0xE0, 0xF0)) | {0xDB, 0xC4, 0xC0, 0xC2}
#: BMP DIB header sizes (BITMAPCOREHEADER through BITMAPV5HEADER).
_BMP_DIB_SIZES = frozenset({12, 40, 52, 56, 64, 108, 124})


def well_formed_bitmap_head(text: str, start: int, end: int) -> bool:
    """Whether the base64 payload ``text[start:end]`` decodes to one complete bitmap file.

    A file signature, or a format's whole fixed header, is a few base64 characters
    anyone can type in front of a secret, so the entire payload is decoded and its
    structure walked as that format lays out a whole file: a PNG's chunks from
    ``IHDR`` through image data to ``IEND``, each with its CRC and nothing after; a
    JPEG's marker segments, each with its length, through a frame and a scan to
    ``EOI``; a GIF's blocks and sub-blocks through an image to its trailer; a WebP's
    RIFF chunks, sized to the file, holding a VP8 frame; a BMP's pixel rows filling
    the file. Linear in the payload. What none of these formats checks is the
    compressed or raw pixel data inside a frame, scan or image block.
    """
    import base64
    import binascii

    payload = text[start:end]
    if len(payload) < _HEAD_B64:
        return False
    try:
        b = base64.b64decode(payload + "=" * (-len(payload) % 4), validate=True)
    except (binascii.Error, ValueError):
        return False
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return b[8:16] == b"\x00\x00\x00\x0dIHDR" and _complete_png(b)
    if b.startswith(b"\xff\xd8\xff"):
        return b[3] in _JPEG_MARKERS and _complete_jpeg(b)
    if b.startswith((b"GIF87a", b"GIF89a")):
        return _complete_gif(b)
    if b.startswith(b"RIFF"):
        return (
            b[8:12] == b"WEBP"
            and int.from_bytes(b[4:8], "little") + 8 == len(b)
            and (_complete_webp(b))
        )
    if b.startswith(b"BM"):
        return int.from_bytes(b[14:18], "little") in _BMP_DIB_SIZES and _complete_bmp(b)
    return False


def _complete_png(b: bytes) -> bool:
    """Whether *b* is a PNG whose every chunk's CRC holds, with image data, ending at ``IEND``."""
    import zlib

    i, n = 8, len(b)
    idat = False
    while i + 12 <= n:
        length = int.from_bytes(b[i : i + 4], "big")
        stop = i + 12 + length
        if stop > n or zlib.crc32(b[i + 4 : stop - 4]) != int.from_bytes(b[stop - 4 : stop], "big"):
            return False
        kind = b[i + 4 : i + 8]
        if kind == b"IEND":
            return idat and stop == n
        idat = idat or kind == b"IDAT"
        i = stop
    return False


#: JPEG start-of-frame markers (``C4`` DHT, ``C8`` reserved and ``CC`` DAC are not frames).
_JPEG_FRAMES = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _complete_jpeg(b: bytes) -> bool:
    """Whether *b* is a JPEG whose marker segments chain from ``SOI`` to a final ``EOI``.

    Each segment's length must land on the next marker; a frame and a scan must come
    before ``EOI``. A scan's coded data runs to the next marker that is not a
    stuffed ``FF 00`` or a restart marker.
    """
    i, n = 2, len(b)
    frame = scan = False
    while i + 1 < n:
        if b[i] != 0xFF:
            return False
        while i + 1 < n and b[i + 1] == 0xFF:  # fill bytes before a marker
            i += 1
        if i + 1 >= n:
            return False
        marker = b[i + 1]
        if marker == 0xD9:
            return frame and scan and i + 2 == n
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        if marker in (0x00, 0xD8) or i + 4 > n:
            return False
        length = int.from_bytes(b[i + 2 : i + 4], "big")
        if length < 2 or i + 2 + length > n:
            return False
        frame = frame or marker in _JPEG_FRAMES
        i += 2 + length
        if marker == 0xDA:
            if not frame:
                return False
            scan = True
            i = _jpeg_scan_end(b, i)
            if i < 0:
                return False
    return False


def _jpeg_scan_end(b: bytes, i: int) -> int:
    """Where a scan's coded data starting at *i* ends (its next marker), or -1."""
    n = len(b)
    while True:
        i = b.find(b"\xff", i)
        if i < 0 or i + 1 >= n:
            return -1
        nxt = b[i + 1]
        if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
            i += 2
            continue
        return i


def _gif_sub_blocks(b: bytes, i: int) -> int:
    """The offset just past a chain of GIF data sub-blocks starting at *i*, or -1."""
    n = len(b)
    while i < n:
        size = b[i]
        i += 1 + size
        if size == 0:
            return i
    return -1


def _complete_gif(b: bytes) -> bool:
    """Whether *b* is a GIF whose blocks chain from its screen descriptor to a final trailer.

    Each extension and image block's sub-blocks must chain to their terminator, and at
    least one image must come before the trailer.
    """
    n = len(b)
    if n < 14:
        return False
    flags = b[10]
    i = 13 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0)
    image = False
    while i < n:
        block = b[i]
        if block == 0x3B:
            return image and i + 1 == n
        if block == 0x21:
            if i + 2 > n:
                return False
            i = _gif_sub_blocks(b, i + 2)
        elif block == 0x2C:
            if i + 11 > n:
                return False
            local = b[i + 9]
            i += 10 + (3 << ((local & 7) + 1) if local & 0x80 else 0)
            if i >= n or not 2 <= b[i] <= 8:  # LZW minimum code size
                return False
            i = _gif_sub_blocks(b, i + 1)
            image = True
        else:
            return False
        if i < 0:
            return False
    return False


def _complete_webp(b: bytes) -> bool:
    """Whether *b*'s RIFF chunks tile the file exactly and carry a VP8 or VP8L frame.

    A ``VP8 `` frame opens with a key frame's start code, a ``VP8L`` one with its
    signature byte; a ``VP8X`` file must hold such a frame or an animation.
    """
    i, n = 12, len(b)
    first = True
    frame = False
    while i < n:
        if i + 8 > n:
            return False
        kind = b[i : i + 4]
        size = int.from_bytes(b[i + 4 : i + 8], "little")
        data = i + 8
        if data + size > n:
            return False
        if kind == b"VP8 ":
            if size < 10 or b[data + 3 : data + 6] != b"\x9d\x01\x2a":
                return False
            frame = True
        elif kind == b"VP8L":
            if size < 5 or b[data] != 0x2F:
                return False
            frame = True
        elif kind in (b"ANIM", b"ANMF"):
            frame = True
        elif first and kind != b"VP8X":
            return False
        first = False
        i = data + size + (size & 1)
    return frame and i == n


#: BMP compressions whose pixel rows are stored uncompressed (``BI_RGB``,
#: ``BI_BITFIELDS``, ``BI_ALPHABITFIELDS``), so their size follows from the header.
_BMP_RAW = frozenset({0, 3, 6})


def _complete_bmp(b: bytes) -> bool:
    """Whether *b* is an uncompressed BMP whose header, palette and pixel rows fill the file.

    The file size field and the pixel data offset must agree with the file, and the
    rows the header describes must end where the file does (two pad bytes allowed).
    A compressed BMP has no size the header fixes, so it is not read as complete.
    """
    n = len(b)
    if n < 26 or int.from_bytes(b[2:6], "little") != n:
        return False
    offset = int.from_bytes(b[10:14], "little")
    dib = int.from_bytes(b[14:18], "little")
    if dib == 12:
        width = int.from_bytes(b[18:20], "little")
        height = int.from_bytes(b[20:22], "little")
        bpp = int.from_bytes(b[24:26], "little")
        compression = 0
    else:
        if n < 14 + dib or dib < 40:
            return False
        width = int.from_bytes(b[18:22], "little", signed=True)
        height = abs(int.from_bytes(b[22:26], "little", signed=True))
        bpp = int.from_bytes(b[28:30], "little")
        compression = int.from_bytes(b[30:34], "little")
    if compression not in _BMP_RAW or bpp not in (1, 4, 8, 16, 24, 32) or width <= 0:
        return False
    if not 14 + dib <= offset <= n:
        return False
    rows = ((bpp * width + 31) // 32) * 4 * height
    return 0 <= n - (offset + rows) <= 2


def _merged(spans: list[tuple[int, int]]) -> tuple[list[int], list[int]]:
    starts: list[int] = []
    ends: list[int] = []
    for s, e in sorted(spans):
        if ends and s < ends[-1]:
            ends[-1] = max(ends[-1], e)
        else:
            starts.append(s)
            ends.append(e)
    return starts, ends


def masks_for(text: str, hits: list[Hit], resolved: list[Hit], table: Table = TABLE) -> list[Mask]:
    """Masks for ``resolved`` Identifier hits; ``hits`` are the raw hits before resolve."""
    modes = cached(table, _modes)
    starts, ends = _merged([(h.start, h.end) for h in hits if h.tier == SECRET])

    def touches_secret(s: int, e: int) -> bool:
        i = bisect.bisect_right(starts, e - 1) - 1
        return i >= 0 and ends[i] > s

    out: list[Mask] = []
    last = -1
    for h in resolved:
        if h.tier != IDENTIFIER:
            continue
        mode = modes.get(h.rule_id, "value")
        if mode == "none":
            continue
        span: tuple[int, int] | None = (h.start, h.end)
        if mode == "sha_segment":
            span = _sha_segment(text, h)
        elif mode == "image_payload":
            span = _image_payload(text, h)
        if span is None or span[0] < last or touches_secret(*span):
            continue
        out.append((span[0], span[1], h.rule_id))
        last = span[1]
    return out


def protect(text: str, table: Table = TABLE) -> tuple[str, list[Hit], list[Mask]]:
    """``(redacted_text, resolved_hits, masks)`` for whole text."""
    hits = scan(text, final=True, table=table).hits
    if not hits:
        return text, [], []
    resolved = resolve(hits)
    return render(text, resolved, 0, len(text)), resolved, masks_for(text, hits, resolved, table)
