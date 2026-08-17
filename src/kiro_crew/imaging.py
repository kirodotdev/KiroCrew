"""Inline-image budget: downscale image bytes to fit the backend's caps.

The owning module for the per-image limits kiro-cli's backend enforces on
every inlined image, and the Pillow machinery that renders arbitrary image
bytes compliant with them. Two consumers share it:

* :mod:`kiro_crew.acp.prompt_blocks` -- the ``session/prompt`` path (every
  channel's image attachments).
* :mod:`kiro_crew.mcp_gateway.image_budget` -- the gateway relay's rewrite of
  image content blocks in brokered MCP tool results.

This module is deliberately a LEAF: it imports nothing from ``kiro_crew.acp``
or ``kiro_crew.mcp_gateway``, because the gateway daemon (``gatewayd``, a
sidecar process respawned by a supervisor) imports it at start via
``mcp_gateway.backend``. Importing the budget from ``acp.prompt_blocks``
there would execute ``acp/__init__``, pulling the whole ACP client into the
broker and closing an import cycle back into ``mcp_gateway`` -- an inverted
dependency that only loads because Python tolerates importing a submodule of
a partially-initialized package.

Pillow is loaded lazily, on the first call that needs it, for the same
reason: a daemon respawn loop must not pay a Pillow import per cycle when no
image block has ever arrived.
"""

from __future__ import annotations

import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Longest-edge cap (px) enforced on every inlined image. Anthropic rejects the
#: ENTIRE request when a many-image conversation (more than 20 images) carries
#: any image whose width or height exceeds 2000px. Because kiro-cli replays the
#: full message history to the model every turn, a single oversized image
#: permanently wedges the session -- the offending block sits at a fixed history
#: index and re-uploading a smaller copy cannot evict it. 2000 is the hard limit
#: itself: valid even past 20 images, yet it keeps more detail than the browser's
#: 1568px pre-upload downscale, which only ever covers dashboard uploads.
MAX_IMAGE_EDGE_PX = 2000

#: Longest base64-encoded payload (bytes) allowed for a single inlined image.
#: The dimension cap above is not sufficient: a raster can sit well inside 2000px
#: and still encode past the backend's per-image byte ceiling.
#:
#: 5 MiB is the value the backend itself reports. It is not derived from which
#: provider kiro-cli happens to route through, which we treat as opaque, but read
#: straight out of its rejection, which names the limit in bytes:
#:
#:     image exceeds 5 MB maximum: 6714372 bytes > 5242880
#:
#: 5242880 is exactly 5 * 1024 * 1024. (Anthropic's published per-image ceiling
#: for Bedrock and Google Cloud agrees, which is corroboration rather than the
#: basis.) base64 inflates by 4/3, so a ~3.9 MiB raster already exceeds it while
#: passing every pre-encode check.
#:
#: This is enforced on the ENCODED payload, after any downscale, because that is
#: the only quantity the backend measures. Being wrong here is safe in one
#: direction only, which is why the cap is set to the observed value rather than
#: a guess: too LOW merely ships a smaller image, while too HIGH ships a payload
#: the backend refuses -- and a rejected image sits at a fixed history index that
#: kiro-cli replays on every subsequent turn, so one oversized payload wedges
#: the session permanently. Callers can override per call if a backend ever
#: reports a different number.
MAX_IMAGE_B64_BYTES = 5 * 1024 * 1024

#: Floor for the encoded-budget shrink loop. Below ~200px Claude's own guidance
#: says accuracy degrades badly, so an image that still will not fit is dropped
#: (the caller falls back to a path reference or a text block) instead of being
#: shrunk into uselessness.
MIN_IMAGE_EDGE_PX = 256

#: Ceiling on the SOURCE pixel count (width x height) this module will decode.
#: The header read that checks it costs no decode, but every downscale pass
#: does a full decode at ~4 bytes per pixel (RGBA), so the source area -- not
#: the byte size -- is what bounds memory and CPU. Pillow's own
#: decompression-bomb guard only hard-errors above ~357M pixels (2x its
#: default), which is ~1.4 GiB of pixel data: a highly compressed
#: small-bytes/huge-area raster below that ladder would otherwise pin an
#: executor worker for seconds and spike memory inside the shared gateway
#: daemon. 64M pixels bounds a single decode at ~256 MiB peak while clearing
#: every real tool-result image: an 8K display frame is 33.2M pixels and
#: current 61MP cameras sit just under. An image over this ceiling has no
#: compliant rendition (fail closed) -- a genuine >64MP source would be
#: downscaled past recognition to fit 2000px anyway.
MAX_IMAGE_SOURCE_PIXELS = 64_000_000

