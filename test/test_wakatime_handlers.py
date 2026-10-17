"""Tests for the WakaTime dashboard export handler.

No network: ``service.build_client`` is patched to a stub client whose read
verbs return canned data, so the handler is exercised end to end through an
aiohttp TestClient without touching WakaTime.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.wakatime as wt
import kiro_crew.wakatime.service as wt_service
from kiro_crew.dashboard.handlers.wakatime import _csv_safe, _rows_by_project


class _StubClient:
    def __init__(
        self,
        *,
        summaries: list | None = None,
        stats: dict | None = None,
        fail: bool = False,
    ) -> None:
        self._summaries = summaries or []
        self._stats = stats or {}
        self._fail = fail
        self.closed = False

    async def fetch_summaries(self, start: str, end: str, *, project: Any = None) -> list:
        if self._fail:
            from kiro_crew.wakatime import WakaTimeUnavailableError

            raise WakaTimeUnavailableError("stub: summaries request failed")
        return self._summaries

    async def fetch_stats(self, wakatime_range: str = "last_7_days") -> dict:
        if self._fail:
            from kiro_crew.wakatime import WakaTimeUnavailableError

            raise WakaTimeUnavailableError("stub: stats request failed")
        return self._stats

    async def close(self) -> None:
        self.closed = True


def _app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/wakatime/stats", wt.api_wakatime_stats)
    app.router.add_get("/api/wakatime/export", wt.api_wakatime_export)
    return app


@pytest.mark.asyncio
async def test_export_csv_groups_by_project() -> None:
    summaries = [
        {
            "projects": [
                {"name": "oneka", "total_seconds": 3600},
                {"name": "noscere", "total_seconds": 1800},
            ]
        },
        {"projects": [{"name": "oneka", "total_seconds": 1800}]},
    ]
    stub = _StubClient(summaries=summaries)
    with patch.object(wt_service, "build_client", return_value=stub):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=2026-09-01&end=2026-09-03")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/csv")
            assert "attachment" in resp.headers["Content-Disposition"]
            body = await resp.text()
    lines = body.strip().splitlines()
    assert lines[0] == "project,hours,seconds"
    # oneka: 5400s = 1.5h sorts first
    assert lines[1].startswith("oneka,1.5,5400")
    assert lines[2].startswith("noscere,0.5,1800")
    assert stub.closed is True


@pytest.mark.asyncio
async def test_stats_returns_payload_when_configured() -> None:
    stub = _StubClient(stats={"languages": [{"name": "Python", "total_seconds": 3600}]})
    with patch.object(wt_service, "build_client", return_value=stub):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/stats?range=last_7_days")
            assert resp.status == 200
            data = await resp.json()
    assert data == {
        "configured": True,
        "range": "last_7_days",
        "stats": {"languages": [{"name": "Python", "total_seconds": 3600}]},
    }
    assert stub.closed is True


@pytest.mark.asyncio
async def test_stats_empty_state_when_unconfigured() -> None:
    with patch.object(wt_service, "build_client", return_value=None):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/stats")
            assert resp.status == 200
            assert await resp.json() == {"configured": False}


@pytest.mark.asyncio
async def test_stats_rejects_unknown_range() -> None:
    with patch.object(wt_service, "build_client", return_value=_StubClient()):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/stats?range=all_time")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_range"


@pytest.mark.asyncio
async def test_stats_returns_502_when_upstream_fails() -> None:
    stub = _StubClient(fail=True)
    with patch.object(wt_service, "build_client", return_value=stub):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/stats?range=last_7_days")
            assert resp.status == 502
            assert (await resp.json())["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_export_json_format() -> None:
    summaries = [{"projects": [{"name": "oneka", "total_seconds": 7200}]}]
    with patch.object(wt_service, "build_client", return_value=_StubClient(summaries=summaries)):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get(
                "/api/wakatime/export?start=2026-09-01&end=2026-09-02&format=json"
            )
            assert resp.status == 200
            # JSON export downloads as a file, so callers do not lose the
            # dashboard to a raw blob in the same tab.
            assert "attachment" in resp.headers["Content-Disposition"]
            assert resp.headers["Content-Type"].startswith("application/json")
            data = await resp.json()
    assert data["configured"] is True
    assert data["projects"] == [{"project": "oneka", "seconds": 7200.0, "hours": 2.0}]


@pytest.mark.asyncio
async def test_export_rejects_bad_format() -> None:
    with patch.object(wt_service, "build_client", return_value=_StubClient()):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get(
                "/api/wakatime/export?start=2026-09-01&end=2026-09-02&format=pdf"
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_format"


@pytest.mark.asyncio
async def test_export_rejects_bad_date() -> None:
    with patch.object(wt_service, "build_client", return_value=_StubClient()):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=2026-9-1&end=2026-09-02")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_date"


@pytest.mark.asyncio
async def test_export_rejects_start_after_end() -> None:
    with patch.object(wt_service, "build_client", return_value=_StubClient()):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=2026-09-05&end=2026-09-01")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_range"


@pytest.mark.asyncio
async def test_export_empty_state_when_unconfigured() -> None:
    with patch.object(wt_service, "build_client", return_value=None):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=2026-09-01&end=2026-09-02")
            assert resp.status == 200
            assert await resp.json() == {"configured": False}


@pytest.mark.asyncio
async def test_export_returns_502_when_summaries_request_fails() -> None:
    stub = _StubClient(fail=True)
    with patch.object(wt_service, "build_client", return_value=stub):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=2026-09-01&end=2026-09-02")
            assert resp.status == 502
            assert (await resp.json())["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_export_empty_range_is_200_csv_header_only() -> None:
    stub = _StubClient(summaries=[])
    with patch.object(wt_service, "build_client", return_value=stub):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=2026-09-01&end=2026-09-02")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/csv")
            body = await resp.text()
    assert body.strip().splitlines() == ["project,hours,seconds"]


@pytest.mark.asyncio
async def test_export_rejects_invalid_calendar_date() -> None:
    stub = _StubClient(summaries=[{"projects": [{"name": "a", "total_seconds": 60}]}])
    with patch.object(wt_service, "build_client", return_value=stub):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=2026-02-30&end=2026-03-01")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_date"


@pytest.mark.asyncio
async def test_export_rejects_compact_date() -> None:
    stub = _StubClient(summaries=[{"projects": [{"name": "a", "total_seconds": 60}]}])
    with patch.object(wt_service, "build_client", return_value=stub):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=20260901&end=2026-09-02")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_date"


def test_rows_by_project_folds_and_sorts() -> None:
    summaries = [
        {"projects": [{"name": "a", "total_seconds": 100}, {"name": "b", "total_seconds": 300}]},
        {"projects": [{"name": "a", "total_seconds": 50}]},
        {"projects": None},
        {},
    ]
    rows = _rows_by_project(summaries)
    assert rows == [
        {"project": "b", "seconds": 300.0, "hours": round(300 / 3600, 4)},
        {"project": "a", "seconds": 150.0, "hours": round(150 / 3600, 4)},
    ]


def test_csv_safe_neutralizes_leading_formula_triggers() -> None:
    assert _csv_safe("=cmd|'/C calc'!A0") == "'=cmd|'/C calc'!A0"
    assert _csv_safe("+1+1") == "'+1+1"
    assert _csv_safe("-2") == "'-2"
    assert _csv_safe("@SUM(A1)") == "'@SUM(A1)"
    assert _csv_safe("\tx") == "'\tx"
    assert _csv_safe("my-project") == "my-project"
    assert _csv_safe("") == ""


# ── OAuth flow handlers ──

from unittest.mock import AsyncMock  # noqa: E402

import kiro_crew.dashboard.handlers_system as _hsys  # noqa: E402
import kiro_crew.wakatime.oauth as wt_oauth  # noqa: E402


def _oauth_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/wakatime/oauth/authorize", wt.api_wakatime_oauth_authorize)
    app.router.add_get("/api/wakatime/oauth/callback", wt.api_wakatime_oauth_callback)
    app.router.add_post("/api/wakatime/oauth/disconnect", wt.api_wakatime_oauth_disconnect)
    app.router.add_get("/api/wakatime/oauth/status", wt.api_wakatime_oauth_status)
    return app


def _allow_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_hsys, "_require_owner", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_authorize_returns_url_and_sets_state_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_owner(monkeypatch)
    monkeypatch.setattr(
        wt_oauth,
        "build_authorize_url",
        lambda redirect, **_: ("https://wakatime.com/oauth/authorize?x=1", "st8"),
    )
    async with TestClient(TestServer(_oauth_app())) as client:
        resp = await client.post("/api/wakatime/oauth/authorize")
        assert resp.status == 200
        assert (await resp.json())["authorize_url"].startswith(
            "https://wakatime.com/oauth/authorize"
        )
        assert "wakatime_oauth_state" in resp.cookies


@pytest.mark.asyncio
async def test_authorize_state_cookie_secure_behind_https_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_owner(monkeypatch)
    monkeypatch.setattr(
        wt_oauth,
        "build_authorize_url",
        lambda redirect, **_: ("https://wakatime.com/oauth/authorize?x=1", "st8"),
    )
    async with TestClient(TestServer(_oauth_app())) as client:
        # The TestClient peer is loopback, which is what is_https_request requires
        # before trusting X-Forwarded-Proto, so a TLS-terminating proxy forwarding
        # plain HTTP still yields a Secure cookie.
        resp = await client.post(
            "/api/wakatime/oauth/authorize", headers={"X-Forwarded-Proto": "https"}
        )
        assert resp.status == 200
        cookie = resp.cookies["wakatime_oauth_state"]
        assert cookie["secure"]


@pytest.mark.asyncio
async def test_authorize_400_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_owner(monkeypatch)

    def _boom(_redirect: str, **_: object) -> None:
        raise wt_oauth.WakaTimeOAuthError("no client id", code="not_configured")

    monkeypatch.setattr(wt_oauth, "build_authorize_url", _boom)
    async with TestClient(TestServer(_oauth_app())) as client:
        resp = await client.post("/api/wakatime/oauth/authorize")
        assert resp.status == 400
        assert (await resp.json())["code"] == "not_configured"


@pytest.mark.asyncio
async def test_status_reports_connected_and_client_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_owner(monkeypatch)
    monkeypatch.setattr(wt_oauth, "read_access_token", lambda: "stored-access")

    class _Cfg:
        class wakatime:
            oauth_client_id = "client-123"

    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda cls: _Cfg())
    )
    async with TestClient(TestServer(_oauth_app())) as client:
        resp = await client.get("/api/wakatime/oauth/status")
        assert resp.status == 200
        assert await resp.json() == {"connected": True, "client_configured": True}


@pytest.mark.asyncio
async def test_status_reports_disconnected_when_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_owner(monkeypatch)
    monkeypatch.setattr(wt_oauth, "read_access_token", lambda: "")

    class _Cfg:
        class wakatime:
            oauth_client_id = ""

    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda cls: _Cfg())
    )
    async with TestClient(TestServer(_oauth_app())) as client:
        resp = await client.get("/api/wakatime/oauth/status")
        assert resp.status == 200
        assert await resp.json() == {"connected": False, "client_configured": False}


@pytest.mark.asyncio
async def test_callback_redirects_error_on_state_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_owner(monkeypatch)
    async with TestClient(TestServer(_oauth_app())) as client:
        # No state cookie set, so the echoed state cannot match.
        resp = await client.get(
            "/api/wakatime/oauth/callback?code=abc&state=forged", allow_redirects=False
        )
        assert resp.status == 302
        assert "wakatime_oauth=error" in resp.headers["Location"]
        assert "state_mismatch" in resp.headers["Location"]


@pytest.mark.asyncio
async def test_callback_exchanges_and_redirects_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_owner(monkeypatch)
    monkeypatch.setattr(
        wt_oauth,
        "exchange_code",
        AsyncMock(
            return_value=wt_oauth.OAuthTokens(access_token="a", refresh_token="r", expires_at=1)
        ),
    )
    monkeypatch.setattr(wt_oauth, "store_tokens", lambda _t: None)
    async with TestClient(TestServer(_oauth_app())) as client:
        client.session.cookie_jar.update_cookies({"wakatime_oauth_state": "good"})
        resp = await client.get(
            "/api/wakatime/oauth/callback?code=abc&state=good", allow_redirects=False
        )
        assert resp.status == 302
        assert "wakatime_oauth=connected" in resp.headers["Location"]


@pytest.mark.asyncio
async def test_disconnect_revokes_and_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_owner(monkeypatch)
    revoke = AsyncMock()
    cleared: list[bool] = []
    monkeypatch.setattr(wt_oauth, "revoke_tokens", revoke)
    monkeypatch.setattr(wt_oauth, "clear_tokens", lambda: cleared.append(True))
    async with TestClient(TestServer(_oauth_app())) as client:
        resp = await client.post("/api/wakatime/oauth/disconnect")
        assert resp.status == 200
        assert (await resp.json()) == {"disconnected": True}
    revoke.assert_awaited_once()
    assert cleared == [True]


@pytest.mark.asyncio
async def test_disconnect_clears_even_when_revoke_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_owner(monkeypatch)

    async def _revoke_boom() -> None:
        raise asyncio.TimeoutError

    cleared: list[bool] = []
    monkeypatch.setattr(wt_oauth, "revoke_tokens", _revoke_boom)
    monkeypatch.setattr(wt_oauth, "clear_tokens", lambda: cleared.append(True))
    async with TestClient(TestServer(_oauth_app())) as client:
        resp = await client.post("/api/wakatime/oauth/disconnect")
        # The provider revoke blew up, but the local tokens must still be gone,
        # so the request surfaces a server error rather than falsely reporting a
        # clean disconnect (aiohttp maps the uncaught TimeoutError to 504).
        assert resp.status >= 500
    assert cleared == [True]


def _mock_config(monkeypatch: pytest.MonkeyPatch, dashboard_url: str) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    class _Dash:
        url = dashboard_url

    class _Cfg:
        dashboard = _Dash()

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))


def test_oauth_redirect_uri_prefers_configured_dashboard_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiohttp.test_utils import make_mocked_request

    _mock_config(monkeypatch, "https://kiro.example.com:8443")
    request = make_mocked_request(
        "GET", "/api/wakatime/oauth/authorize", headers={"Host": "attacker.example"}
    )
    uri = wt._oauth_redirect_uri(request)
    assert uri == "https://kiro.example.com:8443/api/wakatime/oauth/callback"


def test_oauth_redirect_uri_falls_back_to_request_origin_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiohttp.test_utils import make_mocked_request

    _mock_config(monkeypatch, "")
    request = make_mocked_request(
        "GET", "/api/wakatime/oauth/authorize", headers={"Host": "localhost:5476"}
    )
    uri = wt._oauth_redirect_uri(request)
    assert uri == "http://localhost:5476/api/wakatime/oauth/callback"
