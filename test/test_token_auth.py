"""Property tests for dashboard token authentication.

This file keeps the token primitives (generation, URL safety, duration parsing, IP
binding, consumption and the link window), the middleware's query-param and cookie paths
with the stale-token fallback, cookie names keyed by the Host port, the public bypass
paths, the ``/apps/`` auth boundary and how ``/apps/`` paths are classified, the
internal and mixed internal paths, nonce invalidation, and the signing key's first load:
it survives a reload, racing first boots converge on one key, a contended read or create
is retried, and the key's name only ever appears complete. The siblings hold the rest:
``test_token_auth_signing_secret.py`` publishes and stages the key through filesystem
failures and warms it before serving, ``test_token_auth_session_lifecycle.py`` follows a
session from link to logout, ``test_token_auth_spa_shell.py`` covers the deny page and
the public SPA shell, ``test_token_auth_credential_scope.py`` holds app-token scope and
credential checks, and ``test_token_auth_tailnet_pin.py`` holds identity-pinned tailnet
sessions. The shared fixture and request helpers live in ``token_auth_helpers.py``.
"""

from __future__ import annotations

import os
import string
import threading
from unittest.mock import patch

import pytest
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _ok_handler,
    clear_nonces,
)

from kiro_crew.dashboard.token_auth import (
    MAX_CONCURRENT_NONCES,
    MAX_SESSION_TTL_SECS,
    bind_token_ip,
    check_token_ip,
    claims_an_app_unverified,
    generate_token,
    is_consumed,
    mark_consumed,
    parse_duration,
    revoke_all_sessions,
    token_auth_middleware,
    token_embed_parent_port,
    try_consume,
    validate_token,
)

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
    """Token link window (5 min) expires — the URL stops being valid."""
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


def test_link_window_never_outlives_a_short_session() -> None:
    """The link-click window clamps to the session TTL, never exceeds it.

    The query-param path validates against ``exp`` alone (the nonce set is
    membership-only), so an uncapped 5-minute window would let the raw link
    keep authenticating after ``session_exp`` passed. A token whose session
    lifetime was capped by its caller's own bounds (the mobile-link and
    tailnet-QR mints lend the caller's remaining lifetime) would then outlive
    the session that authorized it by up to the full window — the residual
    half of the laundering those mints exist to prevent.
    """
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        token = generate_token("user6c", ttl_seconds=60)
    # Past the short session but still inside the nominal 5-minute window:
    # the LINK path must refuse too, not just the cookie path.
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0 + 61
        link_valid, _, link_reason = validate_token(token)
        cookie_valid, _, _ = validate_token(token, use_session_exp=True)
    assert link_valid is False
    assert "expired" in link_reason
    assert cookie_valid is False
    # A full-length session keeps the whole window: the clamp only ever
    # tightens, so ordinary links are unaffected.
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        long_token = generate_token("user6d", ttl_seconds=3600)
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0 + 299
        valid, uid, _ = validate_token(long_token)
    assert valid is True
    assert uid == "user6d"


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
    """The connect link token carries an embed_parent_port
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
    # revoked — the first-hit blank-pane fix.
    req.__setitem__.assert_any_call("embed_parent_port", "5476")


@pytest.mark.asyncio
async def test_no_refresh_claim_survives_session_exchange() -> None:
    """The ``no_refresh`` bound must survive the link→cookie exchange like
    ``boot`` does. The exchange already honors it by never minting the refresh
    chain, but a downstream consumer that reads the SESSION cookie to learn the
    caller's bounds (the mobile-link mint) would otherwise see an unbounded
    session and re-mint an unbounded, refresh-chained credential — laundering
    the exact ceiling the claim encodes."""
    import json as _json

    from kiro_crew.dashboard.token_auth import _b64url_decode

    mw = token_auth_middleware()
    token = generate_token("qruser", ttl_seconds=300, extra={"no_refresh": "1"})
    req = _make_request(query={"token": token}, remote="10.0.0.1")

    resp = await mw(req, _ok_handler)
    assert resp.status == 200

    cookie_header = resp.cookies.get("mc_token_5476")
    assert cookie_header is not None
    assert cookie_header.value != token
    claims = _json.loads(_b64url_decode(cookie_header.value.split(".", 1)[0]))
    assert claims["no_refresh"] == "1"


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


# -- Stale ?token= must not veto a valid session cookie (query→cookie fallback) --


@pytest.mark.asyncio
async def test_expired_query_token_falls_back_to_valid_cookie() -> None:
    """Re-opening a bookmarked link replays a long-expired ``?token=`` alongside
    the still-valid session cookie that link was exchanged for. The dead query
    token must be ignored, not act as a one-vote veto over the live cookie."""
    mw = token_auth_middleware()
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        stale = generate_token("staleuser", ttl_seconds=300)
    cookie = generate_token("staleuser", ttl_seconds=3600)
    bind_token_ip(cookie, "127.0.0.1")
    mark_consumed(cookie)

    req = _make_request(query={"token": stale}, cookies={"mc_token_5476": cookie})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    # Fallback lands on the COOKIE path: no token→session exchange, no re-set.
    assert "mc_token_5476" not in resp.cookies


@pytest.mark.asyncio
async def test_expired_query_token_without_cookie_still_denied() -> None:
    """The fallback adds no capability: a dead query token alone stays denied
    (the SPA-shell carve-out aside — this data path is not a shell request)."""
    mw = token_auth_middleware()
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        stale = generate_token("staleuser2", ttl_seconds=300)
    req = _make_request(path="/api/status", query={"token": stale})
    resp = await mw(req, _ok_handler)
    assert resp.status in (401, 403)


@pytest.mark.asyncio
async def test_expired_query_token_with_expired_cookie_denied() -> None:
    """When BOTH credentials are dead, the query token's failure reason stands
    (the credential the caller actually presented) and the request is denied."""
    mw = token_auth_middleware()
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        stale = generate_token("staleuser3", ttl_seconds=300)
        dead_cookie = generate_token("staleuser3", ttl_seconds=300)
    req = _make_request(
        path="/api/status", query={"token": stale}, cookies={"mc_token_5476": dead_cookie}
    )
    resp = await mw(req, _ok_handler)
    assert resp.status in (401, 403)


@pytest.mark.asyncio
async def test_valid_query_token_still_wins_over_cookie() -> None:
    """The fallback fires only on an INVALID query token. A valid one keeps
    today's precedence — including its token→session exchange (fresh cookie)."""
    mw = token_auth_middleware()
    fresh = generate_token("precedence", ttl_seconds=300)
    cookie = generate_token("someoneelse", ttl_seconds=3600)
    bind_token_ip(cookie, "127.0.0.1")
    mark_consumed(cookie)
    req = _make_request(query={"token": fresh}, cookies={"mc_token_5476": cookie})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert resp.cookies.get("mc_token_5476") is not None


