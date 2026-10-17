"""Tests for the WakaTime REST client.

No network: a fake aiohttp-style session records the calls and returns canned
responses. The API key is a placeholder; several tests assert it never reaches
a log line, since a WakaTime error or a request URL can carry it.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import aiohttp
import pytest

from kiro_crew.wakatime.client import (
    WakaTimeAuthError,
    WakaTimeClient,
    WakaTimeUnavailableError,
)

pytestmark = pytest.mark.asyncio

_FAKE_KEY = "waka_fake_0123456789abcdef"


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
    """Records request calls and replays a queue of responses."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    async def close(self) -> None:
        self.closed = True


def _client_with(session: _FakeSession) -> WakaTimeClient:
    client = WakaTimeClient(api_key=_FAKE_KEY)
    client._session = session  # type: ignore[assignment]
    return client


async def test_auth_header_is_basic_base64_of_the_key() -> None:
    session = _FakeSession([_FakeResp(200, {"data": []})])
    client = _client_with(session)
    await client.get_summaries("2026-09-01", "2026-09-03")
    sent = session.calls[0]["headers"]["Authorization"]
    expected = base64.b64encode(f"{_FAKE_KEY}:".encode()).decode("ascii")
    assert sent == f"Basic {expected}"


async def test_auth_header_is_bearer_when_oauth_token_configured() -> None:
    session = _FakeSession([_FakeResp(200, {"data": []})])
    client = WakaTimeClient(api_key=_FAKE_KEY, oauth_access_token="waka_tok_read")
    client._session = session  # type: ignore[assignment]
    await client.get_summaries("2026-09-01", "2026-09-03")
    assert session.calls[0]["headers"]["Authorization"] == "Bearer waka_tok_read"


async def test_auth_header_falls_back_to_basic_when_oauth_token_is_empty() -> None:
    session = _FakeSession([_FakeResp(200, {"data": []})])
    client = WakaTimeClient(api_key=_FAKE_KEY, oauth_access_token="")
    client._session = session  # type: ignore[assignment]
    await client.get_summaries("2026-09-01", "2026-09-03")
    expected = base64.b64encode(f"{_FAKE_KEY}:".encode()).decode("ascii")
    assert session.calls[0]["headers"]["Authorization"] == f"Basic {expected}"


async def test_send_heartbeats_uses_bearer_when_oauth_token_configured() -> None:
    session = _FakeSession([_FakeResp(200, {"responses": [[{}, 201]]})])
    client = WakaTimeClient(api_key=_FAKE_KEY, oauth_access_token="waka_tok_read")
    client._session = session  # type: ignore[assignment]
    await client.send_heartbeats([{"entity": "a.py", "type": "file", "time": 1}])
    assert session.calls[0]["headers"]["Authorization"] == "Bearer waka_tok_read"


async def test_get_summaries_returns_the_data_list() -> None:
    session = _FakeSession([_FakeResp(200, {"data": [{"grand_total": {"hours": 3}}]})])
    client = _client_with(session)
    result = await client.get_summaries("2026-09-01", "2026-09-03", project="oneka")
    assert result == [{"grand_total": {"hours": 3}}]
    assert session.calls[0]["params"] == {
        "start": "2026-09-01",
        "end": "2026-09-03",
        "project": "oneka",
    }


async def test_get_stats_returns_the_data_dict() -> None:
    session = _FakeSession([_FakeResp(200, {"data": {"languages": [{"name": "Python"}]}})])
    client = _client_with(session)
    result = await client.get_stats("last_7_days")
    assert result == {"languages": [{"name": "Python"}]}
    assert session.calls[0]["url"].endswith("/users/current/stats/last_7_days")


async def test_send_heartbeats_counts_accepted_rows() -> None:
    body = {"responses": [[{}, 201], [{}, 201], [{}, 400]]}
    session = _FakeSession([_FakeResp(201, body)])
    client = _client_with(session)
    accepted = await client.send_heartbeats([{"entity": "a.py", "type": "file", "time": 1.0}])
    assert accepted == 2


async def test_empty_heartbeats_makes_no_request() -> None:
    session = _FakeSession([])
    client = _client_with(session)
    assert await client.send_heartbeats([]) == 0
    assert session.calls == []


async def test_429_is_retried_once_then_succeeds() -> None:
    session = _FakeSession(
        [
            _FakeResp(429, headers={"Retry-After": "0"}),
            _FakeResp(200, {"data": []}),
        ]
    )
    client = _client_with(session)
    result = await client.get_summaries("2026-09-01", "2026-09-03")
    assert result == []
    assert len(session.calls) == 2


async def test_transport_error_degrades_to_empty() -> None:
    session = _FakeSession([aiohttp.ClientError("boom")])
    client = _client_with(session)
    assert await client.get_durations("2026-09-03") == []


async def test_fetch_summaries_returns_data_on_success() -> None:
    session = _FakeSession([_FakeResp(200, {"data": [{"grand_total": {"hours": 3}}]})])
    client = _client_with(session)
    result = await client.fetch_summaries("2026-09-01", "2026-09-03")
    assert result == [{"grand_total": {"hours": 3}}]


