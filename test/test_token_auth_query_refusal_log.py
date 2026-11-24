"""A refused ``?token=`` that a session cookie stood in for is recorded, with its reason.

The middleware lets a valid session cookie authenticate a request whose ``?token=``
was refused, so an old link cannot veto a live session. That request answers 200 and
the page loads signed in, so without a record a link that did not work looks exactly
like one that did. A Slack challenge link is the case that hurts: its window opens only
when the link itself is exchanged, so the person is challenged again with nothing
saying why. These tests pin the SEL row that says why, and pin that the refused link
still grants nothing: no session exchange and no token-consumed observer call.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _ok_handler,
    _session_token,
    clear_nonces,
)

import kiro_crew.dashboard.token_auth as token_auth_mod
from kiro_crew.dashboard.token_auth import (
    bind_token_ip,
    generate_token,
    mark_consumed,
    token_auth_middleware,
)

_COOKIE = "mc_token_5476"


def _capture_sel(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    class _FakeSel:
        def log_api_access(self, **kw: object) -> None:
            calls.append(kw)

    monkeypatch.setattr(token_auth_mod, "_sel_fn", lambda: _FakeSel())
    return calls


def _live_cookie(user: str) -> str:
    cookie = _session_token(user)
    bind_token_ip(cookie, "127.0.0.1")
    mark_consumed(cookie)
    return cookie


def _expired_challenge_link(user: str) -> str:
    with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        return generate_token(user, extra={"channel": "D123"})


def _refusal_rows(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c.get("outcome") == "query_token_rejected"]


def _observer() -> tuple[MagicMock, SimpleNamespace]:
    consumed = MagicMock()
    ctx = SimpleNamespace(dashboard=SimpleNamespace(on_token_consumed=consumed))
    return consumed, ctx


@pytest.mark.asyncio
async def test_expired_link_beside_a_live_cookie_logs_why_and_grants_nothing(monkeypatch) -> None:
    calls = _capture_sel(monkeypatch)
    link = _expired_challenge_link("owner")
    consumed, ctx = _observer()
    mw = token_auth_middleware()
    req = _make_request(query={"token": link}, cookies={_COOKIE: _live_cookie("owner")})
    with patch("kiro_crew.platform.current_context", return_value=ctx):
        resp = await mw(req, _ok_handler)

    assert resp.status == 200
    rows = _refusal_rows(calls)
    assert len(rows) == 1, calls
    assert rows[0]["operation"] == "dashboard.token_auth"
    assert rows[0]["error"] == "token expired"
    assert rows[0]["caller"] == "owner"
    # The token is a credential: no part of it reaches the audit row.
    assert all(link not in str(v) and link.split(".")[0] not in str(v) for v in rows[0].values())
    # Fail-closed: the refused link opened no window and minted no session.
    consumed.assert_not_called()
    assert _COOKIE not in resp.cookies


@pytest.mark.asyncio
async def test_damaged_link_beside_a_live_cookie_logs_invalid_signature(monkeypatch) -> None:
    """A link cut short in transit fails its signature; the row names that."""
    calls = _capture_sel(monkeypatch)
    link = generate_token("owner", extra={"channel": "D123"})
    mw = token_auth_middleware()
    req = _make_request(query={"token": link[:-6]}, cookies={_COOKIE: _live_cookie("owner")})
    resp = await mw(req, _ok_handler)

    assert resp.status == 200
    rows = _refusal_rows(calls)
    assert [r["error"] for r in rows] == ["invalid signature"]


@pytest.mark.asyncio
async def test_valid_link_beside_a_live_cookie_completes_and_logs_no_refusal(monkeypatch) -> None:
    """A good challenge link wins over the cookie: it is exchanged and the observer runs."""
    calls = _capture_sel(monkeypatch)
    link = generate_token("owner", extra={"channel": "D123", "thread_ts": "1.2"})
    consumed, ctx = _observer()
    mw = token_auth_middleware()
    req = _make_request(query={"token": link}, cookies={_COOKIE: _live_cookie("owner")})
    with patch("kiro_crew.platform.current_context", return_value=ctx):
        resp = await mw(req, _ok_handler)

    assert resp.status == 200
    assert _refusal_rows(calls) == []
    consumed.assert_called_once()
    assert consumed.call_args.args[:2] == ("owner", "D123")
    assert resp.cookies.get(_COOKIE) is not None


@pytest.mark.asyncio
async def test_internal_path_cookie_fallback_logs_the_refused_link(monkeypatch) -> None:
    """The internal-path helper adopts the cookie the same way, so it records the same row."""
    calls = _capture_sel(monkeypatch)
    link = _expired_challenge_link("intuser")
    mw = token_auth_middleware(internal_paths=frozenset({"/api/spawn"}), internal_secret="s")
    req = _make_request(
        path="/api/spawn", query={"token": link}, cookies={_COOKIE: _live_cookie("intuser")}
    )
    resp = await mw(req, _ok_handler)

    assert resp.status == 200
    assert [r["error"] for r in _refusal_rows(calls)] == ["token expired"]


@pytest.mark.asyncio
async def test_internal_path_row_names_the_verified_tailnet_peer(monkeypatch) -> None:
    """On a resolved tailnet peer the internal-path row names the peer login, not the
    cookie's token subject, so it agrees with the request's own granted row."""
    from multidict import CIMultiDict

    from kiro_crew.dashboard import tailnet
    from kiro_crew.dashboard.tailnet import TailnetTrust
    from kiro_crew.dashboard.token_auth import bind_token_peer

    monkeypatch.setattr(tailnet, "IS_POSIX", True)
    tailnet._whois_cache.clear()
    whois = {"Node": {"Name": "phone.tail.ts.net"}, "UserProfile": {"LoginName": "you@example.com"}}
    monkeypatch.setattr(tailnet, "_run_json_detail", MagicMock(return_value=(whois, False)))
    monkeypatch.setattr(tailnet, "loopback_client_is_daemon", lambda *_a: True)
    calls = _capture_sel(monkeypatch)
    cookie = _session_token("tsuser")
    bind_token_peer(cookie, "ts:node:you@example.com|phone.tail.ts.net")
    mark_consumed(cookie)
    mw = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/spawn"}),
        tailnet_trust=TailnetTrust(
            trust_identity=True, allowed_logins=("you@example.com",), pin_scope="node"
        ),
    )
    headers = CIMultiDict()
    headers.add("X-Forwarded-For", "100.64.0.5")
    req = _make_request(
        query={"token": _expired_challenge_link("tsuser")},
        cookies={_COOKIE: cookie},
        remote="127.0.0.1",
        headers=headers,
    )
    req.path = "/api/spawn"
    try:
        resp = await mw(req, _ok_handler)
    finally:
        tailnet._whois_cache.clear()

    assert resp.status == 200
    rows = _refusal_rows(calls)
    assert [(r["caller"], r["error"]) for r in rows] == [("you@example.com", "token expired")]
    granted = [c for c in calls if c.get("outcome") == "granted"]
    assert granted and granted[-1]["caller"] == rows[0]["caller"]
