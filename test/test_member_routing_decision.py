"""The gateway records a select_crew routing decision in the member's activity log."""

from __future__ import annotations

import asyncio
import json
import unittest.mock
from unittest.mock import MagicMock

import pytest
from test_select_crew import _write_cfg

from kiro_crew import members

SK = "dashboard:chat-1"


def _request(body, *, session=SK, internal=True):
    req = MagicMock()
    req.headers = {"X-Session-Key": session}
    req.get = lambda k, d=None: {"internal_auth": internal}.get(k, d)
    req.can_read_body = True
    req.app = {"state": object()}

    async def _json():
        return body

    req.json = _json
    return req


@pytest.fixture()
def live():
    return {"mode": "persistent"}


@pytest.fixture()
def audits():
    return []


@pytest.fixture()
def gateway(tmp_path, monkeypatch, live, audits):
    import kiro_crew.dashboard.handlers.member_routing as h
    from kiro_crew.history import ConversationLog

    async def _resolve(state, key):
        if isinstance(live["mode"], Exception):
            raise live["mode"]
        return live["mode"]

    monkeypatch.setattr(h, "session_key_is_attested", lambda req, sk: sk == SK)
    monkeypatch.setattr(h, "resolve_session_memory_mode", _resolve)
    monkeypatch.setattr(h, "sel", lambda: MagicMock(log_api_access=lambda **kw: audits.append(kw)))
    cfg = _write_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg):
        log = ConversationLog()
        log.update_metadata(SK, {"memory_mode": "persistent"})
        yield h, log


def _decide(h, body, **kw):
    resp = asyncio.run(h.api_member_routing_decision(_request(body, **kw)))
    return resp.status, json.loads(resp.body)


def _routing_entries():
    rows = members.read_activity(members.member_slug("oncall"))
    return [r for r in rows if r.get("via") == "select_crew"]


def test_an_attested_persistent_session_records_the_decision(gateway):
    h, _ = gateway
    assert _decide(h, {"crew": "oncall"}) == (200, {"recorded": True})
    entries = _routing_entries()
    assert [(e["member"], e["decided_in"]) for e in entries] == [("oncall", SK)]


@pytest.mark.parametrize(
    "kw, status, code",
    [
        ({"internal": False}, 403, "internal_secret_required"),
        ({"session": ""}, 400, "missing_session_key"),
        ({"session": "dashboard:someone-else"}, 403, "unattested_caller"),
    ],
)
def test_an_unverified_caller_records_nothing(gateway, kw, status, code):
    h, _ = gateway
    got_status, body = _decide(h, {"crew": "oncall"}, **kw)
    assert (got_status, body.get("code")) == (status, code)
    assert _routing_entries() == []


def test_an_unknown_crew_records_nothing(gateway):
    h, _ = gateway
    status, body = _decide(h, {"crew": "ghost"})
    assert (status, body.get("code")) == (404, "unknown_crew")
    assert members.read_activity(members.member_slug("ghost")) == []


@pytest.mark.parametrize("mode", ["incognito", "temporary", ValueError("unavailable")])
def test_a_session_the_gateway_holds_private_records_nothing(gateway, live, mode):
    h, _ = gateway
    live["mode"] = mode
    assert _decide(h, {"crew": "oncall"}) == (200, {"recorded": False})
    assert _routing_entries() == []


def test_a_private_transcript_header_vetoes_a_persistent_live_mode(gateway):
    h, log = gateway
    log.update_metadata(SK, {"memory_mode": "temporary"})
    assert _decide(h, {"crew": "oncall"}) == (200, {"recorded": False})
    assert _routing_entries() == []


class _HeaderStub:
    def __init__(self, status):
        self._status = status

    def get_metadata_status(self, key):
        if isinstance(self._status, Exception):
            raise self._status
        return self._status

    def get_metadata(self, key):
        return {}


@pytest.mark.parametrize("failure", ["corrupt", "unreadable", "raises"])
def test_a_header_that_cannot_be_read_records_nothing(gateway, monkeypatch, failure):
    h, log = gateway
    if failure == "corrupt":
        log._path(SK).write_text("{not json\n", encoding="utf-8")
    else:
        status = ({}, False) if failure == "unreadable" else OSError("disk")
        monkeypatch.setattr(h, "ConversationLog", lambda: _HeaderStub(status))
    assert _decide(h, {"crew": "oncall"}) == (200, {"recorded": False})
    assert _routing_entries() == []


def test_a_subagent_session_without_a_header_records_on_its_live_mode(gateway, monkeypatch):
    h, _ = gateway
    sub = "subagent:run-1"
    monkeypatch.setattr(h, "session_key_is_attested", lambda req, sk: sk == sub)
    assert _decide(h, {"crew": "oncall"}, session=sub) == (200, {"recorded": True})
    assert [e["decided_in"] for e in _routing_entries()] == [sub]


def test_a_crew_whose_memory_cannot_bind_records_nothing(gateway, monkeypatch):
    from kiro_crew.memory_stores import UnknownMemoryStore

    h, _ = gateway
    monkeypatch.setattr(
        h, "resolve_agent_bindings", MagicMock(side_effect=UnknownMemoryStore("gone"))
    )
    status, body = _decide(h, {"crew": "oncall"})
    assert (status, body.get("code")) == (409, "crew_unavailable")
    assert _routing_entries() == []


def test_an_unattested_caller_is_audited(gateway, audits):
    h, _ = gateway
    _decide(h, {"crew": "oncall"}, session="dashboard:someone-else")
    assert [(a["caller"], a["outcome"]) for a in audits] == [("dashboard:someone-else", "denied")]


def test_the_route_is_strict_internal_and_registered():
    from aiohttp import web

    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS
    from kiro_crew.dashboard.server_runtime.mcp_routes import _register_mcp_routes

    assert "/api/member-routing" in _STRICT_INTERNAL_API_PATHS
    app = web.Application()
    _register_mcp_routes(app)
    routes = {(r.method, r.resource.canonical) for r in app.router.routes()}
    assert ("POST", "/api/member-routing/decision") in routes