#: Per-attempt edge multiplier and attempt cap for the encoded-budget shrink.
#: Encoded size falls roughly with area, so 0.8 on the edge sheds ~36% per pass
#: and 6 passes span a 4x linear reduction -- enough to bring any image that the
#: dimension cap admitted under a 5 MiB encoding.
_ENCODE_SHRINK_FACTOR = 0.8
_MAX_ENCODE_ATTEMPTS = 6

#: mime -> Pillow save format for a re-encoded downscale. GIF collapses to a PNG
#: first frame: vision models read frame 0 only (animation is invisible to them)
#: and rescaling a palette image is lossy, so a lossless still is faithful.
#: A mime OUTSIDE this table (e.g. a gateway-relayed ``image/tiff``) also
#: re-encodes as PNG, and the emitted mime tracks the format actually written
#: via ``_FORMAT_MIME`` -- labeling PNG bytes with the source mime would ship a
#: self-contradictory block.
_PIL_SAVE_FORMAT: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
    "image/bmp": "BMP",
    "image/gif": "PNG",
}

#: Pillow save format -> the mime the re-encoded bytes really are. The inverse
#: direction of ``_PIL_SAVE_FORMAT``, keyed by what was WRITTEN (or, on the
#: pass-through branch, what the header DETECTED), so the emitted mimeType is
#: correct even when the source mime had no dedicated save format -- or lied.
#: GIF appears only for the pass-through direction: a re-encode never writes GIF.
_FORMAT_MIME: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "GIF": "image/gif",
}

# Lazy Pillow handle: unset until the first probe, then (Image, ImageOps) or
# None when Pillow is absent. Pillow ships as a hard dependency (via
# qrcode[pil]/pdfplumber); the probe only keeps a hand-stripped install from
# crashing at import, and the laziness keeps ~80ms of Pillow import off the
# gateway daemon's start when no image ever arrives.
_pil_checked = False
_pil_handle: tuple[Any, Any] | None = None


def _pil() -> tuple[Any, Any] | None:
    """Return ``(Image, ImageOps)``, importing Pillow on first use, or ``None``.

    Benign under races: the interpreter serializes the import itself and the
    cached assignment is idempotent.
    """
    global _pil_checked, _pil_handle
    if not _pil_checked:
        try:
            # Function-local import (documented): this leaf module is imported
            # by the gateway daemon at start; Pillow is only needed once an
            # image block actually arrives. See the module docstring.
            from PIL import Image, ImageOps

            _pil_handle = (Image, ImageOps)
        except ImportError:  # pragma: no cover - Pillow is a hard dependency
            _pil_handle = None
        _pil_checked = True
    return _pil_handle


def pil_available() -> bool:
    """Whether Pillow can be loaded -- i.e. whether dimensions are verifiable.

    Callers that must fail CLOSED on unverifiable images (the gateway relay)
    check this before trusting a pass-through result.
    """
    return _pil() is not None


