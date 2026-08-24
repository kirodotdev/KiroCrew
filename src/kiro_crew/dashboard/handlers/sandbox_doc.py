"""Serve model-authored HTML to a sandboxed iframe from a real URL.

Two dashboard surfaces render HTML the model wrote — saved artifacts
(``ArtifactBody``) and inline chat widgets (``WidgetFrame``) — and both used to
hand the frame a ``blob:`` URL built in the browser. That works in Chromium and
fails in some WebKit-based in-app browsers, which refuse the load outright
("invalid url or response") and can take the whole page down with it. ``srcdoc``
is not the way out either: a sandboxed ``srcdoc`` frame blank-renders on WebKit.
A plain ``https`` document is the only form observed to load everywhere.

The bytes are already in the browser, so this is deliberately a STASH rather than
a per-source reader: the caller POSTs the html it already holds and gets back a
short-lived URL. One mechanism then covers both surfaces — and the inline-widget
case, whose bytes are not persisted anywhere and so have no other URL to give.

Security posture, mirroring the webapp-preview channel next door:

* The GET route is on the auth-middleware bypass list, and the **HMAC path token
  IS the credential**. It is bound to the requesting client address, because the
  token rides in the frame's own location where model-authored script can read
  it — an exfiltrated token is useless from another connection.
* ``Content-Security-Policy: sandbox`` makes the response an opaque origin
  **even when opened top-level**, so the URL existing on the dashboard's own
  origin does not turn model HTML into same-origin script. The sandbox flags
  match what the embedding iframes already grant, so nothing the frame could do
  before is newly permitted or newly denied.
* Every authorization decision is SEL-audited, rejected probes included. The
  token itself is never logged.
* Entries expire, are capped in count and size, and are evicted oldest-first, so
  a long dashboard session cannot grow the stash without bound.
"""

from __future__ import annotations

import logging
import secrets as _secrets
import threading
import time
from collections import OrderedDict

from aiohttp import web

from kiro_crew.sel import sel

from .path_token import PathTokenSigner

logger = logging.getLogger(__name__)

#: Path prefix of the serving route. Mirrored in the auth middleware's bypass
#: list — the token is the credential, so a session cookie is not required (and a
#: sandboxed frame in some engines does not send one).
SANDBOX_DOC_PREFIX = "/sandbox-doc/"

#: Long enough for a frame to load and reload on a theme change, short enough
#: that a leaked URL is stale before it is useful.
_TOKEN_TTL_SECS = 900

#: Bounds on the stash. A widget is typically a few KB; the cap is generous
#: enough for a large artifact and small enough that the ceiling is bounded.
_MAX_ENTRIES = 64
_MAX_BYTES = 4 * 1024 * 1024

#: Per-process, so tokens do not survive a restart. That is intentional: the
#: stash does not either, and the frame re-mints on mount.
# Its own signer, hence its own independent secret. See path_token for why a
# shared key across channels would be the wrong shape.
_signer = PathTokenSigner("sandbox-doc", _TOKEN_TTL_SECS)

_lock = threading.Lock()
#: doc id -> (expiry, utf-8 encoded document). Ordered so eviction is oldest-first.
#: Bytes, not str: encoding happens at the mint so serving cannot fail, and the
#: byte cap then measures what is actually held rather than a character count.
_stash: OrderedDict[str, tuple[float, bytes]] = OrderedDict()


#: How long a freshly minted document counts as IN FLIGHT and must not be
#: evicted to make room for a newer one. A frame issues its GET immediately
#: after the mint resolves, so this only has to cover that round trip.
_IN_FLIGHT_GRACE_SECS = 10


def _prune(now: float) -> bool:
    """Drop what is safe to drop; report whether the stash is inside its caps.

    Eviction never touches an entry that is still IN FLIGHT. An entry is removed
    when it is SERVED, so every live entry is one nobody has fetched yet, and
    dropping the oldest to make room for a newer one would silently invalidate a
    URL some frame is about to load — the mint succeeded, so the frontend has no
    failure to show and the frame just renders blank. That is reachable without
    an attacker: a gallery of image-bearing artifacts can push several MB of
    pending documents through here at once.

    So a caller that cannot be given room is REFUSED instead, which surfaces as
    a visible retry rather than a silent blank frame.
    """
    for doc_id in [k for k, (exp, _) in _stash.items() if exp < now]:
        _stash.pop(doc_id, None)

    def _evictable() -> str | None:
        for doc_id, (exp, _) in _stash.items():  # oldest first
            if (exp - _TOKEN_TTL_SECS) + _IN_FLIGHT_GRACE_SECS <= now:
                return doc_id
        return None

    def _over() -> bool:
        return (
            len(_stash) > _MAX_ENTRIES or sum(len(html) for _, html in _stash.values()) > _MAX_BYTES
        )

    while _over():
        stale = _evictable()
        if stale is None:
            return False
        _stash.pop(stale, None)
    return True


def _audit(outcome: str, resources: str) -> None:
    """SEL-audit an authorization decision (sanitized: never the token)."""
    try:
        sel().log_api_access(
            caller="dashboard:sandbox-doc",
            operation="sandbox_doc.serve",
            outcome=outcome,
            source="dashboard",
            resources=resources[:512],
        )
    except Exception:  # pragma: no cover - auditing must never break serving
        logger.debug("sandbox-doc: audit failed", exc_info=True)


