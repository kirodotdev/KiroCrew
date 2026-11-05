"""Hand a browser-built download back to the browser from a real URL.

Several dashboard downloads (an artifact, a diagram export, a share image, a
lightbox picture, a file's last copy) build the file in the browser and click an
``<a download>`` pointing at a ``blob:`` URL. An Android in-app browser (an app
embedding a WebView) drops that click: a WebView saves nothing on its own, it
passes the URL to the host app, and the host app cannot fetch a ``blob:`` URL
because those bytes live only inside the page.

So the page POSTs the exact bytes it would have saved and gets back a short-lived
same-origin URL to download instead. A stash, not a reader of the stored
artifact: the browser's export is the file the user expects (an HTML export
carries its own Content-Security-Policy, built by the page), and serving the same
bytes keeps that policy instead of re-deriving it here.

Security posture:

* Only the few types those downloads produce are accepted. ``image/png`` is the
  one binary type, sent base64-encoded; every type is served the same way.
* Ordinary dashboard auth on both routes; neither is on the token-auth bypass
  list. The caller only gets back bytes it sent.
* The GET answers ``Content-Disposition: attachment`` with
  ``Content-Security-Policy: sandbox`` and ``nosniff``, so model-authored HTML
  or SVG served from the dashboard's own origin never runs as same-origin
  script, even when the URL is opened directly.
* NOT single use: a WebView aborts its own fetch of a download and the host app
  fetches the URL again, so an entry stays readable until it expires.
* Entries expire, are capped in count and size and evicted oldest-first, and
  ``Cache-Control: private, no-store`` keeps any proxy or browser copy away.
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
import threading
import time
import urllib.parse
from collections import OrderedDict

from aiohttp import web

from kiro_crew.constants import ARTIFACT_MAX_CONTENT_BYTES
from kiro_crew.dashboard.handlers._shared import read_bounded_json

#: Long enough for the host app's own download request, short enough that the
#: stash does not hold a document much past the tap that made it.
_TTL_SECS = 300

#: One document: the largest artifact the store keeps plus room for what the
#: page's HTML export adds around it (theme, inlined runtime).
_MAX_BYTES = ARTIFACT_MAX_CONTENT_BYTES + 2 * 1024 * 1024
#: All documents together, and their count.
_MAX_TOTAL_BYTES = 2 * _MAX_BYTES
_MAX_ENTRIES = 16
#: The request body: JSON escaping can grow a document, so the body may be up to
#: twice the file before the exact file cap is checked after decoding. Stays
#: under the dashboard app's own body limit.
_MAX_REQUEST_BYTES = 2 * _MAX_BYTES + 1024 * 1024

#: The types the browser-built downloads use; anything else is refused.
_CONTENT_TYPES = {
    "text/plain": "text/plain; charset=utf-8",
    "text/html": "text/html; charset=utf-8",
    "application/json": "application/json; charset=utf-8",
    "image/svg+xml": "image/svg+xml; charset=utf-8",
    "image/png": "image/png",
}
#: Types whose bytes are not text, so they must arrive base64-encoded.
_BINARY_TYPES = frozenset({"image/png"})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: The plain ``filename=`` fallback must be a quoted ASCII token, and some
#: Android download handlers read only that form, not ``filename*``.
_UNSAFE_ASCII = re.compile(r"[^A-Za-z0-9._\- ]")
#: Room for an artifact name at its own cap plus the version and extension.
_MAX_NAME_CHARS = 255

_lock = threading.Lock()
#: doc id -> (expiry, content type, filename, utf-8 bytes); oldest first.
_stash: OrderedDict[str, tuple[float, str, str, bytes]] = OrderedDict()


def _prune(now: float) -> None:
    for doc_id in [k for k, (exp, *_rest) in _stash.items() if exp < now]:
        _stash.pop(doc_id, None)
    while len(_stash) > _MAX_ENTRIES or sum(len(e[3]) for e in _stash.values()) > _MAX_TOTAL_BYTES:
        _stash.popitem(last=False)


def _content_disposition(filename: str) -> str:
    ascii_name = _UNSAFE_ASCII.sub("_", filename) or "download"
    quoted = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def _bad(error: str, code: str, status: int = 400) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=status)


async def api_stash_artifact_download(request: web.Request) -> web.Response:
    """``POST /api/artifact-downloads`` — stash a file, return a URL to download it.

    Body: ``{"filename": str, "type": str, "content": str, "encoding"?: "base64"}``,
    where ``type`` is one of the accepted content types. With ``encoding:
    "base64"`` the decoded bytes are kept exactly and served with the bare type,
    no charset, since a text file the page did not build (a legacy svg) may not be
    UTF-8. A binary type must use base64.
    """
    # Capped before the read: the app-wide body limit is far larger than one file.
    body, refusal = await read_bounded_json(request, max_bytes=_MAX_REQUEST_BYTES)
    if refusal is not None:
        return refusal
    if body is None:
        return _bad("invalid json", "bad_body")
    filename = body.get("filename")
    content = body.get("content")
    declared_type = str(body.get("type") or "")
    content_type = _CONTENT_TYPES.get(declared_type)
    if not isinstance(filename, str) or not filename.strip() or len(filename) > _MAX_NAME_CHARS:
        return _bad("filename required", "bad_filename")
    if "/" in filename or "\\" in filename:
        return _bad("filename must not hold a path", "bad_filename")
    if content_type is None:
        return _bad("unsupported type", "bad_type")
    if not isinstance(content, str):
        return _bad("content required", "bad_content")
    as_base64 = body.get("encoding") == "base64" or declared_type in _BINARY_TYPES
    if as_base64:
        content_type = declared_type
    try:
        if as_base64:
            raw = base64.b64decode(content, validate=True)
        else:
            raw = content.encode("utf-8")
    # ValueError: b64decode refuses a str holding non-ASCII characters.
    except (UnicodeEncodeError, binascii.Error, ValueError):
        return _bad("content is not encodable", "bad_encoding")
    # A binary body must be what it claims, so nothing else rides the png type.
    if declared_type == "image/png" and not raw.startswith(_PNG_SIGNATURE):
        return _bad("content is not a png", "bad_content")
    if len(raw) > _MAX_BYTES:
        return _bad("file too large", "too_large", status=413)

    doc_id = secrets.token_urlsafe(16)
    now = time.time()
    with _lock:
        _stash[doc_id] = (now + _TTL_SECS, content_type, filename, raw)
        _prune(now)
    return web.json_response({"url": f"/api/artifact-downloads/{doc_id}"})


async def api_artifact_download(request: web.Request) -> web.Response:
    """``GET /api/artifact-downloads/{doc_id}`` — the stashed file as an attachment.

    404 for an unknown or expired id.
    """
    doc_id = request.match_info.get("doc_id", "")
    with _lock:
        _prune(time.time())
        entry = _stash.get(doc_id)
    if entry is None:
        return _bad("not found", "not_found", status=404)
    _exp, content_type, filename, raw = entry
    return web.Response(
        body=raw,
        headers={
            "Content-Type": content_type,
            "Content-Disposition": _content_disposition(filename),
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


def register_artifact_download_routes(app: web.Application) -> None:
    app.router.add_post("/api/artifact-downloads", api_stash_artifact_download)
    app.router.add_get("/api/artifact-downloads/{doc_id}", api_artifact_download)
