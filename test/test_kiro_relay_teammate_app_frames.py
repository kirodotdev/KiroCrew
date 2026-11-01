"""A relayed teammate has no app surface on the dashboard's live and list feeds.

The remote limits refuse a teammate every app route. These tests cover the feeds
that carry an app's output outside those routes: the dashboard WebSocket (an
app's published events, its push notifications and its notification-channel
settings), the SSE stream and the notification list. The owner, and every
non-relayed dashboard user, keep them.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_state
from token_auth_helpers import clear_nonces  # noqa: F401 -- the autouse fixture registers by import

from kiro_crew.dashboard.handlers.messaging import api_notifications
from kiro_crew.dashboard.kiro_relay.caller import (
    RELAY_CALLER_KEY,
    RELAY_TEAMMATE_WS_FLAG,
    CrewKind,
    HostBinding,
    RelayCaller,
    RelayCallerKind,
    is_relay_teammate,
)
from kiro_crew.dashboard.kiro_relay.ingress import (
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.dashboard.ws_event_scope import app_origin_frame
from kiro_crew.testing.wait import finished

OWNER = "kiro-user.owner-0001"
TEAMMATE = "kiro-user.teammate-0002"

#: Frames that carry an app's own output.
_APP_FRAMES = [
    pytest.param(
        "app_event", {"app": "mochi", "event": "chat", "data": {"text": "private"}}, id="event"
    ),
    pytest.param("notification", {"source": "app:mochi", "body": "private"}, id="notification"),
    pytest.param("notification_channel_settings", {"channel": "mochi.reminders"}, id="channel"),
]
#: The Crew's own frames, which a teammate keeps.
_CREW_FRAMES = [
    pytest.param("notification", {"source": "system", "body": "done"}, id="notification"),
    pytest.param("notification_channel_settings", {"channel": "system.approval"}, id="channel"),
    pytest.param("refresh", {"kinds": "crons"}, id="refresh"),
]


class _WS:
    """A dashboard-user socket double that the hub's fan-out can address."""

    def __init__(self, *, relay_teammate: bool) -> None:
        self.closed = False
        self._flags: dict = {"_is_dashboard_user": True, RELAY_TEAMMATE_WS_FLAG: relay_teammate}

    def get(self, key, default=None):
        return self._flags.get(key, default)

    def __setitem__(self, key, value):
        self._flags[key] = value

    def pop(self, key, default=None):
        return self._flags.pop(key, default)


def _fan_out(tmp_path, monkeypatch, msg_type: str, data: dict) -> tuple[_WS, _WS, list]:
    state = _make_state(tmp_path)
    teammate, owner = _WS(relay_teammate=True), _WS(relay_teammate=False)
    state.register_ws(teammate, owner=False)
    state.register_ws(owner, owner=True)
    sent: list = []
    monkeypatch.setattr(state, "_spawn_ws_send", lambda ws, _payload: sent.append(ws))
    state._send_ws_all(msg_type, data, "{}")
    return teammate, owner, sent


@pytest.fixture
def withheld(monkeypatch) -> list[str]:
    """The event types a relayed teammate was withheld, as their SEL rows record them."""
    from kiro_crew.dashboard import ws_event_scope

    rows: list[str] = []

    def record(app: str, event_type: str, reason: str) -> None:
        if reason == ws_event_scope.RELAY_TEAMMATE_APP_FRAME:
            assert app == ws_event_scope.DASHBOARD_USER_AUDITEE
            rows.append(event_type)

    monkeypatch.setattr(ws_event_scope, "_audit_deny", record)
    return rows


@pytest.mark.parametrize(("msg_type", "data"), _APP_FRAMES)
def test_an_app_frame_never_reaches_a_relayed_teammate_socket(
    tmp_path, monkeypatch, withheld, msg_type, data
):
    teammate, owner, sent = _fan_out(tmp_path, monkeypatch, msg_type, data)
    assert teammate not in sent
    assert owner in sent
    assert withheld == [msg_type]


@pytest.mark.parametrize(("msg_type", "data"), _CREW_FRAMES)
def test_a_crew_frame_still_reaches_a_relayed_teammate_socket(
    tmp_path, monkeypatch, withheld, msg_type, data
):
    teammate, owner, sent = _fan_out(tmp_path, monkeypatch, msg_type, data)
    assert teammate in sent and owner in sent
    assert withheld == []


