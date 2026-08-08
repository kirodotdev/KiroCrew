"""Property tests for dashboard token authentication."""

from __future__ import annotations

import errno
import os
import socket
import string
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.token_auth import (
    MAX_CONCURRENT_NONCES,
    MAX_SESSION_TTL_SECS,
    RevokedNonceStore,
    _api_pattern_matches,
    _app_owns_path,
    app_token_path_allowed,
    bind_token_ip,
    check_token_ip,
    generate_token,
    is_consumed,
    mark_consumed,
    parse_duration,
    revoke_access_cookie,
    revoke_all_sessions,
    token_auth_middleware,
    token_embed_parent_port,
    try_consume,
    validate_token,
    validate_token_with_app,
)


@pytest.fixture(autouse=True)
def clear_nonces(tmp_path, monkeypatch):
    """Isolate token state per test.

    Points config_dir at a tmp dir so the persisted revocation-generation file
    is not written to the real ~/.kirocrew, resets the in-process gen to 0, and
    clears the nonce store. Uses _state.clear_all() (not revoke_all_sessions)
    so the gen isn't bumped between unrelated tests.
    """
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(_ta, "_REVOCATION_GEN", 0)
    # Reset the per-session revoked-nonce store so each test gets a fresh store
    # bound to its own tmp_path config_dir (the singleton would otherwise pin
    # the first test's path and leak revocations across tests).
    monkeypatch.setattr(_ta, "_revoked_store_singleton", None)
    _ta._state.clear_all()
    _ta._app_perms_cache.clear()
    yield
    monkeypatch.setattr(_ta, "_REVOCATION_GEN", 0)
    monkeypatch.setattr(_ta, "_revoked_store_singleton", None)
    _ta._state.clear_all()
    _ta._app_perms_cache.clear()


URL_SAFE_B64_CHARS = set(string.ascii_letters + string.digits + "-_.")


# -- Property 1: Token generation round-trip --


@pytest.mark.parametrize("user_id", ["alice", "bob@corp", "user-123", "a", "x" * 200])
def test_generate_then_validate_roundtrip(user_id: str) -> None:
    token = generate_token(user_id, ttl_seconds=60)
    valid, returned_id, reason = validate_token(token)
    assert valid is True
    assert returned_id == user_id
    assert reason == ""


# -- Property 2: Token URL safety --


@pytest.mark.parametrize("user_id", ["alice", "user/with/slashes", "emoji-☺", "a" * 300])
def test_token_url_safe_chars(user_id: str) -> None:
    token = generate_token(user_id)
    assert all(c in URL_SAFE_B64_CHARS for c in token)


# -- Property 3: Valid duration parsing --


@pytest.mark.parametrize("n", [0, 1, 5, 24, 100, 9999])
def test_parse_duration_hours(n: int) -> None:
    assert parse_duration(f"{n}h") == min(n * 3600, MAX_SESSION_TTL_SECS)


@pytest.mark.parametrize("n", [0, 1, 5, 30, 60, 9999])
def test_parse_duration_minutes(n: int) -> None:
    assert parse_duration(f"{n}m") == min(n * 60, MAX_SESSION_TTL_SECS)


# -- Property 4: Invalid duration strings rejected --


@pytest.mark.parametrize(
    "s",
    [
        "",
        "h",
        "m",
        "10",
        "10s",
        "10d",
        "abc",
        "-1h",
        "1.5h",
        "1H",
        "1M",
        " 1h",
        "1h ",
        "10hm",
        "h1",
        "m1",
    ],
)
def test_parse_duration_invalid(s: str) -> None:
    assert parse_duration(s) is None


# -- Property 13: IP binding enforcement --


def test_ip_binding_accepts_same_ip() -> None:
    token = generate_token("user1")
    bind_token_ip(token, "10.0.0.1")
    assert check_token_ip(token, "10.0.0.1") is True


def test_ip_binding_rejects_different_ip() -> None:
    token = generate_token("user2")
    bind_token_ip(token, "10.0.0.1")
    assert check_token_ip(token, "192.168.1.1") is False


def test_unbound_token_accepts_any_ip() -> None:
    token = generate_token("user3")
    assert check_token_ip(token, "10.0.0.1") is True
    assert check_token_ip(token, "192.168.1.1") is True


# -- Property 14: Token consumption --


def test_consumed_token_returns_true() -> None:
    token = generate_token("user4")
    assert is_consumed(token) is False
    mark_consumed(token)
    assert is_consumed(token) is True


def test_unconsumed_token_returns_false() -> None:
    token = generate_token("user5")
    assert is_consumed(token) is False


def test_try_consume_returns_true_once_then_false() -> None:
    """Verify try_consume atomicity: first call consumes, subsequent calls return False."""
    token = generate_token("user_try_consume")
    assert try_consume(token) is True, "first call should consume the token"
    assert try_consume(token) is False, "second call should report already consumed"
    assert is_consumed(token), "token should be marked consumed"


# -- Additional validation edge cases --


def test_expired_token_rejected() -> None:
    """Token link window (5 min) expires — URL no longer valid."""
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        token = generate_token("user6", ttl_seconds=3600)
    # Advance past the 5-minute link window
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0 + 301
        valid, _, reason = validate_token(token)
    assert valid is False
    assert "expired" in reason


def test_session_exp_still_valid_after_link_window() -> None:
    """Cookie-based access uses session_exp, not the link window."""
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        token = generate_token("user6b", ttl_seconds=3600)
    # Past link window but within session TTL
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0 + 301
        valid, uid, _ = validate_token(token, use_session_exp=True)
    assert valid is True
    assert uid == "user6b"


def test_tampered_token_rejected() -> None:
    token = generate_token("user7")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    valid, _, reason = validate_token(tampered)
    assert valid is False
    assert reason in ("invalid signature", "invalid encoding")


def test_malformed_token_rejected() -> None:
    valid, _, reason = validate_token("no-dot-here")
    assert valid is False
    assert reason == "malformed token"


# -- Middleware helpers --


async def _ok_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def _make_request(
    path: str = "/",
    query: dict | None = None,
    cookies: dict | None = None,
    remote: str = "127.0.0.1",
    headers: dict | None = None,
    method: str = "GET",
) -> MagicMock:
    """Build a mock aiohttp request."""
    req = MagicMock(spec=web.Request)
    req.path = path
    req.query = query or {}
    req.cookies = cookies or {}
    req.remote = remote
    req.headers = headers or {}
    req.method = method
    return req


# -- Property 5: Middleware accepts valid tokens via query param or cookie --


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["query", "cookie"])
async def test_middleware_accepts_valid_token(via: str) -> None:
    mw = token_auth_middleware()
    token = generate_token("testuser", ttl_seconds=300)

    if via == "cookie":
        # Pre-bind IP and mark consumed so cookie path works
        bind_token_ip(token, "127.0.0.1")
        mark_consumed(token)
        req = _make_request(cookies={"mc_token_5476": token})
    else:
        req = _make_request(query={"token": token})

    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert resp.text == "ok"


# -- Property 6: Cookie set with correct attributes on query-param auth --


@pytest.mark.asyncio
async def test_cookie_set_on_query_param_auth() -> None:
    mw = token_auth_middleware()
    token = generate_token("cookieuser", ttl_seconds=300)
    req = _make_request(query={"token": token}, remote="10.0.0.1")

    resp = await mw(req, _ok_handler)
    assert resp.status == 200

    cookie_header = resp.cookies.get("mc_token_5476")
    assert cookie_header is not None
    # Token→session exchange (CWE-613): the cookie must NOT be the raw URL
    # token — it's a distinct, freshly-minted session credential …
    assert cookie_header.value != token
    # … that is nonetheless valid for the same user on the cookie path.
    _ok, _uid, _ = validate_token(cookie_header.value, use_session_exp=True)
    assert _ok is True
    assert _uid == "cookieuser"
    assert cookie_header["httponly"] is True or "httponly" in str(cookie_header).lower()
    assert cookie_header["samesite"] == "Lax"
    assert cookie_header["path"] == "/"


@pytest.mark.asyncio
async def test_embed_parent_port_claim_survives_session_exchange() -> None:
    """PR #118 follow-up: the connect link token carries an embed_parent_port
    claim, but token_auth_middleware exchanges the link token for a fresh
    session cookie (CWE-613, never reuse the URL token). That exchange MUST
    carry the claim across, or the cookie the framed document actually presents
    drops it and server._extra_frame_ancestors falls back to bare
    frame-ancestors 'self' — the blank embedded-pane bug."""
    mw = token_auth_middleware()
    token = generate_token("embeduser", ttl_seconds=300, extra={"embed_parent_port": "5476"})
    req = _make_request(query={"token": token}, remote="10.0.0.1")

    resp = await mw(req, _ok_handler)
    assert resp.status == 200

    cookie_header = resp.cookies.get("mc_token_5476")
    assert cookie_header is not None
    # Distinct re-minted session token (CWE-613 exchange) …
    assert cookie_header.value != token
    # … that nonetheless preserves the frame-ancestors parent-port claim, so the
    # cookie-authenticated framed document still authorizes the parent origin.
    assert token_embed_parent_port(cookie_header.value) == 5476
    # And the middleware stashed the validated parent port on the request BEFORE
    # revoking the link nonce, so the first ?token= framed document's header
    # (server._extra_frame_ancestors) sees it even though the link token is now
    # revoked (PR #129 follow-up — the first-hit blank-pane fix).
    req.__setitem__.assert_any_call("embed_parent_port", "5476")


# -- Property 7: Cookie not re-set when already matching --


@pytest.mark.asyncio
async def test_cookie_not_reset_when_present() -> None:
    mw = token_auth_middleware()
    token = generate_token("existing", ttl_seconds=300)
    # Simulate prior query-param auth
    bind_token_ip(token, "127.0.0.1")
    mark_consumed(token)

    req = _make_request(cookies={"mc_token_5476": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    # Cookie should NOT be re-set on cookie-based auth
    assert "mc_token_5476" not in resp.cookies


# -- Cookie keyed by browser-facing (Host) port for tunneled multi-instance --


@pytest.mark.asyncio
async def test_cookie_named_by_host_port_under_tunnel() -> None:
    """Server on 5476 reached via tunneled localhost:7778 -> cookie is
    mc_token_7778 (Host port), not mc_token_5476 (server port). Lets two
    same-server-port instances coexist without colliding in the localhost jar."""
    mw = token_auth_middleware()  # default server port 5476
    token = generate_token("tunneluser", ttl_seconds=300)
    req = _make_request(
        query={"token": token}, remote="10.0.0.1", headers={"Host": "localhost:7778"}
    )

    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert resp.cookies.get("mc_token_7778") is not None  # keyed by Host port
    assert resp.cookies.get("mc_token_5476") is None  # not the server port


@pytest.mark.asyncio
async def test_cookie_read_uses_host_port() -> None:
    """A cookie named by the Host port authenticates the matching dashboard."""
    mw = token_auth_middleware()  # server port 5476
    token = generate_token("readuser", ttl_seconds=300)
    bind_token_ip(token, "127.0.0.1")
    mark_consumed(token)

    req = _make_request(cookies={"mc_token_7778": token}, headers={"Host": "localhost:7778"})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_cookie_server_port_name_denied_when_host_differs() -> None:
    """A server-port-named cookie does NOT authenticate a different Host port,
    so a sibling instance's cookie can't be mistaken for this one."""
    mw = token_auth_middleware()  # server port 5476
    token = generate_token("wrongport", ttl_seconds=300)
    bind_token_ip(token, "127.0.0.1")
    mark_consumed(token)

    req = _make_request(
        path="/api/status", cookies={"mc_token_5476": token}, headers={"Host": "localhost:7778"}
    )
    resp = await mw(req, _ok_handler)
    assert resp.status != 200


# -- Property 8: Static asset paths bypass token validation --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/assets/style.css",
        "/static/app.js",
        "/fonts/AWSDiatype-Regular.woff2",
        "/vendor/tailwindcss-browser.js",
        "/logo.png",
        "/manifest.json",
        "/sw.js",
        "/icon-192.png",
        "/icon-512.png",
        "/apps/some-app/ui/index.mjs",
        "/apps/some-app/ui/chunks/lazy-chunk.mjs",
    ],
)
async def test_static_assets_bypass_auth(path: str) -> None:
    mw = token_auth_middleware()
    req = _make_request(path=path)  # No token at all
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


