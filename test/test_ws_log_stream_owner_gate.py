"""The gateway log over ``/api/ws`` reaches the owner and teammates only.

``GET /api/logs`` and ``POST /api/logs/level`` are owner-or-teammate routes. The
WebSocket ``subscribe_logs`` path carries the same log, twice: the ring replay
in ``dashboard/ws.py`` and the live fan-out, which rechecks every send through
``WebSocketHub._ws_client_allowed``. Both admitted every dashboard-user socket,
and a non-owner dashboard session (a Telegram, Teams or Webex allowlist link)
is a dashboard user too. The owner set (``_owner_ws_clients``, filled from
``is_owner_dashboard_request`` at connect, so it holds teammates as well)
decides, the boundary ``MEMBER_LOG_EVENTS`` already uses.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.websocket_hub import WebSocketHub
from kiro_crew.shared_crew.teammate_access import TEAMMATE_WS_FLAG

_LINE = {"level": "INFO", "msg": "gateway: secret-bearing log line"}


class _Socket:
    """A dashboard-user socket: the positive flag plus a recording send."""

    def __init__(self, *, teammate: bool = False) -> None:
        self.closed = False
        self.sent: list[str] = []
        self._flags: dict = {"_is_dashboard_user": True, TEAMMATE_WS_FLAG: teammate}

    def get(self, key: str, default=None):
        return self._flags.get(key, default)

    def __getitem__(self, key: str):
        return self._flags[key]

    def __setitem__(self, key: str, value) -> None:
        self._flags[key] = value

    async def send_str(self, payload: str) -> None:
        self.sent.append(payload)


def _hub(owner_sockets: set) -> WebSocketHub:
    owner = SimpleNamespace(_owner_ws_clients=owner_sockets)
    return WebSocketHub(
        owner,  # type: ignore[arg-type]
        serving_loop_provider=lambda: None,
        logger_provider=lambda: logging.getLogger("test"),
    )


def _live_send(hub: WebSocketHub, ws: _Socket) -> tuple[list[str], bool]:
    """Push one line through the real log fan-out send for *ws*."""
    from kiro_crew.dashboard.handlers import updates as upd

    state = MagicMock()
    state._ws_client_allowed = hub._ws_client_allowed
    state._drop_if_stalled = lambda _ws: False
    state._ws_log_subscribers = {ws}
    msg = json.dumps({"type": "log", "data": _LINE})
    with patch("kiro_crew.sel.sel"):
        asyncio.run(upd._safe_ws_send(ws, msg, state))  # type: ignore[arg-type]
    return ws.sent, ws in state._ws_log_subscribers


class TestLiveLogStream:
    def test_owner_socket_keeps_the_live_log(self) -> None:
        ws = _Socket()
        sent, subscribed = _live_send(_hub({ws}), ws)
        assert len(sent) == 1 and subscribed

    def test_teammate_socket_keeps_the_live_log(self) -> None:
        ws = _Socket(teammate=True)
        sent, subscribed = _live_send(_hub({ws}), ws)
        assert len(sent) == 1 and subscribed

    def test_non_owner_dashboard_socket_gets_no_live_log(self) -> None:
        ws = _Socket()
        sent, subscribed = _live_send(_hub(set()), ws)
        assert sent == [], "a dashboard user outside the owner set must not get the log"
        assert not subscribed, "and the subscription is dropped"

    def test_the_refusal_is_audited_as_owner_only(self) -> None:
        ws = _Socket()
        with patch("kiro_crew.dashboard.ws_event_scope._audit_deny") as deny:
            assert _hub(set())._ws_client_allowed(ws, "log", {}) is False  # type: ignore[arg-type]
        deny.assert_called_once()
        assert deny.call_args.args[1:] == ("log", "owner_only")

    def test_other_frames_still_reach_a_non_owner_dashboard_socket(self) -> None:
        """The hub's per-send check withholds only the log; ``/api/ws`` refuses this
        session at connect, so in the gateway it never holds a socket at all."""
        ws = _Socket()
        with patch("kiro_crew.sel.sel"):
            assert _hub(set())._ws_client_allowed(ws, "slots", {}) is True  # type: ignore[arg-type]


class _Msg:
    def __init__(self) -> None:
        from aiohttp import WSMsgType

        self.type = WSMsgType.TEXT
        self.data = json.dumps({"type": "subscribe_logs"})


class _LoopSocket:
    """Ends the status loop at once and yields one ``subscribe_logs`` frame."""

    def __init__(self) -> None:
        self.closed = True
        self.sent: list[dict] = []
        self._flags: dict = {}
        self._yielded = False

    def __setitem__(self, key: str, value) -> None:
        self._flags[key] = value

    def __getitem__(self, key: str):
        return self._flags[key]

    def get(self, key: str, default=None):
        return self._flags.get(key, default)

    async def prepare(self, request) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def send_str(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        return _Msg()


def _replay(monkeypatch, *, owner: bool, host_owner: bool) -> tuple[list[dict], MagicMock]:
    """Drive ``api_ws`` for a dashboard-user socket through one ``subscribe_logs``."""
    from kiro_crew.dashboard import handlers
    from kiro_crew.dashboard import ws as dashboard_ws
    from kiro_crew.dashboard.handlers import source_providers

    owner_sockets: set = set()
    state = MagicMock()
    state.owner_id = "U_OWNER"
    state.serialize_slots.side_effect = lambda **_kw: []
    state._yolo = False
    state._owner_ws_clients = owner_sockets

    def _register(ws, *, owner: bool = False) -> None:
        if owner:
            owner_sockets.add(ws)

    state.register_ws.side_effect = _register

    class Request(dict):
        def __init__(self) -> None:
            super().__init__({"app": ""})
            self["is_dashboard_user"] = True
            self.app = {"state": state}

    fake_ws = _LoopSocket()
    ring = collections.deque([json.dumps(_LINE)])
    monkeypatch.setattr(handlers, "_log_ring", ring)
    monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
    monkeypatch.setattr(dashboard_ws, "LagTolerantWebSocketResponse", lambda **kwargs: fake_ws)
    monkeypatch.setattr(source_providers, "schedule_check_refresh", MagicMock())
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: owner)
    monkeypatch.setattr(source_providers, "is_host_owner_dashboard_request", lambda _r: host_owner)
    with patch("kiro_crew.sel.sel"):
        asyncio.run(dashboard_ws.api_ws(Request()))  # type: ignore[arg-type]
    logs = [frame for frame in fake_ws.sent if frame.get("type") == "log"]
    return logs, state


class TestLogRingReplay:
    def test_owner_socket_gets_the_replay(self, monkeypatch) -> None:
        logs, state = _replay(monkeypatch, owner=True, host_owner=True)
        assert [frame["data"] for frame in logs] == [_LINE]
        state.subscribe_logs.assert_called_once()

    def test_teammate_socket_gets_the_replay(self, monkeypatch) -> None:
        logs, state = _replay(monkeypatch, owner=True, host_owner=False)
        assert [frame["data"] for frame in logs] == [_LINE]
        state.subscribe_logs.assert_called_once()

    def test_non_owner_dashboard_socket_gets_no_replay(self, monkeypatch) -> None:
        logs, state = _replay(monkeypatch, owner=False, host_owner=False)
        assert logs == [], "the ring replay must not reach a dashboard user outside the owner set"
        state.subscribe_logs.assert_not_called()


_OWNER = "U_OWNER"


def _publish_owner(request) -> None:
    request["app"] = ""
    request["is_dashboard_user"] = True
    request["user"] = _OWNER


def _publish_allowlist_link(request) -> None:
    """A Telegram allowlist link: a dashboard user with no Shared Crew claims."""
    request["app"] = ""
    request["is_dashboard_user"] = True
    request["user"] = "telegram:4242"


def _connect_real(monkeypatch, publish) -> tuple[list[dict], MagicMock, set, _LoopSocket]:
    """``api_ws`` with the real owner predicates, the caller's claims from *publish*."""
    from kiro_crew.dashboard import handlers
    from kiro_crew.dashboard import ws as dashboard_ws
    from kiro_crew.dashboard.handlers import source_providers

    owner_sockets: set = set()
    state = MagicMock()
    state.owner_id = _OWNER
    state.serialize_slots.side_effect = lambda **_kw: []
    state._yolo = False
    state._owner_ws_clients = owner_sockets

    def _register(ws, *, owner: bool = False) -> None:
        if owner:
            owner_sockets.add(ws)

    state.register_ws.side_effect = _register

    class Request(dict):
        path = "/api/ws"
        method = "GET"
        headers: dict = {}

        def __init__(self) -> None:
            super().__init__()
            publish(self)
            self.app = {"state": state}

    fake_ws = _LoopSocket()
    monkeypatch.setattr(handlers, "_log_ring", collections.deque([json.dumps(_LINE)]))
    monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
    monkeypatch.setattr(dashboard_ws, "LagTolerantWebSocketResponse", lambda **kwargs: fake_ws)
    monkeypatch.setattr(source_providers, "schedule_check_refresh", MagicMock())
    with patch("kiro_crew.sel.sel"):
        asyncio.run(dashboard_ws.api_ws(Request()))  # type: ignore[arg-type]
    logs = [frame for frame in fake_ws.sent if frame.get("type") == "log"]
    return logs, state, owner_sockets, fake_ws