@pytest.mark.parametrize(("msg_type", "data"), _APP_FRAMES)
def test_app_origin_frame_names_every_app_frame(msg_type, data):
    assert app_origin_frame(msg_type, data)


@pytest.mark.parametrize(("msg_type", "data"), _CREW_FRAMES)
def test_app_origin_frame_leaves_the_crews_own_frames(msg_type, data):
    assert not app_origin_frame(msg_type, data)


def _with_caller(kind: RelayCallerKind | None) -> web.Request:
    request = make_mocked_request("GET", "/api/ws")
    if kind is not None:
        request[RELAY_CALLER_KEY] = RelayCaller(
            kiro_user_id=TEAMMATE if kind is RelayCallerKind.TEAMMATE else OWNER, kind=kind
        )
    return request


def test_only_a_relayed_teammate_reads_as_one() -> None:
    assert is_relay_teammate(_with_caller(RelayCallerKind.TEAMMATE))
    assert not is_relay_teammate(_with_caller(RelayCallerKind.OWNER))
    assert not is_relay_teammate(_with_caller(None))
    assert not is_relay_teammate({RELAY_CALLER_KEY: "teammate"})


# ---------------------------------------------------------------------------
# Over the relay, end to end: the socket is marked, and the list and SSE feeds
# ---------------------------------------------------------------------------


def _team() -> RelayIngress:
    ingress = RelayIngress(enabled=True)
    ingress.bind_host(HostBinding(crew_kind=CrewKind.TEAM))
    return ingress


def _personal() -> RelayIngress:
    ingress = RelayIngress(enabled=True)
    ingress.bind_host(HostBinding(crew_kind=CrewKind.PERSONAL, owner_user_id=OWNER))
    return ingress


def _relay_app(state, ingress: RelayIngress) -> web.Application:
    app = web.Application(
        middlewares=[
            relay_ingress_middleware(strict_internal_paths=_STRICT_INTERNAL_API_PATHS),
            token_auth_middleware(port=5476),
        ]
    )
    app["state"] = state
    app["allowed_origins"] = set()
    app[RELAY_INGRESS_APP_KEY] = ingress
    app.router.add_get("/api/notifications", api_notifications)
    return app


_NOTES = [
    {"ts": "1", "source": "system", "body": "done", "acked": False},
    {"ts": "2", "source": "app:mochi", "body": "private", "acked": False},
    {"ts": "3", "source": "system", "body": "read", "acked": True},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "teammate"])
