"""Live browse screencast — relay screenshots the agent already takes to the dashboard.

The headless browse Chromium runs on the gateway host; the only window onto
it from a laptop is the dashboard (reachable over the reverse SSH tunnel). This
module gives the dashboard a near-real-time mirror **without opening any debug
port on the browser**.

Design (why this shape):
- The Playwright MCP proxy already intercepts every ``browser_take_screenshot``
  response and re-encodes it to JPEG (``mcp_playwright_proxy._save_screenshot``).
  It additionally POSTs that already-captured frame to the gateway's loopback
  ``/api/browser/frame`` ingress, which rebroadcasts it over the existing WS as a
  ``browser_frame`` event. The ``BrowserLiveView`` panel renders the latest frame.
- This rides Playwright's existing (authenticated, pipe-based) control channel —
  it does **not** add a ``--remote-debugging-port``. A CDP debug port would give
  smoother frames, but it is an unauthenticated, full-control endpoint on an
  auth-cookie-bearing browser (a net-new local-process-takeover surface), so it
  is deliberately not used.
- Cadence is sparse — frames arrive only when the agent itself screenshots. A
  follow-up (an active pump) can inject idle-gated screenshots for a
  steady ~1-2 fps if needed; the WS contract here is unchanged by that.

This module is intentionally tiny: the gateway owns no browser connection, only
the WS rebroadcast. ``build_frame_payload`` is a pure helper so the framing
contract is unit-testable without a live browser or proxy.
"""

from __future__ import annotations

import re
from typing import Any

# WS event name the dashboard BrowserLiveView panel listens for.
BROWSER_FRAME_EVENT = "browser_frame"

# Raster formats only. The dashboard renders frames as ``<img src="data:image/
# {format};base64,...">``; "svg" (image/svg+xml) is deliberately excluded because
# an SVG data URI can carry executable script — this allowlist is the load-bearing
# control that keeps the render XSS-safe, so do NOT add "svg" here.
_ALLOWED_FORMATS = {"jpeg", "png", "webp"}

# Standard base64 charset (+ optional padding). ``data`` must match this exactly:
# it structurally excludes ``:`` (so no ``://`` URL), whitespace, and ``<``/``>``
# (so no HTML/script), which is the right boundary control for an image field —
# far better than running text credential/URL redactors on opaque image bytes.
_B64_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")

# Slot key the frame belongs to (from the proxy's KIROCREW_SESSION_KEY). Opaque
# id used only as a lookup key client-side — the dashboard renders the resolved
# session *title* from its own slot store, never this raw value — but bound it to
# a safe charset/length anyway so the WS payload can't carry arbitrary text.
_SESSION_KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def build_frame_payload(body: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a POSTed frame body into the ``browser_frame`` WS payload.

    ``body`` is the JSON the proxy POSTs to ``/api/browser/frame``:
    ``{"data": "<base64>", "format": "jpeg", "device_width"?, "device_height"?}``.

    Returns the payload dict the dashboard renders, or ``None`` if the body has
    no usable image data (caller should reject with 400). ``data`` is validated
    to the base64 charset at this boundary; no text redaction is applied because
    the field is browser-captured image bytes (not LLM output), and the charset
    check structurally rules out URLs/credentials anyway — unlike
    ``/api/browser-event`` which forwards free-text fields.
    """
    data = body.get("data")
    if not isinstance(data, str) or not data:
        return None
    if not _B64_RE.fullmatch(data):
        return None
    fmt = body.get("format")
    if fmt not in _ALLOWED_FORMATS:
        fmt = "jpeg"
    payload: dict[str, Any] = {"data": data, "format": fmt}
    for dim in ("device_width", "device_height"):
        val = body.get(dim)
        # bool is an int subclass, so {"device_width": true} would pass a bare
        # isinstance(int) check and broadcast device_width=True — which the
        # BrowserLiveView panel treats as 1 in JS aspect/size math (a 1px frame
        # hint). Also bound it to a sane pixel range, matching the module's
        # bound-every-field idiom (format allowlist, _B64_RE, _SESSION_KEY_RE).
        if isinstance(val, int) and not isinstance(val, bool) and 0 < val <= 100_000:
            payload[dim] = val
    # Pass the session key through (bounded) so the panel can label which session
    # it mirrors. The dashboard resolves it to a title from its own slot store.
    sk = body.get("session_key")
    if isinstance(sk, str) and _SESSION_KEY_RE.fullmatch(sk):
        payload["session_key"] = sk
    return payload
