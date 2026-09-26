"""Scannability of the shared QR encoder (``kiro_crew.qr``).

Decoders expect the spec's 4-module quiet zone around a QR code, and a module
drawn as a whole number of screen pixels stays sharp. The phone-connect code runs
to about 80 modules, so its module size also decides whether it fits the connect
dialog unscaled. These tests pin that geometry.
"""

from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from kiro_crew import qr


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# An access URL built the way the dashboard builds one: a long MagicDNS host and a
# token of base64url(JSON claims) "." base64url(HMAC-SHA256), carrying the
# boot-bound claims plus a carried no_refresh. A real token's mixed-case characters
# force the encoder's byte mode; a stand-in of one repeated capital letter would
# encode in alphanumeric mode and understate the symbol's size.
_CLAIMS = {
    "sub": "local-app",
    "exp": 1790000300.1234567,
    "session_exp": 1790003600.1234567,
    "iat": 1790000000.1234567,
    "nonce": "0123456789abcdef",
    "gen": 3,
    "boot": "0123456789abcdef0123456789abcdef",
    "no_refresh": "1",
}
_TOKEN = (
    _b64url(json.dumps(_CLAIMS, separators=(",", ":")).encode()) + "." + _b64url(bytes(range(32)))
)
_ACCESS_URL = f"https://workstation-macbook-pro-16in-2024.tail3f2a91.ts.net/?token={_TOKEN}"

# The QR's box inside the connect dialog: 440px wide including 24px of padding and
# a 1px border on each side, less the white frame's 10px padding.
_QR_BOX_WIDTH_PX = 440 - 2 * 24 - 2 * 1 - 2 * 10


def _decode(uri: str) -> Image.Image:
    assert uri.startswith("data:image/png;base64,")
    return Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))).convert("L")


def _first_dark_offset(img: Image.Image) -> int:
    """Pixel offset of the first dark pixel on the image's diagonal."""
    for i in range(img.size[0]):
        if img.getpixel((i, i)) < 128:
            return i
    raise AssertionError("no dark pixel on the diagonal")


@pytest.mark.parametrize("box_size", [4, 8])
def test_quiet_zone_is_the_spec_minimum_of_four_modules(box_size: int) -> None:
    img = _decode(qr.render_qr_data_uri(_ACCESS_URL, box_size=box_size))
    # The top-left finder pattern starts right after the quiet zone.
    assert _first_dark_offset(img) == 4 * box_size


def test_box_size_sets_whole_pixels_per_module() -> None:
    small = _decode(qr.render_qr_data_uri(_ACCESS_URL, box_size=4))
    large = _decode(qr.render_qr_data_uri(_ACCESS_URL, box_size=8))
    assert small.size[0] % 4 == 0
    assert large.size == (small.size[0] * 2, small.size[1] * 2)


def test_default_box_size_is_unchanged_for_other_callers() -> None:
    assert qr.render_qr_data_uri("weixin://dl/login?ticket=abc") == qr.render_qr_data_uri(
        "weixin://dl/login?ticket=abc", box_size=8
    )


def test_mobile_access_code_fits_the_dialog_at_natural_size() -> None:
    from kiro_crew.dashboard.handlers.tailnet_mobile import MOBILE_QR_BOX_SIZE

    img = _decode(qr.render_qr_data_uri(_ACCESS_URL, box_size=MOBILE_QR_BOX_SIZE))
    # The dialog shows the image unscaled, so the whole code must fit its box, and
    # each module (MOBILE_QR_BOX_SIZE CSS pixels) must stay big enough for a camera.
    assert img.size[0] <= _QR_BOX_WIDTH_PX
    assert MOBILE_QR_BOX_SIZE == 4
