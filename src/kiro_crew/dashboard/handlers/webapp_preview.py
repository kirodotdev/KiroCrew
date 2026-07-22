"""Local preview channel for ``kind="webapp"`` artifacts.

A webapp artifact carries ``webapp_metadata.app_dir`` — the local copy of the
app tree that was (or would be) deployed. The dashboard renders the app card's
preview from THIS local copy instead of iframing the remote deployment:
no dependency on the remote site's frame headers, CDN propagation, or network
reachability — and it works for expired / not-yet-deployed apps too, exactly
like html/widget artifacts render from local content.

Two endpoints:

- ``GET /api/artifacts/{slug}/app-preview`` (standard dashboard auth):
  validates the artifact + app_dir and mints a short-lived HMAC path token.
  Returns ``{"base": "/artifact-app/{slug}/{token}/"}``.

- ``GET /artifact-app/{slug}/{token}/{path}`` (auth **bypass** — the token IS
  the auth): serves static files from the app's web root. The sandboxed
  preview iframe loads ``{base}`` (→ index.html) and every relative
  subresource (css/js/img) naturally resolves under the same token prefix,
  so no cookies are needed inside the frame.

Security model (all LLM-influenceable inputs re-validated at serve time):

- ``app_dir`` comes from artifact metadata that agents can write, so it is
  NEVER trusted: the resolved dir must live under the same allow-listed local
  roots the deploy publish path enforces (``_allowed_local_roots``).
- Web root = ``app_dir/public`` when present (deploy-contract layout), else
  ``app_dir`` itself. Serving is confined to the resolved web root: every
  requested file is fully resolved (symlinks followed) and must stay inside
  — traversal (``..``) and symlink escapes both fail closed as 404.
- Path tokens are ``HMAC(secret, slug:webroot:exp)`` with a per-process
  random secret and a short TTL: unauthenticated fetches cannot enumerate
  slugs, and a leaked URL dies with the token. The web root is bound into
  the MAC so a token minted for one tree cannot serve another after a
  metadata swap.
- Responses carry ``Content-Security-Policy: sandbox allow-scripts`` which
  forces an opaque origin even if the document is opened OUTSIDE the
  sandboxed iframe — the previewed app can never reach dashboard cookies,
  storage, or same-origin APIs. ``frame-ancestors 'self'`` keeps third-party
  sites from embedding the preview.
- Dotfiles (and thus ``.kirocrew-deploy.json`` style manifests / VCS dirs)
  are never served.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import mimetypes
import os
import re
import secrets as _secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from kiro_crew.artifacts import ArtifactNotFoundError, ArtifactValidationError, get_default_store
from kiro_crew.dashboard.origin import is_direct_local_request
from kiro_crew.deploy.handlers import _allowed_local_roots
from kiro_crew.hooks import safe_read_file_bytes_nolink
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Route prefix for the token-gated static channel (auth-middleware bypass).
APP_PREVIEW_PREFIX = "/artifact-app/"

_CORS_OK_TYPES = frozenset({
    "application/javascript",
    "text/javascript",
    "application/wasm",
})
# Round-7 F1: mirror the ArtifactStore slug grammar EXACTLY (strict
# lowercase). The previous, looser pattern ([A-Za-z0-9_-]) admitted slugs
# like "A" past this gate that the store's own validator then rejected by
# RAISING — an uncaught ArtifactValidationError on an auth-bypassed route
# is an unauthenticated 500 an attacker can drive repeatedly.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
_TOKEN_TTL_SECS = 15 * 60
_MAX_FILE_BYTES = 32 * 1024 * 1024  # single-file cap; previews are static builds

# Per-process token secret. Tokens are minted per page-load by the SPA, so
# gateway restarts invalidating outstanding tokens is fine (the card simply
# re-mints on its next query).
_TOKEN_SECRET = _secrets.token_bytes(32)

# Round-2 F3: safe_read_file_bytes_nolink's fd-realpath containment check is
# implemented for Linux (/proc/self/fd) and macOS (F_GETPATH) only; elsewhere
# it fails closed and every serve would 404 while the mint endpoint kept
# claiming the preview is available — suppressing the working remote
# fallback. Gate the whole channel on platform support instead.
_PLATFORM_SUPPORTED = sys.platform.startswith(("linux", "darwin"))


def _mac(slug: str, webroot: str, exp: int, client: str) -> str:
    # Round-5 F1: the bearer token rides in the iframe's own location, so
    # previewed (LLM-authored) script can read it and self-navigate it to an
    # attacker. Binding the MAC to the requesting client address makes an
    # exfiltrated token useless from anywhere but the minting browser's
    # connection path.
    msg = f"{slug}\x00{webroot}\x00{exp}\x00{client}".encode()
    return hmac.new(_TOKEN_SECRET, msg, hashlib.sha256).hexdigest()[:43]


def _make_token(slug: str, webroot: str, client: str = "") -> str:
    exp = int(time.time()) + _TOKEN_TTL_SECS
    return f"{exp}.{_mac(slug, webroot, exp, client)}"


def _check_token(slug: str, webroot: str, token: str, client: str = "") -> bool:
    # Round-3 F2: bound the token before parsing — a multi-thousand-digit
    # expiry passes isdigit() but blows past the int conversion digit limit
    # (ValueError → 500). Real tokens are ~55 chars.
    if len(token) > 128:
        return False
    exp_s, _, mac = token.partition(".")
    if not exp_s.isdigit() or not mac:
        return False
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(_mac(slug, webroot, exp, client), mac)


def _resolve_webroot(slug: str) -> Path | None:
    """Resolve + authorize the local web root for a webapp artifact.

    Returns None (fail closed) unless the artifact exists, is kind=webapp,
    carries an app_dir, and the resolved dir sits under an allow-listed root.
    """
    store = get_default_store()
    try:
        art = store.get(slug)
    except (ArtifactNotFoundError, ArtifactValidationError):
        # Round-7 F1 defense-in-depth: even if the route regex and the
        # store grammar ever drift again, a store-side validation reject
        # must fail closed (404), never propagate as a 500.
        return None
    meta = getattr(art, "webapp_metadata", None)
    if art.kind != "webapp" or meta is None:
        return None
    raw = (meta.app_dir or "").strip()
    if not raw:
        return None
    try:
        app_dir = Path(os.path.expanduser(raw)).resolve()
    except (OSError, ValueError):
        # ValueError: embedded NUL in a crafted app_dir (round-3 F1) — the
        # write path rejects control chars, but legacy metadata predating
        # that validation must still fail closed, never 500.
        return None
    if not app_dir.is_dir():
        return None
    for root in _allowed_local_roots():
        try:
            app_dir.relative_to(root)
            break
        except ValueError:
            continue
    else:
        return None
    # Round-2 F1: the web root is REQUIRED to be `app_dir/public` — the
    # deploy-contract layout. Serving app_dir itself as a fallback would turn
    # any workspace directory that happens to contain an index.html into a
    # fully-listable web root (source files, config JSON, ...), reachable by
    # the preview's own scripts. No public/ → no local preview (the card
    # falls back to the remote deployment or the status hero).
    public = app_dir / "public"
    try:
        if not public.is_dir():
            return None
        resolved = public.resolve()
        # Round-1 F1: `public` itself may be a symlink planted by a crafted
        # app_dir tree. The resolved web root must stay INSIDE the validated
        # app_dir.
        resolved.relative_to(app_dir)
        return resolved
    except (OSError, ValueError):
        return None


# ── Round-7 F2: remote framability probe ─────────────────────────────────
# Pre-existing base stacks were created with the OLD ResponseHeadersPolicy
# (X-Frame-Options: SAMEORIGIN), which browsers enforce by silently blanking
# the dashboard's live-site iframe. The frontend cannot read those headers
# cross-origin, but the gateway can: probe the (strictly validated) public
# CloudFront URL once and tell the card whether a remote iframe would render.
# Any uncertainty answers "not framable" — the card then shows its status
# hero with the plain link instead of a blank frame.
_CF_HOST_RE = re.compile(r"^[a-z0-9]+\.cloudfront\.net$")
_FRAMABLE_CACHE: dict[str, tuple[float, bool]] = {}
_FRAMABLE_CACHE_TTL = 300.0
_FRAMABLE_CACHE_MAX = 128


def _probe_url_allowed(url: str) -> bool:
    """SSRF gate: only https URLs on an exact *.cloudfront.net host are ever
    probed (mirrors the frontend's framablePreviewUrl allow-list)."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    return bool(parsed.hostname and _CF_HOST_RE.match(parsed.hostname))


async def _remote_framable(url: str) -> bool:
    if not url or not _probe_url_allowed(url):
        return False
    now = time.time()
    cached = _FRAMABLE_CACHE.get(url)
    if cached and cached[0] > now:
        return cached[1]
    framable = False
    try:
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.head(url, allow_redirects=False) as resp:
                if "X-Frame-Options" not in resp.headers:
                    csp = resp.headers.get("Content-Security-Policy", "")
                    fa = ""
                    for directive in csp.split(";"):
                        d = directive.strip()
                        if d.lower().startswith("frame-ancestors"):
                            fa = d.lower()
                            break
                    framable = not fa or "localhost" in fa or "127.0.0.1" in fa or "kirocrew.localhost" in fa
    except Exception:  # noqa: BLE001 — network failure = not framable
        framable = False
    if len(_FRAMABLE_CACHE) >= _FRAMABLE_CACHE_MAX:
        _FRAMABLE_CACHE.clear()
    _FRAMABLE_CACHE[url] = (now + _FRAMABLE_CACHE_TTL, framable)
    return framable


def _public_url_for(slug: str) -> str:
    """Blocking helper: read the artifact's deployed public_url ("" if none)."""
    try:
        art = get_default_store().get(slug)
    except (ArtifactNotFoundError, ArtifactValidationError):
        return ""
    meta = getattr(art, "webapp_metadata", None)
    dt = getattr(meta, "deploy_target", None) if meta else None
    return (getattr(dt, "public_url", "") or "").strip()


async def api_artifact_app_preview(request: web.Request) -> web.Response:
    """Mint a preview base URL for a webapp artifact's local copy (authed)."""
    slug = request.match_info.get("slug", "")
    if not _SLUG_RE.match(slug):
        return web.json_response({"error": "invalid slug"}, status=400)
    if not _PLATFORM_SUPPORTED:
        return web.json_response({"available": False})
    if not is_direct_local_request(request):
        _audit("webapp_preview.mint", "denied_remote", slug)
        return web.json_response({"available": False})
    # Round-1 F2: store lookup + path resolution are blocking filesystem
    # work — keep them off the event loop.

    def _resolve() -> Path | None:
        wr = _resolve_webroot(slug)
        if wr is None or not (wr / "index.html").is_file():
            return None
        return wr
    webroot = await asyncio.to_thread(_resolve)
    if webroot is None:
        # One shape for every miss (absent artifact / no app_dir / outside
        # roots / no index) — the card falls back to the remote iframe when
        # the deployed site is provably framable, else its status hero.
        _audit("webapp_preview.mint", "denied", slug)
        public_url = await asyncio.to_thread(_public_url_for, slug)
        return web.json_response({
            "available": False,
            "remote_framable": await _remote_framable(public_url),
        })
    _audit("webapp_preview.mint", "allowed", slug)
    token = _make_token(slug, str(webroot), client=request.remote or "")
    return web.json_response({
        "available": True,
        "base": f"{APP_PREVIEW_PREFIX}{slug}/{token}/",
        "expires_in": _TOKEN_TTL_SECS,
    })


async def serve_artifact_app_file(request: web.Request) -> web.Response:
    """Serve one static file from a webapp artifact's local web root.

    Auth = the HMAC path token (this route is on the middleware bypass list;
    sandboxed iframes have no cookies to send). Fails closed as 404 for every
    invalid condition to avoid oracle behavior.
    """
    slug = request.match_info.get("slug", "")
    token = request.match_info.get("token", "")
    rel = request.match_info.get("path", "")
    if not _SLUG_RE.match(slug) or not _PLATFORM_SUPPORTED:
        raise web.HTTPNotFound()
    # Round-1 F2: the whole resolve→validate→read pipeline is blocking
    # filesystem I/O — run it in a worker thread and keep the event loop
    # free (a slow disk or large file must never freeze the gateway).
    body, ctype = await asyncio.to_thread(
        _resolve_and_read, slug, token, rel, request.remote or "")
    if body is None or ctype is None:
        # Round-5 F3: this route bypasses session auth (the path token IS the
        # credential), so every authorization decision must leave an SEL
        # record — rejected probing included. The token itself is never
        # logged.
        _audit("webapp_preview.serve", "denied", f"{slug}:{rel}")
        raise web.HTTPNotFound()
    _audit("webapp_preview.serve", "allowed", f"{slug}:{rel}")
    resp = web.Response(body=body, content_type=ctype)
    # Round-2 F1 (egress): beyond the opaque-origin sandbox, pin every fetch
    # destination to the serving origin — a malicious app tree can render,
    # but its scripts cannot exfiltrate file contents to an external host
    # (connect/img/script/style all fall under default-src 'self').
    resp.headers["Content-Security-Policy"] = (
        "sandbox allow-scripts; "
        "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
        "frame-ancestors 'self'"
    )
    # Round-2 F2 + Round-4 F1: the sandboxed document runs with an OPAQUE
    # origin, so its module-script loads are cross-origin and need CORS —
    # but a wildcard ACAO on EVERY response also let a malicious app script
    # fetch() sibling data files (config.json, index.html) and read them for
    # exfil via self-navigation. Scope CORS to the content types that
    # actually require it to execute (module JS, wasm, fonts); data-bearing
    # types stay opaque to cross-origin readers, closing the read channel.
    if ctype in _CORS_OK_TYPES or ctype.startswith("font/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _audit(operation: str, outcome: str, resources: str) -> None:
    """SEL-audit a preview authorization decision (sanitized: no token)."""
    try:
        sel().log_api_access(
            caller="dashboard:webapp-preview",
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources[:512],
        )
    except Exception:
        logger.debug("SEL emit failed for %s", operation, exc_info=True)


def _resolve_and_read(
    slug: str, token: str, rel: str, client: str = ""
) -> tuple[bytes | None, str | None]:
    """Blocking helper: full token check + path validation + safe read.

    Returns (None, None) for every rejection (single 404 shape, no oracle).
    """
    webroot = _resolve_webroot(slug)
    if webroot is None or not _check_token(slug, str(webroot), token, client):
        return None, None
    if not rel or rel.endswith("/"):
        rel = rel + "index.html"
    # Reject dotfile components outright (manifests, .git, .env style files
    # in the app tree must never be exposed through the preview channel).
    parts = [p for p in rel.split("/") if p]
    if any(p.startswith(".") for p in parts):
        return None, None
    try:
        target = (webroot / Path(*parts)).resolve()
    except OSError:
        return None, None
    # Containment check AFTER full resolution: covers ../ traversal and
    # symlinks pointing outside the web root in one test.
    try:
        target.relative_to(webroot)
    except ValueError:
        return None, None
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        return None, None
    # Round-1 F1: never serve sensitive paths even inside the web root, and
    # read through the inode-pinned nolink helper (O_NOFOLLOW + fstat +
    # fd-realpath containment) so a hardlink/symlink swapped in after the
    # containment check above cannot exfiltrate anything.
    if is_sensitive_path(str(target)):
        return None, None
    try:
        if target.stat().st_size > _MAX_FILE_BYTES:
            return None, None
    except OSError:
        return None, None
    body = safe_read_file_bytes_nolink(str(target), within_root=str(webroot))
    if body is None:
        return None, None
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    # Per the backend-security-controls rule: files
    # under app_dir are agent-influenced output reaching the dashboard
    # surface — every response must pass the mandatory redaction scan.
    # Gating the scan on the GUESSED content type left a bypass: credential
    # text in an extensionless file (or .bin) guesses as octet-stream and
    # skipped both scanners. Scan the decoded bytes of EVERY body instead
    # (binary content decodes lossily but any embedded ASCII credential
    # still surfaces). Redacting in place would silently corrupt source
    # assets, so a file that trips either scanner is REJECTED (fail
    # closed) instead of served mangled.
    text = body.decode("utf-8", errors="replace")
    _, cred_hits = redact_credentials(text)
    _, url_hits = redact_exfiltration_urls(text)
    if cred_hits or url_hits:
        return None, None
    return body, ctype


def register_webapp_preview_routes(app: web.Application) -> None:
    app.router.add_get("/api/artifacts/{slug}/app-preview", api_artifact_app_preview)
    app.router.add_get(
        APP_PREVIEW_PREFIX.rstrip("/") + "/{slug}/{token}/{path:.*}",
        serve_artifact_app_file,
    )
