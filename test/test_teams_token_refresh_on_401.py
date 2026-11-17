"""A Connector 401 on a cached token mints a fresh token and retries once.

The token's expiry is kept on the monotonic clock, which does not advance while
the host sleeps, so a token Azure AD already expired can still look fresh. A
401 must not then fail every send until the local clock catches up.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from test_teams_client import _APP_ID, _FakeResp, _FakeSession

from kiro_crew.teams.client import TeamsClient, TeamsSendError
from kiro_crew.testing.wait import await_until

_SERVICE = "https://smba.trafficmanager.net/"


def _client_with_stale_token(responses: list[_FakeResp]) -> TeamsClient:
    c = TeamsClient(app_id=_APP_ID, app_password="pw")
    c._token = "stale"
    c._token_expiry = time.monotonic() + 999
    c._session = _FakeSession(responses)  # type: ignore[assignment]
    return c


@pytest.mark.asyncio
async def test_a_401_refreshes_the_token_and_the_send_lands() -> None:
    c = _client_with_stale_token(
        [
            _FakeResp(status=401),
            _FakeResp(json_data={"access_token": "fresh", "expires_in": 3600}),
            _FakeResp(json_data={"id": "activity-1"}),
        ]
    )
    assert await c.send_message("conv-1", "hi", _SERVICE) == "activity-1"
    calls = c._session.calls  # type: ignore[union-attr]
    assert "oauth2/v2.0/token" in calls[1][0]
    assert calls[2][1]["headers"]["Authorization"] == "Bearer fresh"
    assert c.last_error == ""


@pytest.mark.asyncio
async def test_a_second_401_is_a_real_failure() -> None:
    c = _client_with_stale_token(
        [
            _FakeResp(status=401),
            _FakeResp(json_data={"access_token": "fresh", "expires_in": 3600}),
            _FakeResp(status=401),
        ]
    )
    with pytest.raises(TeamsSendError):
        await c.send_message("conv-1", "hi", _SERVICE)
    assert "401" in c.last_error
    assert len(c._session.calls) == 3  # type: ignore[union-attr]


class _TokenAwareSession:
    """The Connector answers 401 for the stale token and 200 for any other."""

    closed = False

    def __init__(self) -> None:
        self.mints = 0
        self.rejected = 0
        self.gate = asyncio.Event()

    def post(self, url: str, **kwargs: Any) -> _FakeResp:
        self.mints += 1
        return _FakeResp(json_data={"access_token": f"fresh-{self.mints}", "expires_in": 3600})

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        stale = kwargs["headers"]["Authorization"] == "Bearer stale"
        if stale:
            self.rejected += 1
            if self.rejected == 2:
                self.gate.set()
        return _GatedResp(self.gate, _FakeResp(status=401) if stale else _FakeResp(json_data={}))


class _GatedResp:
    """Holds the 401 until both sends have one, so they overlap like real turns."""

    def __init__(self, gate: asyncio.Event, resp: _FakeResp) -> None:
        self._gate = gate
        self._resp = resp

    async def __aenter__(self) -> _FakeResp:
        if self._resp.status == 401:
            await await_until(self._gate, what="both sends rejected")
        return self._resp

    async def __aexit__(self, *a: Any) -> bool:
        return False


@pytest.mark.asyncio
async def test_sends_rejected_for_one_token_share_one_mint() -> None:
    c = TeamsClient(app_id=_APP_ID, app_password="pw")
    c._token = "stale"
    c._token_expiry = time.monotonic() + 999
    session = _TokenAwareSession()
    c._session = session  # type: ignore[assignment]
    first = asyncio.ensure_future(c.send_typing("conv-1", _SERVICE))
    second = asyncio.ensure_future(c.send_typing("conv-2", _SERVICE))
    await await_until(asyncio.gather(first, second), what="both sends")
    assert session.mints == 1
    assert c._token == "fresh-1"
