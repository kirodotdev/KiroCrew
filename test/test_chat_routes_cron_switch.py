"""``agent.session_control`` binds a script cron on the two chat routes it writes to.

A script cron opens a dashboard session with ``POST /api/chat/slots`` and seeds
it with ``POST /api/chat``, presenting its ``cron:<job id>`` key behind the
internal secret. While the switch is off, ``private_chat_route_refusal`` refuses
that caller on those two routes with the same body the session-control routes
send. Owner and member callers keep their own gates, and every other chat route
is left alone.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers import _shared

_CRON_KEY = "cron:nightly-dispatcher"
_DISABLED_BODY = {
    "error": "session control is disabled in config (agent.session_control)",
    "code": "session_control_disabled",
}


def _internal_request(method: str, path: str, session_key: str = _CRON_KEY):
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={})
    req = make_mocked_request(method, path, app=app, headers={"X-Session-Key": session_key})
    req["internal_auth"] = True
    req["peer_verified"] = True
    return req


@pytest.fixture
def unscoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rest of the gate admits the caller, so only the switch can refuse it."""

    async def _scope(_request, _operation, **_kwargs):
        return None, None

    monkeypatch.setattr(_shared, "internal_memory_scope", _scope)


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Record SEL denials instead of writing them."""
    events: list[dict] = []
    recorder = SimpleNamespace(log_api_access=lambda **kw: events.append(kw))
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: recorder)
    return events


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch):
    """Pin ``session_control_enabled`` and count how often the gate reads it."""
    reads: list[bool] = []

    def _set(enabled: bool) -> list[bool]:
        def _read() -> bool:
            reads.append(enabled)
            return enabled

        monkeypatch.setattr(sc, "session_control_enabled", _read)
        return reads

    return _set


def _body(resp: web.Response) -> dict:
    return json.loads(resp.body)


class TestTheSwitchOff:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/api/chat/slots", "/api/chat/slots/"])
    async def test_a_cron_key_is_refused_on_opening_a_session(self, unscoped, audit, switch, path):
        switch(False)
        resp = await _shared.private_chat_route_refusal(_internal_request("POST", path))

        assert resp is not None
        assert resp.status == 403
        assert _body(resp) == _DISABLED_BODY
        (event,) = audit
        assert event["operation"] == "chat.control"
        assert event["outcome"] == "denied"
        assert event["error"] == "session_control_disabled"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/api/chat", "/api/chat?ws=1"])
    async def test_a_cron_key_is_refused_on_seeding_a_session(self, unscoped, audit, switch, path):
        switch(False)
        resp = await _shared.private_chat_route_refusal(_internal_request("POST", path))

        assert resp is not None
        assert resp.status == 403
        assert _body(resp) == _DISABLED_BODY

    @pytest.mark.asyncio
    async def test_a_cron_key_reading_the_session_list_is_not_refused_by_the_switch(
        self, unscoped, audit, switch
    ):
        reads = switch(False)

        assert (
            await _shared.private_chat_route_refusal(_internal_request("GET", "/api/chat/slots"))
            is None
        )
        assert reads == []
        assert audit == []

    @pytest.mark.asyncio
    async def test_a_dashboard_key_is_not_refused_by_the_switch(self, unscoped, audit, switch):
        reads = switch(False)
        req = _internal_request("POST", "/api/chat/slots", session_key="dashboard:chat-1")

        assert await _shared.private_chat_route_refusal(req) is None
        assert reads == []
        assert audit == []


class TestTheSwitchOn:
    @pytest.mark.asyncio
    async def test_a_cron_key_opening_a_session_is_not_refused_by_the_switch(
        self, unscoped, audit, switch
    ):
        reads = switch(True)

        assert (
            await _shared.private_chat_route_refusal(_internal_request("POST", "/api/chat/slots"))
            is None
        )
        assert reads == [True]
        assert audit == []