def _live_after_connect(owner_sockets: set, fake_ws: _LoopSocket) -> list[str]:
    """The live fan-out send for the socket ``api_ws`` registered, owner set as it left it."""
    live = _Socket(teammate=bool(fake_ws.get(TEAMMATE_WS_FLAG, False)))
    owners = {live} if fake_ws in owner_sockets else set()
    sent, _ = _live_send(_hub(owners), live)
    return sent


class TestRealClaims:
    """The predicates run for real: no owner gate is patched."""

    def test_owner_gets_replay_and_live(self, monkeypatch) -> None:
        logs, state, owners, ws = _connect_real(monkeypatch, _publish_owner)
        assert [f["data"] for f in logs] == [_LINE]
        state.subscribe_logs.assert_called_once()
        assert len(_live_after_connect(owners, ws)) == 1

    def test_ssm_invite_teammate_gets_replay_and_live(self, monkeypatch) -> None:
        from teammate_caller import enable_teammates, publish_teammate

        enable_teammates(monkeypatch)
        logs, state, owners, ws = _connect_real(monkeypatch, publish_teammate)
        assert ws.get(TEAMMATE_WS_FLAG) is True, "fixture must read as a teammate socket"
        assert [f["data"] for f in logs] == [_LINE]
        state.subscribe_logs.assert_called_once()
        assert len(_live_after_connect(owners, ws)) == 1

    def test_relay_teammate_gets_replay_and_live(self, monkeypatch) -> None:
        from teammate_caller import publish_relay_teammate

        logs, state, owners, ws = _connect_real(monkeypatch, publish_relay_teammate)
        assert ws.get(TEAMMATE_WS_FLAG) is True, "fixture must read as a teammate socket"
        assert [f["data"] for f in logs] == [_LINE]
        state.subscribe_logs.assert_called_once()
        assert len(_live_after_connect(owners, ws)) == 1

    def test_allowlist_link_gets_neither(self, monkeypatch) -> None:
        logs, state, owners, ws = _connect_real(monkeypatch, _publish_allowlist_link)
        assert logs == []
        state.subscribe_logs.assert_not_called()
        assert ws not in owners

    def test_ssm_teammate_with_preview_off_gets_neither(self, monkeypatch) -> None:
        """Preview off means the SSM claims admit nobody: the same answer as GET /api/logs."""
        from teammate_caller import publish_teammate

        monkeypatch.delenv("KIROCREW_SHARED_CREW_PREVIEW", raising=False)
        logs, state, _owners, _ws = _connect_real(monkeypatch, publish_teammate)
        assert logs == []
        state.subscribe_logs.assert_not_called()


@pytest.fixture(autouse=True)
def _clear_audit_rate_limit():
    from kiro_crew.dashboard import ws_event_scope

    ws_event_scope._sel_last_audit.clear()
    yield
    ws_event_scope._sel_last_audit.clear()