# -- Which credential authenticated, published for the in-banner re-auth --------
#
# Both cases below carry the SAME user on the query token and on the cookie, and
# both answer 200. That is deliberate: the identity is identical either way, so
# comparing ``user_id`` across the exchange cannot tell them apart, and neither
# can the status. Only the middleware's own record of which credential it
# validated separates them.


@pytest.mark.asyncio
async def test_valid_query_token_for_the_same_user_reports_the_token_authenticated() -> None:
    """A valid ``?token=`` authenticates even when the cookie beside it is also
    valid and names the same user, so the published bit is True and a fresh
    session cookie is minted -- the two facts are one decision."""
    mw = token_auth_middleware()
    fresh = generate_token("sameuser", ttl_seconds=300)
    cookie = generate_token("sameuser", ttl_seconds=3600)
    bind_token_ip(cookie, "127.0.0.1")
    mark_consumed(cookie)

    req = _make_request(query={"token": fresh}, cookies={"mc_token_5476": cookie})
    resp = await mw(req, _ok_handler)

    assert resp.status == 200
    req.__setitem__.assert_any_call("auth_from_query_token", True)
    assert resp.cookies.get("mc_token_5476") is not None


@pytest.mark.asyncio
async def test_invalid_query_token_with_valid_cookie_reports_the_cookie_authenticated() -> None:
    """The invalid query token is replaced by the cookie, so the request is
    authenticated and answers 200 while the presented token was NOT accepted.
    The published bit is False, which is what stops a caller exchanging a pasted
    token from reading this 200 as its own token working."""
    mw = token_auth_middleware()
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        stale = generate_token("sameuser2", ttl_seconds=300)
    cookie = generate_token("sameuser2", ttl_seconds=3600)
    bind_token_ip(cookie, "127.0.0.1")
    mark_consumed(cookie)

    req = _make_request(query={"token": stale}, cookies={"mc_token_5476": cookie})
    resp = await mw(req, _ok_handler)

    assert resp.status == 200
    req.__setitem__.assert_any_call("auth_from_query_token", False)
    # No exchange happened, so no fresh cookie -- the same condition, seen from
    # the side a browser cannot read.
    assert "mc_token_5476" not in resp.cookies


