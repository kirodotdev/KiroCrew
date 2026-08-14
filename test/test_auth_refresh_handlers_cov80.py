"""Coverage tests for the auth-refresh HTTP endpoints (``dashboard/handlers/auth_refresh.py``).

``test_refresh_tokens.py`` covers the token module and says so explicitly ("Handler
integration tests are out of scope here"), so the three endpoints in this module —
``/api/auth/me``, ``/api/auth/refresh``, ``/api/auth/logout`` — had no tests of their
own. What is asserted here is the part that actually decides who stays logged in:

  * the CSRF (``bad_origin``) refusal on both mutating endpoints;
  * the refresh taxonomy — 429 rate-limited, 401 ``no_refresh_cookie``, 401
    ``invalid_refresh``, 401 ``refresh_chain_revoked`` (both the presented-revoked
    and the reuse-detected routes), the multi-tab grace replay, and the happy-path
    rotation;
  * that the grace replay never leaks a raw token into the response BODY — the
    ``_``-prefixed keys must stay in Set-Cookie only;
  * ``/api/auth/me``'s 401 and its two expiry claims;
  * logout's chain revocation, its invalid-cookie branch, and the per-session
    access-cookie revocation (CWE-613);
  * the rate limiter's window eviction and the two cookie helpers' refusal to set
    a cookie for an already-expired session.

Requests are built with ``make_mocked_request`` (no socket bound), the refresh state
manager is pinned into ``tmp_path``, the module's rate-limit globals are reset around
every test, and no wall-clock sleeping happens — times are passed in explicitly.
"""

from __future__ import annotations

import collections
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import refresh_tokens as rt
from kiro_crew.dashboard.handlers import auth_refresh as h
from kiro_crew.dashboard.refresh_tokens import (
    MAX_REFRESH_TTL_SECS,
    RefreshStateManager,
    generate_refresh_token,
    refresh_cookie_name,
)
from kiro_crew.dashboard.tailnet import TailnetTrust
from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS, generate_token

PORT = 7777


@pytest.fixture(autouse=True)
def _reset_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-IP buckets are module globals — give every test a clean map."""
    monkeypatch.setattr(h, "_refresh_rate_buckets", {})
    monkeypatch.setattr(h, "_refresh_rate_last_sweep", float("-inf"))


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RefreshStateManager:
    """Pin the refresh-state singleton at a tmp_path-backed manager."""
    mgr = RefreshStateManager(state_path=tmp_path / "refresh_chains.json")
    monkeypatch.setattr(rt, "_state_singleton", mgr)
    return mgr


@pytest.fixture()
def audit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, str]]:
    """Record the module's audit calls instead of writing a real SEL entry."""
    calls: list[tuple[str, str, str, str]] = []

    def _record(user_id: str, operation: str, outcome: str, error: str = "") -> None:
        calls.append((user_id, operation, outcome, error))

    monkeypatch.setattr(h, "_audit", _record)
    return calls


def _mk(
    method: str = "POST",
    path: str = "/api/auth/refresh",
    *,
    cookies: dict[str, str] | None = None,
    origin: str | None = None,
    remote: str = "203.0.113.9",
    user: str = "",
    app_keys: dict[str, Any] | None = None,
) -> web.Request:
    app = web.Application()
    app["port"] = PORT
    app["allowed_origins"] = {"http://localhost:7777"}
    for key, value in (app_keys or {}).items():
        app[key] = value
    headers: dict[str, str] = {"Host": f"localhost:{PORT}"}
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    if origin is not None:
        headers["Origin"] = origin
    # ``Request.remote`` is derived from the transport's peername, so the peer is
    # injected through a stub transport rather than assigned onto the request.
    transport = MagicMock()
    transport.get_extra_info = lambda key, default=None: (
        (remote, 44444) if key == "peername" else default
    )
    request = make_mocked_request(method, path, headers=headers, app=app, transport=transport)
    if user:
        request["user"] = user
    return request


def _body(response: web.StreamResponse) -> Any:
    assert isinstance(response, web.Response)
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _set_cookie_names(response: web.StreamResponse) -> list[str]:
    """The cookie names this response actually sets."""
    return [morsel.key for morsel in response.cookies.values()]


# --- rate limiter ------------------------------------------------------------


def test_rate_limited_denies_an_empty_client_ip() -> None:
    """Fail-closed: an unattributable request cannot be bucketed, so it is denied."""
    assert h._rate_limited("") is True


