"""Tests for the WakaTime dashboard export handler.

No network: ``service.build_client`` is patched to a stub client whose read
verbs return canned data, so the handler is exercised end to end through an
aiohttp TestClient without touching WakaTime.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.wakatime as wt
import kiro_crew.wakatime.service as wt_service
from kiro_crew.dashboard.handlers.wakatime import _csv_safe, _rows_by_project

pytestmark = pytest.mark.asyncio


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


async def test_stats_empty_state_when_unconfigured() -> None:
    with patch.object(wt_service, "build_client", return_value=None):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/stats")
            assert resp.status == 200
            assert await resp.json() == {"configured": False}


async def test_stats_rejects_unknown_range() -> None:
    with patch.object(wt_service, "build_client", return_value=_StubClient()):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/stats?range=all_time")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_range"


async def test_stats_returns_502_when_upstream_fails() -> None:
    stub = _StubClient(fail=True)
    with patch.object(wt_service, "build_client", return_value=stub):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/stats?range=last_7_days")
            assert resp.status == 502
            assert (await resp.json())["code"] == "upstream_unavailable"


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


async def test_export_rejects_bad_format() -> None:
    with patch.object(wt_service, "build_client", return_value=_StubClient()):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get(
                "/api/wakatime/export?start=2026-09-01&end=2026-09-02&format=pdf"
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_format"


async def test_export_rejects_bad_date() -> None:
    with patch.object(wt_service, "build_client", return_value=_StubClient()):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=2026-9-1&end=2026-09-02")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_date"


async def test_export_rejects_start_after_end() -> None:
    with patch.object(wt_service, "build_client", return_value=_StubClient()):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=2026-09-05&end=2026-09-01")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_range"


async def test_export_empty_state_when_unconfigured() -> None:
    with patch.object(wt_service, "build_client", return_value=None):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=2026-09-01&end=2026-09-02")
            assert resp.status == 200
            assert await resp.json() == {"configured": False}


async def test_export_returns_502_when_summaries_request_fails() -> None:
    stub = _StubClient(fail=True)
    with patch.object(wt_service, "build_client", return_value=stub):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=2026-09-01&end=2026-09-02")
            assert resp.status == 502
            assert (await resp.json())["code"] == "upstream_unavailable"


async def test_export_empty_range_is_200_csv_header_only() -> None:
    stub = _StubClient(summaries=[])
    with patch.object(wt_service, "build_client", return_value=stub):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=2026-09-01&end=2026-09-02")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/csv")
            body = await resp.text()
    assert body.strip().splitlines() == ["project,hours,seconds"]


async def test_export_rejects_invalid_calendar_date() -> None:
    stub = _StubClient(summaries=[{"projects": [{"name": "a", "total_seconds": 60}]}])
    with patch.object(wt_service, "build_client", return_value=stub):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/wakatime/export?start=2026-02-30&end=2026-03-01")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_date"


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
