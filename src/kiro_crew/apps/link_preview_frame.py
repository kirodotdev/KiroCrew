"""Link-preview frames: the sandboxed document an app's hover card runs in, and its fetch.

An app's ``contributes.linkPreviews`` module never runs in the dashboard's document.
It runs in an iframe the dashboard positions next to the hovered link, and that
iframe is ``sandbox="allow-scripts"`` with no ``allow-same-origin``, so its origin is
opaque: it cannot read the dashboard's DOM, its cookies or its token, and the
dashboard talks to it only through ``postMessage``.

Three routes:

``POST /api/apps/{name}/link-preview``
    Dashboard sessions only (an app token, an internal caller or a link token is
    refused). Returns the URL of the frame document and the app's capability: a
    credential that reaches the app's own backend routes and nothing else.

``GET /link-preview-frame/{name}/{frame_id}/{token}``
    The frame document. On the auth middleware's bypass list because a sandboxed
    frame does not reliably send the session cookie; the HMAC path token is the
    credential. The response's own ``Content-Security-Policy: sandbox
    allow-scripts`` makes it an opaque origin even when opened top-level.

``/link-preview-fetch/{name}/{path}``
    ``ctx.fetch`` from inside the frame. Authenticated by the capability in the
    ``X-KiroCrew-Capability`` header, never by a cookie. It reaches exactly the
    app's own backend: the process backend through the same signed reverse proxy
    as ``/apps/{name}/api/``, or the routes the app registered through
    ``backend.routes``. Core routes under ``/api/apps/{name}/`` (config, update,
    enable, ...) are not reachable. CORS answers only the opaque ``null`` origin,
    without credentials.

Each frame has its own id. Both its tokens are bound to the app, the requesting
client address and that id, are short-lived, and are per-process: a restart
invalidates them and the dashboard mints again. The capability is read by the fetch
route alone; no other route, and not the auth middleware, accepts it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets as _secrets
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.apps.link_preview_grants import link_preview_grant_row
from kiro_crew.apps.manager import get_app_manifest, is_app_enabled, resolve_mcp_backend_url
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.path_token import PathTokenSigner

logger = logging.getLogger("kiro_crew.apps.routes")

#: Route prefixes. Mirrored in ``dashboard/token_auth_paths.py`` (auth bypass, SPA
#: exclusion) and, for the fetch route, in the CSRF exemption.
FRAME_PREFIX = "/link-preview-frame/"
FETCH_PREFIX = "/link-preview-fetch/"
#: The header the frame's ``ctx.fetch`` sends its capability in.
CAPABILITY_HEADER = "X-KiroCrew-Capability"

#: Long enough to load and reload the frame, short enough that a copied URL is stale.
_FRAME_TTL_SECS = 900
#: Short-lived: the dashboard renews it at half this while the frame lives
#: (``linkPreviewFrames.ts``), and the frame asks again on a refusal.
_CAPABILITY_TTL_SECS = 900
#: Each frame gets its own id, and both its tokens are bound to it.
_FRAME_ID_RE = re.compile(r"^[A-Za-z0-9_-]{12,32}$")

_signers: dict[str, PathTokenSigner] = {}


def _signer(kind: str) -> PathTokenSigner:
    # Imported here: path_token reaches dashboard.token_auth, which this package's
    # manager imports, so a module-level import would form a cycle.
    from kiro_crew.dashboard.handlers.path_token import PathTokenSigner

    signer = _signers.get(kind)
    if signer is None:
        ttl = _FRAME_TTL_SECS if kind == "frame" else _CAPABILITY_TTL_SECS
        signer = _signers[kind] = PathTokenSigner(f"link-preview-{kind}", ttl)
    return signer


def _audit(operation: str, outcome: str, name: str, error: str = "") -> None:
    """SEL-audit an authorization decision. Never the token."""
    try:
        sel().log_api_access(
            caller=f"app:{name}",
            operation=operation,
            outcome=outcome,
            source="link_preview",
            resources=name,
            error=error,
        )
    except Exception:
        logger.debug("link-preview: audit failed", exc_info=True)


def _provider_entry(name: str) -> tuple[str, bool, str] | None:
    """``(entry, process_backed, app name)`` for an enabled app that declares a provider.

    The name is the manifest's own, so what the frame document embeds comes from the
    installed app, never from the request path. None when there is no such provider.
    """
    if not is_app_enabled(name):
        return None
    manifest = get_app_manifest(name)
    if manifest is None or not manifest.contributes.linkPreviews:
        return None
    entry = manifest.contributes.linkPreviews[0].entry.removeprefix("./")
    if not entry or ".." in entry or entry.startswith("/"):
        return None
    if manifest.name != name:
        return None
    # Revoked or never approved: no host and no text pattern the owner approved.
    grant = link_preview_grant_row(name, manifest).get("linkPreviewGrant") or {}
    if not grant.get("hosts") and not grant.get("textPatterns"):
        return None
    # Proxied when the gateway proxy can reach a backend for it: a spawned
    # ``backend.entryPoint`` or a self-managed loopback ``mcpServers`` URL, the same
    # two shapes the proxy resolves and the app secret is issued for.
    process_backed = bool(manifest.backend.entryPoint) or (
        resolve_mcp_backend_url(manifest.mcpServers) is not None
    )
    return entry, process_backed, manifest.name


# ---------------------------------------------------------------------------
# Mint
# ---------------------------------------------------------------------------


def _mint_frame_token(request: web.Request, name: str, client: str, frame_id: str) -> str:
    """``<parent port>-<signed token>``: the frame's path credential.

    The frame document is fetched without a cookie (the iframe is credentialless
    and the route is on the auth bypass), so it cannot find the embedding parent
    dashboard the way a dashboard page does. The mint can: it runs on the person's
    session, whose validated ``embed_parent_port`` claim names that parent. The
    port is bound into the signature, so the frame request can trust it; ``0`` means
    the dashboard is not embedded.
    """
    from kiro_crew.dashboard.server import _embed_parent_port

    port = _embed_parent_port(request, request.app) or 0
    return f"{port}-{_signer('frame').mint(name, client, frame_id, str(port))}"


def _frame_token_parent(token: str, name: str, client: str, frame_id: str) -> int | None:
    """The parent port a valid frame token was minted with (``0``: none), else ``None``."""
    port_s, sep, signed = token.partition("-")
    if not sep or not port_s.isdigit() or len(port_s) > 5:
        return None
    if not _signer("frame").verify(signed, name, client, frame_id, port_s):
        return None
    return int(port_s)


async def handle_link_preview_mint(request: web.Request) -> web.Response:
    """``POST /api/apps/{name}/link-preview`` — the frame URL and the app's capability."""
    name = request.match_info["name"]
    # A person's dashboard session, not an app, an agent or a one-time link: the
    # capability acts for the person viewing the card.
    if (
        request.get("is_dashboard_user") is not True
        or request.get("app")
        or request.get("internal_auth") is True
    ):
        _audit("link_preview_mint", "denied", name, "not a dashboard session")
        return web.json_response(
            {"code": "dashboard_session_required", "error": "dashboard session required"},
            status=403,
        )
    found = await asyncio.to_thread(_provider_entry, name)
    if found is None:
        _audit("link_preview_mint", "denied", name, "no enabled link-preview provider")
        return web.json_response(
            {"code": "no_link_preview", "error": f"app {name!r} has no enabled link preview"},
            status=404,
        )
    client = request.remote or ""
    # A renewal names the frame it is for, so the new capability is that frame's.
    frame_id = request.query.get("frame", "")
    if not _FRAME_ID_RE.fullmatch(frame_id):
        frame_id = _secrets.token_urlsafe(12)
    _audit("link_preview_mint", "granted", name)
    return web.json_response(
        {
            "frame": f"{FRAME_PREFIX}{name}/{frame_id}/{_mint_frame_token(request, name, client, frame_id)}",
            "frameId": frame_id,
            "capability": f"{frame_id}~{_signer('fetch').mint(name, client, frame_id)}",
            "ttl": _CAPABILITY_TTL_SECS,
        },
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Frame document
# ---------------------------------------------------------------------------

# The frame's whole runtime. It loads the app's module, builds the `ctx` the module
# receives, and turns parent messages into provider calls and `ctx` calls into
# messages. Not a security boundary: the app's own code runs beside it and could post
# anything. The dashboard checks every message (LinkPreviewLayer.tsx).
_BOOTSTRAP = r"""
const P = window.parent;
const CFG = JSON.parse(document.getElementById('lp-config').textContent);
// Behind a reverse proxy at a sub-path the gateway never sees the mount, but this
// document's own URL carries it: prefix the module and fetch paths with it.
const MOUNT = location.pathname.slice(0, Math.max(0, location.pathname.indexOf('/link-preview-frame/')));
CFG.entry = MOUNT + CFG.entry;
CFG.fetchBase = MOUNT + CFG.fetchBase;
const TAG = 'kirocrew-link-preview';
let cap = null;
let provider = null;
let renewWaiters = [];
const send = (type, data) => { try { P.postMessage(Object.assign({ [TAG]: 1, type }, data || {}), '*'); } catch (_) {} };

function relativeOnly(rel) {
  return !(/^[a-z][a-z0-9+.-]*:/i.test(rel) || rel.startsWith('/') || rel.includes('\\'));
}
function renew() {
  return new Promise((done) => {
    renewWaiters.push(done);
    send('capabilityExpired');
    setTimeout(done, 5000);
  });
}
async function capFetch(path, init) {
  const rel = String(path || '');
  if (!relativeOnly(rel)) throw new Error('[link-preview] ' + CFG.app + ': path must be relative to the app routes');
  const url = new URL(rel, new URL(CFG.fetchBase, location.href));
  if (!url.pathname.startsWith(CFG.fetchBase)) throw new Error('[link-preview] ' + CFG.app + ': path leaves the app routes');
  const go = () => {
    const headers = new Headers((init && init.headers) || {});
    if (cap) headers.set('X-KiroCrew-Capability', cap);
    return fetch(url.pathname + url.search, Object.assign({}, init || {}, { headers, credentials: 'omit', mode: 'cors' }));
  };
  // A refused capability arrives as a network error: the refusal carries no CORS
  // headers. Renew once and try again.
  try {
    const res = await go();
    if (res.status !== 401) return res;
  } catch (_) {}
  await renew();
  return go();
}
const clampNum = (n) => (Number.isFinite(n) ? n : 0);
// Card controls belong to one show: each carries that show's invocation, fixed
// when it is made, so a slow completion for an earlier hover names the earlier
// show and the dashboard drops it.
function cardControls(invocation) {
  return Object.freeze({
    open: () => send('open', { invocation }),
    close: () => send('close', { invocation }),
    resize: (size) => send('resize', { invocation, width: clampNum(size && size.width), height: clampNum(size && size.height) }),
    pin: (pinned) => send('pin', { invocation, pinned: pinned === true }),
    dragBy: (dx, dy) => send('drag', { invocation, dx: clampNum(dx), dy: clampNum(dy) }),
    dock: (edge) => send('dock', { invocation, edge: edge === 'left' || edge === 'right' ? edge : null }),
  });
}
const ctx = {
  appName: CFG.app,
  theme: () => CFG.theme,
  fetch: capFetch,
  setTextPatternsOff: (ids) => send('textPatternsOff', { ids: Array.isArray(ids) ? ids.map(String).slice(0, 64) : [] }),
  // Overlay mode: the frame covers the viewport, clipped to these rects.
  setRegions: (rects) => send('regions', {
    rects: (Array.isArray(rects) ? rects : []).slice(0, 32).map((r) => ({
      left: clampNum(r && r.left), top: clampNum(r && r.top), width: clampNum(r && r.width), height: clampNum(r && r.height),
    })),
  }),
};
function call(name, arg) {
  if (!provider || typeof provider[name] !== 'function') return;
  try { provider[name](arg); } catch (err) { console.error('[link-preview] ' + CFG.app + ':', err); }
}
async function start() {
  try {
    const mod = await import(CFG.entry);
    if (typeof (mod && mod.activate) !== 'function') throw new Error('module exports no activate(ctx)');
    const p = mod.activate(ctx);
    if (!p || typeof p.show !== 'function') throw new Error('activate(ctx) returned no provider');
    provider = p;
    send('ready');
  } catch (err) {
    console.error('[link-preview] ' + CFG.app + ' failed to load:', err);
    send('failed', { message: String((err && err.message) || err).slice(0, 200) });
  }
}
let started = false;
window.addEventListener('message', (e) => {
  if (e.source !== P) return;
  const m = e.data;
  if (!m || typeof m !== 'object' || m[TAG] !== 1) return;
  switch (m.type) {
    case 'init':
      if (started) return;
      started = true;
      cap = typeof m.capability === 'string' ? m.capability : null;
      CFG.theme = m.theme === 'dark' ? 'dark' : 'light';
      start();
      return;
    case 'capability':
      cap = typeof m.capability === 'string' ? m.capability : null;
      renewWaiters.splice(0).forEach((done) => done());
      return;
    case 'show': {
      if (!Number.isInteger(m.invocation)) return;
      const link = { rect: m.rect, theme: m.theme === 'dark' ? 'dark' : 'light', card: cardControls(m.invocation) };
      if (typeof m.url === 'string') link.url = m.url;
      if (typeof m.text === 'string') link.text = m.text;
      if (m.modifiers && typeof m.modifiers === 'object') {
        const k = m.modifiers;
        link.modifiers = { alt: k.alt === true, shift: k.shift === true, ctrl: k.ctrl === true, meta: k.meta === true };
      }
      CFG.theme = link.theme;
      call('show', link);
      return;
    }
    case 'leave': call('leave'); return;
    case 'hide': call('hide', String(m.reason || 'other')); return;
    case 'theme': CFG.theme = m.theme === 'dark' ? 'dark' : 'light'; call('theme', CFG.theme); return;
    case 'dispose': call('dispose'); provider = null; return;
    case 'key': {
      if (typeof m.key !== 'string' || m.key.length !== 1) return;
      const init = { key: m.key, code: typeof m.code === 'string' ? m.code : '', shiftKey: m.shift === true, bubbles: true, composed: true, cancelable: true };
      const target = document.activeElement || document.body;
      target.dispatchEvent(new KeyboardEvent('keydown', init));
      target.dispatchEvent(new KeyboardEvent('keyup', init));
      return;
    }
  }
});
// Keys the dashboard would see if the card were its own element.
window.addEventListener('keydown', (e) => { if (e.key === 'Escape') send('escape'); });
// Links leave through the dashboard's link bridge: this frame may not open a window.
const input = () => { try { P.postMessage({ type: 'kirocrew-dashboard:input' }, '*'); } catch (_) {} };
['keydown', 'mousedown', 'pointerdown', 'pointerup', 'touchend'].forEach((t) => window.addEventListener(t, input, { capture: true, passive: true }));
function webUrl(raw) {
  try { const u = new URL(String(raw || ''), location.href); return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null; } catch (_) { return null; }
}
// Bubble phase on the window, after every handler of the card: a click the card
// handled itself (in-card navigation, a nested card) is left alone.
const onLink = (e) => {
  if (e.defaultPrevented) return;
  if (e.type === 'auxclick' && e.button !== 1) return;
  if (e.type === 'click' && e.button !== 0) return;
  const path = e.composedPath();
  const a = path.find((n) => n && n.nodeType === 1 && n.localName === 'a' && n.hasAttribute('href'));
  if (!a) return;
  const raw = a.getAttribute('href') || '';
  if (raw.startsWith('#')) return;
  e.preventDefault();
  const url = webUrl(new URL(raw, a.baseURI || location.href).href);
  if (url) { try { P.postMessage({ type: 'kirocrew-dashboard:open', url }, '*'); } catch (_) {} }
};
window.addEventListener('click', onLink);
window.addEventListener('auxclick', onLink);
window.open = (raw) => { const url = webUrl(raw); if (url) { try { P.postMessage({ type: 'kirocrew-dashboard:open', url }, '*'); } catch (_) {} } return null; };
"""


def _frame_html(name: str, entry: str, nonce: str) -> str:
    config = {
        "app": name,
        "entry": f"/apps/{name}/ui/{entry}",
        "fetchBase": f"{FETCH_PREFIX}{name}/",
        "theme": "light",
    }
    # ``</`` cannot appear inside the JSON island: a `<` is always escaped.
    island = json.dumps(config).replace("<", "\\u003c")
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer">'
        "<style>html,body{margin:0;padding:0;background:transparent;}</style>"
        f'<script type="application/json" id="lp-config">{island}</script>'
        f'<script type="module" nonce="{nonce}">{_BOOTSTRAP}</script>'
        "</head><body></body></html>"
    )