# -- Property 8a: CLI endpoints that self-authenticate must bypass the gate --
#
# These three carry NO dashboard token: the `kirocrew` CLI authenticates to them
# with loopback + the local secret in an X-Local-Secret header, and each handler
# re-checks both itself. The middleware only honors X-Internal-Secret, so if one
# of them is missing from _BYPASS_EXACT the middleware denies it 403 before the
# handler ever runs — which is exactly how `kirocrew logout` broke.

_CLI_LOCAL_SECRET_PATHS = ("/api/token/local", "/api/shutdown", "/api/logout")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _CLI_LOCAL_SECRET_PATHS)
async def test_cli_local_secret_endpoints_bypass_auth(path: str) -> None:
    mw = token_auth_middleware()
    # No token, no cookie — only the header the CLI actually sends.
    req = _make_request(path=path, method="POST", headers={"X-Local-Secret": "irrelevant-here"})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200, (
        f"{path} was denied by the auth middleware; the CLI sends no dashboard "
        f"token, so the handler's own loopback + local-secret check never runs. "
        f"Add {path!r} to _BYPASS_EXACT in token_auth.py."
    )


def test_cli_local_secret_endpoints_are_in_bypass_exact() -> None:
    """The set membership itself, independent of middleware behaviour."""
    import kiro_crew.dashboard.token_auth as ta

    missing = [p for p in _CLI_LOCAL_SECRET_PATHS if p not in ta._BYPASS_EXACT]
    assert not missing, f"CLI local-secret endpoints missing from _BYPASS_EXACT: {missing}"


# -- Property 8b: /api/apps/* still requires auth (security boundary) --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/apps",
        "/api/apps/some-app/data/config.json",
        "/api/apps/some-app/storage/state",
    ],
)
async def test_apps_api_still_requires_auth(path: str) -> None:
    """The /apps/ static bypass MUST NOT leak into /api/apps/* paths.

    Static UI bundles are public-equivalent (same as /static/), but app data
    and storage live behind /api/* and continue to require a valid token.
    """
    mw = token_auth_middleware()
    req = _make_request(path=path)  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


# -- Property 8c: non-UI paths under /apps/{name}/ still require auth --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        # The reverse-proxy route at /apps/{name}/api/{path:.*} forwards to
        # the app's backend (handle_app_api_proxy). It MUST stay
        # authenticated — the proxy's HMAC only proves the request came
        # from the gateway, not that the user was authenticated.
        "/apps/some-app/api/things",
        "/apps/some-app/api/data/sensitive",
        "/apps/some-app/api/state?op=delete",
        # Future non-UI public paths under /apps/{name}/ are deny-by-default.
        # If a real need surfaces, add a dedicated regex with its own audit;
        # do not widen this bypass.
        "/apps/some-app/manifest.json",
        "/apps/some-app/config/settings.json",
        "/apps/some-app/admin",
    ],
)
async def test_apps_non_ui_paths_still_require_auth(path: str) -> None:
    """The /apps/{name}/ui/ bypass MUST NOT leak into other /apps/{name}/* paths.

    The bypass is anchored to /ui/ only (federated-app RFC §3.8). Anything
    else under /apps/{name}/ — most importantly the reverse-proxy at
    /apps/{name}/api/* — continues to require a valid token.
    """
    mw = token_auth_middleware()
    req = _make_request(path=path)  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


# -- Property 8d: /apps/{name}/ui/ bypass is restricted to safe methods --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["GET", "HEAD"],
)
async def test_apps_ui_bypass_allows_safe_methods(method: str) -> None:
    """GET and HEAD on /apps/{name}/ui/* bypass auth (static file serving)."""
    mw = token_auth_middleware()
    req = _make_request(path="/apps/some-app/ui/index.mjs", method=method)
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def test_apps_ui_bypass_blocks_unsafe_methods(method: str) -> None:
    """Non-safe methods on /apps/{name}/ui/* MUST require auth.

    The bypass is for static file serving only. If a write-capable handler
    is ever registered under /apps/{name}/ui/, it must remain auth-protected
    rather than silently inheriting the bypass.
    """
    mw = token_auth_middleware()
    req = _make_request(path="/apps/some-app/ui/upload", method=method)  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


# -- Property 8e: bare /apps/{name} paths are SPA navigations, not data routes --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/apps/code-review-sage",
        "/apps/mochi",
        "/apps/system-monitor",
        "/apps/some-app-with-dashes",
    ],
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_bare_app_path_is_spa_shell_request(path: str, method: str) -> None:
    """Bare /apps/{name} paths (no sub-path) are React Router navigation
    entries that must be treated as SPA shell requests so that a browser
    refresh (which issues a direct GET to the gateway) returns index.html
    rather than 404.

    Regression for: GET /apps/code-review-sage on refresh returned 404 because
    SPA_FALLBACK_EXCLUDED_PREFIXES included '/apps/' wholesale, causing the
    spa_fallback middleware to re-raise HTTPNotFound instead of serving shell.
    """
    import kiro_crew.dashboard.token_auth as ta

    req = _make_request(path=path, method=method)
    assert ta._is_spa_shell_request(
        req
    ), f"{method} {path} should be a SPA shell request (browser refresh must work)"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/apps/some-app/api/things",
        "/apps/some-app/ui/index.mjs",
        "/apps/some-app/ui/chunks/lazy.mjs",
        "/apps/some-app/api/data/sensitive",
    ],
)
def test_apps_sub_paths_are_not_spa_shell(path: str) -> None:
    """/apps/{name}/api/* and /apps/{name}/ui/* have real server-side handlers
    and must NOT be treated as SPA shell requests."""
    import kiro_crew.dashboard.token_auth as ta

    req = _make_request(path=path, method="GET")
    assert not ta._is_spa_shell_request(
        req
    ), f"GET {path} should NOT be a SPA shell request (has a real server-side handler)"


@pytest.mark.parametrize(
    "path",
    [
        "/apps/detail/task-runner",
        "/apps/detail/code-review-sage",
        "/apps/migrate/legacy-app",
        # An app whose name collides with a sub-namespace verb. The app name and
        # the router's detail/migrate verbs share a segment position, so these
        # are 3-segment client routes, not the 4-plus-segment server routes
        # (/apps/{name}/api/{path}). An earlier `(?:/|$)` excluded them.
        "/apps/detail/api",
        "/apps/detail/ui",
        "/apps/migrate/api",
        "/apps/migrate/ui",
    ],
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_apps_router_subpaths_are_spa_shell(path: str, method: str) -> None:
    """React Router owns /apps/detail/{name} and /apps/migrate/{name} (App.tsx).
    Neither has a server-side route, so both must get the SPA shell.

    Regression for: _APPS_SPA_EXCLUDED_RE matched any /apps/{seg}/ path, so it
    read "detail" and "migrate" as the app name and excluded these from the
    shell. Pasting /apps/detail/task-runner into the address bar (or refreshing
    on it) returned 404; the routes worked only via in-app navigation.
    """
    import kiro_crew.dashboard.token_auth as ta

    req = _make_request(path=path, method=method)
    assert ta._is_spa_shell_request(req), (
        f"{method} {path} should be a SPA shell request (React Router owns it, "
        f"there is no server-side handler for this path)"
    )


def test_apps_server_routes_are_excluded_from_shell() -> None:
    """Drift guard: every route apps/routes.py registers under /apps/ must be
    excluded from the shell, or the SPA would shadow a real handler.

    Reads the route literals from source so adding a third /apps/ sub-namespace
    without extending _APPS_SPA_EXCLUDED_RE fails here.
    """
    import re as _re

    import kiro_crew.apps.routes as ar
    import kiro_crew.dashboard.token_auth as ta

    source = open(ar.__file__, encoding="utf-8").read()
    # add_get("/path", h) / add_route("*", "/path", h) / add_post("/path", h)
    literals = _re.findall(r'add_(?:get|post|route)\(\s*(?:"[^"]*",\s*)?"([^"]+)"', source)
    apps_routes = [p for p in literals if p.startswith("/apps/")]
    assert apps_routes, "expected /apps/ route literals in apps/routes.py"

    # Concretize aiohttp placeholders into a path the regex can be run against.
    def concretize(p: str) -> str:
        p = p.replace("{name}", "sample-app")
        return _re.sub(r"\{[^}]+\}", "segment", p)

    offenders = [p for p in apps_routes if not ta._APPS_SPA_EXCLUDED_RE.match(concretize(p))]
    assert not offenders, (
        f"/apps/ route(s) with a real handler are NOT excluded from the SPA "
        f"shell and would be shadowed by index.html: {offenders}. Extend "
        f"_APPS_SPA_EXCLUDED_RE in token_auth.py to cover their sub-namespace."
    )


@pytest.mark.parametrize(
    "path",
    [
        "/app-assets/auto-research/icon.svg",
        "/app-assets/workflows/hero-light.svg",
        "/app-assets/auto-research/hero-dark.svg",
    ],
)
def test_app_assets_paths_are_not_spa_shell(path: str) -> None:
    """/app-assets/* are static brand files (builtin icons + hero images), not
    SPA navigation. They MUST be excluded from the SPA shell fallback so a
    request resolves against the /app-assets static mount (or 404s cleanly,
    letting the <img onError> fallback run) instead of being answered with
    index.html.

    Regression for: recently added colorful builtin icons / hero images did not
    render because /app-assets/ had no static route AND was not in
    SPA_FALLBACK_EXCLUDED_PREFIXES, so the SVG requests were served index.html
    (HTML) and every <img> tripped its placeholder fallback.
    """
    import kiro_crew.dashboard.token_auth as ta

    assert "/app-assets/" in ta.SPA_FALLBACK_EXCLUDED_PREFIXES
    req = _make_request(path=path, method="GET")
    assert not ta._is_spa_shell_request(
        req
    ), f"GET {path} should NOT be a SPA shell request (static brand asset)"


# -- Property 9: Loopback no longer bypasses auth (port-forward fix) --


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/status", "/api/agents"])
async def test_loopback_requires_token(path: str) -> None:
    # Being on loopback (127.0.0.1) does NOT grant access to data APIs — the
    # port-forward fix. (Non-API GET navigations now serve the public SPA
    # shell instead of 403; see test_spa_shell_served_without_token.)
    mw = token_auth_middleware()
    req = _make_request(path=path)  # No token, loopback
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_internal_path_trusts_loopback() -> None:
    secret = "test-secret-123"
    mw = token_auth_middleware(internal_paths=frozenset({"/api/spawn"}), internal_secret=secret)
    req = _make_request(path="/api/spawn", headers={"X-Internal-Secret": secret})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_internal_path_non_loopback_denied_in_local_only_mode() -> None:
    """Default local_only=True denies non-loopback even with valid secret."""
    secret = "test-secret-123"
    mw = token_auth_middleware(internal_paths=frozenset({"/api/spawn"}), internal_secret=secret)
    req = _make_request(path="/api/spawn", remote="10.0.0.1", headers={"X-Internal-Secret": secret})
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_internal_path_non_loopback_cookie_auth_when_not_local_only() -> None:
    """When local_only=False, non-loopback with valid cookie is granted."""
    token = generate_token("testuser", ttl_seconds=300)
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret="s", local_only=False
    )
    req = _make_request(path="/api/spawn", remote="10.0.0.1", cookies={"mc_token_5476": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_internal_path_non_loopback_no_cookie_denied() -> None:
    """When local_only=False, non-loopback without cookie is denied."""
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret="s", local_only=False
    )
    req = _make_request(path="/api/spawn", remote="10.0.0.1")
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_internal_path_non_loopback_wrong_secret_denied() -> None:
    """When local_only=False, wrong X-Internal-Secret is denied even with cookie."""
    token = generate_token("testuser", ttl_seconds=300)
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret="real", local_only=False
    )
    req = _make_request(
        path="/api/spawn",
        remote="10.0.0.1",
        headers={"X-Internal-Secret": "wrong"},
        cookies={"mc_token_5476": token},
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_internal_path_non_loopback_valid_secret_and_cookie_granted() -> None:
    """Both valid secret and valid cookie on non-loopback → granted."""
    token = generate_token("testuser", ttl_seconds=300)
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret="real", local_only=False
    )
    req = _make_request(
        path="/api/spawn",
        remote="10.0.0.1",
        headers={"X-Internal-Secret": "real"},
        cookies={"mc_token_5476": token},
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_internal_path_non_loopback_valid_secret_no_cookie_denied() -> None:
    """Valid secret alone is not enough for non-loopback; cookie is still required."""
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret="real", local_only=False
    )
    req = _make_request(
        path="/api/spawn",
        remote="10.0.0.1",
        headers={"X-Internal-Secret": "real"},
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_internal_path_rejects_wrong_secret() -> None:
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret="real-secret"
    )
    req = _make_request(path="/api/spawn", headers={"X-Internal-Secret": "wrong-secret"})
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_internal_path_matches_sub_paths() -> None:
    """GET /api/spawn/{id} should be granted via /api/spawn prefix."""
    secret = "test-secret-123"
    mw = token_auth_middleware(internal_paths=frozenset({"/api/spawn"}), internal_secret=secret)
    req = _make_request(path="/api/spawn/abc123", headers={"X-Internal-Secret": secret})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_internal_path_does_not_match_sibling_prefix() -> None:
    """GET /api/spawnfoo must NOT be treated as internal via /api/spawn."""
    secret = "test-secret-123"
    mw = token_auth_middleware(internal_paths=frozenset({"/api/spawn"}), internal_secret=secret)
    req = _make_request(path="/api/spawnfoo", headers={"X-Internal-Secret": secret})
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_sessions_summarize_is_registered_internal_path() -> None:
    """Regression: the MCP-only /api/sessions/summarize route (list_sessions
    summarize leg) MUST be in the strict internal allowlist. Its sole caller
    authenticates with X-Internal-Secret and sends no browser token, so if the
    route is missing from the allowlist it falls through to token auth and
    every summarize call silently degrades to titles.

    Drives the REAL _STRICT_INTERNAL_API_PATHS from server.py through the
    middleware so a future edit that drops the entry fails here."""
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/sessions/summarize" in _STRICT_INTERNAL_API_PATHS
    secret = "test-secret-123"
    mw = token_auth_middleware(internal_paths=_STRICT_INTERNAL_API_PATHS, internal_secret=secret)
    # Internal secret on loopback → granted (the MCP _post path).
    ok = await mw(
        _make_request(
            path="/api/sessions/summarize",
            method="POST",
            headers={"X-Internal-Secret": secret},
        ),
        _ok_handler,
    )
    assert ok.status == 200
    # No token / no secret → denied (proves it is a protected route, not open).
    denied = await mw(_make_request(path="/api/sessions/summarize", method="POST"), _ok_handler)
    assert denied.status == 403


