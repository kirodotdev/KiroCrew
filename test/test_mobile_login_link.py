"""Regression coverage for the authenticated mobile-login recovery link."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

from aiohttp import web

from kiro_crew.dashboard.handlers import _shared, auth_mobile
from kiro_crew.dashboard.token_auth import (
    LINK_WINDOW_SECS,
    MAX_SESSION_TTL_SECS,
    _b64url_decode,
    generate_token,
)


def _request(
    *,
    user: str = "alice",
    app: str = "",
    dashboard_url: str = "",
    cookie_token: str = "",
    query_token: str = "",
    auth_token: str | None = None,
    state: object | None = None,
) -> MagicMock:
    request = MagicMock(spec=web.Request)
    # ``auth_token`` is what token_auth publishes: the credential it ACTUALLY
    # validated. Defaults to the one the middleware would have picked for a
    # request carrying these credentials (query token preferred when it is
    # valid), so a test only sets it explicitly to model the case where the two
    # diverge — i.e. the middleware fell back to the cookie because the query
    # token was invalid.
    published = auth_token if auth_token is not None else (query_token or cookie_token)
    request.get.side_effect = {"user": user, "app": app, "auth_token": published}.get
    request.app = {"dashboard_url": dashboard_url, "port": 7777}
    if state is not None:
        request.app["state"] = state
    request.headers = {"Origin": "https://dashboard.example"}
    request.cookies = {"mc_token_7777": cookie_token} if cookie_token else {}
    request.query = {"token": query_token} if query_token else {}
    return request


def _call(request: MagicMock, *, valid_origin: bool = True):
    with patch("kiro_crew.dashboard.handlers.auth_mobile.check_origin", return_value=valid_origin):
        return asyncio.run(auth_mobile.api_auth_mobile_link(request))


def _minted_claims(payload: dict) -> dict:
    token = payload["url"].split("token=", 1)[1]
    return json.loads(_b64url_decode(token.split(".", 1)[0]))


def _caller_token(*, peer_key: str = "", **extra: str) -> str:
    return generate_token(
        "alice",
        ttl_seconds=MAX_SESSION_TTL_SECS,
        peer_key=peer_key,
        extra=extra or None,
    )


def test_mobile_link_uses_configured_external_origin():
    response = _call(
        _request(dashboard_url="https://dashboard.example", cookie_token=_caller_token())
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["url"].startswith("https://dashboard.example?token=")
    assert "localhost" not in payload["url"]
    assert payload["expires_in"] == LINK_WINDOW_SECS
    assert response.headers["Cache-Control"] == "no-store"
    # An unbounded caller mints an unbounded link: no carried bound claims.
    claims = _minted_claims(payload)
    assert "no_refresh" not in claims
    assert "boot" not in claims


def test_mobile_link_refuses_missing_external_origin():
    response = _call(_request())

    assert response.status == 409
    assert json.loads(response.text) == {
        "error": "external_origin_unavailable",
        "code": "external_origin_unavailable",
    }


def test_mobile_link_refuses_app_scoped_token():
    response = _call(_request(app="calendar", dashboard_url="https://dashboard.example"))

    assert response.status == 403
    assert json.loads(response.text) == {
        "error": "app_token_forbidden",
        "code": "app_token_forbidden",
    }


def test_mobile_link_requires_an_authenticated_dashboard_session():
    response = _call(_request(user="", dashboard_url="https://dashboard.example"))

    assert response.status == 401
    assert json.loads(response.text) == {"error": "unauthenticated", "code": "unauthenticated"}


def test_mobile_link_refuses_invalid_origin():
    response = _call(_request(dashboard_url="https://dashboard.example"), valid_origin=False)

    assert response.status == 403
    assert json.loads(response.text) == {"error": "bad_origin", "code": "bad_origin"}


def test_mobile_link_refuses_a_restricted_session():
    """An incognito/temporary/channel-guest slot must not mint a durable link."""
    state = MagicMock()
    with patch(
        "kiro_crew.dashboard.handlers.auth_mobile._is_restricted_session", return_value=True
    ):
        response = _call(_request(dashboard_url="https://dashboard.example", state=state))

    assert response.status == 403
    assert json.loads(response.text) == {
        "error": "restricted_session",
        "code": "restricted_session",
    }


def test_mobile_link_carries_the_callers_bounds_into_the_minted_token():
    """A boot-bound / no-refresh caller mints a link with the same bounds."""
    response = _call(
        _request(
            dashboard_url="https://dashboard.example",
            cookie_token=_caller_token(boot="boot-abc", no_refresh="1"),
        )
    )

    assert response.status == 200
    claims = _minted_claims(json.loads(response.text))
    assert claims["boot"] == "boot-abc"
    assert claims["no_refresh"] == "1"


def test_mobile_link_carries_the_callers_exact_device_binding():
    """A delegated login link cannot widen a peer-bound session."""
    peer_key = "ts:node:alice@example.com|phone.tail.ts.net"
    response = _call(
        _request(
            dashboard_url="https://dashboard.example",
            cookie_token=_caller_token(require_peer="1", peer_key=peer_key),
        )
    )

    assert response.status == 200
    claims = _minted_claims(json.loads(response.text))
    assert claims["require_peer"] == "1"
    assert claims["peer_key"] == peer_key


def test_mobile_link_caps_ttl_at_the_callers_remaining_session():
    """A short-lived caller cannot mint a longer-lived credential."""
    short = generate_token("alice", ttl_seconds=600)
    response = _call(_request(dashboard_url="https://dashboard.example", cookie_token=short))

    assert response.status == 200
    claims = _minted_claims(json.loads(response.text))
    remaining = claims["session_exp"] - time.time()
    assert remaining <= 600 + 5  # never beyond the caller's own ceiling
    assert remaining > 0


def test_mobile_link_reports_the_live_window_for_a_short_caller():
    """``expires_in`` reports the clamped click window, not the constant.

    generate_token clamps the link-click ``exp`` to the session TTL, so a
    caller lending less than the nominal window mints a link that dies with
    its remaining lifetime. Reporting the constant would make the UI
    countdown promise minutes the link does not have.
    """
    short = generate_token("alice", ttl_seconds=120)
    response = _call(_request(dashboard_url="https://dashboard.example", cookie_token=short))

    assert response.status == 200
    payload = json.loads(response.text)
    assert 0 < payload["expires_in"] <= 120 < LINK_WINDOW_SECS
    # The reported window matches the minted token's actual exp.
    claims = _minted_claims(payload)
    assert claims["exp"] <= time.time() + payload["expires_in"] + 5


def test_mobile_link_fails_closed_on_an_unreadable_caller_token():
    """Bounds that cannot be established yield a bounded (no-refresh) link."""
    response = _call(
        _request(dashboard_url="https://dashboard.example", cookie_token="not-a-token")
    )

    assert response.status == 200
    claims = _minted_claims(json.loads(response.text))
    assert claims["no_refresh"] == "1"


def test_mobile_link_bounds_come_from_the_validated_credential_not_the_query_token():
    """The cookie-fallback case: bounds must follow what the middleware VALIDATED.

    When the query token is invalid, token_auth falls back to the session cookie
    and publishes the COOKIE as request["auth_token"]. A bounded cookie session
    must keep its bounds even though the URL still carries a permissive
    (unverified, attacker-settable) query token — re-deriving with a fixed
    query-then-cookie order would read that unverified value, drop
    ``no_refresh`` and raise the TTL ceiling to the full maximum: the
    ceiling-escape this endpoint exists to prevent.
    """
    permissive_query = _caller_token()  # unverified: no bounds, full-length TTL
    bounded_cookie = generate_token(
        "alice", ttl_seconds=600, extra={"boot": "boot-abc", "no_refresh": "1"}
    )

    response = _call(
        _request(
            dashboard_url="https://dashboard.example",
            query_token=permissive_query,
            cookie_token=bounded_cookie,
            auth_token=bounded_cookie,  # what the middleware validated
        )
    )

    assert response.status == 200
    claims = _minted_claims(json.loads(response.text))
    assert claims["no_refresh"] == "1"
    assert claims["boot"] == "boot-abc"
    remaining = claims["session_exp"] - time.time()
    assert 0 < remaining <= 600 + 5


def test_mobile_link_bounds_fail_closed_when_no_credential_was_published():
    """No published credential means no verified claims to lend: bound the mint.

    Trusting a re-extracted token here is exactly the unverified read the
    published-credential contract removes, so the mint is bounded instead.
    """
    response = _call(
        _request(
            dashboard_url="https://dashboard.example",
            cookie_token=_caller_token(boot="boot-abc"),
            auth_token="",  # middleware published nothing
        )
    )

    assert response.status == 200
    claims = _minted_claims(json.loads(response.text))
    assert claims["no_refresh"] == "1"
    assert "boot" not in claims


def test_mobile_link_bounds_come_from_the_query_token_not_a_stray_cookie():
    """Bounds must be read from the credential the middleware actually validated.

    The middleware prefers ``?token=`` over the cookie, and only the credential
    it validated has a verified signature. A request authenticated with a
    bounded query token while carrying a permissive (unverified,
    attacker-settable) cookie must still inherit the bounded claims — reading
    the cookie first would drop ``no_refresh`` and raise the TTL ceiling to the
    full maximum, escaping the very ceiling this endpoint enforces.
    """
    bounded_query = generate_token(
        "alice", ttl_seconds=600, extra={"boot": "boot-abc", "no_refresh": "1"}
    )
    permissive_cookie = _caller_token()  # no bounds, full-length TTL

    response = _call(
        _request(
            dashboard_url="https://dashboard.example",
            query_token=bounded_query,
            cookie_token=permissive_cookie,
        )
    )

    assert response.status == 200
    claims = _minted_claims(json.loads(response.text))
    assert claims["no_refresh"] == "1"
    assert claims["boot"] == "boot-abc"
    remaining = claims["session_exp"] - time.time()
    assert remaining <= 600 + 5
    assert remaining > 0


def test_mobile_link_refuses_a_caller_with_no_lifetime_left():
    """An exhausted remaining lifetime is refused, never rounded up to a floor.

    Clamping a non-positive remainder to one second would mint a link that
    outlives the session authorizing it, and the recipient's exchange opens a
    fresh window — so repeating the mint would walk the expiry forward
    indefinitely from a session that should already be dead.
    """
    expired = generate_token("alice", ttl_seconds=MAX_SESSION_TTL_SECS)
    with patch.object(_shared.time, "time", return_value=time.time() + 10**7):
        response = _call(_request(dashboard_url="https://dashboard.example", cookie_token=expired))

    assert response.status == 403
    assert json.loads(response.text) == {
        "error": "caller_session_expired",
        "code": "caller_session_expired",
    }


def test_mobile_link_writes_its_audit_off_the_event_loop():
    """SEL's first post-restart write initializes the log synchronously.

    On the event loop that stalls every other in-flight request, so the record
    goes through ``asyncio.to_thread`` — the same shape as the sibling
    ``tailnet_mobile._audit_async``.
    """
    with patch.object(auth_mobile.asyncio, "to_thread", wraps=auth_mobile.asyncio.to_thread) as spy:
        response = _call(
            _request(dashboard_url="https://dashboard.example", cookie_token=_caller_token())
        )

    assert response.status == 200
    assert spy.call_args_list, "the audit record was written on the event loop"
    # Two legitimate off-loop callees: the SEL audit and the mobile-connect
    # governance re-check (profile resolution can touch the filesystem). The
    # audit MUST be among them — that is the property this test pins.
    callees = [call.args[0] for call in spy.call_args_list]
    assert auth_mobile._audit in callees, "the audit record was written on the event loop"
    allowed = {auth_mobile._audit, auth_mobile.mint_denied_reason}
    assert all(fn in allowed for fn in callees)