async def api_stash_sandbox_doc(request: web.Request) -> web.Response:
    """``POST /api/sandbox-doc`` — stash html, return the URL a frame can load.

    Ordinary session auth applies here: this is the authenticated dashboard
    asking for a URL for bytes it already has.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json", "code": "bad_body"}, status=400)
    html = body.get("html") if isinstance(body, dict) else None
    if not isinstance(html, str) or not html:
        return web.json_response({"error": "html required", "code": "bad_html"}, status=400)
    if len(html) > _MAX_BYTES:
        return web.json_response({"error": "document too large", "code": "too_large"}, status=413)

    # Encode HERE, not at serve time. A document carrying a lone surrogate is
    # representable in JSON and in a Python str but NOT in UTF-8, and encoding it
    # after the single-use pop turned that into a 500 plus a document that could
    # never be fetched again. Rejecting at the mint is the honest place: the
    # caller still holds the bytes and gets a reason.
    try:
        raw = html.encode("utf-8")
    except UnicodeEncodeError:
        return web.json_response(
            {"error": "document is not encodable", "code": "bad_encoding"}, status=400
        )

    doc_id = _secrets.token_urlsafe(16)
    now = time.time()
    with _lock:
        _stash[doc_id] = (now + _TOKEN_TTL_SECS, raw)
        if not _prune(now):
            # Everything in the stash is still in flight, so there is no room
            # that can be taken without invalidating a URL another frame is
            # about to load. Refusing here is the visible failure; evicting
            # would be a silent blank frame somewhere else on the page.
            _stash.pop(doc_id, None)
            return web.json_response(
                {"error": "too many documents in flight", "code": "stash_full"},
                status=503,
            )
    token = _signer.mint(doc_id, request.remote or "")
    return web.json_response({"url": f"{SANDBOX_DOC_PREFIX}{doc_id}/{token}"})


async def serve_sandbox_doc(request: web.Request) -> web.Response:
    """``GET /sandbox-doc/{doc_id}/{token}`` — the document itself, ONCE.

    Auth = the HMAC path token (this route is on the middleware bypass list).
    Fails closed as 404 for every invalid condition, so the route is not an
    oracle for which ids exist.

    **Single use.** The entry is popped before the body is written, so a URL that
    leaks is already spent: the frame it was minted for consumed it. This is the
    load-bearing control rather than the client binding below, because
    ``request.remote`` is the PROXY's address whenever the dashboard is reached
    through one (a tunnel, a reverse proxy), and every client then shares it — the
    binding is worth nothing in exactly the deployments where a URL is most
    reachable. The binding stays as a second, cheap layer for the direct case.

    The cost is that a frame the BROWSER reloads on its own (rather than a page
    reload, which remounts the app and mints again) finds its document spent and
    gets this 404 inside the frame. Nothing in the frontend observes the GET —
    the document is served with an opaque origin, so the parent cannot read
    whether it loaded — and the retry control appears only when the MINT itself
    fails. Recovering a spent URL therefore needs a page reload. Detecting it
    would take a beacon injected into the document plus a deadline, which is
    not built; do not describe one here until it is.
    """
    doc_id = request.match_info.get("doc_id", "")
    token = request.match_info.get("token", "")
    client = request.remote or ""
    if not _signer.verify(token, doc_id, client):
        _audit("denied", doc_id[:8])
        raise web.HTTPNotFound()
    now = time.time()
    with _lock:
        entry = _stash.pop(doc_id, None)
        if entry is not None and entry[0] < now:
            entry = None
    if entry is None:
        _audit("denied", doc_id[:8])
        raise web.HTTPNotFound()
    _audit("allowed", doc_id[:8])

    # Already bytes: the encode happened at MINT time, so serving cannot raise.
    # It used to encode here, which meant a document carrying a lone surrogate
    # (JSON accepts "\ud800", Python holds it in a str, UTF-8 cannot represent it)
    # popped the entry and THEN raised — a 500 for the frame and a document lost
    # to the single-use pop, with no way to ask for it again.
    resp = web.Response(body=entry[1], content_type="text/html", charset="utf-8")
    # The load form changed; the trust level must not. `sandbox` gives the
    # document an opaque origin even opened top-level, and the flags are exactly
    # those the embedding frames grant — no more. `allow-forms` in particular is
    # NOT granted: neither ArtifactBody nor WidgetFrame allows it, so including it
    # here would let a document opened top-level submit forms that the same
    # document cannot submit inside the frame. No `default-src` is added on
    # purpose: these documents could previously reach the network from a blob:
    # origin, and silently cutting that off would break existing widgets rather
    # than fix them.
    resp.headers["Content-Security-Policy"] = (
        "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; "
        "frame-ancestors 'self'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def register_sandbox_doc_routes(app: web.Application) -> None:
    app.router.add_post("/api/sandbox-doc", api_stash_sandbox_doc)
    app.router.add_get(
        SANDBOX_DOC_PREFIX + "{doc_id}/{token}",
        serve_sandbox_doc,
    )
