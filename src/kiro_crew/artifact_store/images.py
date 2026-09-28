"""Raster images stored as ``kind="image"`` artifacts: the mime allowlist and header sniffing.

``_IMAGE_MIME_EXT`` is both the create-time allowlist and the table every read
re-derives the sidecar extension from, so a record naming any other mime is
refused on the way in and on the way out. The dimension sniffers read only the
few header bytes each format keeps its size in, with the standard library alone.
"""

from __future__ import annotations

#: Raster image mime → sidecar file extension. This IS the create-time
#: allowlist for image artifacts: a mime not present here is rejected. SVG is
#: deliberately absent — it is markup (stored as ``kind="svg"`` text), not a
#: raster asset, and serving attacker-authored SVG as an image is an XSS vector.
_IMAGE_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
}


def _sniff_image_dimensions(data: bytes, mime: str) -> tuple[int | None, int | None]:
    """Best-effort natural (width, height) from a raster file header.

    Pure stdlib, no decode, no third-party dependency (no Pillow): it reads only
    the few header bytes each format puts its dimensions in. Any parse failure —
    truncated file, unexpected layout, an exotic encoding — returns
    ``(None, None)`` rather than raising, because an unmeasured image is still a
    perfectly storable one; dimensions are a rendering nicety, not a gate.
    """
    try:
        if mime == "image/png":
            # 8-byte signature, then the IHDR chunk (len+type) at 8..16, with
            # width/height as big-endian uint32 immediately after the type.
            if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
                return (
                    int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"),
                )
        elif mime == "image/gif":
            # Logical-screen descriptor: width/height as little-endian uint16.
            if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
                return (
                    int.from_bytes(data[6:8], "little"),
                    int.from_bytes(data[8:10], "little"),
                )
        elif mime == "image/jpeg":
            return _sniff_jpeg_dimensions(data)
        elif mime == "image/webp":
            return _sniff_webp_dimensions(data)
        elif mime == "image/bmp":
            # BITMAPINFOHEADER: signed little-endian int32 width at 18 and
            # height at 22. A negative height means a top-down bitmap, so take
            # the magnitude rather than reporting a negative dimension.
            if len(data) >= 26 and data[:2] == b"BM":
                width = int.from_bytes(data[18:22], "little", signed=True)
                height = int.from_bytes(data[22:26], "little", signed=True)
                if width and height:
                    return abs(width), abs(height)
    except Exception:  # pragma: no cover — sniffing must never raise
        return None, None
    return None, None


def _sniff_jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Walk JPEG marker segments to the frame header (SOFn) for dimensions."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None, None
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # Padding fill bytes and standalone markers (SOI/EOI/RSTn/TEM) carry no
        # length field — step over them without reading a segment length.
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
        # SOF0..SOF15 hold the frame dimensions; exclude the non-frame C-markers
        # DHT (0xC4), JPG (0xC8) and DAC (0xCC).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return width, height
        if seg_len < 2:
            return None, None  # malformed length — stop rather than loop
        i += 2 + seg_len
    return None, None


def _sniff_webp_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Dimensions for the three WebP chunk layouts (VP8 / VP8L / VP8X)."""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None, None
    chunk = data[12:16]
    if chunk == b"VP8 ":
        # Lossy: 3-byte start code 0x9d012a, then two little-endian 14-bit dims.
        if data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return width, height
    elif chunk == b"VP8L":
        # Lossless: 0x2f signature, then 14-bit (width-1) and (height-1) packed
        # across the next four bytes.
        if data[20] == 0x2F:
            b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
            width = ((b1 & 0x3F) << 8 | b0) + 1
            height = ((b3 & 0x0F) << 10 | b2 << 2 | (b1 & 0xC0) >> 6) + 1
            return width, height
    elif chunk == b"VP8X":
        # Extended: 24-bit little-endian (canvas dim - 1) at bytes 24 and 27.
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None, None
