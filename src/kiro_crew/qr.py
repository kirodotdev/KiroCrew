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

#: Quiet-zone width, in modules. The spec's minimum is 4; 2 is enough for a
#: screen-displayed code a phone camera reads from a lit panel, and it keeps the
#: image compact enough to sit in a dashboard card without scaling.
_QR_BORDER = 2

#: Pixels per module. 8 keeps a URL-length payload readable by a phone held at a
#: normal distance from a laptop screen.
_QR_BOX_SIZE = 8


def render_qr_data_uri(payload: str) -> str:
    """Encode *payload* as a PNG ``data:`` URI.

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

    qr = qrcode.QRCode(border=_QR_BORDER, box_size=_QR_BOX_SIZE)
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