def _downscale_within_limits(raw_bytes: bytes, mime: str, max_edge: int) -> tuple[bytes, str] | None:
    """Downscale *raw_bytes* so its longest edge is <= *max_edge*, keeping aspect.

    Returns ``(bytes, mime)`` -- the ORIGINAL pair when the image is already
    within the cap AND in a format the backend accepts, the downscaled or
    format-converted pair otherwise. The mime can change (``image/gif`` ->
    ``image/png``, and any format outside the known-good table converts to
    PNG at original dimensions) because the re-encode picks a still format
    the backend accepts.

    Returns ``None`` when the image is oversized (or its size is unknowable) but
    a compliant copy could NOT be produced -- an undecodable file, or a Pillow
    decompression-bomb rejection on a huge-but-small-bytes raster. The caller
    must then NOT inline it: shipping the un-shrunk original is precisely the
    >2000px payload that poisons the session, so a failure fails CLOSED (leave
    the path as text) rather than open. The no-Pillow pass-through only keeps
    image support alive on a hand-stripped install where dimensions cannot be
    verified at all.
    """
    pil = _pil()
    if pil is None or max_edge <= 0:
        return raw_bytes, mime
    image_mod, imageops_mod = pil
    try:
        with image_mod.open(io.BytesIO(raw_bytes)) as img:
            # Header read only for the dimension check -- no decompression --
            # so a within-cap image never pays for a decode.
            detected = img.format or ""
            if max(img.width, img.height) <= max_edge and detected in _FORMAT_MIME:
                # Two conditions guard the byte-identical fast path. Dimension:
                # over-cap images must shrink. Format: bytes in a format
                # OUTSIDE the known-good table (e.g. TIFF from an MCP server)
                # would enter the replayed history as a payload the backend
                # rejects -- the same permanent wedge as an oversized image --
                # so they fall through to the re-encode below instead.
                # And NEVER pass bytes through unverified: a truncated file
                # with a readable header would otherwise ride through
                # byte-identical and enter the conversation history as a
                # corrupt block. verify() walks the container structure
                # (checksums, markers) without a full pixel decode and raises
                # on truncation, landing in the fail-closed handler below.
                # The emitted mime is the header-DETECTED format's (the
                # caller's claim can lie); captured before verify(), which
                # invalidates the reader.
                img.verify()
                return raw_bytes, _FORMAT_MIME[detected]
            # Bake EXIF orientation into the pixels BEFORE resizing: the re-encode
            # (PNG in particular) drops the orientation tag, so a phone photo
            # would otherwise reach the model rotated.
            oriented = imageops_mod.exif_transpose(img) or img
            # Palette frames upsample poorly; promote to RGBA so the resample
            # interpolates real channels rather than palette indices.
            src = oriented.convert("RGBA") if oriented.mode in ("P", "PA") else oriented
            long_edge = max(src.width, src.height)
            if long_edge > max_edge:
                scale = max_edge / long_edge
                new_size = (max(1, round(src.width * scale)), max(1, round(src.height * scale)))
                resample = getattr(image_mod, "LANCZOS", getattr(image_mod, "ANTIALIAS", 1))
                resized = src.resize(new_size, resample)
            else:
                # Within the cap but in a format outside the known-good table:
                # a format conversion, not a resize.
                resized = src
            fmt = _PIL_SAVE_FORMAT.get(mime, "PNG")
            if fmt in ("JPEG", "BMP") and resized.mode not in ("RGB", "L"):
                # JPEG/BMP carry no alpha channel; flatten before encoding.
                resized = resized.convert("RGB")
            buf = io.BytesIO()
            if fmt == "JPEG":
                resized.save(buf, format="JPEG", quality=90)
            elif fmt == "WEBP":
                # A source WEBP may be lossless; PIL defaults to lossy q80, so
                # stay lossless to avoid re-encode artifacts on the downscale.
                resized.save(buf, format="WEBP", lossless=True)
            else:
                resized.save(buf, format=fmt)
            out_mime = _FORMAT_MIME.get(fmt, "image/png")
            return buf.getvalue(), out_mime
    except Exception:
        # Fail CLOSED: an image we could not shrink -- or could not verify as
        # structurally intact -- must not be inlined, or the payload reaches
        # the model's replayed history and poisons the session.
        logger.warning(
            "imaging: image failed downscale or integrity verification (mime=%s); dropping",
            mime,
            exc_info=True,
        )
        return None