@pytest.mark.asyncio
async def test_internal_path_expired_query_token_falls_back_to_cookie() -> None:
    """The internal-path helper (_extract_and_validate_token) applies the same
    rule: a stale ``?token=`` on a polled internal path must not 401 a request
    whose session cookie is still valid."""
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        stale = generate_token("intuser", ttl_seconds=300)
    cookie = generate_token("intuser", ttl_seconds=3600)
    bind_token_ip(cookie, "127.0.0.1")
    mark_consumed(cookie)
    mw = token_auth_middleware(internal_paths=frozenset({"/api/spawn"}), internal_secret="s")
    req = _make_request(
        path="/api/spawn", query={"token": stale}, cookies={"mc_token_5476": cookie}
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_expired_app_token_does_not_fall_back_to_user_cookie() -> None:
    """An expired APP token must NOT adopt the dashboard user's cookie.

    An installed app's UI is served from this same origin, so the browser sends
    the user's session cookie alongside the app's own ``?token=``. If the
    fallback fired here, ``app_name`` would come back empty, the app-scope gate
    would become a no-op, and an app whose token merely expired would silently
    gain the user's full API reach. It must stay denied so the app re-exchanges
    its secret instead.
    """
    mw = token_auth_middleware()
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        stale_app = generate_token("appsub", ttl_seconds=300, app="someapp")
    cookie = generate_token("realuser", ttl_seconds=3600)
    bind_token_ip(cookie, "127.0.0.1")
    mark_consumed(cookie)

    req = _make_request(
        path="/api/status", query={"token": stale_app}, cookies={"mc_token_5476": cookie}
    )
    resp = await mw(req, _ok_handler)
    assert resp.status in (401, 403)


@pytest.mark.asyncio
async def test_internal_path_expired_app_token_does_not_fall_back_to_cookie() -> None:
    """The internal-path helper applies the same app-token exception."""
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        stale_app = generate_token("appsub2", ttl_seconds=300, app="someapp")
    cookie = generate_token("realuser2", ttl_seconds=3600)
    bind_token_ip(cookie, "127.0.0.1")
    mark_consumed(cookie)
    mw = token_auth_middleware(internal_paths=frozenset({"/api/spawn"}), internal_secret="s")
    req = _make_request(
        path="/api/spawn", query={"token": stale_app}, cookies={"mc_token_5476": cookie}
    )
    resp = await mw(req, _ok_handler)
    assert resp.status in (401, 403)


def test_claims_an_app_unverified_only_ever_refuses() -> None:
    """The unverified reader answers True only for a payload that names an app.

    Garbage and app-less payloads answer False, so the claim can never widen the
    fallback — it can only withhold it.
    """
    assert claims_an_app_unverified(generate_token("u", ttl_seconds=60, app="a")) is True
    assert claims_an_app_unverified(generate_token("u", ttl_seconds=60)) is False
    assert claims_an_app_unverified("not-a-token") is False
    assert claims_an_app_unverified("") is False


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
        "/favicon.ico",
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
        f"Add {path!r} to _BYPASS_EXACT in token_auth_paths.py."
    )


def test_cli_local_secret_endpoints_are_in_bypass_exact() -> None:
    """The set membership itself, independent of middleware behaviour."""
    import kiro_crew.dashboard.token_auth as ta

    missing = [p for p in _CLI_LOCAL_SECRET_PATHS if p not in ta._BYPASS_EXACT]
    assert not missing, f"CLI local-secret endpoints missing from _BYPASS_EXACT: {missing}"


# -- Property 7-bis: the update-revalidate poke reaches its handler --
#
# `kirocrew update` (git checkout) POSTs to /api/update/revalidate over loopback
# with the local secret and NO dashboard token. The handler re-checks loopback +
# secret itself, but only if the request gets past this middleware first. It is
# scoped to POST — the only method routed and the only one the handler's self-
# auth covers — so a non-POST on the same path stays on the ordinary token gate.


