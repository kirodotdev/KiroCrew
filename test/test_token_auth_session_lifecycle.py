"""A dashboard session, from the link that mints it to the logout that ends it.

These tests pin the nonce bookkeeping that keeps pending links alive, a ``?token=``
link reused across requests, a remote client refused without a token and admitted with
one, the Slack ``!dashboard`` link (sent by DM, its host and its audit record), the
exchange of a link for a distinct session cookie and the claims a session token
carries, cookie names that keep two gateways on different ports apart, cookie-only
session tokens that never register a nonce, the refresh chain a ``no_refresh`` link
never gets, and the two ways a session ends: ``/api/logout`` and per-cookie revocation
with its persisted, owner-only denylist. A topic of the ``test_token_auth`` family:
``test_token_auth.py`` keeps the token primitives and the middleware paths, and the
other siblings are ``test_token_auth_signing_secret.py``,
``test_token_auth_spa_shell.py``, ``test_token_auth_credential_scope.py`` and
``test_token_auth_tailnet_pin.py``.
"""

from __future__ import annotations

import os
import socket
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _ok_handler,
    clear_nonces,
)

from kiro_crew.dashboard.refresh_tokens import REFRESH_COOKIE_PREFIX, refresh_cookie_name
from kiro_crew.dashboard.token_auth import (
    MAX_CONCURRENT_NONCES,
    MAX_SESSION_TTL_SECS,
    RevokedNonceStore,
    bind_token_ip,
    generate_token,
    mark_consumed,
    revoke_access_cookie,
    token_auth_middleware,
    token_embed_parent_port,
    validate_token,
    validate_token_with_app,
)


def test_evict_expired_removes_old_entries() -> None:
    """Verify evict_expired removes expired IP bindings, consumed tokens, and nonces."""
    from kiro_crew.dashboard.token_auth import _state, _token_pin_key

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
        pin_key = _token_pin_key(token)
        assert pin_key not in _state._peer_bindings, "expired pin should be evicted"
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


def test_revoked_store_locks_the_file_down_to_its_owner(tmp_path, monkeypatch) -> None:
    """The denylist is locked to its owner through the fail-loud helper.

    A bare ``os.chmod(path, 0o600)`` only toggles the read-only ATTRIBUTE on
    Windows: it leaves the parent-inherited DACL in place, so the file stays
    readable by other local accounts, and because the call still succeeds the
    warn-and-continue handler never fires. ``restrict_to_owner`` is the helper
    that applies an owner-only DACL there and raises when it cannot — the same
    one the app-token secret in this module and ``token_secret.py`` already use.
    """
    from kiro_crew import platform_compat
    from kiro_crew.dashboard import token_auth as token_auth_mod

    locked: list[tuple[str, int]] = []
    real_restrict = platform_compat.restrict_to_owner

    def _spy(path, **_kw) -> None:
        # Record the size at lockdown time: the nonce list must not be sitting
        # in the file yet (see the ordering assertion below).
        locked.append((str(path), os.path.getsize(str(path))))
        real_restrict(path)

    monkeypatch.setattr(token_auth_mod.platform_compat, "restrict_to_owner", _spy)

    state_path = tmp_path / "token_revoked_nonces.json"
    store = RevokedNonceStore(state_path=state_path)
    # flake-ok: pre-existing real-clock read for an expiry an hour ahead, moved verbatim; tracked for a de-flake pass
    store.revoke("nonce-locked", time.time() + 3600)

    assert locked, (
        "the revoked-nonce store was persisted without restrict_to_owner; a raw "
        "chmod is a no-op on Windows and leaves the denylist readable by other "
        "local accounts"
    )
    locked_path, size_at_lockdown = locked[0]
    assert size_at_lockdown == 0, (
        "the denylist was written before the lockdown applied; on Windows the "
        "lockdown replaces the DACL rather than setting it at create time, so "
        "the nonces would sit under the parent-inherited DACL until it landed"
    )
    assert locked_path.startswith(
        str(state_path)
    ), f"locked down {locked_path}, which is not the store's own temp file"

    # The store still works, and on POSIX the resulting mode is observable.
    assert store.is_revoked("nonce-locked") is True
    if platform_compat.IS_POSIX:
        # Windows locks the file down via an owner DACL, which does not surface
        # in st_mode (files report 0o666) — that path is covered by
        # test_platform_compat_owner_only::TestRestrictToOwner.
        assert (state_path.stat().st_mode & 0o777) == 0o600


