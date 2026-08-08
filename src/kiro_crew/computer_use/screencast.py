"""Computer-use live view (PiP) — relay the frame the agent already captured.

The desktop the agent drives may not be the desktop the operator is sitting at
(a cloud Mac, a headless-ish session over a reverse SSH tunnel, or simply another
Space). The dashboard is the only window onto it, so this module mirrors the
JPEG that :mod:`capture_macos` **already encoded** for the model into a floating
``ComputerUseLiveView`` panel. It adds no capture of its own: no second
``CGWindowListCreateImage`` call, no timer, no full-screen grab. One frame per
tool-call capture, and only when that capture already happened.

Shape (deliberately the ``browser/screencast.py`` shape, for the same reasons):

* :func:`emit_snapshot_frame` POSTs the already-encoded bytes to the gateway's
  loopback ``/api/computer-use/frame`` ingress, which rebroadcasts them to OWNER
  websockets as a ``computer_use_frame`` event.
* :func:`build_frame_payload` is a pure normalizer so the wire contract is
  unit-testable without a window, a driver or a gateway.

**Why an HTTP POST when the capture already runs inside the gateway.** Capture is
executed on a worker thread (``handlers/computer_use._dispatch_off_loop``), and
``DashboardState.deliver_ws_owners``/``broadcast_ws`` are event-loop objects —
``asyncio.ensure_future`` from a non-loop thread raises. The loopback POST is the
thread→loop hop, and it keeps the ``computer_use`` package free of any dashboard
import (the package's "importing it is side-effect free" property).

**Three suppressions, all evaluated here, none of them trusting the caller:**

1. **No ambient scope → no frame.** The identity of the calling surface is not a
   parameter of the capture layer (``SnapshotRequest`` carries budgets, not a
   session key), so the invoke handler publishes it for the duration of one
   dispatch via :func:`frame_scope`. A capture with no scope (a CLI probe, a
   future caller that skipped the handler) emits NOTHING rather than guessing an
   identity.
2. **A secure window is never mirrored.** ``Snapshot.has_secure`` is the driver's
   own predicate (the one ``capture_snapshot_image`` refuses on); it is re-read
   here rather than re-derived, so there is exactly one definition of "this
   window holds a password field".
3. **A withheld ``screenshot`` channel emits nothing** — consulted through
   ``gate.permitted_observation_channels``, the same helper the tool path and the
   Settings snapshot use. That helper currently permits every channel (the
   observation ceiling was removed), so this is the seam that keeps the relay
   honest if one is ever reintroduced, not a live restriction.

Wire constants live in this module rather than in ``types.py`` because they are
the transport contract of this one relay (exactly as ``BROWSER_FRAME_EVENT`` and
the browse frame's field validators live in ``browser/screencast.py``), not part
of the tool/observation vocabulary the rest of the package shares.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from kiro_crew.computer_use.types import MAX_SCREENSHOT_MAX_PX, OBS_SCREENSHOT, Snapshot
from kiro_crew.config.paths import config_dir
from kiro_crew.loopback_http import loopback_urlopen

logger = logging.getLogger(__name__)

# WS event name the dashboard ``ComputerUseLiveView`` panel listens for.
COMPUTER_USE_FRAME_EVENT = "computer_use_frame"

# Loopback ingress that rebroadcasts one frame. Registered in
# ``server._STRICT_INTERNAL_API_PATHS``.
FRAME_INGRESS_PATH = "/api/computer-use/frame"

# Header the ingress authenticates with (the gateway's own per-run local secret).
FRAME_SECRET_HEADER = "X-Internal-Secret"
_LOCAL_SECRET_FILE = ".local_secret"

# Seconds to wait on the POST. The mirror is best-effort decoration on a tool
# call that has already produced its result; a slow or dead gateway must cost the
# relay thread seconds, not minutes.
FRAME_POST_TIMEOUT_SECS = 2.0

# The ONLY format this path may carry. ``capture_macos`` encodes exclusively
# through ImageIO's JPEG destination with ``kCGImageDestinationImageMaxPixelSize``
# applied, so a frame is always an already-downscaled JPEG. Keeping the allowlist
# to one value is what makes "never mirror a full-resolution PNG" structural
# rather than a convention: a caller cannot opt into another encoding.
FRAME_FORMAT = "jpeg"
_ALLOWED_FORMATS = frozenset({FRAME_FORMAT})

# Standard base64 charset (+ optional padding). Matching this exactly structurally
# excludes ``:`` (so no ``://`` URL), whitespace, and ``<``/``>`` (so no HTML or
# script) — the right boundary control for an opaque image field, and far better
# than running text redactors over JPEG bytes.
# Standard base64, expressed as whole 4-character groups with an optional padded
# final group. Deliberately NOT ``[A-Za-z0-9+/]+={0,2}``: that shape is a
# polynomial-ReDoS (CodeQL ``py/polynomial-redos``, high) because on a long run of
# charset characters that cannot complete the match the engine retries every split
# point — and this pattern is applied to a caller-supplied string of up to
# ``MAX_FRAME_B64_CHARS``. The quad-group form has exactly one way to match at each
# position, so it is linear (measured: 4.4ms on 400k chars vs. quadratic growth).
#
# It is also STRICTER, which is a bonus rather than the point: it enforces real
# base64 quad structure, so ``QUJDRA=`` (one ``=`` where two are required) is
# correctly rejected.
_B64_RE = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")

# Cap on the encoded frame. A 1280px/q55 window measures ~25KB (~34KB base64);
# the ceiling here bounds the pathological case (a 4096px/q100 operator config)
# so one frame cannot become a multi-megabyte websocket write.
MAX_FRAME_B64_CHARS = 4_000_000

# Opaque session id, used client-side only as a lookup key (the panel renders the
# resolved session TITLE from its own slot store, never this value). Bounded
# anyway so the payload cannot carry arbitrary text. Mirrors the browse ingress.
_SESSION_KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

# The mirrored application's display name, for the panel header. Bundle ids and
# app names contain dots, spaces, ampersands and hyphens; anything outside this
# charset (or over length) is dropped rather than sanitized, so the field can
# never smuggle free text onto the wire.
_APP_LABEL_RE = re.compile(r"[A-Za-z0-9 ._+&()-]{1,96}")


@dataclass(frozen=True)
class FrameScope:
    """Which surface a capture belongs to, for the duration of one dispatch.

    The three fields are exactly the identity axes
    ``gate.permitted_observation_channels`` resolves against, so the mirror is
    governed by the same decision as the tool result it accompanies.
    """

    session_key: str
    agent: str = ""
    app: str = ""


# Per-thread, because the dispatch runs synchronously on ONE worker thread
# (``run_in_executor(subprocess_executor(), …)``) and the pool reuses threads. A
# module-global would leak one surface's identity into the next dispatch on a
# different thread; a contextvar would not survive the executor hop.
_scope_local = threading.local()


@contextmanager
def frame_scope(*, session_key: str, agent: str = "", app: str = "") -> Iterator[None]:
    """Publish the calling surface's identity for the enclosed dispatch.

    Installed by ``handlers/computer_use.api_computer_use_invoke`` around the one
    blocking dispatch call. Restores the previous value on exit (rather than
    clearing) so nesting can never strand a stale identity on a pooled thread.
    """
    previous = getattr(_scope_local, "scope", None)
    _scope_local.scope = FrameScope(session_key=session_key, agent=agent, app=app)
    try:
        yield
    finally:
        _scope_local.scope = previous


def active_scope() -> "FrameScope | None":
    """The scope published for this thread, or ``None`` when there is none."""
    scope = getattr(_scope_local, "scope", None)
    return scope if isinstance(scope, FrameScope) else None


def build_frame_payload(body: dict[str, Any]) -> "dict[str, Any] | None":
    """Normalize a POSTed frame body into the ``computer_use_frame`` WS payload.

    ``body`` is the JSON :func:`emit_snapshot_frame` sends:
    ``{"data": "<base64 jpeg>", "format": "jpeg", "width": …, "height": …,
    "session_key": …, "app": …}``.

    Returns the payload the panel renders, or ``None`` when the body carries no
    usable frame (the caller answers 400). Every field is bounded at this
    boundary: ``data`` to the base64 charset and a size cap, ``format`` to the
    single allowed encoding, the dimensions to the encoder's own pixel ceiling,
    and the two text fields to explicit charsets. No text redaction is applied to
    ``data`` because it is opaque image bytes, and the charset check already rules
    out URLs and markup.
    """
    data = body.get("data")
    if not isinstance(data, str) or not data or len(data) > MAX_FRAME_B64_CHARS:
        return None
    if not _B64_RE.fullmatch(data):
        return None
    # Unlike the browse ingress there is no "unknown format falls back to jpeg"
    # branch: a frame that claims anything other than JPEG did not come from the
    # in-process encoder and is refused outright rather than relabelled.
    if body.get("format") not in _ALLOWED_FORMATS:
        return None
    payload: dict[str, Any] = {"data": data, "format": FRAME_FORMAT}
    for key in ("width", "height"):
        value = body.get(key)
        # ``isinstance(True, int)`` is True in Python, so a JSON ``true`` would
        # otherwise broadcast width=1 and make the panel's aspect math render a
        # one-pixel frame. Bounded above by the encoder's own ceiling.
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if 0 < value <= MAX_SCREENSHOT_MAX_PX:
            payload[key] = value
    session_key = body.get("session_key")
    if isinstance(session_key, str) and _SESSION_KEY_RE.fullmatch(session_key):
        payload["session_key"] = session_key
    app = body.get("app")
    if isinstance(app, str) and _APP_LABEL_RE.fullmatch(app):
        payload["app"] = app
    return payload


def emit_snapshot_frame(snap: Snapshot) -> bool:
    """Mirror *snap*'s already-encoded JPEG to the dashboard. Never raises.

    Returns whether a relay was started — ``False`` means the frame was
    deliberately suppressed (no scope, a secure window, a governance deny, or no
    encoded bytes), which is also what a caller's test asserts on. ``True`` means
    a best-effort POST thread was spawned; delivery itself is not guaranteed and
    is never awaited, because the tool result must not depend on the mirror.
    """
    try:
        if not snap.image_jpeg:
            return False
        # (2) The always-on floor, read from the driver's own predicate rather
        # than re-derived here. ``capture_snapshot_image`` refuses before encoding
        # a secure window, so reaching this with ``has_secure`` would mean a
        # future capture path skipped that check — and the mirror must hold anyway.
        if snap.has_secure:
            return False
        # (1) No published identity → no mirror. Fail-closed by construction: a
        # frame we cannot attribute to a surface cannot be governed for it.
        scope = active_scope()
        if scope is None:
            return False
        # (3) The governance ceiling, via the same evaluator the tool path uses.
        from kiro_crew.computer_use import gate

        channels = gate.permitted_observation_channels(
            session_key=scope.session_key, agent=scope.agent, app=scope.app
        )
        if OBS_SCREENSHOT not in channels:
            return False
        payload = {
            "data": base64.b64encode(snap.image_jpeg).decode("ascii"),
            "format": FRAME_FORMAT,
            "width": snap.image_width,
            "height": snap.image_height,
            "session_key": scope.session_key,
            # The resolved application's display name, for the panel header. NOT
            # the window title: titles are their own observation channel and can
            # carry document names and paths.
            "app": snap.app.name,
        }
        thread = threading.Thread(
            target=_post_frame,
            args=(payload,),
            name="computer-use-frame",
            daemon=True,
        )
        thread.start()
        return True
    except Exception:
        # A mirror failure must never degrade the observation the model asked for.
        logger.debug("computer-use live frame could not be emitted", exc_info=True)
        return False


def _post_frame(payload: dict[str, Any]) -> None:
    """POST one frame to the loopback ingress. Swallows every failure.

    Runs on a daemon thread so the capture call returns immediately. The gateway
    may be on another port, mid-restart, or not listening at all — the agent's
    screenshot must not care.
    """
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- the host is the literal 127.0.0.1 and the path is the module-level FRAME_INGRESS_PATH constant; only the PORT varies, and it comes from local config via parse_dashboard_url. Nothing agent- or request-controlled reaches urlopen. Same trust profile as the mcp_core / mcp_computer loopback posts.  # noqa: E501
        request = urllib.request.Request(
            _ingress_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers=_headers(),
            method="POST",
        )
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- see the Request() justification directly above  # noqa: E501
        with loopback_urlopen(request, timeout=FRAME_POST_TIMEOUT_SECS):
            pass
    except Exception:
        logger.debug("computer-use live frame POST failed", exc_info=True)


def _ingress_url() -> str:
    """Loopback URL of the frame ingress on the gateway this install serves.

    Resolved through ``parse_dashboard_url`` (which honours ``KIROCREW_PORT`` and
    then ``dashboard.url``) so a dev instance on 6777 mirrors to itself rather
    than to a production gateway on 5476. Imported lazily: ``dashboard.origin``
    pulls in aiohttp, and this module is reachable from the capture path.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.origin import parse_dashboard_url

    _host, port = parse_dashboard_url(KiroCrewConfig.load().dashboard.url)
    return f"http://127.0.0.1:{port}{FRAME_INGRESS_PATH}"


def _headers() -> dict[str, str]:
    """Content type plus the per-run local secret the strict ingress requires.

    An unreadable secret yields no header, the ingress refuses the POST, and the
    frame is dropped — which is the correct outcome: the mirror is not worth
    weakening the ingress for.
    """
    headers = {"Content-Type": "application/json"}
    try:
        secret = (config_dir() / _LOCAL_SECRET_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return headers
    if secret:
        headers[FRAME_SECRET_HEADER] = secret
    return headers


__all__ = [
    "COMPUTER_USE_FRAME_EVENT",
    "FRAME_FORMAT",
    "FRAME_INGRESS_PATH",
    "FRAME_POST_TIMEOUT_SECS",
    "FrameScope",
    "MAX_FRAME_B64_CHARS",
    "active_scope",
    "build_frame_payload",
    "emit_snapshot_frame",
    "frame_scope",
]