def test_rate_limited_defaults_now_to_wall_clock() -> None:
    assert h._rate_limited("198.51.100.1") is False
    assert h._refresh_rate_buckets["198.51.100.1"]


def test_rate_limited_evicts_timestamps_that_aged_out_of_the_window() -> None:
    now = 10_000.0
    h._refresh_rate_buckets["198.51.100.2"] = collections.deque(
        [now - h._REFRESH_RATE_WINDOW_SECS - 5] * 3
    )
    assert h._rate_limited("198.51.100.2", now=now) is False
    # The three stale stamps were dropped and only this call's remains.
    assert list(h._refresh_rate_buckets["198.51.100.2"]) == [now]


def test_rate_limited_trips_at_the_per_window_cap() -> None:
    now = 20_000.0
    ip = "198.51.100.3"
    for _ in range(h._REFRESH_RATE_MAX_CALLS):
        assert h._rate_limited(ip, now=now) is False
    assert h._rate_limited(ip, now=now) is True


def test_rate_limited_drops_aged_stamps_when_the_sweep_is_throttled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-bucket eviction is what keeps a throttled sweep from over-counting."""
    now = 30_000.0
    ip = "198.51.100.4"
    # Sweep just ran, so it returns early and leaves the stale stamps in place.
    monkeypatch.setattr(h, "_refresh_rate_last_sweep", now)
    stale = now - h._REFRESH_RATE_WINDOW_SECS - 1
    h._refresh_rate_buckets[ip] = collections.deque([stale] * h._REFRESH_RATE_MAX_CALLS + [now - 1])
    # Without the in-bucket popleft the bucket would look full and deny this call.
    assert h._rate_limited(ip, now=now) is False
    assert list(h._refresh_rate_buckets[ip]) == [now - 1, now]


def test_rate_limited_fails_closed_for_an_unseen_ip_at_the_bucket_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At capacity an unseen IP is denied — never admitted by evicting a live bucket."""
    now = 40_000.0
    monkeypatch.setattr(h, "_refresh_rate_last_sweep", now)
    full = {f"10.0.{i // 256}.{i % 256}": collections.deque([now]) for i in range(4096)}
    assert len(full) == h._REFRESH_RATE_MAX_BUCKETS
    monkeypatch.setattr(h, "_refresh_rate_buckets", full)
    assert h._rate_limited("198.51.100.5", now=now) is True
    # The forced sweep must not have dropped any still-in-window bucket.
    assert len(h._refresh_rate_buckets) == h._REFRESH_RATE_MAX_BUCKETS
    assert "198.51.100.5" not in h._refresh_rate_buckets


# --- audit sink --------------------------------------------------------------


def test_audit_reports_an_unknown_caller_when_no_user_is_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = MagicMock()
    monkeypatch.setattr(h, "_sel_fn", lambda: sink)
    h._audit("", "refresh_token_use", "no_cookie", "detail")
    kwargs = sink.log_api_access.call_args.kwargs
    assert kwargs == {
        "caller": "<unknown>",
        "operation": "refresh_token_use",
        "outcome": "no_cookie",
        "source": "refresh_tokens",
        "resources": "detail",
    }


# --- foreign-port cookie pruning ---------------------------------------------


def test_expire_foreign_port_cookies_leaves_a_small_jar_alone() -> None:
    response = web.json_response({})
    h._expire_foreign_port_cookies(response, _mk(cookies={"mc_token_9999": "x"}))
    assert _set_cookie_names(response) == []


def test_expire_foreign_port_cookies_trims_other_ports_once_the_jar_is_large() -> None:
    """Only cookies belonging to OTHER ports are expired, each on its own path."""
    padding = "p" * 3200
    response = web.json_response({})
    request = _mk(
        cookies={
            f"mc_token_{PORT}": padding,
            "mc_token_9999": padding,
            f"{rt.REFRESH_COOKIE_PREFIX}9999": "r",
        }
    )
    h._expire_foreign_port_cookies(response, request)
    expired = {morsel.key: morsel["path"] for morsel in response.cookies.values()}
    assert expired == {
        "mc_token_9999": rt.ACCESS_COOKIE_PATH,
        f"{rt.REFRESH_COOKIE_PREFIX}9999": rt.REFRESH_COOKIE_PATH,
    }
    assert f"mc_token_{PORT}" not in expired


# --- cookie helpers ----------------------------------------------------------


def test_set_access_cookie_is_a_noop_for_an_expired_session() -> None:
    """Never fall back to the full TTL: 0 is falsy, so an ``or`` would extend it."""
    response = web.json_response({})
    h._set_access_cookie(response, _mk(), "tok", time.time() - 1)
    assert _set_cookie_names(response) == []