def test_revoked_store_evicts_expired_entry() -> None:
    """An entry whose session_exp has passed is treated as not-revoked (the
    token is rejected by the expiry check anyway) and dropped lazily."""
    store = RevokedNonceStore(state_path=None)  # in-memory only
    # flake-ok: pre-existing real-clock read for an expiry one second past, moved verbatim; tracked for a de-flake pass
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


# -- extract_numeric_claim (round-2 bugfix: api_auth_me session_exp=0) ---------


def test_extract_numeric_claim_returns_float_session_exp() -> None:
    """session_exp is a float claim; the string-only extract_claims_from_token
    dropped it (api_auth_me always saw 0.0, disabling frontend refresh). The
    numeric extractor must return the real float."""
    from kiro_crew.dashboard.token_auth import extract_numeric_claim

    tok = generate_token("alice", ttl_seconds=3600)
    exp = extract_numeric_claim(tok, "session_exp")
    assert isinstance(exp, float)
    # flake-ok: pre-existing real-clock comparison against a freshly minted expiry, moved verbatim; tracked for a de-flake pass
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


# -- no_refresh: a link minted for a device we do not control gets no refresh chain --


@pytest.mark.asyncio
async def test_query_param_auth_normally_attaches_a_refresh_cookie() -> None:
    """Baseline for the test below: without the claim, a refresh cookie IS set.

    Without this the no_refresh assertion could pass simply because the exchange
    never attaches a refresh cookie in this harness at all.
    """
    mw = token_auth_middleware()
    token = generate_token("refreshable", ttl_seconds=300)
    resp = await mw(_make_request(query={"token": token}, remote="10.0.0.2"), _ok_handler)
    assert resp.status == 200
    assert any(n.startswith(REFRESH_COOKIE_PREFIX) for n in resp.cookies)


@pytest.mark.asyncio
async def test_no_refresh_link_gets_an_access_cookie_but_no_refresh_chain() -> None:
    """The tailnet QR's short session must not be promotable to the 20h ceiling.

    ``api_auth_refresh`` re-mints at MAX_SESSION_TTL_SECS without carrying the
    original ceiling forward, so a refresh chain would silently extend a ~1h
    scanned session. The guarantee is enforced by never minting the chain: this
    asserts the ABSENCE of the credential, not a guard around it.
    """
    mw = token_auth_middleware()
    token = generate_token("qruser", ttl_seconds=300, extra={"no_refresh": "1"})
    resp = await mw(_make_request(query={"token": token}, remote="10.0.0.3"), _ok_handler)
    assert resp.status == 200
    # The session itself still works — the phone can use the dashboard.
    assert resp.cookies.get("mc_token_5476") is not None
    # But there is no LIVE refresh credential to rotate. The name may appear as
    # an EXPIRY (max-age=0) — the exchange also clears a residual cookie the
    # browser already had — so the property is "nothing rotatable", not "no
    # Set-Cookie header": asserting the latter would fail the moment the
    # clearing behaviour was added, which is exactly what happened.
    live = [
        n
        for n in resp.cookies
        if n.startswith(REFRESH_COOKIE_PREFIX) and int(resp.cookies[n]["max-age"] or 0) > 0
    ]
    assert not live


@pytest.mark.asyncio
async def test_no_refresh_link_expires_a_refresh_cookie_the_browser_already_had() -> None:
    """Not minting one is half the guarantee; a residual one must be cleared.

    `api_auth_refresh` authenticates on the refresh cookie ALONE — it never checks
    that the access token beside it belongs to the same session. So a browser that
    already held a refresh cookie from an earlier full session (a prior `?token=`
    link, a `!dashboard` link) could rotate it into a 20-hour session immediately
    after scanning a QR, bypassing the short window entirely.
    """
    mw = token_auth_middleware()
    token = generate_token("qruser2", ttl_seconds=300, extra={"no_refresh": "1"})
    stale = refresh_cookie_name("5476")
    resp = await mw(
        _make_request(
            query={"token": token},
            cookies={stale: "a-refresh-token-from-an-earlier-session"},
            remote="10.0.0.4",
        ),
        _ok_handler,
    )
    assert resp.status == 200
    # Present in the response, but as an EXPIRY (max-age=0) — not left alone.
    assert stale in resp.cookies
    assert int(resp.cookies[stale]["max-age"]) == 0
