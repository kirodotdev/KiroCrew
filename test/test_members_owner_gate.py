"""The member detail reads and the member event-log frames are the owner's.

``/api/members``, ``/api/members/{slug}/activity`` and
``/api/members/{slug}/projections`` answer the owner's dashboard only, the same
boundary as the briefing and rules reads. A non-owner dashboard session (a
Telegram, Teams or Webex allowlist link) holds an empty app claim like the
owner's, so the app-caller guard alone admits it; the owner gate refuses it
before any config or event-log read. The live ``member_projection`` /
``members_subscribed`` frames carry the same data, so the WS fan-out refuses
them to a non-owner dashboard socket too.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.dashboard.handlers import members as m
from kiro_crew.eventlog import types as eventlog_types

CREW = "code-reviewer"
SLUG = "code-reviewer"
OWNER = "U_OWNER"

HANDLERS = [
    pytest.param(m.api_members, "/api/members", {}, id="list"),
    pytest.param(
        m.api_member_activity,
        f"/api/members/{SLUG}/activity?member={CREW}",
        {"slug": SLUG},
        id="activity",
    ),
    pytest.param(
        m.api_member_projections,
        f"/api/members/{SLUG}/projections?member={CREW}",
        {"slug": SLUG},
        id="projections",
    ),
]


def _fake_cfg():
    return SimpleNamespace(
        agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew", description="d", model="m1")},
        default_agent=CREW,
        memory_stores={},
        workspaces={"default": SimpleNamespace(dir="workspace")},
        default_workspace="default",
        degraded_sections=frozenset(),
    )


def _request(tmp_path, path, match, *, owner_id, app_claim, user):
    state = _make_state(tmp_path)
    state.owner_id = owner_id
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("GET", path, app=app, match_info=match)
    req["app"] = app_claim
    req["user"] = user
    return req


async def _call(handler, req):
    with patch.object(m.KiroCrewConfig, "load", return_value=_fake_cfg()):
        resp = await handler(req)
    return resp.status, json.loads(resp.body)


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler", "path", "match"), HANDLERS)
async def test_owner_is_served(tmp_path, handler, path, match):
    req = _request(tmp_path, path, match, owner_id=OWNER, app_claim="", user=OWNER)
    status, _ = await _call(handler, req)
    assert status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler", "path", "match"), HANDLERS)
async def test_owner_grant_is_audited_and_a_denial_is_not_recorded_as_one(
    tmp_path, monkeypatch, handler, path, match
):
    records: list[dict] = []
    sink = SimpleNamespace(log_api_access=lambda **kw: records.append(kw))
    monkeypatch.setattr(m, "_sel", lambda: sink)

    req = _request(tmp_path, path, match, owner_id=OWNER, app_claim="", user=OWNER)
    status, _ = await _call(handler, req)
    assert status == 200
    granted = [r for r in records if r.get("outcome") == "allowed"]
    assert len(granted) == 1 and granted[0]["operation"].endswith(".read")

    records.clear()
    req = _request(tmp_path, path, match, owner_id=OWNER, app_claim="", user="123456789")
    status, _ = await _call(handler, req)
    assert status == 403
    assert not [r for r in records if r.get("outcome") == "allowed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler", "path", "match"), HANDLERS)
async def test_local_subject_is_served_when_no_owner_is_configured(tmp_path, handler, path, match):
    req = _request(tmp_path, path, match, owner_id="", app_claim="", user="local-app")
    status, _ = await _call(handler, req)
    assert status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler", "path", "match"), HANDLERS)
async def test_non_owner_is_refused_before_any_read(tmp_path, handler, path, match):
    req = _request(tmp_path, path, match, owner_id=OWNER, app_claim="", user="123456789")

    def _no_read(*_a, **_k):
        raise AssertionError("a refused caller must not reach the config or the event log")

    with (
        patch.object(m.KiroCrewConfig, "load", side_effect=_no_read),
        patch.object(m, "load_config_with_content_stamp", side_effect=_no_read),
        patch("kiro_crew.eventlog.service.get_service", side_effect=_no_read),
    ):
        resp = await handler(req)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "owner_only"


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler", "path", "match"), HANDLERS)
async def test_app_token_stays_not_found(tmp_path, handler, path, match):
    req = _request(tmp_path, path, match, owner_id=OWNER, app_claim="someapp", user="someapp")
    status, body = await _call(handler, req)
    assert status == 404
    assert body["code"] == "not_found"


class _WS:
    """Dashboard-user socket double that records the frames handed to it."""

    def __init__(self) -> None:
        self.closed = False
        self._flags: dict = {"_is_dashboard_user": True}

    def get(self, key, default=None):
        return self._flags.get(key, default)

    def __setitem__(self, key, value):
        self._flags[key] = value

    def pop(self, key, default=None):
        return self._flags.pop(key, default)


@pytest.mark.parametrize(
    "msg_type", [eventlog_types.WS_MEMBER_PROJECTION, eventlog_types.WS_MEMBERS_SUBSCRIBED]
)
def test_member_log_frames_reach_only_the_owner_socket(tmp_path, monkeypatch, msg_type):
    state = _make_state(tmp_path)
    owner_ws, other_ws = _WS(), _WS()
    state.register_ws(owner_ws, owner=True)
    state.register_ws(other_ws, owner=False)
    sent: list[object] = []
    monkeypatch.setattr(state, "_spawn_ws_send", lambda ws, _payload: sent.append(ws))

    state._send_ws_all(msg_type, {"slug": SLUG, "key": eventlog_types.PROJ_ROSTER}, "{}")

    assert sent == [owner_ws]


def test_other_dashboard_frames_still_reach_a_non_owner_socket(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    other_ws = _WS()
    state.register_ws(other_ws, owner=False)
    sent: list[object] = []
    monkeypatch.setattr(state, "_spawn_ws_send", lambda ws, _payload: sent.append(ws))

    state._send_ws_all("notification", {"source": "system"}, "{}")

    assert sent == [other_ws]


@pytest.mark.asyncio
async def test_the_baseline_replay_records_its_grant(tmp_path, monkeypatch):
    from kiro_crew.dashboard import ws_event_scope
    from kiro_crew.eventlog.service import get_service

    state = _make_state(tmp_path)
    ws = _WS()
    ws.send_str = _async_noop  # type: ignore[attr-defined]
    state.register_ws(ws, owner=True)
    grants: list[str] = []
    monkeypatch.setattr(ws_event_scope, "_audit_allow", lambda _who, event: grants.append(event))
    svc = get_service()

    def _last_seqs():
        state._ws_client_allowed(ws, eventlog_types.WS_MEMBER_PROJECTION, {"slug": "alice"})
        return {"alice": 3}

    monkeypatch.setattr(svc, "last_seqs", _last_seqs)
    monkeypatch.setattr(
        svc,
        "redacted_snapshot",
        lambda slug: {"asOfSeq": 3, "values": {eventlog_types.PROJ_ROSTER: {"slug": slug}}},
    )

    await state.send_members_subscribed(ws)

    assert eventlog_types.WS_MEMBER_PROJECTION in grants


async def _async_noop(_msg: str) -> None:
    return None


def test_the_connect_baseline_is_sent_to_the_owner_socket_only():
    """The connect path cannot be driven without a live socket, so the gate is read.

    ``send_members_subscribed`` writes straight to the socket and replays held
    projections the same way, so it must sit under the owner predicate rather
    than the dashboard-user flag.
    """
    source = (Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard/ws.py").read_text(
        encoding="utf-8"
    )
    call_at = source.index("await state.send_members_subscribed(ws)")
    guard_at = source.rindex("\n        if ", 0, call_at)
    block = source[guard_at:call_at].splitlines()
    assert block[1].strip() == "if owner_request:"
    assert any(
        '_audit_grant_quietly(_grant_auditee(ws, ws_app), "members_subscribed")' in line
        for line in block
    ), "the direct baseline send must record its grant"