def test_set_refresh_cookie_is_a_noop_for_an_expired_session() -> None:
    response = web.json_response({})
    h._set_refresh_cookie(response, _mk(), "tok", time.time() - 1)
    assert _set_cookie_names(response) == []


def test_set_cookies_are_scoped_and_capped_for_a_live_session() -> None:
    response = web.json_response({})
    request = _mk()
    h._set_access_cookie(response, request, "atok", time.time() + 10 * MAX_SESSION_TTL_SECS)
    h._set_refresh_cookie(response, request, "rtok", time.time() + 10 * MAX_REFRESH_TTL_SECS)
    access = response.cookies[f"mc_token_{PORT}"]
    refresh = response.cookies[refresh_cookie_name(str(PORT))]
    assert access["path"] == "/" and int(access["max-age"]) <= MAX_SESSION_TTL_SECS
    assert refresh["path"] == rt.REFRESH_COOKIE_PATH
    assert int(refresh["max-age"]) <= MAX_REFRESH_TTL_SECS


# --- GET /api/auth/me --------------------------------------------------------


@pytest.mark.asyncio
async def test_me_401_without_an_authenticated_user() -> None:
    response = await h.api_auth_me(_mk("GET", "/api/auth/me"))
    assert response.status == 401
    assert _body(response) == {"error": "unauthenticated"}


@pytest.mark.asyncio
async def test_me_reports_both_expiries(state: RefreshStateManager) -> None:
    access = generate_token("alice", ttl_seconds=MAX_SESSION_TTL_SECS, register_nonce=False)
    refresh, _chain, _jti, refresh_exp = generate_refresh_token("alice")
    request = _mk(
        "GET",
        "/api/auth/me",
        user="alice",
        cookies={
            f"mc_token_{PORT}": access,
            refresh_cookie_name(str(PORT)): refresh,
        },
    )
    payload = _body(await h.api_auth_me(request))
    assert payload["user_id"] == "alice"
    assert payload["session_exp"] > time.time()
    assert abs(payload["refresh_exp"] - refresh_exp) < 1.0


@pytest.mark.asyncio
async def test_me_reports_zero_refresh_exp_for_an_unusable_refresh_cookie(
    state: RefreshStateManager,
) -> None:
    request = _mk(
        "GET",
        "/api/auth/me",
        user="alice",
        cookies={refresh_cookie_name(str(PORT)): "garbage.notatoken"},
    )
    payload = _body(await h.api_auth_me(request))
    assert payload["refresh_exp"] == 0.0
    assert payload["session_exp"] == 0.0


# --- POST /api/auth/refresh --------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_403_on_a_foreign_origin(audit: list) -> None:
    request = _mk(origin="https://evil.invalid")
    response = await h.api_auth_refresh(request)
    assert response.status == 403
    assert _body(response) == {"error": "bad_origin"}
    assert audit[-1][2] == "bad_origin"


@pytest.mark.asyncio
async def test_refresh_429_when_rate_limited(monkeypatch: pytest.MonkeyPatch, audit: list) -> None:
    monkeypatch.setattr(h, "_rate_limited", lambda ip, now=None: True)
    response = await h.api_auth_refresh(_mk())
    assert response.status == 429
    assert response.headers["Retry-After"] == str(int(h._REFRESH_RATE_WINDOW_SECS))
    assert audit[-1][2] == "rate_limited"


@pytest.mark.asyncio
async def test_refresh_401_without_a_refresh_cookie(audit: list) -> None:
    response = await h.api_auth_refresh(_mk())
    assert response.status == 401
    assert _body(response) == {"error": "no_refresh_cookie"}
    assert audit[-1][2] == "no_cookie"


@pytest.mark.asyncio
async def test_refresh_401_on_a_malformed_refresh_cookie(
    state: RefreshStateManager, audit: list
) -> None:
    request = _mk(cookies={refresh_cookie_name(str(PORT)): "not.a.token"})
    response = await h.api_auth_refresh(request)
    assert response.status == 401
    assert _body(response) == {"error": "invalid_refresh"}
    assert audit[-1][2] == "invalid"