@pytest.mark.asyncio
async def test_update_revalidate_post_bypasses_auth() -> None:
    mw = token_auth_middleware()
    req = _make_request(
        path="/api/update/revalidate",
        method="POST",
        headers={"X-Local-Secret": "irrelevant-here"},
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200, (
        "POST /api/update/revalidate was denied by the auth middleware; the CLI "
        "sends no dashboard token, so the handler's own loopback + local-secret "
        "check never runs and the post-update badge poke can never succeed. Add "
        "the path to _BYPASS_EXACT_METHODS (POST) in token_auth_paths.py."
    )


@pytest.mark.asyncio
async def test_update_revalidate_get_still_requires_auth() -> None:
    """A non-POST method on the path is NOT in scope, so the token gate holds."""
    mw = token_auth_middleware()
    req = _make_request(path="/api/update/revalidate", method="GET")
    resp = await mw(req, _ok_handler)
    assert resp.status != 200, "GET on the revalidate path bypassed the token gate"


# -- Property 8a-bis: every browser-view relay route form bypasses the gate --
#
# The relay authenticates with the capability token in the PATH (its iframe is
# an opaque-origin sandbox that carries no cookies), and answers a UNIFORM 404
# for tokenless and wrong-token requests alike. Both registered route forms
# must therefore reach the handler: the tokened form via the /browser-view/
# prefix entry, and the bare form via its own _BYPASS_EXACT entry — without
# the latter, the middleware 403s the bare path and hands an unauthenticated
# prober a response that stands out from the uniform 404s.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/browser-view", "/browser-view/", "/browser-view/tok/x.html"])
async def test_browser_view_relay_routes_bypass_auth(path: str) -> None:
    mw = token_auth_middleware()
    req = _make_request(path=path)  # no token, no cookie
    resp = await mw(req, _ok_handler)
    assert resp.status == 200, (
        f"{path} was denied by the auth middleware; the relay handler must "
        f"answer every route form itself (uniform 404 without a valid token)."
    )


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

    Without the shell fallback, GET /apps/code-review-sage on refresh returns 404 because
    SPA_FALLBACK_EXCLUDED_PREFIXES included '/apps/' wholesale, causing the
    spa_fallback middleware to re-raise HTTPNotFound instead of serving shell.
    """
    import kiro_crew.dashboard.token_auth as ta

    req = _make_request(path=path, method=method)
    assert ta._is_spa_shell_request(
        req
    ), f"{method} {path} should be a SPA shell request (browser refresh must work)"


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

    If _APPS_SPA_EXCLUDED_RE matched any /apps/{seg}/ path, it would
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
        f"_APPS_SPA_EXCLUDED_RE in token_auth_paths.py to cover their sub-namespace."
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

    Without the exclusion, the colorful builtin icons / hero images would not
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


# -- Property 9: Loopback does not bypass auth (port-forward fix) --


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
async def test_internal_path_non_loopback_cookie_marks_the_cookie_credential() -> None:
    """A session cookie on the mixed non-loopback branch records a cookie credential.

    The off-loopback mixed branch (DCV/SSH-forwarded browsers) must set
    ``auth_from_query_token`` the same way the loopback branch does, so the proxy
    sees the owner's cookie as an owner session rather than failing closed to
    ``none``.
    """
    token = generate_token("owner", ttl_seconds=300)
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret="s", local_only=False
    )
    req = _make_request(path="/api/spawn", remote="10.0.0.1", cookies={"mc_token_5476": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    marks = {call.args[0]: call.args[1] for call in req.__setitem__.call_args_list}
    assert marks["auth_from_query_token"] is False


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


@pytest.mark.asyncio
async def test_knowledge_agent_document_is_registered_internal_path() -> None:
    """Regression: the MCP-only /api/knowledge/agent-document route
    (knowledge_add_document tool) MUST be in the strict internal allowlist.
    Its sole caller authenticates with X-Internal-Secret and sends no browser
    token, so if the route is missing from the allowlist it falls through to
    cookie auth and every call is denied with 403 "Token required".

    Drives the REAL _STRICT_INTERNAL_API_PATHS from server.py through the
    middleware so a future edit that drops the entry fails here."""
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/knowledge/agent-document" in _STRICT_INTERNAL_API_PATHS
    secret = "test-secret-123"
    mw = token_auth_middleware(internal_paths=_STRICT_INTERNAL_API_PATHS, internal_secret=secret)
    # Internal secret on loopback → granted (the MCP _post path).
    ok = await mw(
        _make_request(
            path="/api/knowledge/agent-document",
            method="POST",
            headers={"X-Internal-Secret": secret},
        ),
        _ok_handler,
    )
    assert ok.status == 200
    # No token / no secret → denied: the fix authenticates the caller, it must
    # not open an unauthenticated route.
    denied = await mw(
        _make_request(path="/api/knowledge/agent-document", method="POST"), _ok_handler
    )
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

    A per-import ``os.urandom(32)`` _SECRET would rotate
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
    # an owner DACL that does not surface in st_mode (files report
    # 0o666), so the POSIX-bit assertion is only meaningful off Windows (the
    # Windows DACL path is covered by test_platform_compat_owner_only::TestRestrictToOwner).
    if os.name != "nt":
        assert (key_file.stat().st_mode & 0o777) == 0o600
    # Second load returns the SAME secret (persistence).
    s2 = ta._load_or_create_secret()
    assert s1 == s2


def test_signing_secret_concurrent_first_init_converges(tmp_path, monkeypatch) -> None:
    """Concurrent first-time inits MUST
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
    # this via chmod 0o600; Windows applies an owner-only DACL that does
    # NOT surface in st_mode (files report 0o666), so the POSIX-bit assertion is
    # only meaningful off Windows — the Windows DACL path has direct coverage in
    # test_platform_compat_owner_only::TestRestrictToOwner.
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
    """A TRANSIENT sharing violation on the PUBLISH of the key file must be
    retried, not degraded to an ephemeral secret.

    On Windows the publishing link can hit a sharing violation
    (``PermissionError`` / WinError 32) while another handle holds the
    destination open — landing on ``os.link``, not the read. The publish-side
    companion to ``test_signing_secret_read_contention_retries_not_ephemeral``;
    reproduced deterministically with the ``os.link`` simulator so it is caught
    on the POSIX dev loop.
    """
    from windows_sim import link_sharing_violation

    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE

    # Fault the FIRST publish of the key file; the retry must succeed.
    with link_sharing_violation(match=ts._SECRET_KEY_FILE, times=1) as state:
        secret = ts._load_or_create_secret()

    assert state["n"] >= 2, "the contended publish was not retried"
    assert key_file.exists(), "key must be persisted after retrying the create"
    on_disk = key_file.read_bytes()
    assert len(on_disk) >= ts._MIN_KEY_BYTES, "retry wrote a short/incomplete key"
    assert secret == on_disk, "must return the persisted key, not an ephemeral one"


def test_signing_secret_destination_never_exists_while_incomplete(tmp_path, monkeypatch) -> None:
    """Regression (Mesh-3720): ``token_signing.key`` must never exist on disk in a
    partial state, so an interrupted update cannot leave a 0-byte key.

    The old creator opened the DESTINATION with ``O_CREAT|O_EXCL`` and only then
    wrote the 32 random bytes. Its ``finally`` cleaned up an exception, but a
    kill has no ``finally``: an update completing during shutdown, or a reboot,
    that ended the process between the create and the write persisted a 0-byte
    key. Every later boot then read <32 bytes, exhausted the retry budget and
    fell back to a fresh ephemeral secret — a dashboard that loads while every
    signed action fails, and a plain restart cannot fix it because it re-reads
    the same empty file.

    The fix stages the whole key into a private sibling and publishes it with
    ``os.link``, so the destination name only ever appears already-complete.
    This samples the destination on every ``os.write`` that carries key bytes:
    with the fix it is absent throughout; before it, it is present at 0 bytes.
    """
    from kiro_crew.dashboard import token_secret as ts

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    key_file = tmp_path / ts._SECRET_KEY_FILE
    assert not key_file.exists()

    # Sizes the destination had at each point a key byte was being written.
    # ``None`` means "did not exist", which is the only acceptable value.
    sizes_during_write: list[int | None] = []
    real_write = os.write

    def _spy(fd, data):  # type: ignore[no-untyped-def]
        try:
            sizes_during_write.append(key_file.stat().st_size)
        except OSError:
            sizes_during_write.append(None)
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", _spy)
    secret = ts._load_or_create_secret()
    monkeypatch.undo()

    on_disk = key_file.read_bytes()
    assert secret == on_disk, "returned key must be the persisted one"
    assert len(on_disk) >= ts._MIN_KEY_BYTES

    partial = [n for n in sizes_during_write if n is not None and n < ts._MIN_KEY_BYTES]
    assert not partial, (
        "the destination existed in a partial state during the write "
        f"(observed sizes {partial}); a kill there would persist a truncated key"
    )
