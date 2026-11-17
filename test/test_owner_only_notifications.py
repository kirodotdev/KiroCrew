"""Notes on an owner-only channel reach the machine owner alone, on every transport.

A ``system.relay`` note names the Team Crews the machine owner's own sign-in can
open (``relay_viewer/crew_notices.py``). A Shared Crew teammate passes the owner
gate (``shared_crew.teammate_access``) and has an owner socket, but must never read
these: the notification list, a change by name, the WebSocket fan-out and the SSE
stream all withhold them from anyone but the machine owner.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from token_auth_helpers import clear_nonces  # noqa: F401 -- the autouse fixture registers by import

from kiro_crew.dashboard import ws_event_scope
from kiro_crew.dashboard.kiro_relay.caller import CrewKind, HostBinding
from kiro_crew.dashboard.kiro_relay.ingress import (
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.shared_crew.teammate_access import TEAMMATE_WS_FLAG
from kiro_crew.testing.wait import finished

OWNER = "kiro-user.owner-0001"
TEAMMATE = "kiro-user.teammate-0002"
#: A note naming a Team Crew the owner's own sign-in can open.
RELAY_NOTE = {
    "source": "system",
    "channel": "system.relay",
    "title": "You've been added to Build box",
}


@pytest.fixture
def withheld(monkeypatch) -> list[str]:
    """The event types withheld as owner-only, as their SEL rows record them."""
    rows: list[str] = []

    def record(app: str, event_type: str, reason: str) -> None:
        if reason == ws_event_scope.OWNER_ONLY_NOTE:
            rows.append(event_type)

    monkeypatch.setattr(ws_event_scope, "_audit_deny", record)
    return rows


def _team() -> RelayIngress:
    ingress = RelayIngress(enabled=True)
    ingress.bind_host(HostBinding(crew_kind=CrewKind.TEAM))
    return ingress


def _personal() -> RelayIngress:
    ingress = RelayIngress(enabled=True)
    ingress.bind_host(HostBinding(crew_kind=CrewKind.PERSONAL, owner_user_id=OWNER))
    return ingress


def _app(state, ingress: RelayIngress) -> web.Application:
    from kiro_crew.dashboard.handlers import messaging, updates

    app = web.Application(
        middlewares=[
            relay_ingress_middleware(strict_internal_paths=_STRICT_INTERNAL_API_PATHS),
            token_auth_middleware(port=5476),
        ]
    )
    app["state"] = state
    app["allowed_origins"] = set()
    app[RELAY_INGRESS_APP_KEY] = ingress
    app.router.add_get("/api/notifications", messaging.api_notifications)
    app.router.add_post("/api/notifications/ack", messaging.api_notification_ack)
    app.router.add_post("/api/notifications/clear", messaging.api_notifications_clear)
    app.router.add_post("/api/notifications/ack-all", messaging.api_notifications_ack_all)
    app.router.add_get("/api/stream", updates.api_stream)
    return app


def _who(who: str) -> tuple[RelayIngress, str]:
    return (_personal(), OWNER) if who == "owner" else (_team(), TEAMMATE)


def _state(tmp_path, monkeypatch, who: str):
    state = _make_state(tmp_path)
    if who == "owner":
        # The relay names the personal Crew's owner by their Kiro user id.
        state.owner_id = f"relay:{OWNER}"
    monkeypatch.setattr(state, "status_snapshot", lambda **_kwargs: {})
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "teammate"])
async def test_the_notification_list_keeps_team_crew_notes_for_the_owner_only(
    tmp_path, monkeypatch, withheld, who
):
    state = _state(tmp_path, monkeypatch, who)
    state._notification_log = [
        {"ts": "1", "source": "system", "channel": "system.agent", "body": "done", "acked": False},
        {"ts": "2", "acked": False, **RELAY_NOTE},
    ]
    ingress, user = _who(who)
    async with TestClient(TestServer(_app(state, ingress))) as client:
        resp = await client.get(
            "/api/notifications", headers=ingress.stamp_headers([], relay_user=user)
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    channels = [note["channel"] for note in body["notifications"]]
    if who == "owner":
        assert channels == ["system.agent", "system.relay"]
        assert withheld == []
    else:
        assert channels == ["system.agent"]
        assert body["unread"] == 1
        assert withheld == ["notification"]


@pytest.mark.asyncio
async def test_a_teammate_cannot_change_a_team_crew_note_by_name(tmp_path, monkeypatch, withheld):
    state = _state(tmp_path, monkeypatch, "teammate")
    state._notification_log = [{"ts": "9", "acked": False, **RELAY_NOTE}]
    ingress = _team()
    async with TestClient(TestServer(_app(state, ingress))) as client:
        resp = await client.post(
            "/api/notifications/ack",
            json={"ts": "9"},
            headers=ingress.stamp_headers([], relay_user=TEAMMATE),
        )
        assert resp.status == 403, await resp.text()
    assert state._notification_log[0]["acked"] is False
    assert withheld == ["notifications.ack"]


class _WS:
    def __init__(self, flags: dict) -> None:
        self.closed = False
        self._flags = flags

    def get(self, key, default=None):
        return self._flags.get(key, default)

    def __setitem__(self, key, value):
        self._flags[key] = value

    def pop(self, key, default=None):
        return self._flags.pop(key, default)


def test_a_team_crew_note_reaches_only_the_machine_owners_socket(tmp_path, monkeypatch, withheld):
    state = _make_state(tmp_path)
    owner = _WS({"_is_dashboard_user": True})
    # A teammate's socket is an owner socket too, so membership alone must not do.
    teammate = _WS({"_is_dashboard_user": True, TEAMMATE_WS_FLAG: True})
    other = _WS({"_is_dashboard_user": True})  # a non-owner dashboard session
    state.register_ws(owner, owner=True)
    state.register_ws(teammate, owner=True)
    state.register_ws(other, owner=False)
    sent: list = []
    monkeypatch.setattr(state, "_spawn_ws_send", lambda ws, _payload: sent.append(ws))
    state._send_ws_all("notification", dict(RELAY_NOTE), "{}")
    assert owner in sent
    assert teammate not in sent and other not in sent
    assert withheld == ["notification", "notification"]


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "teammate"])
async def test_the_sse_stream_keeps_team_crew_notes_for_the_owner_only(
    tmp_path, monkeypatch, withheld, who
):
    from kiro_crew.dashboard.handlers import updates

    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    state = _state(tmp_path, monkeypatch, who)
    ingress, user = _who(who)
    try:
        async with TestClient(TestServer(_app(state, ingress))) as client:
            resp = await client.get(
                "/api/stream", headers=ingress.stamp_headers([], relay_user=user)
            )
            assert resp.status == 200
            first = await finished(resp.content.readuntil(b"\n\n"), "the first SSE frame")
            assert first.startswith(b"event: dashboard")
            state._broadcast(dict(RELAY_NOTE))
            state._broadcast({"source": "system", "body": "fence"})
            blocks: list[bytes] = []
            while b"fence" not in (blocks[-1] if blocks else b""):
                blocks.append(await finished(resp.content.readuntil(b"\n\n"), "an SSE frame"))
            resp.close()
    finally:
        stopped.set()
    got = [block for block in blocks if b"Build box" in block]
    if who == "owner":
        assert got
        assert withheld == []
    else:
        assert not got
        assert withheld == ["notification"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/notifications/clear", "/api/notifications/ack-all"])
async def test_a_teammates_filtered_change_that_cannot_be_saved_changes_nothing(
    tmp_path, monkeypatch, path
):
    state = _state(tmp_path, monkeypatch, "teammate")
    log = [
        {"ts": "1", "source": "system", "channel": "system.agent", "body": "done", "acked": False},
        {"ts": "2", "acked": False, **RELAY_NOTE},
    ]
    state._notification_log = [dict(note) for note in log]
    sent: list = []
    monkeypatch.setattr(state, "broadcast_ws", lambda kind, data: sent.append((kind, data)))
    monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", lambda notes: False)
    ingress = _team()
    async with TestClient(TestServer(_app(state, ingress))) as client:
        resp = await client.post(path, headers=ingress.stamp_headers([], relay_user=TEAMMATE))
        assert resp.status == 500, await resp.text()
        assert (await resp.json())["code"] == "persist_failed"
    assert state._notification_log == log
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/notifications/clear"])
async def test_a_note_delivered_during_a_failed_filtered_change_is_kept(
    tmp_path, monkeypatch, path
):
    """The write goes first and memory changes only once it lands, so a note that
    arrives while it is out survives a write that fails, and nothing it read changes."""
    state = _state(tmp_path, monkeypatch, "teammate")
    state._notification_log = [
        {"ts": "1", "source": "system", "channel": "system.agent", "body": "done", "acked": False},
        {"ts": "2", "acked": False, **RELAY_NOTE},
    ]
    late = {
        "ts": "3",
        "source": "system",
        "channel": "system.agent",
        "body": "late",
        "acked": False,
    }

    def deliver_then_fail(notes) -> bool:
        state._notification_log.append(late)
        return False

    monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", deliver_then_fail)
    ingress = _team()
    async with TestClient(TestServer(_app(state, ingress))) as client:
        resp = await client.post(path, headers=ingress.stamp_headers([], relay_user=TEAMMATE))
        assert resp.status == 500, await resp.text()
    assert [note["ts"] for note in state._notification_log] == ["1", "2", "3"]
    assert [note["acked"] for note in state._notification_log] == [False, False, False]


@pytest.mark.asyncio
async def test_a_teammates_clear_keeps_a_note_delivered_while_it_is_written(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch, "teammate")
    state._notification_log = [
        {"ts": "1", "source": "system", "channel": "system.agent", "body": "done", "acked": False},
        {"ts": "2", "acked": False, **RELAY_NOTE},
    ]
    late = {
        "ts": "3",
        "source": "system",
        "channel": "system.agent",
        "body": "late",
        "acked": False,
    }
    written: list = []

    def deliver_then_write(notes) -> bool:
        state._notification_log.append(late)
        written.append([note["ts"] for note in notes])
        return True

    monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", deliver_then_write)
    ingress = _team()
    async with TestClient(TestServer(_app(state, ingress))) as client:
        resp = await client.post(
            "/api/notifications/clear", headers=ingress.stamp_headers([], relay_user=TEAMMATE)
        )
        assert resp.status == 200, await resp.text()
    assert written == [["2"]]
    assert [note["ts"] for note in state._notification_log] == ["2", "3"]


@pytest.mark.asyncio
async def test_a_failed_filtered_ack_all_keeps_an_ack_made_while_it_was_written(
    tmp_path, monkeypatch
):
    state = _state(tmp_path, monkeypatch, "teammate")
    state._notification_log = [
        {"ts": "1", "source": "system", "channel": "system.agent", "body": "done", "acked": False},
        {"ts": "2", "acked": False, **RELAY_NOTE},
    ]

    def acked_meanwhile_then_fail(notes) -> bool:
        state._notification_log[0]["acked"] = True  # another view acked this one
        return False

    monkeypatch.setattr(
        "kiro_crew.dashboard.state._rewrite_notifications", acked_meanwhile_then_fail
    )
    ingress = _team()
    async with TestClient(TestServer(_app(state, ingress))) as client:
        resp = await client.post(
            "/api/notifications/ack-all", headers=ingress.stamp_headers([], relay_user=TEAMMATE)
        )
        assert resp.status == 500, await resp.text()
    assert [note["acked"] for note in state._notification_log] == [True, False]