@pytest.mark.asyncio
async def test_refresh_401_and_clears_the_cookie_when_the_chain_is_revoked(
    state: RefreshStateManager, audit: list
) -> None:
    token, chain_id, _jti, _exp = generate_refresh_token("alice")
    state.revoke_chain(chain_id, time.time() + MAX_REFRESH_TTL_SECS)
    request = _mk(cookies={refresh_cookie_name(str(PORT)): token})
    response = await h.api_auth_refresh(request)
    assert response.status == 401
    assert _body(response) == {"error": "refresh_chain_revoked"}
    # The dead cookie is expired rather than left for the browser to keep resending.
    assert refresh_cookie_name(str(PORT)) in _set_cookie_names(response)
    assert audit[-1][2] == "chain_revoked"


@pytest.mark.asyncio
async def test_refresh_happy_path_rotates_both_cookies(
    state: RefreshStateManager, audit: list
) -> None:
    token, chain_id, jti, _exp = generate_refresh_token("alice")
    request = _mk(cookies={refresh_cookie_name(str(PORT)): token})
    response = await h.api_auth_refresh(request)
    assert response.status == 200
    payload = _body(response)
    assert payload["session_exp"] > time.time()
    assert payload["refresh_exp"] > payload["session_exp"]
    # No raw token in the BODY — tokens travel in Set-Cookie only.
    assert not [k for k in payload if k.startswith("_")]
    assert sorted(_set_cookie_names(response)) == sorted(
        [f"mc_token_{PORT}", refresh_cookie_name(str(PORT))]
    )
    # Rotation-on-use: the presented jti is consumed, the chain lives on.
    assert state.is_consumed(jti)
    assert audit[-1] == ("alice", "refresh_token_use", "ok", "")
    assert chain_id


@pytest.mark.asyncio
async def test_refresh_replays_the_same_pair_inside_the_grace_window(
    state: RefreshStateManager, audit: list
) -> None:
    """A second tab presenting the same jti from the same IP gets the cached pair."""
    token, _chain, jti, _exp = generate_refresh_token("alice")
    cookies = {refresh_cookie_name(str(PORT)): token}
    first = _body(await h.api_auth_refresh(_mk(cookies=cookies)))
    replay = await h.api_auth_refresh(_mk(cookies=cookies))
    assert replay.status == 200
    payload = _body(replay)
    assert payload == first
    assert not [k for k in payload if k.startswith("_")]
    assert sorted(_set_cookie_names(replay)) == sorted(
        [f"mc_token_{PORT}", refresh_cookie_name(str(PORT))]
    )
    assert audit[-1][2] == "grace_replay"
    assert state.is_consumed(jti)


@pytest.mark.asyncio
async def test_refresh_revokes_the_chain_on_genuine_reuse(
    state: RefreshStateManager, audit: list
) -> None:
    """Reuse from a different IP is theft, not a multi-tab race: kill the chain."""
    token, chain_id, jti, exp = generate_refresh_token("alice")
    state.mark_consumed(jti, chain_id=chain_id, exp=exp, ip="198.51.100.7", replacement="")
    request = _mk(cookies={refresh_cookie_name(str(PORT)): token}, remote="203.0.113.9")
    response = await h.api_auth_refresh(request)
    assert response.status == 401
    assert _body(response) == {"error": "refresh_chain_revoked"}
    assert refresh_cookie_name(str(PORT)) in _set_cookie_names(response)
    assert audit[-1][2] == "reuse_detected"
    assert state.is_chain_revoked(chain_id)


@pytest.mark.asyncio
async def test_refresh_revokes_the_chain_when_the_cached_replacement_is_corrupt(
    state: RefreshStateManager, audit: list
) -> None:
    """An undecodable grace payload must fall through to revocation, not 500."""
    token, chain_id, jti, exp = generate_refresh_token("alice")
    state.mark_consumed(jti, chain_id=chain_id, exp=exp, ip="203.0.113.9", replacement="{not json")
    request = _mk(cookies={refresh_cookie_name(str(PORT)): token}, remote="203.0.113.9")
    response = await h.api_auth_refresh(request)
    assert response.status == 401
    assert audit[-1][2] == "reuse_detected"


# --- _rebind_rotated_token_to_peer -------------------------------------------


@pytest.mark.asyncio
async def test_rebind_is_a_noop_without_tailnet_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    bind = MagicMock()
    monkeypatch.setattr(h, "bind_token_peer", bind)
    await h._rebind_rotated_token_to_peer(_mk(), "tok", time.time() + 60)
    bind.assert_not_called()


@pytest.mark.asyncio
async def test_rebind_is_a_noop_when_no_login_is_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bind = MagicMock()
    monkeypatch.setattr(h, "bind_token_peer", bind)
    trust = TailnetTrust(trust_identity=True, allowed_logins=())
    await h._rebind_rotated_token_to_peer(
        _mk(app_keys={"tailnet_trust": trust}), "tok", time.time() + 60
    )
    bind.assert_not_called()