def _b64_len(raw_len: int) -> int:
    """Exact base64 length for *raw_len* bytes (4 chars per 3-byte group, padded)."""
    return 4 * ((raw_len + 2) // 3)


def image_dimensions(raw_bytes: bytes) -> tuple[int, int] | None:
    """``(width, height)`` of *raw_bytes*, or ``None`` if it cannot be read.

    Header-only read -- no decompression -- so this is cheap enough to call
    before every decode decision. Public: the gateway's per-frame decode
    budget accounts source pixels with it before committing to a decode.
    """
    pil = _pil()
    if pil is None:
        return None
    image_mod = pil[0]
    try:
        with image_mod.open(io.BytesIO(raw_bytes)) as img:
            return img.width, img.height
    except Exception:
        return None


def _long_edge(raw_bytes: bytes) -> int | None:
    """Longest edge of *raw_bytes* in px, or ``None`` if it cannot be read."""
    dims = image_dimensions(raw_bytes)
    return None if dims is None else max(dims)


def _fit_encoded_budget(
    raw_bytes: bytes, mime: str, max_edge: int, max_b64_bytes: int
) -> tuple[bytes, str] | None:
    """Render *raw_bytes* so it obeys BOTH the dimension and encoded-size caps.

    Applies the dimension cap first, then keeps shrinking while the base64
    encoding still exceeds *max_b64_bytes*. Returns the ``(bytes, mime)`` pair to
    inline, or ``None`` when no compliant rendition exists -- the caller must then
    not inline anything, since a payload the backend rejects poisons every later
    turn in the session.

    The shrink target is derived from the rendition's OWN long edge rather than
    from *max_edge*: :func:`_downscale_within_limits` is a deliberate no-op for an
    image already inside the cap, so an oversized-but-small-dimension file (the
    exact case that trips the 5 MiB ceiling) would otherwise loop without ever
    making progress. Each pass re-decodes the ORIGINAL bytes on purpose --
    resampling a resample compounds quality loss -- and the source-pixel ceiling
    enforced by :func:`downscale_image_block` is what bounds that per-pass cost.
    """
    fitted = _downscale_within_limits(raw_bytes, mime, max_edge)
    if fitted is None:
        return None
    out_bytes, out_mime = fitted
    if max_b64_bytes <= 0 or _b64_len(len(out_bytes)) <= max_b64_bytes:
        return out_bytes, out_mime

    edge = _long_edge(out_bytes)
    if edge is None:
        # Dimensions unknown (no Pillow, or an undecodable raster): the encoded
        # size is known to be over budget and cannot be reduced, so fail closed.
        return None
    for _ in range(_MAX_ENCODE_ATTEMPTS):
        edge = max(MIN_IMAGE_EDGE_PX, int(edge * _ENCODE_SHRINK_FACTOR))
        fitted = _downscale_within_limits(raw_bytes, mime, edge)
        if fitted is None:
            return None
        out_bytes, out_mime = fitted
        if _b64_len(len(out_bytes)) <= max_b64_bytes:
            return out_bytes, out_mime
        if edge <= MIN_IMAGE_EDGE_PX:
            break
    return None


def downscale_image_block(
    data: bytes,
    mime: str,
    *,
    max_edge: int = MAX_IMAGE_EDGE_PX,
    max_b64_bytes: int = MAX_IMAGE_B64_BYTES,
) -> tuple[bytes, str] | None:
    """Fit arbitrary image bytes inside the inline-image budget.

    The stable public entry point over :func:`_fit_encoded_budget`. Both hard
    limits kiro-cli's backend enforces per inlined image apply: the longest
    edge is capped at *max_edge* px and the base64 encoding of the result at
    *max_b64_bytes*. An image already inside both caps is returned
    byte-identical (header-only inspection, no re-encode).

    A source whose pixel count exceeds :data:`MAX_IMAGE_SOURCE_PIXELS`
    (resolved at call time, so tests can patch the module constant) is refused
    BEFORE any decode: the check reads only the header, so an adversarially
    compressed small-bytes/huge-area raster costs nothing to reject.

    Returns the ``(bytes, mime)`` pair to inline -- the mime can change
    (``image/gif`` -> ``image/png``) -- or ``None`` when no compliant rendition
    exists. A ``None`` MUST fail closed: the caller must not inline the
    original, because a rejected image sits at a fixed history index that
    kiro-cli replays on every later turn, wedging the session permanently.
    """
    limit = MAX_IMAGE_SOURCE_PIXELS
    dims = image_dimensions(data)
    if dims is not None and limit > 0 and dims[0] * dims[1] > limit:
        logger.warning(
            "imaging: refusing to decode %dx%d image (%d px > %d px source ceiling)",
            dims[0],
            dims[1],
            dims[0] * dims[1],
            limit,
        )
        return None
    return _fit_encoded_budget(data, mime, max_edge, max_b64_bytes)