async def test_a_relayed_socket_is_marked_by_who_opened_it(tmp_path, monkeypatch, who):
    """``/api/ws`` marks the socket a relayed teammate opens, and only that one."""
    from kiro_crew.dashboard import ws as ws_module
    from kiro_crew.dashboard import ws_event_scope

    stopped = asyncio.Event()
    monkeypatch.setattr(ws_module, "shutdown_event", stopped)
    monkeypatch.setattr(ws_event_scope, "is_app_enabled", lambda _name: True)
    state = _make_state(tmp_path)
    monkeypatch.setattr(state, "status_snapshot", lambda **_kwargs: {})
    ingress = _personal() if who == "owner" else _team()
    user = OWNER if who == "owner" else TEAMMATE
    app = _relay_app(state, ingress)
    app.router.add_get("/api/ws", ws_module.api_ws)
    try:
        async with TestClient(TestServer(app)) as client:
            origin = str(client.make_url("/")).rstrip("/")
            app["allowed_origins"].add(origin)
            headers = dict(ingress.stamp_headers([("Origin", origin)], relay_user=user))
            socket = await client.ws_connect("/api/ws", headers=headers)
            # The connect-time slots frame proves the socket is registered.
            first = await finished(socket.receive_json(), "the connect-time slots frame")
            assert first["type"] == "slots"
            marks = [ws.get(RELAY_TEAMMATE_WS_FLAG) for ws in state._ws_clients]
            await socket.close()
    finally:
        stopped.set()
    assert marks == [who == "teammate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "teammate"])
async def test_the_notification_list_withholds_app_pushes_from_a_teammate(tmp_path, withheld, who):
    state = _make_state(tmp_path)
    state._notification_log = [dict(note) for note in _NOTES]
    state._unread_count = 3  # three non-passive deliveries since the last clear
    ingress = _personal() if who == "owner" else _team()
    user = OWNER if who == "owner" else TEAMMATE
    async with TestClient(TestServer(_relay_app(state, ingress))) as client:
        resp = await client.get(
            "/api/notifications", headers=ingress.stamp_headers([], relay_user=user)
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    sources = [note["source"] for note in body["notifications"]]
    if who == "owner":
        assert sources == ["system", "app:mochi", "system"]
        assert body["unread"] == 3
        assert withheld == []
    else:
        assert sources == ["system", "system"]
        assert body["unread"] == 2
        assert withheld == ["notification"]


class _Channels:
    """Stand-ins for the notification bus and the stored channel settings."""

    registered = {"mochi.reminders": "normal", "system.approval": "urgent"}

    def __init__(self) -> None:
        self.updates: list[str] = []

    def channels(self) -> dict[str, str]:
        return dict(self.registered)

    def all_settings(self) -> dict[str, dict]:
        return {}

    def update(self, channel: str, **_kwargs) -> dict:
        self.updates.append(channel)
        return {"muted": True}


class _SelRecorder:
    """Stands in for the SEL: keeps every row any writer logs, by method."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def __getattr__(self, method: str):
        return lambda *_args, **fields: self.rows.append((method, fields))


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "teammate"])
async def test_a_teammate_neither_sees_nor_sets_an_apps_channels(
    tmp_path, monkeypatch, withheld, who
):
    from kiro_crew.dashboard.handlers.messaging import (
        api_notification_channel_settings,
        api_notification_channels,
    )

    sel_rows = _SelRecorder()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_rows)
    state = _make_state(tmp_path)
    store = _Channels()
    state.notification_bus = store
    state.notification_channel_settings = store
    ingress = _personal() if who == "owner" else _team()
    user = OWNER if who == "owner" else TEAMMATE
    app = _relay_app(state, ingress)
    app.router.add_get("/api/notifications/channels", api_notification_channels)
    app.router.add_put("/api/notifications/channels/settings", api_notification_channel_settings)
    async with TestClient(TestServer(app)) as client:
        listed = await client.get(
            "/api/notifications/channels", headers=ingress.stamp_headers([], relay_user=user)
        )
        assert listed.status == 200, await listed.text()
        names = [row["channel"] for row in (await listed.json())["channels"]]
        statuses = {}
        for channel in ("mochi.reminders", "system.approval"):
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": channel, "muted": True},
                headers=ingress.stamp_headers([], relay_user=user),
            )
            statuses[channel] = (resp.status, (await resp.json()).get("code"))
    refusals = [
        fields
        for method, fields in sel_rows.rows
        if method == "log_api_access"
        and fields.get("operation") == "notifications.channel_settings"
    ]
    if who == "owner":
        assert names == ["mochi.reminders", "system.approval"]
        assert statuses == {"mochi.reminders": (200, None), "system.approval": (200, None)}
        assert store.updates == ["mochi.reminders", "system.approval"]
        assert withheld == [] and refusals == []
    else:
        assert names == ["system.approval"]
        assert statuses == {
            "mochi.reminders": (403, "relay_apps_teammate"),
            "system.approval": (200, None),
        }
        assert store.updates == ["system.approval"]
        # Both decisions are in the SEL: the withheld channel list and the refusal.
        assert withheld == ["notification_channel_settings"]
        assert len(refusals) == 1
        assert refusals[0]["outcome"] == "denied"
        assert refusals[0]["error"] == "relay_apps_teammate"
        assert refusals[0]["caller"] == f"relay:{TEAMMATE}"


def _feed_app(state, ingress: RelayIngress) -> web.Application:
    from kiro_crew.dashboard.handlers import messaging

    app = _relay_app(state, ingress)
    app.router.add_post("/api/notifications/clear", messaging.api_notifications_clear)
    app.router.add_post("/api/notifications/ack-all", messaging.api_notifications_ack_all)
    app.router.add_delete("/api/notifications", messaging.api_notification_delete)
    app.router.add_post("/api/notifications/ack", messaging.api_notification_ack)
    app.router.add_post("/api/notifications/unack", messaging.api_notification_unack)
    return app


def _feed_state(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    state._notification_log = [dict(note) for note in _NOTES]
    state._unread_count = 3
    broadcasts: list[tuple[str, dict]] = []

    async def rewrite() -> None:
        return None

    monkeypatch.setattr(state, "_rewrite_notifications_async", rewrite)
    monkeypatch.setattr(state, "broadcast_ws", lambda kind, data: broadcasts.append((kind, data)))
    return state, broadcasts


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "teammate"])
async def test_a_teammates_clear_keeps_the_app_notifications_it_cannot_see(
    tmp_path, monkeypatch, withheld, who
):
    state, broadcasts = _feed_state(tmp_path, monkeypatch)
    ingress = _personal() if who == "owner" else _team()
    user = OWNER if who == "owner" else TEAMMATE
    async with TestClient(TestServer(_feed_app(state, ingress))) as client:
        resp = await client.post(
            "/api/notifications/clear", headers=ingress.stamp_headers([], relay_user=user)
        )
        assert resp.status == 200, await resp.text()
    remaining = [note["source"] for note in state._notification_log]
    if who == "owner":
        assert remaining == []
        assert ("notifications_clear", {}) in broadcasts
    else:
        assert remaining == ["app:mochi"]
        assert not [kind for kind, _ in broadcasts if kind == "notifications_clear"]
        assert state._unread_count == 1
        assert withheld == ["notifications_clear"]


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "teammate"])
async def test_a_teammates_ack_all_leaves_the_app_notifications_unread(
    tmp_path, monkeypatch, withheld, who
):
    state, broadcasts = _feed_state(tmp_path, monkeypatch)
    ingress = _personal() if who == "owner" else _team()
    user = OWNER if who == "owner" else TEAMMATE
    async with TestClient(TestServer(_feed_app(state, ingress))) as client:
        resp = await client.post(
            "/api/notifications/ack-all", headers=ingress.stamp_headers([], relay_user=user)
        )
        assert resp.status == 200, await resp.text()
    acked = {note["source"] + "@" + note["ts"]: note["acked"] for note in state._notification_log}
    if who == "owner":
        assert all(acked.values())
        assert ("notification_ack", {"ts": "*"}) in broadcasts
    else:
        assert acked == {"system@1": True, "app:mochi@2": False, "system@3": True}
        assert broadcasts == [("notification_ack", {"ts": "1"})]
        assert withheld == ["notification_ack"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/api/notifications"),
        ("POST", "/api/notifications/ack"),
        ("POST", "/api/notifications/unack"),
    ],
)
async def test_a_teammate_cannot_change_an_app_notification_by_name(
    tmp_path, monkeypatch, method, path
):
    sel_rows = _SelRecorder()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_rows)
    state, _ = _feed_state(tmp_path, monkeypatch)
    before = [dict(note) for note in state._notification_log]
    ingress = _team()
    async with TestClient(TestServer(_feed_app(state, ingress))) as client:
        hidden = await client.request(
            method, path, json={"ts": "2"}, headers=ingress.stamp_headers([], relay_user=TEAMMATE)
        )
        assert hidden.status == 403
        assert (await hidden.json())["code"] == "relay_apps_teammate"
        assert state._notification_log == before
        visible = await client.request(
            method, path, json={"ts": "1"}, headers=ingress.stamp_headers([], relay_user=TEAMMATE)
        )
        assert visible.status == 200, await visible.text()
    refusals = [
        fields
        for name, fields in sel_rows.rows
        if name == "log_api_access" and fields.get("error") == "relay_apps_teammate"
    ]
    assert len(refusals) == 1 and refusals[0]["outcome"] == "denied"


@pytest.mark.asyncio
async def test_the_sse_stream_withholds_app_pushes_from_a_teammate(tmp_path, monkeypatch, withheld):
    from kiro_crew.dashboard.handlers import updates

    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    state = _make_state(tmp_path)
    monkeypatch.setattr(state, "status_snapshot", lambda **_kwargs: {})
    ingress = _team()
    app = _relay_app(state, ingress)
    app.router.add_get("/api/stream", updates.api_stream)
    try:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/stream", headers=ingress.stamp_headers([], relay_user=TEAMMATE)
            )
            assert resp.status == 200
            # The first status frame proves the stream registered its queue.
            first = await finished(resp.content.readuntil(b"\n\n"), "the first SSE frame")
            assert first.startswith(b"event: dashboard")
            state._broadcast({"source": "app:mochi", "body": "private"})
            state._broadcast({"source": "system", "body": "fence"})
            blocks: list[bytes] = []
            while b"fence" not in (blocks[-1] if blocks else b""):
                blocks.append(await finished(resp.content.readuntil(b"\n\n"), "an SSE frame"))
            resp.close()
    finally:
        stopped.set()
    assert not [block for block in blocks if b"private" in block]
    assert withheld == ["notification"]