# -- Property 9b: mixed_internal_paths (loopback MCP + non-loopback browser) --


@pytest.mark.asyncio
async def test_mixed_path_loopback_with_secret_granted() -> None:
    """MCP path: loopback + X-Internal-Secret → granted via fast-path."""
    secret = "test-secret-123"
    mw = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/spawn"}), internal_secret=secret
    )
    req = _make_request(path="/api/spawn", headers={"X-Internal-Secret": secret})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_mixed_path_non_loopback_with_valid_cookie_granted() -> None:
    """DCV/SSH-forwarded browser: non-loopback + valid cookie → granted (no false banner)."""
    mw = token_auth_middleware(mixed_internal_paths=frozenset({"/api/spawn"}))
    token = generate_token("dcvuser", ttl_seconds=300)
    bind_token_ip(token, "10.0.0.1")
    mark_consumed(token)
    req = _make_request(path="/api/spawn", remote="10.0.0.1", cookies={"mc_token_5476": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_mixed_path_non_loopback_without_cookie_denied() -> None:
    """Non-loopback + no cookie → still denied (security preserved)."""
    mw = token_auth_middleware(mixed_internal_paths=frozenset({"/api/spawn"}))
    req = _make_request(path="/api/spawn", remote="10.0.0.1")
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_strict_path_non_loopback_still_hard_denied() -> None:
    """Strict internal path: non-loopback → hard-denied even with valid cookie
    (invariant: machine-to-machine isolation preserved)."""
    mw = token_auth_middleware(internal_paths=frozenset({"/api/send-message"}))
    token = generate_token("attacker", ttl_seconds=300)
    bind_token_ip(token, "10.0.0.1")
    mark_consumed(token)
    req = _make_request(
        path="/api/send-message", remote="10.0.0.1", cookies={"mc_token_5476": token}
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


# -- Property 10: Nonce-based token invalidation --


def test_oldest_token_evicted_after_max_concurrent() -> None:
    """Token beyond MAX_CONCURRENT_NONCES evicts the oldest nonce."""
    tokens = [generate_token("user1") for _ in range(MAX_CONCURRENT_NONCES + 1)]
    valid_old, _, reason = validate_token(tokens[0])
    valid_new, _, _ = validate_token(tokens[-1])
    assert (
        not valid_old
    ), f"oldest token should be evicted after {MAX_CONCURRENT_NONCES + 1} generations"
    assert reason == "token superseded"
    assert valid_new, "most recently issued token should remain valid"
    # Verify second-oldest survives (only one evicted)
    valid_survivor, _, _ = validate_token(tokens[1])
    assert valid_survivor, "second-oldest token should survive when only one is evicted"


def test_concurrent_tokens_within_limit_all_valid() -> None:
    """Up to MAX_CONCURRENT_NONCES tokens should all remain valid."""
    tokens = [generate_token(f"user{i}") for i in range(MAX_CONCURRENT_NONCES)]
    for i, token in enumerate(tokens):
        valid, uid, _ = validate_token(token)
        assert valid, f"token {i} should be valid within concurrent limit"
        assert uid == f"user{i}"


def test_token_rejected_when_no_nonces_registered() -> None:
    """Verify deny-by-default: tokens rejected after an explicit revoke.

    revoke_all_sessions() both clears the nonce store AND bumps the persisted
    revocation generation, so a token minted before it is rejected either as
    'session revoked' (gen check, which fires first) or 'no active sessions'
    (nonce check) — both are valid deny-by-default rejections.
    """
    token = generate_token("user1")
    revoke_all_sessions()
    valid, _, reason = validate_token(token)
    assert not valid
    assert reason in ("session revoked", "no active sessions")


def test_cookie_auth_survives_nonce_store_wipe() -> None:
    """Regression: an established session cookie must survive a gateway RESTART.

    A restart reloads the persisted signing secret + revocation generation
    unchanged and re-initializes the in-memory nonce store empty. The cookie
    path (use_session_exp=True) must pass on signature + session_exp + matching
    gen alone — requiring the per-process nonce would log everyone out on every
    restart. The LINK path still enforces the nonce. We model the restart by
    clearing ONLY the nonce store (gen untouched), not via revoke_all_sessions.
    """
    from kiro_crew.dashboard.token_auth import _state

    token = generate_token("user_cookie")
    _state.clear_all()  # simulate restart: in-memory nonce store re-initialized empty
    # LINK click still requires the nonce → rejected.
    link_valid, _, link_reason = validate_token(token, use_session_exp=False)
    assert not link_valid
    assert link_reason in ("no active sessions", "token superseded")
    # COOKIE re-auth survives the restart (gen unchanged).
    cookie_valid, uid, cookie_reason = validate_token(token, use_session_exp=True)
    assert cookie_valid, f"cookie should survive restart, got: {cookie_reason}"
    assert uid == "user_cookie"


def test_revoke_all_sessions_kills_established_cookie() -> None:
    """Explicit revoke (kirocrew logout) MUST end established cookie sessions.

    Unlike a restart, revoke_all_sessions() bumps the persisted revocation
    generation, so a cookie minted before the revoke carries a stale gen and is
    rejected on its next request — even though its HMAC signature and
    session_exp are still valid. This is the control the nonce store could not
    provide for cookies (it is per-process and restart-cleared).
    """
    token = generate_token("user_cookie")
    # Cookie is valid before revoke.
    valid_before, _, _ = validate_token(token, use_session_exp=True)
    assert valid_before
    revoke_all_sessions()  # explicit operator logout
    # Cookie is now rejected as revoked.
    valid_after, _, reason = validate_token(token, use_session_exp=True)
    assert not valid_after
    assert reason == "session revoked"


def test_signing_secret_persisted_across_loads(tmp_path, monkeypatch) -> None:
    """Regression: the HMAC signing secret must persist across processes.

    Previously _SECRET was os.urandom(32) per import, so every restart rotated
    the key and invalidated all outstanding tokens/cookies ("invalid
    signature"). The secret is now loaded-or-created from a 0600 key file.
    """
    from kiro_crew.dashboard import token_auth as ta

    monkeypatch.setattr(ta, "config_dir", lambda: tmp_path, raising=False)
    # First load creates the key file.
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    s1 = ta._load_or_create_secret()
    key_file = tmp_path / ta._SECRET_KEY_FILE
    assert key_file.exists()
    assert len(s1) >= 32
    # Owner-only permissions. POSIX enforces this via chmod 0o600; Windows applies
    # an owner DACL (icacls) that does not surface in st_mode (files report
    # 0o666), so the POSIX-bit assertion is only meaningful off Windows (the
    # Windows DACL path is covered by test_platform_compat::TestRestrictToOwner).
    if os.name != "nt":
        assert (key_file.stat().st_mode & 0o777) == 0o600
    # Second load returns the SAME secret (persistence).
    s2 = ta._load_or_create_secret()
    assert s1 == s2


def test_signing_secret_concurrent_first_init_converges(tmp_path, monkeypatch) -> None:
    """Regression (PR #338 / GPT 5.6 HIGH): concurrent first-time inits MUST
    converge on a single signing key.

    ``warm_auth_singletons()`` primes the signing secret BEFORE the gateway
    binds its port, so two fresh gateways sharing one data home can both
    observe ``token_signing.key`` as absent at once. The old load-or-create
    used ``O_CREAT | O_TRUNC``: each racer generated its OWN key and the last
    writer's bytes landed on disk, while the loser kept a divergent in-memory
    key — every token it signed was then invalid to a sibling instance / after
    a restart (silent auth corruption). The fix makes creation exclusive
    (``O_CREAT | O_EXCL`` + read-the-winner with bounded retry), so every racer
    returns the ONE key that is persisted on disk.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE
    assert not key_file.exists()

    n = 8
    barrier = threading.Barrier(n)
    results: list[bytes] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _worker() -> None:
        try:
            # Release all workers simultaneously to maximise the odds they all
            # observe the key file as absent — the exact first-init race.
            barrier.wait()
            secret = ts._load_or_create_secret()
        except BaseException as exc:  # noqa: BLE001 - surfaced via assert below
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(secret)

    threads = [threading.Thread(target=_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"workers raised: {errors!r}"
    assert len(results) == n
    on_disk = key_file.read_bytes()
    assert len(on_disk) >= 32
    # Crux: every racer converged on the SINGLE key persisted on disk.
    assert all(r == on_disk for r in results), "concurrent inits diverged from the on-disk key"
    assert len(set(results)) == 1, "more than one distinct signing key was issued"
    # Winner's create still locked the file down to owner-only. POSIX enforces
    # this via chmod 0o600; Windows applies an owner-only DACL (icacls) that does
    # NOT surface in st_mode (files report 0o666), so the POSIX-bit assertion is
    # only meaningful off Windows — the Windows DACL path has direct coverage in
    # test_platform_compat::TestRestrictToOwner.
    if os.name != "nt":
        assert (key_file.stat().st_mode & 0o777) == 0o600


def test_signing_secret_read_contention_retries_not_ephemeral(tmp_path, monkeypatch) -> None:
    """A TRANSIENT read failure on the key file — e.g. a Windows sharing
    violation while a concurrent creator holds it open to write the fresh key —
    must be RETRIED, not degraded to an ephemeral secret. Otherwise racing
    first-inits diverge from the persisted key (silent auth corruption). This is
    the root cause of the flaky Windows `test_signing_secret_concurrent_first_init_converges`
    divergence; reproduced deterministically here by faulting the first read.
    """
    import pathlib

    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE
    persisted = b"K" * 48  # a valid, already-persisted key (>= 32 bytes)
    key_file.write_bytes(persisted)

    real_read_bytes = pathlib.Path.read_bytes
    calls = {"n": 0}

    def _flaky_read(self):  # type: ignore[no-untyped-def]
        # Fault ONLY the first read of the key file with a sharing-violation-style
        # PermissionError; the retry (and every other read) hits the real call.
        if self.name == ts._SECRET_KEY_FILE:
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("simulated Windows sharing violation")
        return real_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", _flaky_read)

    got = ts._load_or_create_secret()
    assert calls["n"] >= 2, "the contended read was not retried"
    assert got == persisted, "must return the persisted key, not an ephemeral one"


def test_signing_secret_create_contention_retries_not_ephemeral(tmp_path, monkeypatch) -> None:
    """A TRANSIENT sharing violation on the EXCLUSIVE-CREATE of the key file must
    be retried, not degraded to an ephemeral secret.

    On Windows a racer's ``os.open(..., O_CREAT|O_EXCL)`` can hit a sharing
    violation (``PermissionError`` / WinError 32) while the winner holds the
    freshly-created file open — landing on ``os.open``, not the read. The
    create-side companion to ``test_signing_secret_read_contention_retries_not_ephemeral``;
    reproduced deterministically with the ``os.open`` simulator so it is caught
    on the POSIX dev loop.
    """
    from windows_sim import open_sharing_violation

    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE

    # Fault the FIRST exclusive-create of the key file; the retry must succeed.
    with open_sharing_violation(match=ts._SECRET_KEY_FILE, times=1) as state:
        secret = ts._load_or_create_secret()

    assert state["n"] >= 2, "the contended exclusive-create was not retried"
    assert key_file.exists(), "key must be persisted after retrying the create"
    on_disk = key_file.read_bytes()
    assert len(on_disk) >= ts._MIN_KEY_BYTES, "retry wrote a short/incomplete key"
    assert secret == on_disk, "must return the persisted key, not an ephemeral one"


def test_signing_secret_binary_write_survives_windows_text_mode(tmp_path, monkeypatch) -> None:
    """Regression: the key must be written in BINARY mode.

    On Windows ``os.open()`` defaults to TEXT mode, so the ``os.write()`` that
    persists the random key translates every ``0x0A`` ('\\n') byte to
    ``0x0D 0x0A`` ('\\r\\n'). The on-disk key then grows and no longer equals
    the creator's in-memory bytes (nor a sibling's read) — silent auth
    divergence, and the root cause of the flaky Windows failures in
    ``test_signing_secret_{concurrent_first_init_converges,create_contention_retries_not_ephemeral,write_failure_cleans_up_incomplete_file}``.
    The fix opens the file with ``os.O_BINARY``. Reproduced deterministically on
    POSIX via the text-mode simulator plus a key that contains an ``0x0A`` byte.
    """
    from windows_sim import windows_text_mode_write

    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE

    # A deterministic 32-byte key that CONTAINS 0x0A (LF) at index 10, so a
    # text-mode write would demonstrably corrupt it (a purely random key might
    # by chance contain no 0x0A and hide the bug).
    fixed_key = bytes(range(ts._MIN_KEY_BYTES))
    assert b"\n" in fixed_key
    monkeypatch.setattr(os, "urandom", lambda n: fixed_key[:n])

    with windows_text_mode_write(match=ts._SECRET_KEY_FILE):
        secret = ts._load_or_create_secret()

    on_disk = key_file.read_bytes()
    # With the O_BINARY fix, no translation occurs: the persisted key is the
    # exact 32 bytes and equals what was returned. Without it, the simulator
    # would have written 33 bytes ('\r\n' for the '\n') and these would differ.
    assert len(on_disk) == ts._MIN_KEY_BYTES, "newline translation changed the key length"
    assert on_disk == fixed_key, "persisted key was corrupted by text-mode newline translation"
    assert secret == on_disk, "returned key diverged from the persisted key"


def test_signing_secret_existing_file_never_overwritten(tmp_path, monkeypatch) -> None:
    """Regression (PR #338): warming must never overwrite or truncate an
    existing key file.

    A pre-existing valid key is read verbatim across repeated warms (multiple
    gateway boots against the same home); its bytes, size, and inode are
    untouched, so restarts / sibling instances keep signing with the identical
    key.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE
    original = b"K" * 48  # deterministic, >= 32 bytes
    key_file.write_bytes(original)
    os.chmod(key_file, 0o600)
    stat_before = key_file.stat()

    for _ in range(5):
        got = ts._load_or_create_secret()
        assert got == original, "existing key must be returned verbatim"

    assert key_file.read_bytes() == original, "existing key file was mutated"
    stat_after = key_file.stat()
    assert stat_after.st_size == len(original), "existing key file was truncated/rewritten"
    assert stat_after.st_ino == stat_before.st_ino, "existing key file was recreated"


def test_signing_secret_write_failure_cleans_up_incomplete_file(tmp_path, monkeypatch) -> None:
    """Regression (PR #338 / GPT 5.6 HIGH): a write failure DURING exclusive
    creation must NOT leave a poisoned short key file behind.

    The exclusive creator opens ``token_signing.key`` with ``O_CREAT|O_EXCL``
    then writes 32 bytes. If that write fails partway (ENOSPC, quota) the file
    exists but is < 32 bytes. Previously the incomplete file was left on disk:
    every future boot's fast-path read saw < 32 bytes, the O_EXCL create then
    hit FileExistsError, the bounded retry budget exhausted, and the gateway
    fell back to a FRESH ephemeral key on EVERY restart (tokens die on each
    restart; concurrent gateways cannot validate one another) until a human
    deleted the file by hand.

    The fix removes the creator's OWN incomplete file (guarded by a
    device+inode identity check so a racing sibling's valid key is never
    deleted) before degrading to an ephemeral secret, so the NEXT init can
    create a valid, persisted key.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE
    assert not key_file.exists()

    real_write = os.write
    calls = {"n": 0}

    def _failing_write(fd, data):  # type: ignore[no-untyped-def]
        # Fail the FIRST os.write (the key-persist write inside the
        # exclusive-create path) with ENOSPC; delegate every other write to the
        # real syscall so the second init below can persist a real key.
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", _failing_write)

    # (a) A write failure still yields a working (ephemeral) secret this run.
    secret = ts._load_or_create_secret()
    assert calls["n"] >= 1, "the faulting write path was never exercised"
    assert len(secret) >= ts._MIN_KEY_BYTES

    # (b) The creator removed its OWN incomplete file — nothing short/empty is
    #     left behind to poison future boots.
    assert not key_file.exists(), "incomplete key file was left on disk (poisoned)"

    # Restore a working write and prove the path is no longer poisoned: the
    # next init must create a full, durable, owner-only key and return it.
    monkeypatch.setattr(os, "write", real_write)
    secret2 = ts._load_or_create_secret()
    assert key_file.exists(), "next init failed to create a key file"
    on_disk = key_file.read_bytes()
    assert len(on_disk) >= ts._MIN_KEY_BYTES, "next init wrote a short/incomplete key"
    assert secret2 == on_disk, "next init did not return the persisted key"
    # POSIX-only: Windows locks the key down via an owner DACL, not st_mode bits
    # (see the concurrent-init test above / test_platform_compat).
    if os.name != "nt":
        assert (key_file.stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "Simulates the sibling-substitution race by os.replace()-ing the key "
        "file while THIS process still holds it open. Windows forbids replacing "
        "an open handle (WinError 32 sharing violation), so the distinct-inode "
        "substitution cannot be reproduced. The identity guard's st_dev/st_ino "
        "logic keeps full coverage on the POSIX matrix."
    ),
)
def test_signing_secret_incomplete_file_not_deleted_if_replaced(tmp_path, monkeypatch) -> None:
    """The write-failure cleanup must NOT delete a valid key that a racing
    sibling substituted at the same path (identity guard on st_dev/st_ino).

    Simulate the race deterministically: the failing write, before raising,
    replaces the just-created (still-empty) key file with a DIFFERENT inode
    holding a valid 32-byte key — exactly what a sibling gateway that won a
    subsequent create would leave. The cleanup's ``os.lstat`` identity check
    must see the mismatch and leave the sibling's key untouched.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE
    sibling_key = b"S" * 48  # distinct, valid (>= 32 bytes)

    real_write = os.write
    calls = {"n": 0}

    def _failing_write(fd, data):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            # Replace our empty file with a sibling's valid key at a NEW inode
            # (atomic rename over the path), then fail our own write.
            tmp = tmp_path / "sibling.key"
            tmp.write_bytes(sibling_key)
            os.replace(tmp, key_file)
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", _failing_write)

    secret = ts._load_or_create_secret()
    # We failed our write and fell back to ephemeral for THIS run...
    assert len(secret) >= ts._MIN_KEY_BYTES
    # ...but the sibling's valid key at the substituted inode was NOT deleted.
    assert key_file.exists(), "identity guard wrongly deleted the sibling's key"
    assert key_file.read_bytes() == sibling_key, "sibling's key bytes were mutated"


def test_evict_expired_removes_old_entries() -> None:
    """Verify evict_expired removes expired IP bindings, consumed tokens, and nonces."""
    from kiro_crew.dashboard.token_auth import _state

    # Generate a token and bind IP / mark consumed
    token = generate_token("evict_user")
    bind_token_ip(token, "10.0.0.1", session_exp=1000.0)  # Already expired
    mark_consumed(token, session_exp=1000.0)  # Already expired

    # Manually add an expired nonce
    with _state._lock:
        _state._nonces["expired_nonce"] = 1000.0  # Already expired

    # Evict with current time > 1000
    _state.evict_expired(2000.0)

    # Verify expired entries were removed
    with _state._lock:
        assert token not in _state._ip_bindings, "expired IP binding should be evicted"
        assert token not in _state._consumed, "expired consumed token should be evicted"
        assert "expired_nonce" not in _state._nonces, "expired nonce should be evicted"


def test_token_reusable_across_multiple_validations() -> None:
    token = generate_token("user1")
    for _ in range(5):
        valid, _, _ = validate_token(token)
        assert valid, "same token should be reusable across browsers/tabs/apps"


def test_active_nonce_survives_eviction_via_refresh() -> None:
    """Validating a token refreshes its nonce position, preventing eviction."""
    old_token = generate_token("old_user")
    # Fill remaining slots
    for i in range(MAX_CONCURRENT_NONCES - 1):
        generate_token(f"filler{i}")
    # old_token is now the oldest — validate it to refresh its position
    valid, _, _ = validate_token(old_token, use_session_exp=True)
    assert valid, "old token should still be valid before overflow"
    # Generate one more to trigger eviction — old_token should survive
    generate_token("overflow")
    valid_after, _, reason = validate_token(old_token, use_session_exp=True)
    assert valid_after, f"actively-used token should survive eviction, got: {reason}"


# -- Property 12: /api/* paths get JSON 403, non-API non-GET paths get HTML 403 --


@pytest.mark.asyncio
async def test_api_path_gets_json_403() -> None:
    mw = token_auth_middleware()
    req = _make_request(path="/api/status", remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert resp.content_type == "application/json"


@pytest.mark.asyncio
async def test_non_api_path_gets_html_403() -> None:
    # Non-API GET navigations now serve the SPA shell (see
    # test_spa_shell_served_without_token). A non-API request with a
    # state-changing method still hits the deny path and gets HTML 403.
    mw = token_auth_middleware()
    req = _make_request(path="/dashboard", method="POST", remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert resp.content_type == "text/html"


# -- Property 12b: SPA shell is public so the app can cold-start refresh --


async def _shell_handler(request: web.Request) -> web.Response:
    """Stand-in for handlers.index — returns a distinguishable body so tests
    can prove the SHELL was served (default-deny) rather than the matched
    route handler (_ok_handler)."""
    return web.Response(text="SHELL", status=200)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/", "/index.html", "/dashboard", "/chat/abc123", "/some/deep/route"]
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_spa_shell_served_without_token(path: str, method: str) -> None:
    """GET/HEAD to a non-API SPA route with no token serves the shell (200),
    not a 403 — so the React app boots and self-recovers via the refresh
    cookie. The shell handler serves DIRECTLY (default-deny: the matched route
    handler `_ok_handler` is never invoked for an unauthenticated request)."""
    mw = token_auth_middleware(spa_shell_handler=_shell_handler)
    req = _make_request(path=path, method=method, remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert resp.text == "SHELL"  # shell served, NOT the route handler


@pytest.mark.asyncio
async def test_spa_shell_served_with_expired_cookie() -> None:
    """Cold-start variant: an expired/invalid access cookie is still present
    (browser hasn't dropped it yet). The shell still serves so the SPA can
    boot and refresh."""
    mw = token_auth_middleware(spa_shell_handler=_shell_handler)
    req = _make_request(
        path="/", cookies={"mc_token_5476": "garbage.invalid.token"}, remote="10.0.0.1"
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert resp.text == "SHELL"


@pytest.mark.asyncio
async def test_shell_not_served_when_handler_unconfigured() -> None:
    """Default-deny: with no spa_shell_handler wired, a shell GET with no token
    is DENIED (403), not served. The bypass never fails open."""
    mw = token_auth_middleware()  # no shell handler
    req = _make_request(path="/", remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/status", "/api/agents", "/apps/some-app/api/things"])
async def test_data_paths_still_gated_when_shell_public(path: str) -> None:
    """The shell bypass MUST NOT leak to data paths: /api/* and the
    /apps/{name}/api/* reverse proxy still require a valid token even with the
    shell handler wired."""
    mw = token_auth_middleware(spa_shell_handler=_shell_handler)
    req = _make_request(path=path, remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_shell_bypass_restricted_to_safe_methods(method: str) -> None:
    """Only GET/HEAD serve the shell. A state-changing method to a shell path
    still requires auth — no write ever bypasses."""
    mw = token_auth_middleware(spa_shell_handler=_shell_handler)
    req = _make_request(path="/", method=method, remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_valid_token_on_shell_still_mints_cookie() -> None:
    """A valid ?token= on the shell path is NOT short-circuited by the shell
    bypass — it flows through the normal exchange and mints the access cookie
    (URL-mint flow preserved)."""
    mw = token_auth_middleware(spa_shell_handler=_shell_handler)
    token = generate_token("shell_user", ttl_seconds=300)
    req = _make_request(query={"token": token}, remote="10.0.0.1")  # path="/" default
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert resp.text == "ok"  # routed to the real handler, not the shell
    cookie_header = resp.cookies.get("mc_token_5476")
    assert cookie_header is not None
    # CWE-613: the URL/link token is NEVER reused verbatim as the session cookie;
    # a SEPARATE session token is minted (fresh nonce, same identity). The cookie
    # must therefore differ from the link token but still validate.
    assert cookie_header.value != token
    ok, uid, _ = validate_token(cookie_header.value, use_session_exp=True)
    assert ok is True and uid == "shell_user"


def test_no_get_route_outside_shell_exclusions() -> None:
    """Robust drift guard: every statically-registered GET route in server.py
    must be shell-safe — the index/shell route, a known public PWA/static file,
    under a SPA_FALLBACK_EXCLUDED_PREFIXES data/static prefix, or under the
    /apps/ sub-namespace handled by _APPS_SPA_EXCLUDED_RE. A new GET added
    under a data namespace NOT covered by either guard (e.g. a future
    `GET /v1/models`) fails this test, forcing the exclusion set to be updated
    so the shell bypass can never serve that route unauthenticated.

    Note: /apps/ routes are validated against _APPS_SPA_EXCLUDED_RE (which
    requires a sub-path after {name}/), NOT against SPA_FALLBACK_EXCLUDED_PREFIXES
    (which no longer contains "/apps/" since that entry was dead code after
    _is_spa_shell_request gained its own /apps/ early-return branch).
    (Reads source rather than importing server.py to avoid heavy import side effects.)
    """
    import os
    import re as _re

    import kiro_crew.dashboard.token_auth as ta

    server_path = os.path.join(os.path.dirname(ta.__file__), "server.py")
    source = open(server_path, encoding="utf-8").read()
    get_paths = _re.findall(r'add_get\(\s*["\']([^"\']+)["\']', source)
    assert get_paths, "expected add_get route literals in server.py"

    # The one sanctioned non-literal registration: app window entries, where
    # add_get(route_path, ...) is fed by startup filesystem discovery. Those
    # routes are excluded from the shell fallback BY CONSTRUCTION — the same
    # loop that registers each route appends it to register_app_window_paths()
    # (see test_app_window_entries_register_route_and_exclusion). Assert the
    # coupling is still in place, then assert discovery is the ONLY dynamic
    # add_get so a future one cannot ride in under this exemption.
    dynamic_gets = _re.findall(r"add_get\((?!\s*[\"'])\s*(\w+)", source)
    assert dynamic_gets == ["route_path"], (
        f"non-literal add_get registrations in server.py: {dynamic_gets}. Only "
        f"the app-window-entry discovery loop may register dynamic GET routes, "
        f"and it must pair every route with register_app_window_paths()."
    )
    assert "register_app_window_paths(window_paths)" in source

    nonshell = tuple(ta.SPA_FALLBACK_EXCLUDED_PREFIXES)
    bypass_exact = ta._BYPASS_EXACT

    def shell_safe(p: str) -> bool:
        return (
            p == "/"  # the SPA shell itself
            or p in bypass_exact  # explicit public files (logo, etc.)
            or p.startswith("/{")  # pattern route (PWA: manifest/sw/icon/worklet)
            or p.startswith(nonshell)  # data/static namespaces (/api/, /v1/, etc.)
            # /apps/ routes: safe when they have a sub-path after {name}/ (real
            # handler) OR when they are aiohttp pattern routes (contain "{" in
            # the path, e.g. /apps/{name}/ui/{path:.*}). _APPS_SPA_EXCLUDED_RE
            # matches concrete /apps/{lower-name}/<sub-path> from the live router;
            # pattern-literal routes (with { placeholders) are always real handlers.
            or (p.startswith("/apps/") and (ta._APPS_SPA_EXCLUDED_RE.match(p) or "{" in p))
        )

    offenders = [p for p in get_paths if not shell_safe(p)]
    assert not offenders, (
        f"GET route(s) outside the shell-exclusion set would be served the SPA "
        f"shell unauthenticated: {offenders}. Add their namespace to "
        f"SPA_FALLBACK_EXCLUDED_PREFIXES (or _APPS_SPA_EXCLUDED_RE for /apps/ "
        f"sub-paths) in token_auth.py."
    )


@pytest.mark.asyncio
async def test_shell_bypass_does_not_preempt_ip_mismatch() -> None:
    """A VALID token from the wrong IP on a shell path is still a hard 403 —
    the shell bypass lives only in the no-token / invalid-token branches, so a
    valid token flows through to the IP-binding check. Pins the 'IP mismatch
    stays 403' invariant: this test fails if the bypass is ever moved above
    validation/IP-binding."""
    mw = token_auth_middleware(spa_shell_handler=_shell_handler)
    token = generate_token("ipuser", ttl_seconds=300)
    bind_token_ip(token, "10.0.0.1")
    mark_consumed(token)
    req = _make_request(
        path="/", cookies={"mc_token_5476": token}, remote="10.0.0.99"
    )  # valid token, different IP, shell path
    resp = await mw(req, _ok_handler)
    assert resp.status == 403  # IP mismatch wins; shell bypass does NOT fire


@pytest.mark.asyncio
async def test_invalid_token_shell_serve_emits_distinct_sel_outcome(monkeypatch) -> None:
    """The invalid/forged-token shell serve MUST log a distinct, non-"ok" SEL
    outcome so anomaly detection still flags credential-forgery probing on GET
    navigations. Pins the observability signal: fails if the outcome reverts
    to "ok"."""
    import kiro_crew.dashboard.token_auth as ta

    calls: list[dict] = []

    class _FakeSel:
        def log_api_access(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(ta, "_sel_fn", lambda: _FakeSel())
    mw = ta.token_auth_middleware(spa_shell_handler=_shell_handler)
    req = _make_request(
        path="/", cookies={"mc_token_5476": "garbage.invalid.token"}, remote="10.0.0.1"
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    outcomes = [c.get("outcome") for c in calls]
    assert "shell_unauth_invalid_token" in outcomes  # forgery signal preserved
    assert "ok" not in outcomes  # NOT logged as an authenticated success


# -- Property 15: non-local mode forces token auth for all requests --


@pytest.mark.asyncio
async def test_query_param_token_reusable_across_requests() -> None:
    """Same token can be used from multiple browsers/tabs/apps."""
    mw = token_auth_middleware()
    token = generate_token("reuse_user", ttl_seconds=300)

    # First use: succeeds
    req1 = _make_request(query={"token": token}, remote="10.0.0.1")
    resp1 = await mw(req1, _ok_handler)
    assert resp1.status == 200

    # Second use of the same token via query param: also succeeds
    req2 = _make_request(query={"token": token}, remote="10.0.0.1")
    resp2 = await mw(req2, _ok_handler)
    assert resp2.status == 200


@pytest.mark.asyncio
async def test_non_local_requires_auth() -> None:
    """Non-loopback clients require auth (data paths; non-API GET serves the
    public SPA shell — see test_spa_shell_served_without_token)."""
    mw = token_auth_middleware()
    req = _make_request(path="/api/status", remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_non_local_accepts_valid_token() -> None:
    """Non-loopback clients with valid tokens are granted access."""
    mw = token_auth_middleware()
    token = generate_token("remote_user", ttl_seconds=300)
    req = _make_request(query={"token": token}, remote="10.0.0.1")
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert resp.text == "ok"


# -- Property 16: URL uses hostname for remote access, localhost for local-only --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dashboard_url, expected_host",
    [
        ("http://myhostname:8080", "myhostname"),
        ("", "localhost"),  # no URL → localhost-only default
    ],
)
async def test_dashboard_url_host_selection(dashboard_url: str, expected_host: str) -> None:
    """!dashboard sends presigned link via DM, never in channel."""
    from kiro_crew.slack.handler import _handle_slash_command

    slack = MagicMock()
    slack.post_message = AsyncMock(return_value=None)
    slack.open_dm = AsyncMock(return_value="D_DM")
    slack.post_blocks = AsyncMock(return_value=None)
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)

    mock_cfg = MagicMock()
    mock_cfg.dashboard.url = dashboard_url

    expected_port = 8080 if dashboard_url else 5476

    # Unset KIROCREW_PORT so parse_dashboard_url (which reads os.environ at
    # call time) uses the port from the URL or the hard-coded default.
    with (
        patch("kiro_crew.slack.allowlist.KiroCrewConfig.load", return_value=mock_cfg),
        patch("kiro_crew.dashboard.origin.socket.gethostname", return_value="myhostname"),
        patch("kiro_crew.dashboard.origin.socket.gethostbyname", return_value="10.0.0.1"),
        patch("kiro_crew.dashboard.origin.socket.getaddrinfo", side_effect=socket.gaierror),
        patch.dict(os.environ, {}, KIROCREW_PORT=""),
        patch("kiro_crew.slack.allowlist.sel") as mock_sel,
    ):
        mock_sel.return_value.log_api_access = MagicMock()
        await _handle_slash_command(
            "!dashboard", slack, sessions, "C123", "ts1", "ts2", "sess1", "U001"
        )

    # Link sent via DM (open_dm called), not in the channel
    slack.open_dm.assert_called_once_with("U001")
    dm_msg = slack.post_message.call_args_list[0][0]
    assert dm_msg[0] == "D_DM"  # sent to DM channel
    assert f"http://{expected_host}:{expected_port}/?token=" in dm_msg[1]


# -- Property 10: SEL logs contain operation='slack.dashboard_token' with user_id and TTL --


# -- api_logout handler tests --


@pytest.mark.asyncio
async def test_api_logout_success_from_loopback() -> None:
    """POST /api/logout succeeds from loopback with valid secret."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import api_logout

    app = web.Application()
    app["local_secret"] = "test-secret-123"
    app.router.add_post("/api/logout", api_logout)

    # Generate a token first so there's something to revoke
    generate_token("user1")

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/logout",
            json={},
            headers={"X-Local-Secret": "test-secret-123"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True


@pytest.mark.asyncio
async def test_api_logout_rejects_non_loopback() -> None:
    """POST /api/logout rejects requests from non-loopback IPs."""
    from unittest.mock import patch

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import api_logout

    app = web.Application()
    app["local_secret"] = "test-secret-123"
    app.router.add_post("/api/logout", api_logout)

    async with TestClient(TestServer(app)) as client:
        # Patch is_loopback to return False (simulating non-loopback request)
        with patch("kiro_crew.dashboard.handlers.is_loopback", return_value=False):
            resp = await client.post(
                "/api/logout",
                json={},
                headers={"X-Local-Secret": "test-secret-123"},
            )
            assert resp.status == 403
            data = await resp.json()
            assert data["error"] == "loopback only"


@pytest.mark.asyncio
async def test_api_logout_rejects_invalid_secret() -> None:
    """POST /api/logout rejects requests with invalid secret."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import api_logout

    app = web.Application()
    app["local_secret"] = "correct-secret"
    app.router.add_post("/api/logout", api_logout)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/logout",
            json={},
            headers={"X-Local-Secret": "wrong-secret"},
        )
        assert resp.status == 403
        data = await resp.json()
        assert data["error"] == "invalid secret"


@pytest.mark.asyncio
async def test_api_logout_rejects_missing_secret() -> None:
    """POST /api/logout rejects requests without secret header."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import api_logout

    app = web.Application()
    app["local_secret"] = "correct-secret"
    app.router.add_post("/api/logout", api_logout)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/logout", json={})
        assert resp.status == 403
        data = await resp.json()
        assert data["error"] == "invalid secret"


@pytest.mark.asyncio
async def test_api_logout_success_revokes_sessions() -> None:
    """POST /api/logout with loopback + the right secret actually revokes.

    The reject paths above are covered; this locks in the success path, which is
    what makes the /api/logout entry in _BYPASS_EXACT meaningful -- the endpoint
    has to be reachable AND still do its job. revoke_all_sessions is patched so
    the test never touches the real nonce store / persisted generation.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import api_logout

    app = web.Application()
    app["local_secret"] = "correct-secret"
    app.router.add_post("/api/logout", api_logout)

    with patch("kiro_crew.dashboard.token_auth.revoke_all_sessions") as mock_revoke:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/logout",
                json={},
                headers={"X-Local-Secret": "correct-secret"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            mock_revoke.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("duration_arg, expected_ttl", [("", 3600), ("2h", 7200), ("30m", 1800)])
async def test_dashboard_sel_log(duration_arg: str, expected_ttl: int) -> None:
    """!dashboard logs SEL with operation='slack.dashboard_token', caller, and ttl."""
    from kiro_crew.slack.handler import _handle_slash_command

    slack = MagicMock()
    slack.post_message = AsyncMock(return_value=None)
    slack.open_dm = AsyncMock(return_value="D_DM")
    slack.post_blocks = AsyncMock(return_value=None)
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)

    mock_cfg = MagicMock()
    mock_cfg.dashboard.url = ""

    cmd_text = f"!dashboard {duration_arg}".strip()

    with (
        patch("kiro_crew.slack.allowlist.KiroCrewConfig.load", return_value=mock_cfg),
        patch("kiro_crew.dashboard.origin.socket.gethostname", return_value="myhostname"),
        patch("kiro_crew.dashboard.origin.socket.gethostbyname", return_value="10.0.0.1"),
        patch("kiro_crew.slack.allowlist.sel") as mock_sel,
    ):
        mock_log = MagicMock()
        mock_sel.return_value.log_api_access = mock_log
        await _handle_slash_command(
            cmd_text, slack, sessions, "C123", "ts1", "ts2", "sess1", "U_TEST"
        )

    mock_log.assert_called_once_with(
        caller="U_TEST",
        operation="slack.dashboard_token",
        outcome="ok",
        resources=f"ttl={expected_ttl}",
    )


# -- Property 17: Port-specific cookie names prevent multi-server collision --


@pytest.mark.asyncio
async def test_different_ports_use_different_cookie_names() -> None:
    """Two servers on different ports must not share cookies (RFC 6265 §8.5)."""
    mw_a = token_auth_middleware(port=5476)
    mw_b = token_auth_middleware(port=6777)
    token_a = generate_token("user_a", ttl_seconds=300)
    token_b = generate_token("user_b", ttl_seconds=300)

    # Server A sets mc_token_5476
    req_a = _make_request(query={"token": token_a}, remote="127.0.0.1")
    resp_a = await mw_a(req_a, _ok_handler)
    assert resp_a.status == 200
    assert "mc_token_5476" in resp_a.cookies
    assert "mc_token_6777" not in resp_a.cookies
    # Verify legacy mc_token cookie is expired on upgrade
    legacy = resp_a.cookies.get("mc_token")
    assert legacy is not None, "Legacy mc_token cookie should be set for expiration"
    assert legacy["max-age"] == "0"

    # Server B sets mc_token_6777
    req_b = _make_request(query={"token": token_b}, remote="127.0.0.1")
    resp_b = await mw_b(req_b, _ok_handler)
    assert resp_b.status == 200
    assert "mc_token_6777" in resp_b.cookies
    assert "mc_token_5476" not in resp_b.cookies


@pytest.mark.asyncio
async def test_wrong_port_cookie_rejected() -> None:
    """Server A must reject a cookie set by server B (different port suffix)."""
    mw_a = token_auth_middleware(port=5476)
    token_b = generate_token("user_b", ttl_seconds=300)
    bind_token_ip(token_b, "127.0.0.1")
    mark_consumed(token_b)

    # Send server B's cookie to server A — wrong cookie name
    req = _make_request(path="/api/status", cookies={"mc_token_6777": token_b}, remote="127.0.0.1")
    resp = await mw_a(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_non_default_port_full_cycle() -> None:
    """Full query-param → cookie-set → cookie-read cycle on non-default port."""
    mw = token_auth_middleware(port=6777)
    token = generate_token("user_6777", ttl_seconds=300)

    # Step 1: query-param auth sets cookie
    req1 = _make_request(query={"token": token}, remote="10.0.0.1")
    resp1 = await mw(req1, _ok_handler)
    assert resp1.status == 200
    cookie = resp1.cookies.get("mc_token_6777")
    assert cookie is not None
    # Token→session exchange: cookie is a distinct valid session token.
    assert cookie.value != token
    _ok, _uid, _ = validate_token(cookie.value, use_session_exp=True)
    assert _ok is True and _uid == "user_6777"

    # Step 2: cookie-based auth on subsequent request uses the EXCHANGED cookie
    req2 = _make_request(cookies={"mc_token_6777": cookie.value}, remote="10.0.0.1")
    resp2 = await mw(req2, _ok_handler)
    assert resp2.status == 200


# -- Per-session access-cookie revocation (CWE-613) --------------------------


def test_revoke_access_cookie_kills_only_that_cookie() -> None:
    """revoke_access_cookie must reject the revoked cookie on the cookie path
    while a DIFFERENT session minted separately keeps working — i.e. it is NOT
    the nuclear revoke_all_sessions().
    """
    victim = generate_token("alice", ttl_seconds=3600)
    other = generate_token("bob", ttl_seconds=3600)

    # Both valid as cookies before logout.
    assert validate_token(victim, use_session_exp=True)[0] is True
    assert validate_token(other, use_session_exp=True)[0] is True

    assert revoke_access_cookie(victim) is True

    ok, _uid, reason = validate_token(victim, use_session_exp=True)
    assert ok is False
    assert reason == "session revoked"

    # The other session is untouched.
    assert validate_token(other, use_session_exp=True)[0] is True


def test_revoke_access_cookie_rejects_invalid_token() -> None:
    """A malformed/garbage token writes nothing to the denylist (deny-by-default)."""
    assert revoke_access_cookie("not.a.real.token") is False
    assert revoke_access_cookie("") is False


def test_revoked_cookie_survives_store_reload(tmp_path) -> None:
    """The denylist is persisted, so a revoked cookie stays dead across a
    gateway restart (new store instance reading the same file)."""
    token = generate_token("alice", ttl_seconds=3600)
    assert revoke_access_cookie(token) is True

    # Simulate restart: fresh store reading the persisted file.
    reloaded = RevokedNonceStore(state_path=tmp_path / "token_revoked_nonces.json")
    import json

    from kiro_crew.dashboard.token_auth import _b64url_decode

    nonce = json.loads(_b64url_decode(token.split(".")[0]))["nonce"]
    assert reloaded.is_revoked(nonce) is True


def test_revoked_store_evicts_expired_entry() -> None:
    """An entry whose session_exp has passed is treated as not-revoked (the
    token is rejected by the expiry check anyway) and dropped lazily."""
    store = RevokedNonceStore(state_path=None)  # in-memory only
    store.revoke("nonce-past", time.time() - 1)
    assert store.is_revoked("nonce-past") is False


def test_revoke_access_cookie_no_op_on_link_path() -> None:
    """The denylist only gates the cookie path (use_session_exp=True). The
    one-time LINK validation has its own nonce-set semantics and never consults
    the denylist, so cookie revocation does not change link-path behaviour."""
    token = generate_token("alice", ttl_seconds=3600)
    # Link path is valid before revocation (nonce still in the active set).
    assert validate_token(token, use_session_exp=False)[0] is True
    assert revoke_access_cookie(token) is True
    # Cookie path now rejected, link path unchanged (still valid).
    assert validate_token(token, use_session_exp=True)[2] == "session revoked"
    assert validate_token(token, use_session_exp=False)[0] is True


# -- App-token least-privilege scope (CWE-269) -------------------------------


def test_api_pattern_matches_boundaries() -> None:
    # Bare prefix: exact + child under a path boundary, but not a longer name.
    assert _api_pattern_matches("/api/chat", "/api/chat")
    assert _api_pattern_matches("/api/chat", "/api/chat/slots")
    assert not _api_pattern_matches("/api/chat", "/api/chatx")
    # Trailing /* — inclusive of the base and its children.
    assert _api_pattern_matches("/api/chat/*", "/api/chat")
    assert _api_pattern_matches("/api/chat/*", "/api/chat/slots/1")
    # Bare * — raw prefix.
    assert _api_pattern_matches("/api/status*", "/api/status-page")
    assert not _api_pattern_matches("", "/anything")


def test_app_owns_path_boundaries() -> None:
    assert _app_owns_path("foo", "/apps/foo/api/x")
    assert _app_owns_path("foo", "/apps/foo/ui/index.js")
    assert _app_owns_path("foo", "/api/apps/foo/config")
    # Path boundary: foo must not match foo-bar.
    assert not _app_owns_path("foo", "/apps/foo-bar/api/x")
    assert not _app_owns_path("foo", "/api/apps/foo-bar/config")
    # Unrelated dashboard endpoints are never owned.
    assert not _app_owns_path("foo", "/api/sessions")
    assert not _app_owns_path("foo", "/api/config/kirocrew")


def test_app_token_path_allowed_empty_name_denies() -> None:
    # Defensive: an empty app_name must never be treated as "allow all".
    assert app_token_path_allowed("", "/api/sessions") is False


@pytest.mark.asyncio
async def test_app_token_denied_on_unscoped_endpoint(monkeypatch) -> None:
    """The pentest scenario: an app token hitting /api/sessions must be 403."""
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ())
    mw = token_auth_middleware()
    token = generate_token("file-explorer", ttl_seconds=300, app="file-explorer")
    req = _make_request(path="/api/sessions", query={"token": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_app_token_allowed_on_own_namespace(monkeypatch) -> None:
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ())
    mw = token_auth_middleware()
    token = generate_token("file-explorer", ttl_seconds=300, app="file-explorer")
    req = _make_request(path="/apps/file-explorer/api/list", query={"token": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_app_token_allowed_on_declared_api(monkeypatch) -> None:
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ("/api/widgets/*",))
    mw = token_auth_middleware()
    token = generate_token("file-explorer", ttl_seconds=300, app="file-explorer")
    req = _make_request(path="/api/widgets/abc", query={"token": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_user_token_unaffected_by_app_scope() -> None:
    """A dashboard-user token (no app claim) still reaches /api/sessions."""
    mw = token_auth_middleware()
    token = generate_token("alice", ttl_seconds=300)
    req = _make_request(path="/api/sessions", query={"token": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_app_token_denied_on_mixed_internal_path(monkeypatch) -> None:
    """Escalation guard: an app token on a mixed_internal path (e.g. /api/chat)
    over loopback must NOT be silently treated as the dashboard user. Before the
    fix this branch never set request['app'] and granted unconditionally."""
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ())
    mw = token_auth_middleware(mixed_internal_paths=frozenset({"/api/chat"}))
    token = generate_token("file-explorer", ttl_seconds=300, app="file-explorer")
    req = _make_request(
        path="/api/chat",
        cookies={"mc_token_5476": token},
        remote="127.0.0.1",
        method="POST",
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_app_token_denied_on_strict_internal_path(monkeypatch) -> None:
    """Impersonation guard: an app token hitting a strict-internal path such as
    /api/send-message over loopback (no secret header) must be scope-denied —
    otherwise a compromised app could send notifications impersonating the
    system (app-sandbox-roadmap threat)."""
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ())
    mw = token_auth_middleware(internal_paths=frozenset({"/api/send-message"}))
    token = generate_token("file-explorer", ttl_seconds=300, app="file-explorer")
    req = _make_request(
        path="/api/send-message",
        cookies={"mc_token_5476": token},
        remote="127.0.0.1",
        method="POST",
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


# -- Token→session exchange (CWE-613, decouple URL token from cookie) ---------


@pytest.mark.asyncio
async def test_url_token_exchanged_for_distinct_session_cookie() -> None:
    """The cookie set on link-click auth must be a fresh session token, NOT the
    raw URL token — so leaking the link (URL/Slack/logs) does not hand over the
    long-lived session credential."""
    mw = token_auth_middleware()
    url_token = generate_token("linkuser", ttl_seconds=MAX_SESSION_TTL_SECS)

    req = _make_request(query={"token": url_token}, remote="10.0.0.2")
    resp = await mw(req, _ok_handler)
    assert resp.status == 200

    cookie = resp.cookies.get("mc_token_5476")
    assert cookie is not None
    # Distinct credential …
    assert cookie.value != url_token
    # … valid on the cookie path for the same identity …
    ok, uid, _ = validate_token(cookie.value, use_session_exp=True)
    assert ok is True and uid == "linkuser"
    # … and the two tokens carry different nonces (independent sessions).
    import json as _json

    from kiro_crew.dashboard.token_auth import _b64url_decode

    url_nonce = _json.loads(_b64url_decode(url_token.split(".")[0]))["nonce"]
    cookie_nonce = _json.loads(_b64url_decode(cookie.value.split(".")[0]))["nonce"]
    assert url_nonce != cookie_nonce


@pytest.mark.asyncio
async def test_exchanged_cookie_preserves_app_claim() -> None:
    """If an app token ever arrives via the URL flow, the exchanged cookie must
    keep the ``app`` claim so app-scope enforcement continues to apply."""
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch_allow = lambda name: ("/api/anything/*",)  # noqa: E731
    _ta._app_perms_cache.clear()
    orig = _ta._app_api_allowlist
    _ta._app_api_allowlist = monkeypatch_allow  # type: ignore[assignment]
    try:
        mw = token_auth_middleware()
        url_token = generate_token("some-app", ttl_seconds=MAX_SESSION_TTL_SECS, app="some-app")
        req = _make_request(
            path="/apps/some-app/api/x", query={"token": url_token}, remote="10.0.0.3"
        )
        resp = await mw(req, _ok_handler)
        assert resp.status == 200
        cookie = resp.cookies.get("mc_token_5476")
        assert cookie is not None
        _valid, _uid, _reason, app_name = validate_token_with_app(
            cookie.value, use_session_exp=True
        )
        assert _valid is True
        assert app_name == "some-app"
    finally:
        _ta._app_api_allowlist = orig  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_leaked_link_token_rejected_as_cookie_after_exchange() -> None:
    """After the link→session exchange, a captured copy of the URL token must
    NOT be replayable as a session cookie (CWE-613). The exchange revokes the
    link token's nonce on the cookie path."""
    mw = token_auth_middleware()
    url_token = generate_token("linkuser2", ttl_seconds=MAX_SESSION_TTL_SECS)

    # First click over the query-param path performs the exchange.
    req1 = _make_request(query={"token": url_token}, remote="10.0.0.9")
    resp1 = await mw(req1, _ok_handler)
    assert resp1.status == 200

    # Attacker replays the SAME url token directly as a cookie → rejected.
    req2 = _make_request(cookies={"mc_token_5476": url_token}, remote="10.0.0.9")
    resp2 = await mw(req2, _ok_handler)
    assert resp2.status == 403

    # …but validate_token confirms the denial reason is the cookie denylist.
    ok, _uid, reason = validate_token(url_token, use_session_exp=True)
    assert ok is False
    assert reason == "session revoked"


@pytest.mark.asyncio
async def test_link_token_still_reusable_via_query_param_after_exchange() -> None:
    """The denylist is cookie-path only: re-navigating the same /?token= URL
    (e.g. remote-instance iframes, self-nudge polling) must still work within
    the 5-minute link window and re-exchange for a fresh cookie."""
    mw = token_auth_middleware()
    url_token = generate_token("linkuser3", ttl_seconds=MAX_SESSION_TTL_SECS)

    req1 = _make_request(query={"token": url_token}, remote="10.0.1.1")
    resp1 = await mw(req1, _ok_handler)
    assert resp1.status == 200
    first_cookie = resp1.cookies.get("mc_token_5476").value

    # Second navigation with the SAME url token via query param — still 200,
    # re-exchanged to another fresh session cookie.
    req2 = _make_request(query={"token": url_token}, remote="10.0.1.1")
    resp2 = await mw(req2, _ok_handler)
    assert resp2.status == 200
    second_cookie = resp2.cookies.get("mc_token_5476").value
    assert first_cookie != url_token and second_cookie != url_token


@pytest.mark.asyncio
async def test_repeated_query_param_exchange_does_not_evict_link_nonces() -> None:
    """Tool-usage guard: repeated /?token= exchanges (self-nudge polling,
    instance-iframe re-navigation) must NOT churn the bounded link-nonce set
    and evict a pending one-time link (e.g. a Slack dashboard link). The
    exchanged session token is minted with register_nonce=False for exactly
    this reason."""
    mw = token_auth_middleware()
    # A pending Slack-style link token, registered and awaiting its click.
    pending_link = generate_token("slackuser", ttl_seconds=MAX_SESSION_TTL_SECS)
    # A local-app token repeatedly presented as ?token= by a background tool.
    local_token = generate_token("local-app", ttl_seconds=MAX_SESSION_TTL_SECS)

    for _ in range(120):  # well beyond MAX_CONCURRENT_NONCES (50)
        req = _make_request(
            path="/api/autonudge",
            query={"token": local_token},
            remote="127.0.0.1",
            method="POST",
        )
        resp = await mw(req, _ok_handler)
        assert resp.status == 200

    # The pending link must still validate on its (query-param) link path —
    # i.e. it was NOT evicted by the 120 exchanges.
    ok, uid, reason = validate_token(pending_link, use_session_exp=False)
    assert ok is True, f"pending link nonce was evicted: {reason!r}"
    assert uid == "slackuser"


@pytest.mark.asyncio
async def test_served_shell_is_auth_independent() -> None:
    """Load-bearing invariant for the cold-start bypass: the SPA shell that
    the middleware serves UNAUTHENTICATED must be byte-identical to the shell
    an authenticated client gets, and must carry no injected server/user state.

    The bypass is only safe while index.html is a static, secret-free bundle
    (see the SECURITY CONTRACT on handlers.core.index). If a future change
    inlines bootstrap state (a username, token, feature flags, session blob)
    into the shell, an unauthenticated GET / would leak it across the auth
    boundary. This test fails if the served body becomes request-dependent or
    starts carrying credential/state markers.
    """
    import kiro_crew.dashboard.handlers.core as core

    # Unauthenticated cold-start request vs an "authenticated-looking" one
    # (cookies + remote set). index() must ignore request state entirely.
    anon = _make_request(path="/", remote="10.0.0.1")
    authed = _make_request(
        path="/",
        cookies={"mc_token_5476": "someminted.token.value"},
        remote="10.0.0.1",
    )
    resp_anon = await core.index(anon)
    resp_authed = await core.index(authed)

    # 1. Request-independent: the unauth shell == the authed shell.
    assert resp_anon.text == resp_authed.text

    # 2. No credential/server-state markers leaked into the served shell.
    body = resp_anon.text
    for marker in (
        "mc_token",
        "mc_refresh",
        "refresh_chains",
        "security_events",
        "BEGIN PRIVATE",
        "someminted.token.value",
    ):
        assert marker not in body, f"shell leaked state marker: {marker!r}"


@pytest.mark.asyncio
async def test_index_serves_guidance_when_bundle_missing(tmp_path, monkeypatch) -> None:
    """Fallback branch: when the React build cannot be read, index() serves the
    static guidance page (recognizable heading + cause + restart hint) instead
    of a bare error. It must remain request-independent and secret-free -- the
    same cold-start security contract as the normal shell (this handler is
    served unauthenticated). The legacy dashboard.html fallback was removed
    (security-review); dist/index.html is the sole shell source.
    """
    import kiro_crew.dashboard.handlers.core as core

    # Point the React build index at a non-existent location so index() falls
    # into the FileNotFoundError -> guidance-page branch.
    monkeypatch.setattr(core, "_DIST_INDEX", tmp_path / "no-dist" / "index.html")

    anon = _make_request(path="/", remote="10.0.0.1")
    authed = _make_request(
        path="/",
        cookies={"mc_token_7777": "someminted.token.value"},
        remote="10.0.0.1",
    )
    resp_anon = await core.index(anon)
    resp_authed = await core.index(authed)

    assert resp_anon.status == 200
    assert resp_anon.content_type == "text/html"
    body = resp_anon.text
    # Recognizable heading preserved + actionable guidance added.
    assert "<h1>Dashboard HTML not found</h1>" in body
    assert "restarting Kiro Crew" in body
    assert "kirocrew service restart" in body
    # Request-independent and secret-free (same contract as the served shell).
    assert resp_anon.text == resp_authed.text
    assert "someminted.token.value" not in body


# -- extract_numeric_claim (round-2 bugfix: api_auth_me session_exp=0) ---------


def test_extract_numeric_claim_returns_float_session_exp() -> None:
    """session_exp is a float claim; the string-only extract_claims_from_token
    dropped it (api_auth_me always saw 0.0, disabling frontend refresh). The
    numeric extractor must return the real float."""
    from kiro_crew.dashboard.token_auth import extract_numeric_claim

    tok = generate_token("alice", ttl_seconds=3600)
    exp = extract_numeric_claim(tok, "session_exp")
    assert isinstance(exp, float)
    assert exp > time.time()  # a real future epoch, not 0.0

    # The string extractor still drops it (documents why the numeric one exists).
    from kiro_crew.dashboard.token_auth import extract_claims_from_token

    assert extract_claims_from_token(tok, ("session_exp",)) == {}


def test_extract_numeric_claim_rejects_invalid_missing_and_bool() -> None:
    from kiro_crew.dashboard.token_auth import extract_numeric_claim

    tok = generate_token("bob", ttl_seconds=3600)
    assert extract_numeric_claim("garbage.sig", "session_exp") is None  # invalid token
    assert extract_numeric_claim(tok, "does_not_exist") is None  # missing claim


# -- register_nonce=False for cookie-only session tokens (nonce-churn bugfix) --


def test_cookie_session_mint_does_not_evict_link_nonce() -> None:
    """A pending one-time LINK nonce must survive many cookie-only session-token
    mints. api_auth_refresh mints generate_token(..., register_nonce=False) per
    rotation; if it registered nonces it would churn/evict the bounded 50-slot
    set and drop pending Slack-challenge links. Mint > the 50-slot bound and
    assert the link token still validates on the link path."""
    from kiro_crew.dashboard.token_auth import MAX_CONCURRENT_NONCES, validate_token

    # A real one-time link token (registers a nonce, validated on the link path).
    link = generate_token("carol", ttl_seconds=3600)
    valid_before, _, _ = validate_token(link)
    assert valid_before

    # Mint well over the bound the way api_auth_refresh does — register_nonce=False.
    for _ in range(MAX_CONCURRENT_NONCES + 10):
        generate_token("carol", ttl_seconds=3600, register_nonce=False)

    # The link nonce was NOT churned out: the link token still validates.
    valid_after, _, _ = validate_token(link)
    assert valid_after, "cookie-only mints must not evict the pending link nonce"

    # Contrast (documents the bug): the OLD default register_nonce=True churns
    # the bounded set and DOES evict the link nonce.
    link2 = generate_token("dave", ttl_seconds=3600)
    assert validate_token(link2)[0]
    for _ in range(MAX_CONCURRENT_NONCES + 10):
        generate_token("dave", ttl_seconds=3600)  # register_nonce=True (default)
    assert not validate_token(link2)[0]


def test_api_auth_refresh_mints_session_token_without_nonce_registration() -> None:
    """Handler regression guard: api_auth_refresh must mint its cookie-only
    session token with register_nonce=False (else it churns pending link
    nonces). Asserts against the handler source so a revert is caught."""
    import inspect

    from kiro_crew.dashboard.handlers.auth_refresh import api_auth_refresh

    src = inspect.getsource(api_auth_refresh)
    assert "register_nonce=False" in src


# --- item #2: /api/deploy reachable via X-Internal-Secret (MCP tool path) ---


@pytest.mark.asyncio
async def test_deploy_path_accessible_via_internal_secret() -> None:
    """/api/deploy/deploy must be reachable with X-Internal-Secret (mixed_internal_paths).

    Regression test: the deploy_artifact MCP tool posts to /api/deploy/deploy with
    X-Internal-Secret. Without /api/deploy in mixed_internal_paths, this falls through
    to cookie auth and the tool NEVER works.
    """
    secret = "deploy-secret-xyz"
    mw = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/deploy"}), internal_secret=secret
    )
    req = _make_request(path="/api/deploy/deploy", headers={"X-Internal-Secret": secret})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_deploy_path_prefix_matching() -> None:
    """All /api/deploy/* sub-routes are covered by the prefix entry."""
    secret = "deploy-secret-xyz"
    mw = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/deploy"}), internal_secret=secret
    )
    for path in ("/api/deploy/deploy", "/api/deploy/list", "/api/deploy/profiles"):
        req = _make_request(path=path, headers={"X-Internal-Secret": secret})
        resp = await mw(req, _ok_handler)
        assert resp.status == 200, f"expected 200 for {path}, got {resp.status}"


def test_token_embed_parent_port_roundtrip() -> None:
    """A token minted with the embed_parent_port claim reads back as that int —
    the signed carrier for the multi-instance CSP frame-ancestor parent origin."""
    tok = generate_token("local-app", extra={"embed_parent_port": "5476"})
    assert token_embed_parent_port(tok) == 5476


def test_token_embed_parent_port_absent_forged_or_oob() -> None:
    """No claim, a forged signature, an empty token, and an out-of-range port all
    yield None so a random local page can never inject a frame-ancestor."""
    assert token_embed_parent_port(generate_token("local-app")) is None
    assert token_embed_parent_port("") is None
    # Forged: appending to a valid token breaks the HMAC signature.
    forged = generate_token("local-app", extra={"embed_parent_port": "5476"}) + "x"
    assert token_embed_parent_port(forged) is None
    # Out-of-range port claim is rejected.
    oob = generate_token("local-app", extra={"embed_parent_port": "70000"})
    assert token_embed_parent_port(oob) is None


# -- Middleware warm-up: signing secret primed at construction ----------------


def test_warm_auth_singletons_primes_both_off_loop(monkeypatch) -> None:
    """Regression: warm_auth_singletons() must prime BOTH _get_secret() and
    _get_revoked_store() — off the event loop (via asyncio.to_thread) — before
    the middleware serves requests.

    Both lazily do blocking file I/O on first use (read/create
    token_signing.key + read the nonce denylist; on Windows an icacls
    subprocess to lock the DACL). They are NO LONGER warmed synchronously in
    the token_auth_middleware() factory, because that factory runs on the loop
    via the async start_dashboard()/start_api_server(). The async startup paths
    await this helper instead, so the first auth op hits warm singletons with
    no blocking I/O on the loop.
    """
    import asyncio

    import kiro_crew.dashboard.token_auth as _ta

    calls = {"secret": 0, "store": 0}
    real_secret = _ta._get_secret
    real_store = _ta._get_revoked_store

    def spy_secret() -> bytes:
        calls["secret"] += 1
        return real_secret()

    def spy_store():
        calls["store"] += 1
        return real_store()

    monkeypatch.setattr(_ta, "_get_secret", spy_secret)
    monkeypatch.setattr(_ta, "_get_revoked_store", spy_store)

    asyncio.run(_ta.warm_auth_singletons())

    assert calls["secret"] >= 1, "warm_auth_singletons must warm _get_secret()"
    assert calls["store"] >= 1, "warm_auth_singletons must warm _get_revoked_store()"


def test_middleware_factory_does_no_blocking_warmup() -> None:
    """Source guard: the token_auth_middleware() factory body must NOT warm the
    auth singletons SYNCHRONOUSLY (a bare `_get_secret()` / `_get_revoked_store()`
    statement) — that would run blocking key-file I/O on the event loop (the
    factory is called from async start paths). Warming lives in the async
    warm_auth_singletons() helper instead, which offloads via asyncio.to_thread.

    NB: the factory's nested request-path middleware still *references*
    `_get_revoked_store()` (e.g. `_get_revoked_store().is_revoked(...)`, and a
    `to_thread`-wrapped revoke) — those are legitimate, not a synchronous
    warm-up — so we check for the bare standalone warm-up STATEMENT, not mere
    substring presence."""
    import inspect

    from kiro_crew.dashboard.token_auth import (
        token_auth_middleware,
        warm_auth_singletons,
    )

    factory_lines = {ln.strip() for ln in inspect.getsource(token_auth_middleware).splitlines()}
    # The bare synchronous warm-up statements must be gone from the factory.
    assert "_get_secret()" not in factory_lines
    assert "_get_revoked_store()" not in factory_lines

    # The async helper must offload BOTH warm-ups to a worker thread.
    warm_src = inspect.getsource(warm_auth_singletons)
    assert "asyncio.to_thread(_get_secret)" in warm_src
    assert "asyncio.to_thread(_get_revoked_store)" in warm_src


def test_start_paths_warm_auth_singletons_off_loop() -> None:
    """Source guard: both async server startup paths must await
    warm_auth_singletons() so the blocking key-file I/O is primed off the loop
    before the middleware chain is built."""
    import inspect

    from kiro_crew.dashboard import server as _srv

    for fn in (_srv.start_dashboard, _srv.start_api_server):
        src = inspect.getsource(fn)
        assert (
            "await warm_auth_singletons()" in src
        ), f"{fn.__name__} must await warm_auth_singletons() before serving"


def test_ambiguous_app_and_window_names_cannot_collide(tmp_path) -> None:
    """The pair that used to collide now yields two distinct routes.

    The old scheme served these flat at ``/<app>-<window>.html``, which is
    ambiguous the moment either name contains a hyphen: app ``foo`` + window
    ``bar-baz`` and app ``foo-bar`` + window ``baz`` both spell
    ``/foo-bar-baz.html``, so one of them had to be refused and the other's
    window was simply unavailable. Keeping the boundary the filesystem already
    has removes the class rather than handling it — this pins that, using the
    exact pair that was the counter-example.
    """
    from kiro_crew.dashboard.server import discover_app_window_entries

    root = tmp_path / "src" / "apps"
    (root / "foo").mkdir(parents=True)
    (root / "foo-bar").mkdir(parents=True)
    (root / "foo" / "bar-baz.html").write_text("<html>first</html>")
    (root / "foo-bar" / "baz.html").write_text("<html>second</html>")
    (root / "foo" / "solo.html").write_text("<html>solo</html>")

    entries = discover_app_window_entries(root)
    routes = dict(entries)

    # Both survive, each addressable, neither shadowing the other.
    assert len(routes) == 3, f"every window must register: {sorted(routes)}"
    assert routes["/app-windows/foo/bar-baz.html"] == root / "foo" / "bar-baz.html"
    assert routes["/app-windows/foo-bar/baz.html"] == root / "foo-bar" / "baz.html"
    assert "/app-windows/foo/solo.html" in routes
    # And the URL a caller builds is derivable from the path, not guessed from it.
    for route, path in entries:
        assert route == f"/app-windows/{path.parent.name}/{path.name}"


def test_app_window_entries_register_route_and_exclusion(tmp_path) -> None:
    """App window entries: discovery must couple route and shell exclusion.

    server.py enumerates dist/src/apps/<app>/<name>.html at startup and, in one
    loop, registers GET /<app>-<name>.html AND excludes that exact path from
    the unauthenticated SPA-shell fallback. This test drives the real
    registration function against a fixture dist tree and asserts both halves,
    plus path-shape safety (the route comes from the enumerated file, never
    from the request).
    """
    from aiohttp import web

    import kiro_crew.dashboard.token_auth as ta

    # Fixture dist: one app with two windows, one unrelated non-html file.
    win = tmp_path / "src" / "apps" / "someapp"
    win.mkdir(parents=True)
    (win / "pet.html").write_text("<html></html>")
    (win / "panel.html").write_text("<html></html>")
    (win / "notes.txt").write_text("not a window")

    # Drive the REAL discovery helper rather than restating the route
    # construction: the previous form duplicated it, and duplicated it in the
    # OLD flat shape, so it kept passing after the scheme changed and asserted
    # nothing about what the gateway actually registers.
    from kiro_crew.dashboard.server import (
        _window_entry_handler,
        discover_app_window_entries,
    )

    app = web.Application()
    window_paths: list[str] = []
    for route_path, entry in discover_app_window_entries(tmp_path / "src" / "apps"):
        # Same handler factory the gateway uses — see server._window_entry_handler
        # for why the path is a closure cell and not a handler parameter.
        app.router.add_get(route_path, _window_entry_handler(entry))
        window_paths.append(route_path)

    prior = ta._APP_WINDOW_EXCLUDED_PATHS
    try:
        ta.register_app_window_paths(window_paths)

        registered = {r.resource.canonical for r in app.router.routes() if r.method == "GET"}
        assert {
            "/app-windows/someapp/panel.html",
            "/app-windows/someapp/pet.html",
        } <= registered
        assert "/app-windows/someapp/notes.html" not in registered  # only .html

        # Both routes are excluded from the shell fallback; a sibling path that
        # was NOT discovered is not (exact-path matching, no prefix bleed).
        assert "/app-windows/someapp/pet.html" in ta._APP_WINDOW_EXCLUDED_PATHS
        assert "/app-windows/someapp/panel.html" in ta._APP_WINDOW_EXCLUDED_PATHS
        assert "/app-windows/someapp/other.html" not in ta._APP_WINDOW_EXCLUDED_PATHS
    finally:
        ta._APP_WINDOW_EXCLUDED_PATHS = prior


def test_app_token_path_allowed_implicit_ws():
    """``/api/ws`` is the only implicitly allowed path.

    It is connection infrastructure rather than a capability, and the implicit
    grant is sound only because the WS layer filters events per app
    (``ws_event_scope.py``). ``/api/status`` has NO such response-level filter
    and discloses ``owner_id_hash``, host specs, cron/usage stats and the live
    safety-override state, so it must be declared like any other capability.
    Functional paths must still be declared, so the negative case is asserted
    alongside.
    """
    from kiro_crew.dashboard.token_auth import app_token_path_allowed

    assert app_token_path_allowed("some-app", "/api/ws") is True
    assert app_token_path_allowed("some-app", "/api/status") is False
    # Undeclared functional paths stay denied.
    assert app_token_path_allowed("some-app", "/api/chat") is False
    assert app_token_path_allowed("some-app", "/api/spawn") is False
    # An empty app name must never be granted, even for implicit paths.
    assert app_token_path_allowed("", "/api/ws") is False
