"""QR encoding: turn a short string into a PNG data URI.

One owner for QR rendering, so the two surfaces that need it — the WeChat login
handshake and tailnet mobile access — cannot drift into two subtly different
encoders. Both want the same thing: a scannable image the dashboard can drop
straight into an ``<img src>`` without a client-side QR library and without a
second HTTP round trip for the image bytes.

``qrcode[pil]`` is a core dependency (``setup.cfg``), but it is imported **lazily
inside the call**. It pulls Pillow, which is a heavy import, and a process that
never renders a QR must not pay for it — the same rule
:mod:`kiro_crew.imaging` follows for the same reason.

Deliberately has **no** knowledge of what it is encoding. It never logs the
payload: a tailnet access URL carries a session token in its query string, so
logging the input here would write a live credential to the gateway log.
"""

from __future__ import annotations

import base64
import io
import logging

logger = logging.getLogger(__name__)

#: Quiet-zone width, in modules. 4 is the spec minimum (ISO/IEC 18004): a decoder
#: needs that light margin to tell the three finder patterns apart from whatever
#: surrounds the image. It is encoded in the image rather than left to the page,
#: which may put too little white space around it.
_QR_BORDER = 4

#: Default pixels per module. 8 keeps a URL-length payload readable by a phone
#: held at a normal distance from a laptop screen.
_QR_BOX_SIZE = 8


def render_qr_data_uri(payload: str, *, box_size: int = _QR_BOX_SIZE) -> str:
    """Encode *payload* as a PNG ``data:`` URI, *box_size* pixels per module.

    A caller that shows the image at its natural size picks *box_size* so the
    whole symbol fits its display box. The browser then draws every module as a
    whole number of screen pixels instead of rescaling the image by a fractional
    factor, which blurs module edges or makes modules uneven.

    Raises whatever the encoder raises (a payload too long for any QR version is
    the realistic case) — the caller decides how to report it, because "the code
    could not be made" means different things on different surfaces. Runs the
    encode synchronously, so callers on the event loop must hand it to a thread.
    """
    # Deferred deliberately, and NOT because it is optional: `qrcode` is a core
    # dependency. It pulls Pillow, which is heavy, and this module is imported by
    # request handlers that load at gateway start -- so a module-scope import would
    # put Pillow on the startup path for every install, including the ones that
    # never render a QR. Kept local with the real reason stated, since the
    # top-level-imports rule is advisory and this is a considered deviation.
    import qrcode  # noqa: PLC0415 - see comment above

    qr = qrcode.QRCode(border=_QR_BORDER, box_size=box_size)
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
