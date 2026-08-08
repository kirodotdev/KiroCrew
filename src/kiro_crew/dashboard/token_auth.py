"""Dashboard token authentication.

HMAC-SHA256 token generation, validation, IP binding, consumption
tracking, and aiohttp middleware for Slack-gated dashboard access.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.dashboard.origin import (
    is_https_request,
    is_loopback,
    is_proxied_request,
)
from kiro_crew.dashboard.refresh_tokens import (
    MAX_REFRESH_TTL_SECS,
    REFRESH_COOKIE_PATH,
    cookie_jar_needs_pruning,
    foreign_port_cookies,
    generate_refresh_token,
    refresh_cookie_name,
)

# Canonical HMAC-secret definitions live in token_secret to break the import
# cycle between token_auth and refresh_tokens. Re-exported here for backwards
# compatibility — callers elsewhere import these names from token_auth. The
# fork keeps the LAZY _get_secret() (NOT an eager module-level _SECRET =
# _load_or_create_secret()) so that merely importing this module never writes
# token_signing.key into $KIROCREW_HOME (the CLI imports token_auth for every
# kirocrew subcommand; an import-time write would break gateway --seed and
# pollute the home for read-only commands).
from kiro_crew.dashboard.token_secret import (  # noqa: F401  # re-exports
    _SECRET_KEY_FILE,
    _get_secret,
    _load_or_create_secret,
)
from kiro_crew.sel import sel as _sel_fn

logger = logging.getLogger(__name__)


_REVOCATION_FILE = "token_revocation.gen"


def _load_revocation_gen() -> int:
    """Return the persisted revocation generation counter (0 if unset).

    Every minted token embeds the current ``gen``; cookie validation rejects a
    token whose ``gen`` is below the current value. ``revoke_all_sessions()``
    bumps and persists it, so an operator ``kirocrew logout`` invalidates ALL
    outstanding tokens/cookies — including established browser cookies, which
    the nonce store (per-process, cleared on restart) could not. Persisting the
    counter is what lets it survive a gateway restart WITHOUT logging users out:
    the gen is reloaded unchanged, so previously-issued cookies still match.
    """
    from kiro_crew.config.loader import config_dir

    try:
        p = config_dir() / _REVOCATION_FILE
        if p.exists():
            return int(p.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        logger.warning("could not read token revocation counter; assuming 0", exc_info=True)
    return 0


def _bump_revocation_gen() -> int:
    """Increment and persist the revocation counter. Returns the new value.

    Falls back to an in-memory bump if the file is unwritable (revocation still
    holds for the life of this process, the pre-existing best-effort behaviour).
    """
    global _REVOCATION_GEN
    _REVOCATION_GEN += 1
    from kiro_crew.config.loader import config_dir

    try:
        p = config_dir() / _REVOCATION_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(_REVOCATION_GEN), encoding="utf-8")
    except OSError:
        logger.warning("could not persist token revocation counter", exc_info=True)
    return _REVOCATION_GEN


_REVOCATION_GEN = _load_revocation_gen()


# -- Per-session access-cookie revocation -------------------------------------

_REVOKED_NONCES_FILE = "token_revoked_nonces.json"


class RevokedNonceStore:
    """Persisted denylist of explicitly-revoked access-cookie nonces.

    Enables PER-SESSION logout (CWE-613). The access cookie is a self-contained
    HMAC-signed token, so clearing it client-side (``Set-Cookie max_age=0``)
    does NOT stop a saved copy being replayed until its ``session_exp`` (up to
    20h). ``POST /api/auth/logout`` records the caller's access-cookie ``nonce``
    here; :func:`validate_token` (cookie path) then rejects any token whose
    nonce is listed — killing exactly that one session WITHOUT bumping the
    global generation counter (``revoke_all_sessions``), which would log out
    every other user too.

    Persisted to disk (mode ``0600``) so a revoked cookie stays dead across a
    gateway restart — unlike the in-memory link-nonce set in
    :class:`TokenStateManager`, which is restart-cleared and intentionally NOT
    consulted for cookies. Each entry stores the token's own ``session_exp`` as
    an eviction floor: once that passes, the expiry check rejects the token
    anyway, so the record is dropped and the file cannot grow without bound.
    """

    def __init__(self, state_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._revoked: dict[str, float] = {}  # nonce -> session_exp (eviction floor)
        self._state_path = state_path
        self._load()

    def revoke(self, nonce: str, session_exp: float) -> None:
        """Record *nonce* as revoked until *session_exp*, evicting expired entries."""
        now = time.time()
        with self._lock:
            self._revoked[nonce] = session_exp
            # Opportunistic eviction so a stream of logouts cannot grow the file.
            expired = [n for n, exp in self._revoked.items() if exp < now]
            for n in expired:
                self._revoked.pop(n, None)
        self._persist()

    def is_revoked(self, nonce: str) -> bool:
        """Return True if *nonce* is on the denylist and not yet past its floor."""
        now = time.time()
        with self._lock:
            exp = self._revoked.get(nonce)
            if exp is None:
                return False
            if exp < now:
                # Stale entry — the token is already rejected by the expiry
                # check, so drop it lazily (no persist on this hot read path).
                self._revoked.pop(nonce, None)
                return False
            return True

    def clear_all(self) -> None:
        """Wipe all revoked-nonce records (used by tests)."""
        with self._lock:
            self._revoked.clear()
        self._persist()

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("could not read revoked-nonce store; starting empty", exc_info=True)
            return
        now = time.time()
        with self._lock:
            for entry in data.get("revoked_nonces", []):
                if isinstance(entry, dict) and "nonce" in entry and "exp" in entry:
                    try:
                        exp = float(entry["exp"])
                    except (TypeError, ValueError):
                        continue
                    if exp >= now:  # skip already-expired records on load
                        self._revoked[str(entry["nonce"])] = exp

    def _persist(self) -> None:
        if self._state_path is None:
            return
        with self._lock:
            data = {
                "revoked_nonces": [{"nonce": n, "exp": exp} for n, exp in self._revoked.items()]
            }
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
                tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    # Security-sensitive state (revoked session nonces). A chmod
                    # failure must be observable, matching token_secret.py.
                    logger.warning(
                        "could not set 0600 on revoked-nonce store %s; "
                        "file may be readable by other users",
                        tmp,
                        exc_info=True,
                    )
                os.replace(tmp, self._state_path)
            except OSError:
                logger.warning("could not persist revoked-nonce store", exc_info=True)


_revoked_store_singleton: RevokedNonceStore | None = None
_revoked_store_lock = threading.Lock()


def _get_revoked_store() -> RevokedNonceStore:
    """Return the lazily-initialized revoked-nonce store singleton.

    Lazy (not module-level) so merely importing token_auth never touches the
    filesystem — the CLI imports this module for every subcommand.
    """
    global _revoked_store_singleton
    if _revoked_store_singleton is None:
        with _revoked_store_lock:
            if _revoked_store_singleton is None:
                from kiro_crew.config.loader import config_dir

                _revoked_store_singleton = RevokedNonceStore(
                    state_path=config_dir() / _REVOKED_NONCES_FILE
                )
    return _revoked_store_singleton


class TokenStateManager:
    """Thread-safe manager for token authentication state.

    Encapsulates all mutable token state (nonces, IP bindings, consumption)
    with consistent locking. Uses OrderedDict for O(1) nonce eviction.

    Threading model: This class uses threading.Lock (not asyncio.Lock) because
    token operations are called from both async contexts (aiohttp middleware)
    and sync contexts (CLI commands like `kirocrew token`). The lock hold time
    is minimal (dict operations only), so blocking the event loop is negligible.
    """

    def __init__(self, max_concurrent_nonces: int = 50) -> None:
        self._lock = threading.Lock()
        self._max_nonces = max_concurrent_nonces
        # OrderedDict maintains insertion order for O(1) oldest eviction
        self._nonces: OrderedDict[str, float] = OrderedDict()
        # Observation latches for the Security Posture surface only — never read
        # by an auth decision. See bind_ip() / proxied_pin_observed().
        self._ip_bindings: dict[str, tuple[str, float, bool]] = {}  # token → (ip, exp, proxied)
        self._consumed: dict[str, float] = {}  # token → exp

    def register_nonce(self, nonce: str, expiry: float) -> str | None:
        """Register a nonce with its expiry time, evicting oldest if over limit."""
        with self._lock:
            self._nonces[nonce] = expiry
            self._nonces.move_to_end(nonce)  # Most recent at end
            if len(self._nonces) > self._max_nonces:
                evicted, _ = self._nonces.popitem(last=False)
                return evicted
            return None

    def is_nonce_valid(self, nonce: str) -> tuple[bool, str]:
        """Check if nonce is valid. Returns (valid, reason).

        Deny-by-default: rejects if no nonces registered or nonce not in set.
        Refreshes the nonce's eviction position on each successful check so
        that actively-used sessions are not evicted by newer token grants.
        """
        with self._lock:
            if not self._nonces:
                return False, "no active sessions"
            if nonce not in self._nonces:
                return False, "token superseded"
            self._nonces.move_to_end(nonce)
            return True, ""

    def bind_ip(self, token: str, ip: str, session_exp: float, proxied: bool = False) -> None:
        """Bind a token to a client IP address.

        ``proxied`` records that *ip* came from a same-host proxy rather than
        from the client itself, which means this binding pins the token to the
        proxy and is therefore shared by every client behind it. It is an
        observation for the Security Posture surface only — it does not change
        the binding or how :meth:`check_ip` compares it.
        """
        with self._lock:
            self._ip_bindings[token] = (ip, session_exp, proxied)

    def proxied_pin_observed(self, now: float) -> bool | None:
        """Report the pin scope of the sessions that are LIVE at *now*.

        ``None`` = no session is currently pinned, so there is no scope to
        report — which is NOT the same as "pins are effective" and must not be
        rendered as if it were. ``True`` = at least one live session is pinned to
        a same-host proxy's address and is therefore shared by every client
        behind it. ``False`` = live sessions are pinned to client addresses.

        Derived from the bindings rather than from a latch, deliberately. A latch
        would outlive the sessions it describes: one tunnelled login would report
        SHARED until the gateway restarted, even after that session expired and
        the user went back to direct access — the same class of stale claim this
        reporting exists to remove.

        Filters on ``exp`` rather than trusting :meth:`evict_expired` to have
        run, so the answer never depends on when eviction last happened.
        """
        with self._lock:
            live_proxied = [p for _, exp, p in self._ip_bindings.values() if exp > now]
            if not live_proxied:
                return None
            return any(live_proxied)

    def check_ip(self, token: str, ip: str) -> bool:
        """Check if token is bound to the given IP (or unbound)."""
        with self._lock:
            entry = self._ip_bindings.get(token)
            return entry is None or entry[0] == ip

    def mark_consumed(self, token: str, session_exp: float) -> None:
        """Mark a token as consumed (used for one-time token patterns)."""
        with self._lock:
            self._consumed[token] = session_exp

    def is_consumed(self, token: str) -> bool:
        """Check if a token has been consumed."""
        with self._lock:
            return token in self._consumed

    def try_consume(self, token: str, session_exp: float) -> bool:
        """Atomically mark token consumed if not already.

        Returns True if this call consumed it, False if already consumed.
        """
        with self._lock:
            if token in self._consumed:
                return False
            self._consumed[token] = session_exp
            return True

    def evict_expired(self, now: float) -> None:
        """Remove all expired entries from all state stores."""
        with self._lock:
            # Evict expired IP bindings
            expired_tokens = [t for t, (_, exp, _p) in self._ip_bindings.items() if exp < now]
            for t in expired_tokens:
                self._ip_bindings.pop(t, None)
            # Evict consumed tokens independently using their own expiry
            expired_consumed = [t for t, exp in self._consumed.items() if exp < now]
            for t in expired_consumed:
                self._consumed.pop(t, None)
            # Evict expired nonces
            expired_nonces = [n for n, exp in self._nonces.items() if exp < now]
            for n in expired_nonces:
                self._nonces.pop(n, None)

    def clear_all(self) -> None:
        """Clear all token state (nonces, IP bindings, consumed tokens)."""
        with self._lock:
            self._nonces.clear()
            self._ip_bindings.clear()
            self._consumed.clear()


# Maximum concurrent valid tokens before oldest is evicted.
# Raised from 5 to 50 so pending Slack challenge links aren't evicted
# by other token minting activity (crons, dashboard links, etc.).
MAX_CONCURRENT_NONCES = 50

# Module-level singleton instance
_state: TokenStateManager = TokenStateManager(max_concurrent_nonces=MAX_CONCURRENT_NONCES)

# Public static-asset prefixes exempt from token auth (GET of non-secret files
# the dashboard HTML references before the auth cookie is established).
# /fonts/ holds the self-hosted AWS Diatype woff2 files (public.html @font-face
# url('/fonts/...')); without the exemption the auth middleware 403s each font
# request and the browser, parsing the 403 HTML body as a font, logs
# "invalid sfntVersion" and falls back to a default typeface.
# /artifact-app/ is the webapp-artifact local preview channel: auth is the
# HMAC path token minted by the authed /api/artifacts/{slug}/app-preview
# endpoint (sandboxed preview iframes carry no cookies). See
# dashboard/handlers/webapp_preview.py for the full security model.
# /vendor/ holds same-origin vendored JS (the Tailwind v4 browser runtime at
# /vendor/tailwindcss-browser.js plus app import-map shims) that sandboxed
# widget/artifact iframes load via <script src>. Those iframes are null-origin
# srcdoc sandboxes (widgetSrcdoc.ts), so the request carries no auth cookie;
# without the exemption the middleware 403s the runtime and every <mcwidget>
# renders unstyled (Tailwind classes silently ignored, inline styles only).
# Same exposure class as /assets/: static non-secret files.
_BYPASS_PREFIXES = ("/assets/", "/static/", "/fonts/", "/vendor/", "/artifact-app/")
_BYPASS_EXACT = {
    "/logo.png",
    "/manifest.json",
    "/sw.js",
    "/pcm-worklet.js",
    "/api/token/local",
    "/api/shutdown",
    # `kirocrew logout` (CLI) authenticates with loopback + the local secret via
    # an X-Local-Secret header, exactly like /api/token/local and /api/shutdown
    # above — api_logout re-checks BOTH itself before revoking anything. It must
    # bypass the cookie/token gate for the same reason they do: the CLI holds no
    # dashboard token, and the middleware only honors X-Internal-Secret, so
    # without this entry every `kirocrew logout` is denied 403 by the middleware
    # before the handler (and its audit events) ever run.
    "/api/logout",
    "/api/theme/boot",
    # Liveness/readiness probes (rec #6): orchestrators / load balancers carry
    # no auth cookie, so these must be reachable without a token. Each exposes
    # only liveness + coarse readiness booleans + the build version — no
    # secrets, paths, ids, or user/session content.
    "/api/health",
    "/api/live",
    "/api/ready",
}

# Exact-path bypasses that apply to SOME methods only, path -> allowed methods.
#
# A path-only bypass is unsound whenever another route pattern also matches the
# same literal path under a different method: the entry opens every one of those
# methods, not just the self-authenticating one it was written for. Scoping the
# entry to the method whose handler does its own auth leaves the rest on the
# ordinary token gate. Every self-authenticating webhook belongs here rather than
# in the path-only set above, whether or not another route currently collides —
# the collision is a property of the route table, which moves.
#
# ``POST /api/hooks/agent`` is the inbound agent webhook: external systems (CI
# runners, code-review bots, deploy pipelines) post here holding a webhook token
# and nothing else — no dashboard cookie, no gateway IPC secret. The handler does
# its OWN auth (api_hooks_agent -> _verify_hook_token compares the bearer against
# the sha256 of every stored token entry with hmac.compare_digest and refuses
# with 401 when none match, including when no token exists at all, so the
# endpoint is closed by default on a fresh install). It is a deliberate exposure
# decision: a valid token authorizes a real agent turn with full tool access, so
# the handler also rate-limits repeated failures per source
# (webhooks.auth_throttle) and records every 401 in the run history.
#
# For that entry the method scope is load-bearing, not tidiness. The literal
# string ``agent`` also matches the ``{hook_id}`` wildcard of the dashboard's own
# hook CRUD routes — PUT and DELETE ``/api/hooks/{hook_id}`` — whose handler
# (api_hook_detail) authenticates via the dashboard token alone. Unscoped, both
# reach it with no credential of any kind.
#
# ``POST /api/messaging/teams`` is the Microsoft Teams inbound webhook: Bot
# Framework (Microsoft's servers, no dashboard cookie) posts activities there and
# the handler does its OWN auth, validating the Bot Framework JWT (issuer +
# App-ID audience + signature) before processing. Only POST is routed today, so
# the scope closes nothing yet — it is here so the shape a future entry gets
# copied from is the safe one.
_BYPASS_EXACT_METHODS: dict[str, frozenset[str]] = {
    "/api/hooks/agent": frozenset({"POST"}),
    "/api/messaging/teams": frozenset({"POST"}),
}

# Anchored bypass for installed-app static UI bundles only (federated-app
# design). Matches /apps/{name}/ui/<anything>, where {name} is the
# canonical app-name pattern. Must NOT match /apps/{name}/api/... — that
# path is the gateway-authenticated reverse proxy to the app backend
# (handle_app_api_proxy in kiro_crew/apps/routes.py) and continues to
# require a valid token. The bounded character class prevents ReDoS.
_APPS_UI_BYPASS_RE = re.compile(r"^/apps/[a-z0-9][a-z0-9_-]*/ui/")

# Single source of truth for "paths that are NEVER the SPA (Single-Page
# Application) shell." One list, read by BOTH consumers below so they cannot
# drift:
#   1. the auth middleware — never serves these the shell on a cold start
#   2. server.py's SPA fallback — never serves these index.html on a 404
# Each entry owns its own response: gated JSON (/api/), the OpenAI-compat
# data API (/v1/), and static bundles. Any GET/HEAD path NOT under one of
# these is a client-side SPA navigation the server answers with index.html.
#
# NOTE: /apps/ is intentionally NOT in this tuple. /apps/ path handling is
# governed solely by _APPS_SPA_EXCLUDED_RE in _is_spa_shell_request:
#   - bare /apps/{name}              → SPA shell (browser refresh must work)
#   - /apps/{name}/api|ui/...        → real server handler (proxy / static)
#   - any other /apps/ path          → SPA shell (React Router owns it, e.g.
#                                      /apps/detail/{name}, /apps/migrate/{name})
# test_no_get_route_outside_shell_exclusions validates /apps/ routes against
# _APPS_SPA_EXCLUDED_RE directly, not this tuple.
SPA_FALLBACK_EXCLUDED_PREFIXES = (
    "/api/",
    "/v1/",
    "/assets/",
    "/static/",
    "/sprites/",
    "/vendor/",
    "/fonts/",
    "/app-assets/",
    "/artifact-app/",
)

# App window entries (`/app-windows/<app>/<name>.html`) are their own Vite bundles, served
# from this origin and authenticated by the same session cookie as the
# dashboard. Without an exclusion they land in the SPA-shell fallback, which
# answers UNAUTHENTICATED GETs so the token bootstrap can load — meaning the
# shell would be handed out for these paths with no session at all. The set is
# registered at startup by dashboard/server.py from the SAME filesystem
# discovery that registers the routes, so route and exclusion cannot drift:
# a served window entry is excluded by construction.
_APP_WINDOW_EXCLUDED_PATHS: frozenset[str] = frozenset()


def register_app_window_paths(paths: Iterable[str]) -> None:
    """Exclude app window-entry paths from the SPA-shell fallback.

    Called once at startup with the concrete route paths server.py registered
    (e.g. every discovered ``/app-windows/<app>/<name>.html``). Exact-path matching, not
    prefixes: the routes are enumerated files, so the full set is known.
    """
    global _APP_WINDOW_EXCLUDED_PATHS
    _APP_WINDOW_EXCLUDED_PATHS = frozenset(paths)


# Regex that matches /apps/ paths with real server-side handlers, which must NOT
# be shadowed by the SPA shell. apps/routes.py registers exactly two
# sub-namespaces under /apps/{name}: /ui/ (the app's static bundle) and /api/
# (the gateway-authenticated reverse proxy to the app backend). Every other
# /apps/ path is a client-side React Router entry and needs the shell.
#
# Naming those two sub-namespaces is load-bearing. Matching any sub-path (the
# earlier `^/apps/[a-z0-9][a-z0-9_-]*/`) read the FIRST segment as the app name,
# so the router's own /apps/detail/{name} and /apps/migrate/{name} entries were
# treated as server routes and returned 404 on direct navigation or refresh.
# test_apps_router_subpaths_are_spa_shell locks that in, and
# test_apps_server_routes_are_excluded_from_shell guards the other direction by
# reading the live route literals out of apps/routes.py.
#
# The trailing slash is also load-bearing. Both handlers are registered with a
# path segment after the sub-namespace (`/apps/{name}/ui/{path:.*}` and
# `/apps/{name}/api/{path:.*}`), and no bare `/apps/{name}/ui` or
# `/apps/{name}/api` route exists. An earlier `(?:/|$)` therefore excluded two
# paths that no handler serves, and since the app name occupies the same segment
# position as the router's `detail`/`migrate` verbs, an app named literally
# "api" or "ui" got a 404 on /apps/detail/api. Requiring the slash costs no
# real server route and resolves that collision toward the client route.
_APPS_SPA_EXCLUDED_RE = re.compile(r"^/apps/[a-z0-9][a-z0-9_-]*/(?:api|ui)/")


def _is_spa_shell_request(request: web.Request) -> bool:
    """True if this GET/HEAD request should be answered with the SPA shell.

    Why: lets the React app boot on a cold start (token expired) so it can run
    its own ``/api/auth/me`` -> ``/api/auth/refresh`` recovery, instead of a
    dead-end 403 whose recovery JS never loads. Safe because the shell is
    static and secret-free and every data namespace is excluded.

    Special case for ``/apps/``: only ``/apps/{name}/api/...`` and
    ``/apps/{name}/ui/...`` have server-side handlers. Every other ``/apps/``
    path is a React Router navigation entry with no server route -- bare
    ``/apps/{name}``, plus ``/apps/detail/{name}`` and ``/apps/migrate/{name}``
    -- so those must fall through to the SPA shell.
    """
    if request.method not in ("GET", "HEAD"):
        return False
    path = request.path
    # Fast-path: most paths don't start with /apps/
    if not path.startswith("/apps/"):
        if path in _APP_WINDOW_EXCLUDED_PATHS:
            return False
        return not path.startswith(SPA_FALLBACK_EXCLUDED_PREFIXES)
    # /apps/ sub-namespace: exclude only the paths apps/routes.py actually
    # serves (/apps/{name}/api/... and /apps/{name}/ui/...). Everything else
    # under /apps/ belongs to React Router — bare /apps/{name} as well as
    # /apps/detail/{name} and /apps/migrate/{name} — and gets the shell.
    return not _APPS_SPA_EXCLUDED_RE.match(path)


# Link click window — URL must be opened within this time
LINK_WINDOW_SECS = 300  # 5 minutes
# Maximum session TTL — cookie cannot exceed this
MAX_SESSION_TTL_SECS = 20 * 3600  # 20 hours

_403_HTML = (
    "<!DOCTYPE html><html><head><meta charset='UTF-8'><meta name='viewport' "
    "content='width=device-width,initial-scale=1'><title>Access Denied</title>"
    "<style>"
    "*{{margin:0;padding:0;box-sizing:border-box}}"
    "body{{font-family:system-ui,-apple-system,sans-serif;display:flex;"
    "align-items:center;justify-content:center;height:100vh;"
    "background:#f8fafc;color:#1e293b}}"
    ".c{{text-align:center;max-width:420px;padding:24px}}"
    ".logo{{font-size:48px;margin-bottom:16px}}"
    "h1{{font-size:20px;margin-bottom:8px}}"
    "p{{color:#64748b;font-size:13px;line-height:1.6;margin-bottom:16px}}"
    "code{{background:#e2e8f0;padding:2px 6px;border-radius:4px;color:#c2410c;"
    "font-size:13px}}"
    "input{{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #cbd5e1;"
    "background:#fff;color:#1e293b;font-size:14px;margin-bottom:10px;outline:none}}"
    "input:focus{{border-color:#f97316}}"
    "button{{padding:8px 24px;border-radius:8px;border:none;cursor:pointer;"
    "background:#f97316;color:#fff;font-size:14px;font-weight:600}}"
    "button:hover{{background:#ea580c}}"
    ".err{{color:#dc2626;font-size:12px;margin-top:8px;display:none}}"
    "@media(prefers-color-scheme:dark){{body{{background:#0f1117;color:#e2e8f0}}"
    "p{{color:#94a3b8}}code{{background:#1e293b;color:#f97316}}"
    "input{{border-color:#334155;background:#1e293b;color:#e2e8f0}}"
    ".err{{color:#ef4444}}}}"
    "</style></head><body>"
    "<div class='c'>"
    "<div class='logo'>👻</div>"
    "<h1>403 — {reason}</h1>"
    "<p>Run <code>kirocrew token</code> in your terminal, then paste the URL below.</p>"
    "<input id='u' type='text' placeholder='Paste token URL or raw token…' autofocus>"
    "<button onclick='go()'>Connect</button>"
    "<div class='err' id='e'>Invalid URL</div>"
    "</div>"
    "<script>"
    "function go(){{var v=document.getElementById('u').value.trim();if(!v)return;"
    "var t;try{{var u=new URL(v);t=u.searchParams.get('token')}}"
    "catch(_){{t=v}}if(t){{window.location.href="
    "window.location.protocol+'//'+window.location.host+'?token='+encodeURIComponent(t)}}"
    "else{{document.getElementById('e').style.display='block'}}}}"
    "document.getElementById('u').addEventListener('keydown',"
    "function(e){{if(e.key==='Enter')go()}});"
    "</script>"
    "</body></html>"
)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (padding % 4))


def _sign(payload: bytes) -> str:
    return _b64url_encode(hmac.new(_get_secret(), payload, hashlib.sha256).digest())


def generate_token(
    user_id: str,
    ttl_seconds: int = 3600,
    *,
    app: str = "",
    prompt: str = "",
    extra: dict[str, str] | None = None,
    register_nonce: bool = True,
) -> str:
    """Return ``base64url(payload).base64url(signature)``.

    The token carries two expiry times:
    - ``exp``: link click window (5 minutes) — URL must be opened before this
    - ``session_exp``: cookie session TTL (capped at 20 hours)

    When *app* is provided, the token payload includes ``"app": app`` so
    downstream middleware can extract the verified app identity.

    When *prompt* is provided, it is included in the signed payload so the
    dashboard can auto-submit the user's original Slack message. The prompt
    is covered by the HMAC signature to prevent tampering.

    *extra* adds further string claims to the signed payload — used by the
    Slack challenge-and-redirect flow to carry ``channel``, ``thread_ts`` and
    an existing linked ``session_key`` so the dashboard can reconnect to (or
    auto-link) the correct Slack-linked session instead of always spawning a
    fresh, disconnected one. Reserved keys (sub/exp/session_exp/iat/nonce/app/
    prompt) cannot be overridden.

    Up to ``_MAX_CONCURRENT_NONCES`` tokens can be valid concurrently.
    When the limit is exceeded, the oldest nonce is evicted (O(1) via OrderedDict).
    """
    _evict_expired()
    now = time.time()
    nonce = os.urandom(8).hex()
    session_ttl = min(ttl_seconds, MAX_SESSION_TTL_SECS)

    # register_nonce=False: the token will only ever be validated on the COOKIE
    # path (use_session_exp=True), which does not consult the link-nonce set.
    # Skipping registration keeps the exchanged session token OUT of the bounded
    # (50-slot) set so high-frequency link→session exchanges (self-nudge polling,
    # instance-iframe re-navigation) don't churn/evict pending one-time link
    # nonces (e.g. Slack challenge links). The nonce is still embedded in the
    # payload so the token remains individually revocable (RevokedNonceStore).
    if register_nonce:
        evicted = _state.register_nonce(nonce, now + session_ttl)
        if evicted:
            _sel_fn().log_api_access(
                caller=user_id,
                operation="nonce_evicted",
                outcome="ok",
                source="token_auth",
                resources=f"evicted_nonce={evicted}",
            )

    payload_dict: dict[str, object] = {
        "sub": user_id,
        "exp": now + LINK_WINDOW_SECS,
        "session_exp": now + session_ttl,
        "iat": now,
        "nonce": nonce,
        # Revocation generation: validate_token rejects a token whose gen is
        # below the current persisted value, so revoke_all_sessions() kills
        # established cookies (not just the per-process nonce store).
        "gen": _REVOCATION_GEN,
    }
    if app:
        payload_dict["app"] = app
    if prompt:
        payload_dict["prompt"] = prompt
    if extra:
        _reserved = {"sub", "exp", "session_exp", "iat", "nonce", "gen", "app", "prompt"}
        for k, v in extra.items():
            if k not in _reserved and isinstance(v, str) and v:
                payload_dict[k] = v
    payload = json.dumps(payload_dict, separators=(",", ":")).encode()
    encoded_payload = _b64url_encode(payload)
    signature = _sign(payload)
    return f"{encoded_payload}.{signature}"


def validate_token(token: str, *, use_session_exp: bool = False) -> tuple[bool, str, str]:
    """Return ``(valid, user_id, reason)``.

    When *use_session_exp* is ``True`` (cookie-based access), validates
    against ``session_exp`` instead of ``exp`` (link click window).
    """
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False, "", "malformed token"
    encoded_payload, sig = parts
    try:
        payload_bytes = _b64url_decode(encoded_payload)
    except Exception:
        return False, "", "invalid encoding"
    expected = _sign(payload_bytes)
    if not hmac.compare_digest(sig, expected):
        return False, "", "invalid signature"
    try:
        data = json.loads(payload_bytes)
    except Exception:
        return False, "", "invalid payload"
    exp_field = "session_exp" if use_session_exp else "exp"
    if time.time() > data.get(exp_field, data.get("exp", 0)):
        return False, "", "token expired"
    # Revocation generation: an explicit revoke_all_sessions() (e.g. kirocrew
    # logout) bumps the persisted counter. A token minted before that — link OR
    # cookie — carries a lower gen and is rejected. This is the ONLY check that
    # invalidates an established cookie (the nonce store is per-process and
    # restart-cleared; the HMAC secret is persisted, not rotated), so it is what
    # makes "revoke all sessions" actually revoke cookie sessions. Tokens minted
    # before this field existed default to gen 0, matching the initial counter.
    if int(data.get("gen", 0)) < _REVOCATION_GEN:
        return False, "", "session revoked"
    # Nonce is a single-use guard for the one-time LINK click only. For an
    # established session cookie (use_session_exp=True), a valid HMAC signature
    # plus an unexpired session_exp is sufficient — requiring the in-memory
    # nonce there would invalidate every live cookie on each gateway restart
    # (the nonce store is per-process), locking users out for no security gain.
    # Cookie revocation is handled by the gen check above, not the nonce.
    token_nonce = data.get("nonce", "")
    if use_session_exp:
        # Per-session logout (CWE-613): POST /api/auth/logout adds THIS cookie's
        # nonce to a persisted server-side denylist. Deny-by-default: a cookie
        # with no nonce cannot be checked against the denylist, so it is
        # rejected outright rather than silently skipping the revocation check.
        # Every token minted by generate_token carries a nonce, so this rejects
        # only malformed/forged cookies — without the nuclear
        # revoke_all_sessions() gen bump that kills every other session. (The
        # in-memory nonce *set* is still not consulted here — that would break
        # all live cookies on restart; only the explicit denylist is.)
        if not token_nonce:
            return False, "", "missing nonce"
        if _get_revoked_store().is_revoked(token_nonce):
            return False, "", "session revoked"
    else:
        valid, reason = _state.is_nonce_valid(token_nonce)
        if not valid:
            return False, "", reason
    return True, data.get("sub", ""), ""


def token_embed_parent_port(token: str) -> int | None:
    """Return the ``embed_parent_port`` claim from a validly-signed token, or None.

    Drives the CSP ``frame-ancestors`` allowlist for the multi-instance embed: the
    parent dashboard's port (the embedding desktop app's ``KIROCREW_PORT``) is
    carried as a signed claim minted at connect time, so the embedded remote can
    authorize exactly that loopback parent origin to frame it — no hardcoded port,
    no wildcard. Verifies HMAC signature + session expiry + revocation gen (a
    forged/revoked token yields None). The single-use link-nonce is intentionally
    NOT required: the claim is read on every framed document load for the life of
    the session, so it is validated on the cookie/session path.
    """
    if not token:
        return None
    valid, _uid, _reason = validate_token(token, use_session_exp=True)
    if not valid:
        return None
    try:
        data = json.loads(_b64url_decode(token.split(".", 1)[0]))
    except Exception:
        return None
    raw = data.get("embed_parent_port")
    if not isinstance(raw, str) or not raw.isdigit():
        return None
    port = int(raw)
    return port if 1 <= port <= 65535 else None


def validate_token_with_app(
    token: str, *, use_session_exp: bool = False
) -> tuple[bool, str, str, str]:
    """Return ``(valid, user_id, reason, app_name)``.

    Extends :func:`validate_token` by also extracting the ``app`` field
    from the token payload.  This avoids changing the existing
    ``validate_token`` signature.
    """
    valid, user_id, reason = validate_token(token, use_session_exp=use_session_exp)
    if not valid:
        return False, user_id, reason, ""
    # Extract app from payload
    app_name = ""
    try:
        payload_bytes = _b64url_decode(token.split(".")[0])
        data = json.loads(payload_bytes)
        app_name = data.get("app", "")
    except Exception:
        pass
    return valid, user_id, reason, app_name


def extract_prompt_from_token(token: str) -> str:
    """Extract the ``prompt`` field from a validated token payload.

    Validates the token first (deny-by-default). Returns the prompt
    string if valid and present, empty string otherwise.
    """
    valid, _user_id, _reason = validate_token(token)
    if not valid:
        return ""
    try:
        payload_bytes = _b64url_decode(token.split(".")[0])
        data = json.loads(payload_bytes)
        return data.get("prompt", "")
    except Exception as exc:
        logger.warning(
            "extract_prompt_from_token: post-validation decode failed (%s)", type(exc).__name__
        )
        return ""


def extract_claims_from_token(token: str, keys: tuple[str, ...]) -> dict[str, str]:
    """Extract selected string claims from a validated token payload.

    Validates the token first (deny-by-default). Returns a dict containing
    only the requested *keys* that are present and string-typed; returns an
    empty dict if the token is invalid. Used by the Slack challenge-redirect
    frontend to recover ``channel``/``thread_ts``/``session_key`` so it can
    reconnect to (or auto-link) the correct Slack-linked session.

    Validates against ``session_exp`` (use_session_exp=True), NOT the 5-minute
    link window: claim recovery happens after the user has clicked through and
    established a session, so binding it to the link ``exp`` would lose the
    thread context (channel/thread_ts/session_key) the moment the click window
    closed, breaking auto-link/reconnect for the rest of the session.
    """
    valid, _user_id, _reason = validate_token(token, use_session_exp=True)
    if not valid:
        return {}
    try:
        data = json.loads(_b64url_decode(token.split(".")[0]))
    except Exception as exc:
        logger.warning(
            "extract_claims_from_token: post-validation decode failed (%s)", type(exc).__name__
        )
        return {}
    out: dict[str, str] = {}
    for k in keys:
        v = data.get(k)
        if isinstance(v, str) and v:
            out[k] = v
    return out


def extract_numeric_claim(token: str, key: str) -> float | None:
    """Extract a single numeric (int/float) claim from a validated token.

    ``extract_claims_from_token`` intentionally returns only STRING claims (it
    serves the Slack-redirect channel/thread_ts recovery path), so it silently
    drops numeric claims like ``session_exp``. Callers that need a numeric claim
    (e.g. ``api_auth_me`` reporting the cookie's ``session_exp`` so the frontend
    can schedule its proactive refresh) must use this instead. Validates the
    token first (deny-by-default); returns ``None`` if the token is invalid, the
    claim is absent, or it is not a real number (bool is rejected).

    Validates against ``session_exp`` (use_session_exp=True), NOT the 5-minute
    link window — matching ``extract_claims_from_token``: the cookies this reads
    outlive the link window, survive restarts, and are minted with
    ``register_nonce=False`` by both the middleware link->session exchange and
    ``api_auth_refresh``, so link-path validation would return ``None`` for all
    of them and the fix would be a runtime no-op.
    """
    valid, _user_id, _reason = validate_token(token, use_session_exp=True)
    if not valid:
        return None
    try:
        data = json.loads(_b64url_decode(token.split(".")[0]))
    except Exception as exc:
        logger.warning(
            "extract_numeric_claim: post-validation decode failed (%s)", type(exc).__name__
        )
        return None
    v = data.get(key)
    if isinstance(v, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def generate_app_secret() -> str:
    """Generate a random 64-char hex secret for app authentication."""
    return os.urandom(32).hex()


def validate_app_secret(app_name: str, provided_secret: str) -> bool:
    """Validate an app secret against the stored secret on disk.

    Reads ``~/.kiro/crew/apps/{app_name}/.app_secret`` and performs
    constant-time comparison via :func:`hmac.compare_digest`.
    Returns ``False`` if the file doesn't exist or doesn't match.
    """
    from kiro_crew.config.loader import config_dir

    secret_path = config_dir() / "apps" / app_name / ".app_secret"
    try:
        stored = secret_path.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return False
    if not stored or not provided_secret:
        return False
    return hmac.compare_digest(stored, provided_secret)


def write_app_secret(app_name: str, secret: str) -> None:
    """Write an app secret to ``~/.kiro/crew/apps/{app_name}/.app_secret``.

    Creates the directory if needed and sets file mode to 0o600.
    """
    from kiro_crew.config.loader import config_dir

    secret_dir = config_dir() / "apps" / app_name
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret_path = secret_dir / ".app_secret"
    # os.O_TRUNC truncates any pre-existing file BEFORE the DACL tightens,
    # then restrict_to_owner locks it down while it is still empty, then we
    # write the secret bytes. This ordering matters on Windows because
    # restrict_to_owner shells out to icacls (subprocess) — if we wrote first
    # the secret would sit under the parent-inherited DACL during the icacls
    # window. On failure we unlink the just-created empty file (mirroring
    # dashboard/server.py:_write_secret_file) so we don't leave a zero-byte
    # .app_secret under the default DACL that a later successful write
    # (which does not re-inherit on O_TRUNC) could then populate.
    fd = os.open(str(secret_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # restrict_to_owner (fail-loud), NOT fchmod_safe: fchmod_safe swallows
        # OSError, which would defeat the cleanup-and-reraise below for this
        # app secret. On POSIX applies chmod 0o600 by path; on
        # Windows an owner-only DACL via icacls (fchmod doesn't exist on
        # Windows, where an IS_POSIX no-op would let per-app secrets
        # land readable by other local users).
        platform_compat.restrict_to_owner(secret_path)
        with os.fdopen(fd, "w") as f:
            fd = -1  # fdopen took ownership; skip the redundant close below
            f.write(secret)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            secret_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _evict_expired() -> None:
    """Remove token state entries whose session has expired."""
    _state.evict_expired(time.time())


def bind_token_ip(
    token: str, ip: str, session_exp: float = 0.0, proxied: bool = False
) -> None:
    """Bind a token to a client IP for session validation.

    ``proxied`` is an observation only (see ``_TokenState.bind_ip``): it records
    that *ip* is a same-host proxy's address rather than the client's, so the
    Security Posture surface can report that the pin is shared. It never changes
    the binding or the comparison.
    """
    _state.bind_ip(token, ip, session_exp or time.time() + MAX_SESSION_TTL_SECS, proxied)


def proxied_pin_observed() -> bool | None:
    """Report the pin scope of the sessions live right now.

    ``None`` = nothing is currently pinned (no scope to report), ``True`` = at
    least one live session is pinned to a same-host proxy address and is
    therefore shared by every client behind it, ``False`` = live sessions are
    pinned to client addresses. Recovers on its own once proxied sessions
    expire — no gateway restart needed.
    """
    return _state.proxied_pin_observed(time.time())


def check_token_ip(token: str, ip: str) -> bool:
    """Check if token is bound to the given IP (or unbound)."""
    return _state.check_ip(token, ip)


def mark_consumed(token: str, session_exp: float = 0.0) -> None:
    """Mark a token as consumed."""
    _state.mark_consumed(token, session_exp or time.time() + MAX_SESSION_TTL_SECS)


def is_consumed(token: str) -> bool:
    """Check if a token has been consumed."""
    return _state.is_consumed(token)


def try_consume(token: str, session_exp: float = 0.0) -> bool:
    """Atomically consume a token if not already consumed.

    Returns True if this call consumed it, False if already consumed.
    """
    return _state.try_consume(token, session_exp or time.time() + MAX_SESSION_TTL_SECS)


def revoke_access_cookie(token: str) -> bool:
    """Revoke a SINGLE access cookie by adding its nonce to the denylist.

    Validates the token (signature + session_exp + gen) first — deny-by-default,
    so attacker-controlled junk is never written into the persisted store. Then
    extracts the per-token ``nonce`` and ``session_exp`` and records the nonce
    as revoked until that expiry. Returns True if a nonce was revoked, False
    otherwise (malformed / already-expired / nonce-less token — nothing to do).

    This is the per-session counterpart to :func:`revoke_all_sessions`: logout
    calls it so the caller's own cookie is rejected immediately, without the
    global generation bump that would terminate every other active session.
    """
    valid, _uid, _reason = validate_token(token, use_session_exp=True)
    if not valid:
        return False
    try:
        data = json.loads(_b64url_decode(token.split(".")[0]))
    except Exception:
        return False
    nonce = data.get("nonce", "")
    if not nonce:
        return False
    try:
        session_exp = float(data.get("session_exp", 0.0))
    except (TypeError, ValueError):
        return False
    if session_exp <= time.time():
        return False
    _get_revoked_store().revoke(nonce, session_exp)
    return True


def revoke_all_sessions() -> None:
    """Revoke all active dashboard sessions (also used for test isolation).

    Emits a SEL audit event before clearing state so the revocation is recorded.
    """
    _sel_fn().log_api_access(
        caller="system",
        operation="dashboard_sessions_revoked",
        outcome="ok",
        source="token_auth",
        resources="action=revoke_all",
    )
    _state.clear_all()
    # Bump the persisted revocation generation so already-issued cookies (which
    # the cleared per-process nonce store cannot touch) are rejected on their
    # next request. This is what makes logout actually end cookie sessions.
    _bump_revocation_gen()


def parse_duration(s: str) -> int | None:
    """Parse ``'<int>h'`` or ``'<int>m'`` into seconds, or *None*.

    Returns *None* for invalid input. Caps at ``MAX_SESSION_TTL_SECS``.
    """
    m = re.fullmatch(r"(\d+)(h|m)", s)
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    secs = value * 3600 if unit == "h" else value * 60
    return min(secs, MAX_SESSION_TTL_SECS)


def _cookie_port_from_host(request: web.Request, fallback: int) -> str:
    """Return the port the browser connects to (Host header), else *fallback*.

    The dashboard cookie is named ``mc_token_<port>``. Keying it by the server's
    own listen port breaks under SSH tunnels: two Cloud Desktops both serving on
    7777 but tunneled to distinct local ports would collide on a single
    ``mc_token_7777`` because browser cookies are not isolated by port (RFC 6265
    scopes by host only). The browser-facing port is unique per dashboard, so it
    is the correct key. Falls back to the server port when the Host header
    carries no port, preserving behavior for direct (non-tunnel) access.
    """
    host = request.headers.get("Host", "")
    if ":" in host:
        candidate = host.rsplit(":", 1)[-1].strip()
        if candidate.isdigit():
            return candidate
    return str(fallback)


# -- App-token scope enforcement (CWE-269, least privilege) -------------------
#
# An app token (payload carries a non-empty ``app`` claim, minted by the
# X-App-Secret exchange at /api/apps/<name>/token) must NOT have the same
# reach as a dashboard-user token. Deny-by-default: an app token may only
# access (1) its own app namespace and (2) the API path prefixes the app
# declared in its manifest ``permissions.api`` allowlist. Everything else is
# rejected. Dashboard-user tokens (empty ``app`` claim) are never subject to
# this — the gate is a no-op for them.

# Short-TTL cache of each app's declared ``permissions.api`` allowlist so the
# hot auth path doesn't read app.json on every request. Permissions change
# rarely; a 30s TTL self-heals after enable/disable/update without any
# invalidation wiring.
_APP_PERMS_TTL = 30.0
_app_perms_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
_app_perms_lock = threading.Lock()


def _app_api_allowlist(app_name: str) -> tuple[str, ...]:
    """Return the app's declared ``permissions.api`` prefixes (cached, deny-safe).

    On any failure (app not installed, manifest unreadable) returns an empty
    tuple — i.e. deny-by-default: the app is confined to its own namespace only.
    """
    now = time.time()
    with _app_perms_lock:
        entry = _app_perms_cache.get(app_name)
        if entry is not None and now - entry[0] < _APP_PERMS_TTL:
            return entry[1]
    allow: tuple[str, ...] = ()
    try:
        # circular import: apps.manager imports generate_app_secret/
        # write_app_secret from this module (token_auth), so a top-level
        # `import` here would form a cycle. Kept function-local deliberately.
        from kiro_crew.apps.manager import get_app_manifest

        manifest = get_app_manifest(app_name)
        if manifest is not None:
            allow = tuple(p for p in manifest.permissions.api if p)
    except Exception:
        logger.warning(
            "app scope: could not load permissions for %r; denying by default",
            app_name,
            exc_info=True,
        )
        allow = ()
    with _app_perms_lock:
        _app_perms_cache[app_name] = (now, allow)
    return allow


def _app_owns_path(app_name: str, path: str) -> bool:
    """True if *path* is within *app_name*'s own namespace.

    Covers the reverse-proxy + UI surface (``/apps/<name>/...``) and the
    per-app management/config surface (``/api/apps/<name>/...``). Membership is
    a path-boundary match so app ``foo`` cannot reach app ``foo-bar``.
    """
    for base in (f"/apps/{app_name}", f"/api/apps/{app_name}"):
        if path == base or path.startswith(base + "/"):
            return True
    return False


def _api_pattern_matches(pattern: str, path: str) -> bool:
    """Match a ``permissions.api`` entry against a request path.

    Supports trailing ``/*`` and ``*`` wildcards; a bare prefix matches the
    exact path or any child under a path boundary (``/api/chat`` matches
    ``/api/chat`` and ``/api/chat/slots`` but NOT ``/api/chatx``).
    """
    pattern = pattern.strip()
    if not pattern:
        return False
    if pattern.endswith("/*"):
        base = pattern[:-2]
        return path == base or path.startswith(base + "/")
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return path == pattern or path.startswith(pattern + "/")


# Protocol-layer paths every app token implicitly needs — connection
# infrastructure, not feature-level permissions. Requiring each app to declare
# them in ``permissions.api`` adds no security value and produces silent 403
# regressions whenever a new app forgets to list them.
#
# ``/api/ws`` is safe to allow implicitly ONLY because the WS layer now applies
# per-app event scope filtering (``ws_event_scope.py``): a connected app token
# receives just the events matching its ``permissions.events`` declarations, so
# connecting no longer grants the full event stream. Contrast with functional
# paths like /api/chat/* or /api/spawn/* — those grant real capabilities and
# MUST stay explicitly declared.
#
# ``/api/status`` is deliberately NOT here. It has no response-level filter to
# match what event scoping does for ``/api/ws``, and ``api_status`` returns far
# more than liveness: ``owner_id_hash``, host specs (os/arch/cpu/memory), cron
# and usage stats, and the live safety-override (``yolo_*``) state. The
# connect/reconnect poll that needs it is the DASHBOARD SPA
# (``useDashboardHealthProbe``), which runs on a dashboard-user token and never
# reaches this list. An app that genuinely wants it declares it in
# ``permissions.api`` — the shipped ``design_critique`` manifest does.
_APP_TOKEN_IMPLICIT_ALLOW: frozenset[str] = frozenset({
    # Connecting grants no events by itself: the socket records the caller's
    # manifest declarations and every frame is filtered per socket, payload AND
    # envelope, in ws_event_scope.py / DashboardState._serialize_for_client.
    "/api/ws",
})


def app_token_path_allowed(app_name: str, path: str) -> bool:
    """Return True if an app token for *app_name* may access *path*.

    Deny-by-default (CWE-269). Only call this for app tokens (non-empty
    ``app_name``); dashboard-user tokens must bypass it entirely.
    """
    if not app_name:
        # Defensive: a caller should never pass an empty app_name here, but if
        # it does, do NOT silently grant — that would turn the gate into a
        # no-op allow. Return False; the caller's non-empty guard is primary.
        return False
    if path in _APP_TOKEN_IMPLICIT_ALLOW:
        # Audit implicit grants so every app-token path decision is in the trail.
        try:
            _sel_fn().log_api_access(
                caller=app_name,
                operation="app_scope_check",
                outcome="granted_implicit",
                source="token_auth",
                resources=path,
            )
        except Exception as exc:
            # Security-relevant audit path (CWE-269 implicit grant); a persistent
            # SEL misconfiguration must be observable, so log rather than pass.
            logger.debug(
                # Message deliberately avoids the module-name prefix: Semgrep's
                # logger-credential-disclosure heuristic fires on the substring
                # alone. The logged values are an app name, a path, and an
                # exception -- no secret material.
                "SEL audit for implicit app-scope allow %s -> %s failed: %s",
                app_name,
                path,
                exc,
            )
        return True
    if _app_owns_path(app_name, path):
        return True
    # Notification push (RFC local notification bus, Phase 2): every app may
    # reach this single push-only endpoint — the handler independently
    # enforces app identity (from the verified token), manifest-declared
    # channels, and per-app rate limits, so the grant confers no cross-app
    # authority. Deliberately NOT /api/notifications: that path also serves
    # GET (read history) and DELETE, which app tokens must not reach.
    if path == "/api/notifications/push":
        return True
    return any(_api_pattern_matches(p, path) for p in _app_api_allowlist(app_name))


def _enforce_app_scope(request: web.Request, app_name: str, path: str) -> web.Response | None:
    """Return a 403 response if an app token is out of scope, else None.

    No-op for dashboard-user tokens (empty *app_name*).
    """
    if not app_name:
        return None
    if app_token_path_allowed(app_name, path):
        return None
    # SEL audit for the permission decision (matches the sibling deny paths in
    # the middleware, which log_api_access in addition to _log_auth).
    _sel_fn().log_api_access(
        caller=app_name,
        operation="app_scope_check",
        outcome="denied",
        source="token_auth",
        resources=path,
        error="app token out of scope",
    )
    _log_auth(request, app_name, "denied", f"app token out of scope: {path}")
    return _deny(request, "app token not permitted for this endpoint")


async def warm_auth_singletons() -> None:
    """Prime the signing-secret and revoked-nonce singletons OFF the event loop.

    Both ``_get_secret()`` and ``_get_revoked_store()`` do blocking file I/O on
    first use (read/create ``token_signing.key`` + read the persisted nonce
    denylist; on Windows an ``icacls`` subprocess to lock the key file's DACL).
    Calling them lazily from the request path — or synchronously inside the
    ``token_auth_middleware()`` factory, which runs on the loop via the async
    ``start_dashboard()`` / ``start_api_server()`` — would land that I/O on the
    event loop (no-blocking-call-on-event-loop).

    The async startup paths ``await`` this exactly once, BEFORE constructing
    the middleware chain and before the server begins accepting connections, so
    the first auth op hits the already-built singletons with no blocking I/O on
    the loop. Idempotent: both callees memoize under a lock.
    """
    await asyncio.to_thread(_get_secret)
    await asyncio.to_thread(_get_revoked_store)


def token_auth_middleware(
    *,
    internal_paths: frozenset[str] = frozenset(),
    mixed_internal_paths: frozenset[str] = frozenset(),
    internal_secret: str = "",
    port: int = 5476,
    local_only: bool = True,
    spa_shell_handler: Callable[..., Any] | None = None,
) -> Callable[..., Any]:
    """Factory returning aiohttp middleware for token-based dashboard auth.

    ALL requests require a valid token — loopback is no longer exempt
    because local port forwarders (socat, ssh -R, custom scripts) make
    remote traffic appear as 127.0.0.1, bypassing auth entirely.

    *internal_paths* are exact paths that internal processes (mcp-core,
    doctor) call — these require loopback AND a matching
    ``X-Internal-Secret`` header (read from ``~/.kiro/crew/.local_secret``).
    Non-loopback access to these paths is always denied.

    *mixed_internal_paths* are paths called by BOTH internal processes
    (loopback + secret) AND the browser (cookie auth).  On non-loopback
    they perform explicit cookie validation (deny-by-default) instead
    of hard-denying, so DCV/SSH-forwarded browsers polling these routes
    (e.g. ``/api/spawn`` every 5s) don't trigger false session-expired
    banners.  Use this for any internal-path that the browser polls.

    """

    # NOTE: the signing-secret and revoked-nonce singletons are NOT warmed
    # here anymore. This factory is invoked from `start_dashboard()` /
    # `start_api_server()`, which are `async def` and therefore run ON the
    # event loop — a synchronous warm-up here (blocking key-file read + a
    # Windows `icacls` subprocess on first create) would block the loop during
    # startup (no-blocking-call-on-event-loop). The async startup paths instead
    # `await warm_auth_singletons()` (which offloads to a worker thread) BEFORE
    # constructing this middleware chain, so the first auth op still hits the
    # already-built singletons without any blocking I/O landing on the loop.

    def _extract_and_validate_token(request: web.Request, _port: int) -> tuple[bool, str, str, str]:
        """Extract token from query param or cookie and validate it.

        Returns ``(valid, user_id, reason, app_name)``. Used by internal-path
        browser/app auth (no secret header). The main auth flow has its own
        extraction with IP-binding and from_cookie tracking that this helper
        intentionally does not replicate.
        """
        cookie_name = f"mc_token_{_cookie_port_from_host(request, _port)}"
        token = request.query.get("token") or request.cookies.get(cookie_name, "")
        if not token:
            return False, "", "no token", ""
        return validate_token_with_app(token, use_session_exp=True)

    @web.middleware
    async def middleware(request: web.Request, handler: object) -> web.StreamResponse:
        path = request.path

        # Internal API paths: loopback + secret grants immediate access.
        # If the secret is missing (browser request), fall through to
        # normal cookie auth so dashboard pages can call these routes.
        _matches_strict = internal_paths and (
            path in internal_paths or any(path.startswith(p + "/") for p in internal_paths)
        )
        _matches_mixed = mixed_internal_paths and (
            path in mixed_internal_paths
            or any(path.startswith(p + "/") for p in mixed_internal_paths)
        )
        # local_only=False: treat ALL internal paths as mixed (backward compat
        # with mainline's local_only semantics — user opted into remote access)
        if not local_only and _matches_strict and not _matches_mixed:
            _matches_mixed = True
            _matches_strict = False
        _matches_internal = _matches_strict or _matches_mixed
        if _matches_internal and is_loopback(request.remote or ""):
            _has_secret_header = "X-Internal-Secret" in request.headers
            if _has_secret_header:
                _provided_secret = request.headers["X-Internal-Secret"]
                # Secret header present — validate it strictly
                if not internal_secret:
                    _sel = _sel_fn()
                    _sel.log_api_access(
                        caller=request.remote or "",
                        operation="internal_auth",
                        outcome="denied",
                        source="token_auth",
                        resources=path,
                        error="no internal secret configured",
                    )
                    _log_auth(request, "internal", "denied", "no internal secret configured")
                    return _deny(request, "Forbidden")
                if hmac.compare_digest(internal_secret, _provided_secret):
                    _sel = _sel_fn()
                    _sel.log_api_access(
                        caller=request.remote or "",
                        operation="internal_auth",
                        outcome="granted",
                        source="token_auth",
                        resources=path,
                    )
                    _log_auth(request, "internal", "granted", "")
                    # Mark the grant so handlers can distinguish "the internal
                    # loopback caller (kiro-cli / MCP) authenticated" from "no
                    # auth ran at all". This branch deliberately leaves
                    # request["app"] unset — there is no app identity — so a
                    # handler that fails closed on an absent app claim would
                    # otherwise reject every MCP call.
                    request["internal_auth"] = True
                    return await handler(request)  # type: ignore[operator]
                # Wrong secret → deny (don't fall through)
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=request.remote or "",
                    operation="internal_auth",
                    outcome="denied",
                    source="token_auth",
                    resources=path,
                    error="wrong secret",
                )
                _log_auth(request, "internal", "denied", "wrong secret")
                return _deny(request, "Forbidden")
            # No secret header (browser request) → verify cookie/query-param auth
            # inline to satisfy deny-by-default: positively confirm auth
            # at the decision point rather than deferring to downstream.
            # NOTE: uses _extract_and_validate_token helper (defined above)
            # for cookie/query-param validation.
            _valid, _uid, _reason, _app = _extract_and_validate_token(request, port)
            if not _valid:
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=request.remote or "",
                    operation="internal_auth",
                    outcome="denied",
                    source="token_auth",
                    resources=path,
                    error=f"cookie auth failed: {_reason}",
                )
                _log_auth(request, "internal", "denied", f"cookie auth failed: {_reason}")
                return _deny(request, "Forbidden")
            # Expose identity so downstream handlers (and app-scope) see it.
            request["user"] = _uid
            request["app"] = _app
            # POSITIVE dashboard-user signal for the WS scope gate: the WS
            # layer must never infer trust from a falsy app claim (CWE-269).
            request["is_dashboard_user"] = not _app
            # App tokens are confined to their declared scope even on internal
            # paths (e.g. /api/chat, /api/spawn are mixed_internal) — otherwise
            # an app token would reach them on loopback with NO app identity set
            # and be treated as the dashboard user (privilege escalation).
            _scope_deny = _enforce_app_scope(request, _app, path)
            if _scope_deny is not None:
                return _scope_deny
            _sel = _sel_fn()
            _sel.log_api_access(
                caller=request.remote or "",
                operation="internal_auth",
                outcome="granted",
                source="token_auth",
                resources=path,
                error="cookie auth (no secret header)",
            )
            _log_auth(request, "internal", "granted", f"cookie auth for {_uid}")
            return await handler(request)  # type: ignore[operator]
        elif _matches_internal:
            if _matches_mixed:
                # Mixed paths on non-loopback (DCV/SSH-forwarded browsers):
                # explicit cookie validation, mirroring the loopback
                # no-secret-header branch above.  Deny-by-default —
                # positively confirm auth at this decision point rather
                # than relying on downstream fall-through.
                # If X-Internal-Secret header is present, validate it first
                # (defense-in-depth: wrong secret = deny, even with valid cookie)
                if "X-Internal-Secret" in request.headers:
                    if not internal_secret or not hmac.compare_digest(
                        internal_secret, request.headers["X-Internal-Secret"]
                    ):
                        _sel = _sel_fn()
                        _sel.log_api_access(
                            caller=request.remote or "",
                            operation="internal_auth",
                            outcome="denied",
                            source="token_auth",
                            resources=path,
                            error="wrong secret (non-loopback mixed)",
                        )
                        _log_auth(
                            request, "internal", "denied", "wrong secret (non-loopback mixed)"
                        )
                        return _deny(request, "Forbidden")
                _valid, _uid, _reason, _app = _extract_and_validate_token(request, port)
                if not _valid:
                    _sel = _sel_fn()
                    _sel.log_api_access(
                        caller=request.remote or "",
                        operation="internal_auth",
                        outcome="denied",
                        source="token_auth",
                        resources=path,
                        error=f"mixed non-loopback cookie auth failed: {_reason}",
                    )
                    _log_auth(
                        request,
                        "internal",
                        "denied",
                        f"mixed non-loopback cookie auth failed: {_reason}",
                    )
                    return _deny(request, "Forbidden")
                # Expose identity + confine app tokens to their declared scope
                # (same rationale as the loopback branch above).
                request["user"] = _uid
                request["app"] = _app
                # POSITIVE dashboard-user signal for the WS scope gate (see
                # the loopback branch above).
                request["is_dashboard_user"] = not _app
                _scope_deny = _enforce_app_scope(request, _app, path)
                if _scope_deny is not None:
                    return _scope_deny
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=request.remote or "",
                    operation="internal_auth",
                    outcome="granted",
                    source="token_auth",
                    resources=path,
                    error="mixed non-loopback cookie auth",
                )
                _log_auth(
                    request, "internal", "granted", f"mixed non-loopback cookie auth for {_uid}"
                )
                return await handler(request)  # type: ignore[operator]
            else:
                # INVARIANT: non-loopback access to strict internal paths is
                # ALWAYS denied.  Do NOT remove this branch — without it,
                # non-loopback requests would silently fall through to
                # normal cookie auth, defeating the machine-to-machine
                # isolation that the internal-secret design provides.
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=request.remote or "",
                    operation="internal_auth",
                    outcome="denied",
                    source="token_auth",
                    resources=path,
                    error="non-loopback source",
                )
                _log_auth(request, "internal", "denied", "non-loopback source")
                return _deny(request, "Forbidden")

        # Bypass static assets
        if any(path.startswith(p) for p in _BYPASS_PREFIXES):
            return await handler(request)  # type: ignore[operator]
        if path in _BYPASS_EXACT:
            return await handler(request)  # type: ignore[operator]
        # Method-scoped exact bypasses. A non-listed method on the same path
        # falls through to the ordinary token gate rather than bypassing it.
        _bypass_methods = _BYPASS_EXACT_METHODS.get(path)
        if _bypass_methods is not None and request.method in _bypass_methods:
            return await handler(request)  # type: ignore[operator]
        # Icon files: anchored regex with bounded digit count to prevent
        # ReDoS and ensure only legitimate PWA icon paths bypass auth.
        if re.fullmatch(r"/icon-\d{1,4}\.png", path):
            return await handler(request)  # type: ignore[operator]

        # Installed-app UI bundles: anchored to /apps/{name}/ui/* only.
        # Does NOT match the reverse-proxy path /apps/{name}/api/*.
        # Restricted to safe methods (GET/HEAD) — static file serving only.
        # If a write-capable handler is ever registered under /apps/{name}/ui/,
        # it stays auth-protected because the bypass never fires for it.
        if _APPS_UI_BYPASS_RE.match(path) and request.method in ("GET", "HEAD"):
            return await handler(request)  # type: ignore[operator]

        # Bypass app token exchange (App Kit §5.1) — app authenticates
        # via X-App-Secret header, not a token cookie.
        if re.match(r"^/api/apps/[a-z0-9][a-z0-9_-]*/token$", path) and request.method == "POST":
            return await handler(request)  # type: ignore[operator]

        # Bypass /api/auth/refresh — the handler authenticates via the
        # refresh cookie (path-restricted to this endpoint), not the access
        # cookie. Adding here lets refresh succeed even when the access
        # cookie has just expired (the whole point of the refresh flow).
        # GET is also allowed for /api/auth/me which is gated by normal auth
        # below — only POST /api/auth/refresh bypasses.
        if path == "/api/auth/refresh" and request.method == "POST":
            return await handler(request)  # type: ignore[operator]

        # Bypass /api/auth/logout — same rationale as /api/auth/refresh:
        # the handler authenticates via the refresh cookie and must work
        # even if the access cookie has just expired (so a user can still
        # tear down their refresh chain on the way out).
        if path == "/api/auth/logout" and request.method == "POST":
            return await handler(request)  # type: ignore[operator]

        # Extract token from query param or cookie
        cookie_name = f"mc_token_{_cookie_port_from_host(request, port)}"
        token = request.query.get("token") or ""
        from_cookie = False
        if not token:
            token = request.cookies.get(cookie_name, "")
            from_cookie = bool(token)

        if not token:
            # Cold-start (no token — e.g. the access cookie expired over a
            # weekend). Serve the shell directly (not the matched handler) so
            # the app boots and self-recovers via the refresh cookie. Default-
            # deny: fires only when a shell handler is wired and the path is a
            # non-data GET/HEAD nav; no cookie minted.
            if spa_shell_handler is not None and _is_spa_shell_request(request):
                _log_auth(request, "", "shell_unauth", "SPA shell served (no token, cold-start)")
                return await spa_shell_handler(request)  # type: ignore[operator]
            _log_auth(request, "", "denied", "Token required")
            return _deny(request, "Token required")

        valid, user_id, reason, app_name = validate_token_with_app(
            token, use_session_exp=from_cookie
        )
        if not valid:
            # Cold-start variant: an expired/forged token is present (cookie
            # survived but its token lapsed). Same rationale — serve the shell
            # so the SPA can boot and silently refresh.
            if spa_shell_handler is not None and _is_spa_shell_request(request):
                # Distinct outcome (NOT "ok"): keep forged-token navigations
                # detectable by SEL anomaly detection while still serving the
                # secret-free shell.
                _log_auth(
                    request,
                    "",
                    "shell_unauth_invalid_token",
                    f"SPA shell served (invalid token: {reason})",
                )
                return await spa_shell_handler(request)  # type: ignore[operator]
            _log_auth(request, "", "denied", reason)
            return _deny(request, reason)

        client_ip = request.remote or "unknown"

        if not check_token_ip(token, client_ip):
            _log_auth(request, user_id, "denied", "IP mismatch")
            return _deny(request, "IP mismatch")

        # Extract session_exp for cookie and IP binding on first query-param use
        session_exp = 0.0
        session_token = token
        if not from_cookie:
            _link_nonce = ""
            _embed_parent_port = ""
            try:
                payload_bytes = _b64url_decode(token.split(".")[0])
                data = json.loads(payload_bytes)
                session_exp = float(data.get("session_exp", 0.0))
                _link_nonce = str(data.get("nonce", ""))
                # Carry the multi-instance frame-ancestors claim (the embedding
                # parent dashboard's port) THROUGH the exchange. The framed
                # document is authenticated by the session cookie minted below,
                # so without this the cookie would drop the claim and the CSP
                # reader (server._extra_frame_ancestors) would fall back to bare
                # ``'self'`` — the blank embedded-pane bug.
                _epp = data.get("embed_parent_port")
                if isinstance(_epp, str) and _epp:
                    _embed_parent_port = _epp
            except Exception:
                session_exp = 0.0
            # Expose the frame-ancestors parent-port claim to the response-header
            # layer NOW, BEFORE the link nonce is revoked below. The FIRST framed
            # instance document is loaded via ``?token=`` and the browser enforces
            # that response's ``frame-ancestors``; re-validating the (about-to-be-
            # revoked) link token in server._extra_frame_ancestors would return
            # None and fall back to bare ``'self'`` (blank pane). Signature is
            # already verified above; this only carries the loopback parent port.
            if _embed_parent_port:
                request["embed_parent_port"] = _embed_parent_port
            # Token→session exchange (CWE-613 / secure token handling): NEVER
            # reuse the one-time URL/link token string as the long-lived session
            # cookie. The link token is exposed in URLs, Slack messages, terminal
            # history, browser history and access/proxy logs. Minting a SEPARATE
            # session token here (fresh nonce, same identity + remaining
            # lifetime) means an observer of any of those channels obtains only
            # the 5-minute link — not the 20-hour session credential. The link
            # token stops being a bearer credential the moment its short ``exp``
            # window closes; the cookie is an unrelated string. Per-session
            # revocation still works because the minted token carries its own
            # nonce (see RevokedNonceStore / api_auth_logout).
            _remaining = int(session_exp - time.time()) if session_exp else MAX_SESSION_TTL_SECS
            if _remaining > 0:
                session_token = generate_token(
                    user_id,
                    ttl_seconds=_remaining,
                    app=app_name,
                    register_nonce=False,
                    extra=(
                        {"embed_parent_port": _embed_parent_port} if _embed_parent_port else None
                    ),
                )
            # Kill the link token AS A COOKIE. Exchange alone is not enough: the
            # link token still carries the 20h ``session_exp``, so a captured
            # copy (from a log/Slack/history) could otherwise be presented
            # directly as ``mc_token_<port>`` and validate on the cookie path for
            # the full session. Adding its nonce to the persisted denylist makes
            # validate_token(use_session_exp=True) reject it. Crucially the
            # query-param LINK path (use_session_exp=False) does NOT consult the
            # denylist, so legitimate re-navigation of the same link URL — remote
            # instance iframes re-deriving /?token=, self-nudge polling — keeps
            # working within the 5-minute window (it just re-exchanges for a
            # fresh session cookie each time). Guarded by is_revoked so repeated
            # exchanges of the same link don't re-write the denylist file.
            if _link_nonce and session_exp and not _get_revoked_store().is_revoked(_link_nonce):
                # revoke() does synchronous file I/O (mkdir/write/chmod/replace);
                # offload so it never blocks the event loop. is_revoked above is
                # an in-memory check and is cheap enough to run inline.
                await asyncio.to_thread(_get_revoked_store().revoke, _link_nonce, session_exp)
            # Bind the SESSION token (what becomes the cookie) to the client IP,
            # not the consumed URL token. ``proxied`` is recorded so Security
            # Posture can tell the user whether that pin is per-client or shared
            # with everyone behind a same-host tunnel — it does not affect the
            # binding itself.
            bind_token_ip(
                session_token,
                client_ip,
                session_exp,
                is_proxied_request(request),
            )

            # Token-consumption anchor seam (Default: no-op, OSS-identical). A
            # Slack challenge-redirect link, once opened on a verified device,
            # consumes its token here — the edition opens the bounded per-(user,
            # channel) auth window that lets follow-up Slack traffic flow inline.
            # channel/thread_ts ride the token's signed ``extra`` payload (``data``);
            # absent for non-challenge tokens, so the window is only opened for a
            # real challenge exchange. Fail-safe: ``safe_context_call`` swallows an
            # observer error (fallback=None) so it never blocks token consumption /
            # login, while still re-raising ``PlatformCompositionError`` — the boot
            # invariant that a mis-composed edition MUST abort rather than silently
            # degrade. Do NOT wrap this in a bare ``except Exception``: that is
            # exactly the swallow ``safe_context_call`` centralizes to prevent.
            _chan = str(data.get("channel", "")) if isinstance(data, dict) else ""
            _thread = (data.get("thread_ts") if isinstance(data, dict) else None) or None
            if _chan:
                from kiro_crew.platform import current_context, safe_context_call

                safe_context_call(
                    lambda: current_context().dashboard.on_token_consumed(
                        user_id, _chan, session_exp, _thread
                    ),
                    fallback=None,
                    log_message="dashboard.on_token_consumed observer failed",
                )

        # Expose authenticated identity to handlers (deny-by-default)
        request["user"] = user_id
        request["app"] = app_name
        # POSITIVE dashboard-user signal for the WS scope gate (see above).
        request["is_dashboard_user"] = not app_name

        # App-token least-privilege gate (CWE-269): an app token is confined to
        # its own namespace + its manifest ``permissions.api`` allowlist. This
        # is the primary enforcement point for the normal cookie/query-param
        # flow (e.g. /api/sessions, /api/config/*, the /apps/<other>/api proxy).
        _scope_deny = _enforce_app_scope(request, app_name, path)
        if _scope_deny is not None:
            return _scope_deny

        # Proceed to handler
        resp = await handler(request)  # type: ignore[operator]

        # Set cookie after handler (needs response object)
        if not from_cookie:
            cookie_max_age = MAX_SESSION_TTL_SECS
            if session_exp:
                remaining = int(session_exp - time.time())
                if 0 < remaining <= MAX_SESSION_TTL_SECS:
                    cookie_max_age = remaining
            resp.set_cookie(
                cookie_name,
                session_token,
                httponly=True,
                samesite="Lax",
                # Secure only when over HTTPS (direct or via a
                # TLS-terminating tunnel/proxy — see is_https_request).
                # Localhost plain HTTP must not set it or the browser
                # refuses to send it back.
                secure=is_https_request(request),
                path="/",
                max_age=cookie_max_age,
            )
            # Clean up legacy cookie from pre-port-specific era
            resp.set_cookie("mc_token", "", max_age=0, path="/")

            # Trim other-port auth cookies from the shared 127.0.0.1 jar so it
            # can't grow past aiohttp's header limit (see
            # refresh_tokens.foreign_port_cookies). Gated on jar size so live
            # co-existing gateways keep their sessions until accumulation
            # genuinely threatens overflow. This page request only carries
            # other-port ACCESS cookies (path "/"); other-port refresh cookies
            # (path "/api/auth") are trimmed on the next refresh call.
            if cookie_jar_needs_pruning(request.cookies):
                for _stale_name, _stale_path in foreign_port_cookies(
                    request.cookies, _cookie_port_from_host(request, port)
                ):
                    resp.set_cookie(_stale_name, "", max_age=0, path=_stale_path)

            # Initial mint via token URL: also attach a refresh cookie so
            # the user does not have to re-mint via URL every ~20h. Inlined
            # here (rather than calling handlers.auth_refresh) to keep the
            # import top-level and the cycle direction one-way:
            # token_auth → refresh_tokens, never the reverse.
            try:
                refresh_token, chain_id, _jti, refresh_exp = generate_refresh_token(user_id)
                refresh_remaining = int(refresh_exp - time.time())
                if refresh_remaining > 0:
                    resp.set_cookie(
                        refresh_cookie_name(_cookie_port_from_host(request, port)),
                        refresh_token,
                        httponly=True,
                        samesite="Lax",
                        secure=is_https_request(request),
                        path=REFRESH_COOKIE_PATH,
                        max_age=min(refresh_remaining, MAX_REFRESH_TTL_SECS),
                    )
                    # Audit the initial-mint event so forensics can trace any
                    # subsequent chain revocation back to the user it was issued to.
                    try:
                        _sel_fn().log_api_access(
                            caller=user_id,
                            operation="refresh_token_initial_mint",
                            outcome="ok",
                            source="refresh_tokens",
                            resources=chain_id,
                        )
                    except Exception as exc:  # pragma: no cover
                        # SEL must never block auth flows, but log the failure
                        # so it's observable.
                        logger.debug("token_auth: SEL audit failed: %s", exc)
            except Exception as _refresh_err:
                # Refresh cookie is best-effort. If something goes wrong
                # here, the access cookie still works as before — the
                # user just won't get the refresh upgrade until next mint.
                logger.warning(
                    "token_auth: failed to attach refresh cookie (%s); "
                    "access cookie still set, user can re-mint as before",
                    _refresh_err,
                )

        _log_auth(request, user_id, "ok", "")
        return resp  # type: ignore[return-value]

    middleware._is_token_auth = True  # type: ignore[attr-defined]  # sentinel for server.py security gate
    return middleware


def _deny(request: web.Request, reason: str) -> web.Response:
    headers = {"X-Auth-Required": "true"}
    if request.path.startswith("/api/"):
        return web.json_response({"error": reason}, status=403, headers=headers)
    return web.Response(
        text=_403_HTML.format(reason=reason),
        status=403,
        content_type="text/html",
        headers=headers,
    )


def _log_auth(request: web.Request, user_id: str, outcome: str, error: str) -> None:
    try:
        _sel_fn().log_api_access(
            caller=user_id or request.remote or "unknown",
            operation="dashboard.token_auth",
            outcome=outcome,
            resources=request.path,
            error=error,
        )
    except Exception:
        logger.warning("Failed to log auth event to SEL", exc_info=True)