@pytest.mark.asyncio
async def test_rebind_is_a_noop_when_no_peer_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    bind = MagicMock()
    monkeypatch.setattr(h, "bind_token_peer", bind)
    monkeypatch.setattr(h, "resolve_forwarded_peer", AsyncMock(return_value=None))
    trust = TailnetTrust(trust_identity=True, allowed_logins=("alice@example.com",))
    await h._rebind_rotated_token_to_peer(
        _mk(app_keys={"tailnet_trust": trust}), "tok", time.time() + 60
    )
    bind.assert_not_called()


@pytest.mark.asyncio
async def test_rebind_pins_the_rotated_token_to_the_resolved_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bind = MagicMock()
    monkeypatch.setattr(h, "bind_token_peer", bind)
    monkeypatch.setattr(h, "resolve_forwarded_peer", AsyncMock(return_value=("alice", "node")))
    monkeypatch.setattr(h, "peer_pin_key", lambda peer, scope: f"pin:{peer[0]}:{scope}")
    trust = TailnetTrust(trust_identity=True, allowed_logins=("alice",))
    exp = time.time() + 60
    await h._rebind_rotated_token_to_peer(_mk(app_keys={"tailnet_trust": trust}), "tok", exp)
    bind.assert_called_once_with("tok", f"pin:alice:{trust.pin_scope}", exp)


# --- POST /api/auth/logout ---------------------------------------------------


@pytest.mark.asyncio
async def test_logout_403_on_a_foreign_origin(audit: list) -> None:
    response = await h.api_auth_logout(
        _mk("POST", "/api/auth/logout", origin="https://evil.invalid")
    )
    assert response.status == 403
    assert _body(response) == {"error": "bad_origin"}
    assert audit[-1][1:3] == ("refresh_token_logout", "bad_origin")


@pytest.mark.asyncio
async def test_logout_without_cookies_still_clears_both(
    state: RefreshStateManager, audit: list
) -> None:
    response = await h.api_auth_logout(_mk("POST", "/api/auth/logout"))
    assert _body(response) == {"logged_out": True}
    assert sorted(_set_cookie_names(response)) == sorted(
        [f"mc_token_{PORT}", refresh_cookie_name(str(PORT))]
    )
    assert audit[-1][2] == "no_cookie"


@pytest.mark.asyncio
async def test_logout_revokes_the_refresh_chain(state: RefreshStateManager, audit: list) -> None:
    token, chain_id, _jti, _exp = generate_refresh_token("alice")
    request = _mk("POST", "/api/auth/logout", cookies={refresh_cookie_name(str(PORT)): token})
    response = await h.api_auth_logout(request)
    assert _body(response) == {"logged_out": True}
    assert state.is_chain_revoked(chain_id)
    assert ("alice", "refresh_token_logout", "ok", chain_id) in audit


@pytest.mark.asyncio
async def test_logout_audits_an_unusable_refresh_cookie(
    state: RefreshStateManager, audit: list
) -> None:
    request = _mk(
        "POST", "/api/auth/logout", cookies={refresh_cookie_name(str(PORT)): "junk.token"}
    )
    response = await h.api_auth_logout(request)
    assert _body(response) == {"logged_out": True}
    assert audit[-1][2] == "invalid_refresh"


@pytest.mark.asyncio
async def test_logout_revokes_the_access_cookie_nonce(
    state: RefreshStateManager, audit: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CWE-613: clearing the browser copy does nothing to a stolen copy."""
    revoke = MagicMock(return_value=True)
    monkeypatch.setattr(h, "revoke_access_cookie", revoke)
    access = generate_token("alice", ttl_seconds=MAX_SESSION_TTL_SECS, register_nonce=False)
    request = _mk("POST", "/api/auth/logout", cookies={f"mc_token_{PORT}": access})
    response = await h.api_auth_logout(request)
    assert _body(response) == {"logged_out": True}
    revoke.assert_called_once_with(access)
    assert ("", "access_cookie_revoked", "ok", "") in audit


@pytest.mark.asyncio
async def test_logout_records_a_noop_when_the_access_cookie_was_not_revocable(
    state: RefreshStateManager, audit: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(h, "revoke_access_cookie", MagicMock(return_value=False))
    request = _mk("POST", "/api/auth/logout", cookies={f"mc_token_{PORT}": "junk"})
    await h.api_auth_logout(request)
    assert ("", "access_cookie_revoked", "noop", "") in audit
