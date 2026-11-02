"""A non-finite server back-off never reaches ``asyncio.sleep``.

``float("nan")`` parses, and NaN passes through ``min(max(nan, lo), hi)``
unchanged, so a ``Retry-After: nan`` header (or a JSON ``NaN`` in Telegram's
``retry_after``) would hand NaN to ``asyncio.sleep`` and park the call forever.
Each client treats a non-finite value as absent and waits its default instead.

No network: every client runs against a fake session, and ``asyncio.sleep`` is
replaced by a recorder, so the asserted value is the delay the client asked for.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest

from kiro_crew.teams.client import TeamsClient
from kiro_crew.webex import client as webex_client
from kiro_crew.webex.client import WebexClient

_NON_FINITE = ["nan", "NaN", "inf", "-inf", "Infinity"]


class _FakeResp:
    def __init__(self, status: int, body: Any = None, headers: dict | None = None) -> None:
        self.status = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def json(self, content_type: Any = None) -> Any:
        return self._body


class _FakeSession:
    def __init__(self, responses: list[_FakeResp]) -> None:
        self._responses = list(responses)
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResp:
        return self._responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> _FakeResp:
        return self._responses.pop(0)


def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def _fake_sleep(delay: float, *a: Any, **k: Any) -> None:
        slept.append(delay)

    # The recorder IS the assertion: it captures the delay handed to sleep, and
    # each test awaits nothing else, so no clock or other task depends on it.
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)  # flake-ok: delay recorder, no clock
    return slept


@pytest.mark.parametrize("raw", _NON_FINITE)
def test_teams_non_finite_retry_after_uses_the_exponential_backoff(raw: str) -> None:
    assert TeamsClient._retry_delay(raw, 0) == 0.5
    assert TeamsClient._retry_delay(raw, 1) == 1.0


@pytest.mark.parametrize(("raw", "expected"), [("2.5", 2.5), ("0.01", 0.5), ("900", 10.0)])
def test_teams_finite_retry_after_is_still_honored(raw: str, expected: float) -> None:
    assert TeamsClient._retry_delay(raw, 0) == expected


@pytest.mark.parametrize("raw", _NON_FINITE)
def test_webex_scan_wait_non_finite_retry_after_uses_the_floor(raw: str) -> None:
    assert webex_client._retry_after({"Retry-After": raw}) == webex_client._RETRY_AFTER_MIN_S


@pytest.mark.parametrize(("raw", "expected"), [("4", 4.0), ("0", 1.0), ("99", 15.0), ("", 1.0)])
def test_webex_scan_wait_finite_retry_after_is_still_honored(raw: str, expected: float) -> None:
    assert webex_client._retry_after({"Retry-After": raw}) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw", "expected"), [(r, 1.0) for r in _NON_FINITE] + [("3", 3.0)])
async def test_webex_api_429_waits_a_finite_delay(
    raw: str, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept = _record_sleeps(monkeypatch)
    c = WebexClient(token="tok")
    session = _FakeSession([_FakeResp(429, headers={"Retry-After": raw}), _FakeResp(200, {})])

    async def _ensure() -> _FakeSession:
        return session

    monkeypatch.setattr(c, "_ensure_session", _ensure)
    assert await c._api("GET", "/people/me", None) == {}
    assert slept == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw", "expected"), [(r, 1.0) for r in _NON_FINITE] + [("3", 3.0)])
async def test_wakatime_429_waits_a_finite_delay(
    raw: str, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.wakatime.client import WakaTimeClient

    slept = _record_sleeps(monkeypatch)
    client = WakaTimeClient(api_key="waka_fake_0123456789abcdef")
    client._session = _FakeSession(  # type: ignore[assignment]
        [_FakeResp(429, headers={"Retry-After": raw}), _FakeResp(200, {"data": []})]
    )
    assert await client.get_summaries("2026-09-01", "2026-09-03") == []
    assert slept == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected"),
    [(math.nan, 1.0), (math.inf, 1.0), (-math.inf, 1.0), (2.0, 2.0), (0.1, 0.5)],
)
async def test_telegram_429_waits_a_finite_delay(
    raw: float, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.telegram.client as client_mod

    monkeypatch.delenv("TELEGRAM_API_BASE_URL", raising=False)
    slept = _record_sleeps(monkeypatch)
    cli = client_mod.TelegramClient(token="123:ABC")
    limited = {"ok": False, "error_code": 429, "parameters": {"retry_after": raw}}
    session = _FakeSession([_FakeResp(429, limited), _FakeResp(200, {"ok": True, "result": 7})])

    async def _ensure() -> _FakeSession:
        return session

    monkeypatch.setattr(cli, "_ensure_session", _ensure)
    assert await cli._api("sendMessage", {"chat_id": 1, "text": "hi"}, record=False) == 7
    assert slept == [expected]
