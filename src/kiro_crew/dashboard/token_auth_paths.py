"""Which requests pass token auth untouched, and what a refused request receives.

The public static-asset and self-authenticating webhook bypasses, the CSRF exemptions,
the SPA shell and app-window path rules, and the shared refusal response, credential
mismatch detail and auth-decision log line every token-auth layer uses.

Served through :mod:`kiro_crew.dashboard.token_auth`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable

from aiohttp import web

from kiro_crew.dashboard.origin import is_proxied_request
from kiro_crew.sel import sel as _sel_fn

logger = logging.getLogger("kiro_crew.dashboard.token_auth")


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
# /sandbox-doc/ is the model-authored-HTML document channel: auth is the HMAC
# path token minted by the authed POST /api/sandbox-doc endpoint. It exists
# because a blob: URL is refused outright by some WebKit-based in-app browsers,
# so artifact and widget frames load a real document instead. See
# dashboard/handlers/sandbox_doc.py for the full security model.
# Same exposure class as /assets/: static non-secret files.
# /browser-view/ is the same-origin relay for the Playwright CLI browser view:
# auth is the per-instance capability token embedded in the path, minted by
# the view supervisor and disclosed only through the cookie-authed, owner-gated
# /api/browser/view payload (the panel frames the relay in an opaque-origin
# sandbox that carries no cookies, exactly like /artifact-app/ above). The
# relay constant-time-compares the token BEFORE running its per-request
# ownership probes and answers a uniform 404 without it. See
# dashboard/handlers/browser_view_relay.py for the full security model.
_BYPASS_PREFIXES = (
    "/assets/",
    "/static/",
    "/fonts/",
    "/vendor/",
    "/artifact-app/",
    "/sandbox-doc/",
    "/browser-view/",
)
_BYPASS_EXACT = {
    "/logo.png",
    # Alias of /logo.png for clients that hardcode the favicon path instead of
    # parsing <link rel="icon"> — same handler, same static-asset exposure.
    "/favicon.ico",
    # The bare relay path (no trailing slash, so the /browser-view/ prefix
    # above misses it). It is a registered relay route carrying no token
    # segment, and the relay's contract is a UNIFORM 404 for every tokenless
    # or wrong-token request — without this entry the middleware answers 403
    # first, handing an unauthenticated prober a response that distinguishes
    # the bare path from the tokened misses.
    "/browser-view",
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

#: The Bot Framework inbound webhook route. Named once because TWO independent
#: middleware exemptions target it (the token gate below and the CSRF Origin
#: check in ``server``), and a hand-copied second spelling is how one control
#: ends up pointed at a route the other is not.
TEAMS_WEBHOOK_PATH = "/api/messaging/teams"

#: The inbound agent webhook route, named once for exactly the same reason
#: :data:`TEAMS_WEBHOOK_PATH` is: the same two independent middleware exemptions
#: target it, and one of them keying off a re-typed literal is how a route ends
#: up exempt from one control and not the other.
AGENT_HOOK_PATH = "/api/hooks/agent"

#: Method scope shared by every self-authenticating external webhook entry: POST
#: is the only method whose handler carries its own credential check.
_SELF_AUTH_WEBHOOK_METHODS = frozenset({"POST"})

#: The CLI's post-update badge-revalidate route. `kirocrew update` (git checkout)
#: POSTs here over loopback holding the local secret in X-Local-Secret and no
#: dashboard token, exactly like /api/logout and /api/token/local; the handler
#: (api_update_revalidate) re-checks BOTH loopback origin and the secret itself
#: before touching the cache. Scoped to POST — the only method routed and the
#: only one the handler's self-auth covers — so a future collision on this path
#: under another method stays on the ordinary token gate.
UPDATE_REVALIDATE_PATH = "/api/update/revalidate"

_BYPASS_EXACT_METHODS: dict[str, frozenset[str]] = {
    AGENT_HOOK_PATH: _SELF_AUTH_WEBHOOK_METHODS,
    TEAMS_WEBHOOK_PATH: _SELF_AUTH_WEBHOOK_METHODS,
    UPDATE_REVALIDATE_PATH: _SELF_AUTH_WEBHOOK_METHODS,
}

# Exact-path exemptions from the CSRF **Origin** check, path -> allowed methods.
# A separate map from the token-auth bypass above, deliberately: skipping the
# cookie gate and skipping the Origin check are two different grants, and a route
# may need one without the other.
#
# ONE entry, and adding a second is a security review rather than a copy of this
# line. `/api/hooks/agent` shares the shape -- self-authenticating, cookie-less,
# server-to-server -- and is in the token-auth bypass above, but it is NOT here:
# no reported failure named it, its own proxy topologies already work, and a
# perimeter exemption is far harder to withdraw once a caller depends on it than
# it is to add later with its own cause.
#
# WHY dropping the Origin check is sound for the Teams route: CSRF exists to stop
# a BROWSER making a cross-origin state-changing request that the browser then
# decorates with the victim's cookies. ``api_teams_activity`` reads no cookie at
# all, so there is nothing for a cross-origin page to ride; it authenticates the
# Bot Framework JWT instead (issuer, App-ID audience, RS256 signature over the
# Bot Framework JWKS, expiry). A cross-origin page can neither obtain nor forge
# one, so an Origin the check would have rejected buys an attacker nothing.
#
# WHY the exemption is REQUIRED and not a convenience: the Connector is
# server-to-server and sends neither ``Origin`` nor ``Referer``.
# ``origin.check_origin`` trusts a header-less request only from a loopback peer
# or the dashboard's unix socket, so such a POST arriving straight at the gateway
# is refused 403 before the credential is ever examined -- which is the "public
# hostname on a VM/App Service" topology in ``docs/teams-integration.md``.
# Nothing an operator can configure widens it: unlike the Host allowlist, which
# ``dashboard.url`` feeds, there is no setting that admits an Origin-less
# non-loopback POST.
#
# The METHOD scope is load-bearing: only POST is exempt, so the ``PUT``/``DELETE``
# ``/api/hooks/{hook_id}`` CRUD routes -- whose handler authenticates by dashboard
# token alone -- keep their Origin check even though the literal ``agent`` matches
# their wildcard.
CSRF_EXEMPT_EXACT_METHODS: dict[str, frozenset[str]] = {
    TEAMS_WEBHOOK_PATH: _SELF_AUTH_WEBHOOK_METHODS,
}


def is_csrf_exempt(path: str, method: str) -> bool:
    """Whether *path* + *method* is a webhook exempt from the CSRF Origin check.

    The single read point for :data:`CSRF_EXEMPT_EXACT_METHODS`, so both dashboard
    middleware chains share one exemption rather than a literal list each.
    """
    return method in CSRF_EXEMPT_EXACT_METHODS.get(path, frozenset())


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
#   - /apps/{name}/api|art|ui/...    → real server handler (proxy / static)
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
    "/sandbox-doc/",
    # Cached feature-video clips and posters (feature_videos_cache.py). A data
    # route: a GET with no session must be refused, never answered with the
    # shell — the browser's <video> would otherwise receive index.html with a
    # 200 and render nothing, and a future non-/api GET registered beside it in
    # routes/realtime.py would inherit the same silent fallback.
    "/feature-videos/",
    # The browser-view relay (handlers/browser_view_relay.py). A data route
    # authenticated by its own capability path token: its handler must always
    # answer — the uniform 404 without the token, the proxied view with it —
    # never the SPA shell, which would render the dashboard inside the
    # Browser panel's own frame.
    "/browser-view",
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
# The trailing slash is also load-bearing. Every handler is registered with a
# path segment after the sub-namespace (`/apps/{name}/ui/{path:.*}`,
# `/apps/{name}/art/{path:.*}` and `/apps/{name}/api/{path:.*}`), and no bare
# `/apps/{name}/ui`, `/apps/{name}/art` or `/apps/{name}/api` route exists. An
# earlier `(?:/|$)` therefore excluded paths that no handler serves, and since
# the app name occupies the same segment position as the router's
# `detail`/`migrate` verbs, an app named literally "api" or "ui" got a 404 on
# /apps/detail/api. Requiring the slash costs no real server route and resolves
# that collision toward the client route.
_APPS_SPA_EXCLUDED_RE = re.compile(r"^/apps/[a-z0-9][a-z0-9_-]*/(?:api|art|ui)/")


def _is_spa_shell_request(request: web.Request) -> bool:
    """True if this GET/HEAD request should be answered with the SPA shell.

    Why: lets the React app boot on a cold start (token expired) so it can run
    its own ``/api/auth/me`` -> ``/api/auth/refresh`` recovery, instead of a
    dead-end 403 whose recovery JS never loads. Safe because the shell is
    static and secret-free and every data namespace is excluded.

    Special case for ``/apps/``: only ``/apps/{name}/api/...``,
    ``/apps/{name}/art/...`` and ``/apps/{name}/ui/...`` have server-side
    handlers. Every other ``/apps/`` path is a React Router navigation entry
    with no server route -- bare ``/apps/{name}``, plus ``/apps/detail/{name}``
    and ``/apps/migrate/{name}`` -- so those must fall through to the SPA shell.
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
    # serves (/apps/{name}/api/..., /apps/{name}/art/... and
    # /apps/{name}/ui/...). Everything else under /apps/ belongs to React
    # Router — bare /apps/{name} as well as /apps/detail/{name} and
    # /apps/migrate/{name} — and gets the shell.
    return not _APPS_SPA_EXCLUDED_RE.match(path)


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
    "<h1>Sign in required — {reason}</h1>"
    "<p>This browser does not have a dashboard session.</p>"
    "<p>On a device already signed in, open <strong>Settings → Security → Sign in on mobile</strong>, "
    "then send the sign-in link to this device. Or paste a sign-in link below.</p>"
    "<p>No other signed-in device? Run <code>kirocrew token</code> in your terminal, then paste the URL below.</p>"
    "<input id='u' type='text' placeholder='Paste sign-in link or token…' autofocus>"
    "<button onclick='go()'>Connect</button>"
    "<div class='err' id='e'>Invalid URL</div>"
    "</div>"
    "<script>"
    "function go(){{var v=document.getElementById('u').value.trim();if(!v)return;"
    "var t;try{{var u=new URL(v);t=u.searchParams.get('token')}}"
    "catch(_){{t=v}}if(t){{var d=new URL(window.location.href);"
    "d.searchParams.set('token',t);window.location.href=d.toString()}}"
    "else{{document.getElementById('e').style.display='block'}}}}"
    "document.getElementById('u').addEventListener('keydown',"
    "function(e){{if(e.key==='Enter')go()}});"
    "</script>"
    "</body></html>"
)