async def test_fetch_summaries_returns_empty_on_genuine_empty_2xx() -> None:
    session = _FakeSession([_FakeResp(200, {"data": []})])
    client = _client_with(session)
    assert await client.fetch_summaries("2026-09-01", "2026-09-03") == []


async def test_fetch_summaries_raises_on_transport_error() -> None:
    session = _FakeSession([aiohttp.ClientError("boom")])
    client = _client_with(session)
    with pytest.raises(WakaTimeUnavailableError):
        await client.fetch_summaries("2026-09-01", "2026-09-03")


async def test_fetch_summaries_raises_on_server_error() -> None:
    session = _FakeSession([_FakeResp(500)])
    client = _client_with(session)
    with pytest.raises(WakaTimeUnavailableError):
        await client.fetch_summaries("2026-09-01", "2026-09-03")


async def test_fetch_summaries_raises_when_response_has_no_data_list() -> None:
    # _api coerces a 200 with a non-JSON body to {}, which carries no data list;
    # a self-hosted backend behind a proxy can return an HTML 200. That must be a
    # failure, not a false-empty export.
    session = _FakeSession([_FakeResp(200, {})])
    client = _client_with(session)
    with pytest.raises(WakaTimeUnavailableError):
        await client.fetch_summaries("2026-09-01", "2026-09-03")


async def test_fetch_stats_returns_data_dict_on_success() -> None:
    session = _FakeSession([_FakeResp(200, {"data": {"languages": [{"name": "Python"}]}})])
    client = _client_with(session)
    result = await client.fetch_stats("last_7_days")
    assert result == {"languages": [{"name": "Python"}]}


async def test_fetch_stats_raises_on_server_error() -> None:
    session = _FakeSession([_FakeResp(500)])
    client = _client_with(session)
    with pytest.raises(WakaTimeUnavailableError):
        await client.fetch_stats("last_7_days")


async def test_fetch_stats_raises_when_response_has_no_data_dict() -> None:
    # A non-JSON 200 coerces to {} in _api; no data dict means failure, not a
    # false-empty stats payload.
    session = _FakeSession([_FakeResp(200, {})])
    client = _client_with(session)
    with pytest.raises(WakaTimeUnavailableError):
        await client.fetch_stats("last_7_days")


async def test_fetch_stats_today_uses_summaries_not_stats() -> None:
    # The stats endpoint answers 400 for "today", so the client must ask the
    # summaries endpoint instead and fold the day into the stats shape.
    day = {
        "grand_total": {"total_seconds": 5400},
        "languages": [
            {"name": "Other", "total_seconds": 1800},
            {"name": "Python", "total_seconds": 3600},
        ],
        "projects": [{"name": "oneka", "total_seconds": 5400}],
    }
    session = _FakeSession([_FakeResp(200, {"data": [day]})])
    client = _client_with(session)
    result = await client.fetch_stats("today")
    assert session.calls[0]["url"].endswith("/users/current/summaries")
    assert session.calls[0]["params"] == {"range": "Today"}
    assert result == {
        "total_seconds": 3600.0,
        "total_seconds_including_other_language": 5400.0,
        "daily_average": 3600.0,
        "daily_average_including_other_language": 5400.0,
        "languages": [
            {"name": "Python", "total_seconds": 3600.0},
            {"name": "Other", "total_seconds": 1800.0},
        ],
        "projects": [{"name": "oneka", "total_seconds": 5400.0}],
    }


async def test_fetch_stats_yesterday_maps_to_summaries_range() -> None:
    session = _FakeSession([_FakeResp(200, {"data": []})])
    client = _client_with(session)
    result = await client.fetch_stats("yesterday")
    assert session.calls[0]["params"] == {"range": "Yesterday"}
    assert result["total_seconds"] == 0.0
    assert result["languages"] == [] and result["projects"] == []


async def test_fetch_stats_today_raises_on_server_error() -> None:
    session = _FakeSession([_FakeResp(500)])
    client = _client_with(session)
    with pytest.raises(WakaTimeUnavailableError):
        await client.fetch_stats("today")


async def test_verify_raises_on_rejected_key() -> None:
    session = _FakeSession([_FakeResp(401)])
    client = _client_with(session)
    with pytest.raises(WakaTimeAuthError):
        await client.verify()


async def test_verify_returns_user_on_success() -> None:
    session = _FakeSession([_FakeResp(200, {"data": {"username": "behordeun"}})])
    client = _client_with(session)
    assert await client.verify() == {"username": "behordeun"}


async def test_api_key_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    session = _FakeSession([_FakeResp(500), _FakeResp(500)])
    client = _client_with(session)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.wakatime.client"):
        await client.get_summaries("2026-09-01", "2026-09-03")
    assert _FAKE_KEY not in caplog.text


async def test_close_is_idempotent() -> None:
    session = _FakeSession([])
    client = _client_with(session)
    await client.close()
    assert session.closed is True
    await client.close()