def frame_csp(nonce: str, frame_ancestors: str) -> str:
    """The frame document's policy. ``sandbox allow-scripts`` is the boundary.

    ``sandbox`` with ``allow-scripts`` and nothing else gives the document an opaque
    origin wherever it is opened, which is what keeps it from the dashboard's DOM,
    cookies and storage. The fetch directives keep the app's own code on its own
    module (``script-src 'self'`` plus the bootstrap's nonce) and its network on the
    gateway (``connect-src 'self'``); ``'self'`` is resolved by the browser against
    the frame's real URL, so it stays right behind a tunnel that rewrites Host.
    Images may come from any https host, as a card shows avatars and icons.
    """
    return (
        "sandbox allow-scripts; "
        "default-src 'none'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' https: data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "media-src 'none'; worker-src 'none'; frame-src 'none'; child-src 'none'; "
        "manifest-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; "
        f"frame-ancestors {frame_ancestors}"
    )


async def handle_link_preview_frame(request: web.Request) -> web.Response:
    """``GET /link-preview-frame/{name}/{frame_id}/{token}`` — the frame document."""
    name = request.match_info.get("name", "")
    frame_id = request.match_info.get("frame_id", "")
    token = request.match_info.get("token", "")
    parent = (
        _frame_token_parent(token, name, request.remote or "", frame_id)
        if _FRAME_ID_RE.fullmatch(frame_id)
        else None
    )
    if parent is None:
        _audit("link_preview_frame", "denied", name, "bad frame token")
        raise web.HTTPNotFound()
    found = await asyncio.to_thread(_provider_entry, name)
    if found is None:
        _audit("link_preview_frame", "denied", name, "no enabled link-preview provider")
        raise web.HTTPNotFound()
    _audit("link_preview_frame", "granted", name)
    from kiro_crew.dashboard.origin import frame_ancestors_value
    from kiro_crew.dashboard.server import parent_port_frame_ancestors

    nonce = _secrets.token_urlsafe(18)
    resp = web.Response(
        text=_frame_html(found[2], found[0], nonce), content_type="text/html", charset="utf-8"
    )
    # The embedding parent (an Instances embed) comes from the signed token, not
    # from a cookie this credentialless request never carries.
    resp.headers["Content-Security-Policy"] = frame_csp(
        nonce, frame_ancestors_value(parent_port_frame_ancestors(parent) if parent else [])
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

#: Request headers the frame may send. ``content-type`` covers JSON bodies.
_CORS_ALLOW_HEADERS = f"content-type, {CAPABILITY_HEADER.lower()}"
_FETCH_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_CORS_ALLOW_METHODS = ", ".join(sorted(_FETCH_METHODS))
#: Marks a request whose response carries the frame's CORS headers: a preflight,
#: or a request whose capability verified. A refusal carries none.
_CORS_KEY = "link_preview_cors"


def _capability_ok(capability: str, name: str, client: str) -> bool:
    """Whether ``<frame id>~<token>`` was minted for this app, client and frame."""
    if len(capability) > 200:
        return False
    frame_id, sep, token = capability.partition("~")
    if not sep or not _FRAME_ID_RE.fullmatch(frame_id):
        return False
    return _signer("fetch").verify(token, name, client, frame_id)


def _cors_headers() -> dict[str, str]:
    # The opaque origin of a sandboxed frame serializes as "null". Credentials are
    # never allowed: the capability header is the only credential this route reads.
    return {
        "Access-Control-Allow-Origin": "null",
        "Access-Control-Allow-Methods": _CORS_ALLOW_METHODS,
        "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


async def add_link_preview_cors(request: web.Request, response: web.StreamResponse) -> None:
    """``on_response_prepare`` hook: CORS headers on every answer of the fetch route.

    A hook rather than headers on each response, because the proxied response is
    prepared inside the reverse proxy. Only a preflight (which carries no credential
    by design and returns no data) and a request whose capability verified get them,
    so the opaque origin can read nothing without a valid capability. A refusal is
    therefore an opaque network error to the frame, which renews and retries once.
    """
    if request.get(_CORS_KEY) is True:
        for key, value in _cors_headers().items():
            response.headers[key] = value


def _unauthorized(name: str, why: str) -> web.Response:
    _audit("link_preview_fetch", "denied", name, why)
    return web.json_response({"code": "capability_invalid", "error": why}, status=401)


async def handle_link_preview_fetch(request: web.Request) -> web.StreamResponse:
    """``/link-preview-fetch/{name}/{path}`` — the frame's ``ctx.fetch``."""
    name = request.match_info.get("name", "")
    path = request.match_info.get("path", "")
    if request.method == "OPTIONS":
        request[_CORS_KEY] = True
        # A preflight carries no credential by design; it grants nothing by itself.
        return web.Response(status=204)
    if request.method not in _FETCH_METHODS:
        return web.json_response(
            {"code": "method_not_allowed", "error": "method not allowed"}, status=405
        )
    if ".." in path or path.startswith("/") or "\\" in path:
        return web.json_response({"code": "invalid_path", "error": "invalid path"}, status=400)
    capability = request.headers.get(CAPABILITY_HEADER, "")
    if not capability or not _capability_ok(capability, name, request.remote or ""):
        return _unauthorized(name, "missing or invalid capability")
    found = await asyncio.to_thread(_provider_entry, name)
    if found is None:
        return _unauthorized(name, "no enabled link-preview provider")
    _audit("link_preview_fetch", "granted", name)
    request[_CORS_KEY] = True
    # The app's own identity, as an app token would carry: the proxy's cross-app
    # guard and its principal claim read it.
    request["app"] = name
    if found[1]:
        # Through the route facade, the one import path of the proxy.
        from kiro_crew.apps import routes as _routes

        return await _routes.handle_app_api_proxy(request)
    from kiro_crew.apps.hooks_integration import get_route_registry

    registry = get_route_registry()
    if registry is None:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    # Only the routes the app registered: the registry's own dispatch, never the
    # router, so a core route under /api/apps/<name>/ cannot match.
    request.match_info["app_name"] = name
    return await registry.dispatch(request)


def register_link_preview_routes(app: web.Application) -> None:
    """Register the three routes and the fetch route's CORS hook."""
    app.router.add_post("/api/apps/{name}/link-preview", handle_link_preview_mint)
    app.router.add_get(FRAME_PREFIX + "{name}/{frame_id}/{token}", handle_link_preview_frame)
    app.router.add_route("*", FETCH_PREFIX + "{name}/{path:.*}", handle_link_preview_fetch)
    if add_link_preview_cors not in app.on_response_prepare:
        app.on_response_prepare.append(add_link_preview_cors)


__all__: list[str] = [
    "CAPABILITY_HEADER",
    "FETCH_PREFIX",
    "FRAME_PREFIX",
    "add_link_preview_cors",
    "frame_csp",
    "handle_link_preview_fetch",
    "handle_link_preview_frame",
    "handle_link_preview_mint",
    "register_link_preview_routes",
]