def _credential_fingerprint(value: str) -> str:
    """Identify a credential without disclosing it: short digest + length.

    ``absent`` for an empty value, which is a distinct and common case (a caller
    that could not read any credential file at all) and must not be confused with
    a caller holding the wrong one.

    Eight hex characters of a SHA-256 is an identifier, not the credential: it
    does not survive inversion for a 128-bit random value, and it goes only to the
    SEL audit log, which already sits on the keystone floor. Without it a
    cross-generation mismatch is indistinguishable from a forged header, which is
    what made a real desync take hours to attribute -- the log said only
    "wrong secret" and named no side.
    """
    if not value:
        return "absent"
    digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"{digest}/len={len(value)}"


def _credential_mismatch_detail(expected: str, provided: str) -> str:
    """Both fingerprints, for the one log line that has to explain a 403."""
    return (
        f"expected={_credential_fingerprint(expected)} "
        f"received={_credential_fingerprint(provided)}"
    )


def _deny(request: web.Request, reason: str, code: str = "") -> web.Response:
    headers = {"X-Auth-Required": "true"}
    if request.path.startswith("/api/"):
        # A machine-readable code alongside the prose: a caller cannot distinguish
        # a credential desync from a genuine permission denial by matching the
        # body text, and a tool that guesses from prose misdiagnoses the other one.
        # Written as a dict LITERAL with the key present so the error-code ratchet
        # can still read this sink statically -- handing it a prebuilt variable
        # would trade a `missing_code` for an `opaque_body`, which is the bucket
        # that hides every future regression here.
        return web.json_response(
            {"error": reason, "code": code or "forbidden"}, status=403, headers=headers
        )
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
            forwarded=is_proxied_request(request),
            operation="dashboard.token_auth",
            outcome=outcome,
            resources=request.path,
            error=error,
        )
    except Exception:
        logger.warning("Failed to log auth event to SEL", exc_info=True)
