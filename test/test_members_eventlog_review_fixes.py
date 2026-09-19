"""Members-surface invariants: egress redaction, frame order, commit-then-mark.

* O1 -- ``/api/members/{slug}/history`` redacted dict VALUES and left dict KEYS
  alone, so a contributor-chosen credential-shaped key crossed to the browser at
  the one egress its three siblings all scrub.
* F6 -- the ``members_subscribed`` baseline is a ceiling the client truncates
  against, and it was sent AFTER the socket was already registered for
  broadcasts, so a projection arriving in between was applied and then thrown
  away.
* F3 -- slot open/close transitions were marked SEEN before their append landed,
  and the seen set is what suppresses a re-emit, so a failed write was a
  permanent, silent loss.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import members
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.eventlog import types
from kiro_crew.eventlog.service import get_service, set_service

CREW = "code-reviewer"


@pytest.fixture(autouse=True)
def _fresh_eventlog(tmp_path, monkeypatch):
    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    set_service(None)
    yield
    set_service(None)


def _fake_config(agents, default=CREW):
    return SimpleNamespace(agents=agents, default_agent=default, memory_stores={})


def _agent(**kw) -> KiroCrewAgentConfig:
    return KiroCrewAgentConfig(kiro_agent=kw.pop("kiro_agent", "reviewer"), **kw)


def _hub():
    """A real WebSocketHub with inert providers.

    Constructed rather than faked: `send_members_subscribed` is the unit under
    test, and its own `_log` is a read-only property fed by a provider.
    """
    import logging
    from types import SimpleNamespace

    from kiro_crew.dashboard.websocket_hub import WebSocketHub

    return WebSocketHub(
        SimpleNamespace(_ws_clients=[], _owner_ws_clients=set()),
        serving_loop_provider=lambda: None,
        logger_provider=lambda: logging.getLogger("test.members-baseline"),
        redact_credentials_provider=lambda: (lambda text: (text, 0)),
        redact_exfiltration_urls_provider=lambda: (lambda text: (text, 0)),
    )


def _members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_member_history

    @web.middleware
    async def _auth(request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members/{slug}/history", api_member_history)
    return app


# ---------------------------------------------------------------------------
# O1 -- the history read redacts KEYS, not only values
# ---------------------------------------------------------------------------
class TestHistoryRedactsKeys:
    @pytest.mark.asyncio
    async def test_a_credential_shaped_data_key_does_not_cross_to_the_browser(
        self, tmp_path, monkeypatch
    ):
        """``check_event_data`` accepts any dict key, so the key is app-chosen text."""
        cfg = _fake_config({CREW: _agent()})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        exfil_key = "https://evil.example/c?token=AKIAIOSFODNN7EXAMPLE"
        svc.append(slug, "demoapp/ping", {exfil_key: 1})

        async with TestClient(TestServer(_members_app(_make_state(tmp_path)))) as client:
            data = await (await client.get(f"/api/members/{slug}/history")).json()

        blob = json.dumps(data)
        assert exfil_key not in blob
        assert "AKIAIOSFODNN7EXAMPLE" not in blob
        assert any(e.get("type") == "demoapp/ping" for e in data["events"])

    @pytest.mark.asyncio
    async def test_the_route_uses_the_shared_redactor(self, tmp_path, monkeypatch):
        """One function at every egress: a local copy is how the two drifted apart.

        Patched at the service module, which is where the three sibling reads take
        it from -- a route that reintroduced its own pass would not be affected and
        would fail this.
        """
        from kiro_crew.eventlog import service as service_mod

        cfg = _fake_config({CREW: _agent()})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, "demoapp/ping", {"k": "v"})

        monkeypatch.setattr(
            service_mod, "_redact_projection_value", lambda value, _depth=1: "SCRUBBED"
        )
        async with TestClient(TestServer(_members_app(_make_state(tmp_path)))) as client:
            data = await (await client.get(f"/api/members/{slug}/history")).json()
        assert any(e.get("data") == "SCRUBBED" for e in data["events"])


# ---------------------------------------------------------------------------
# F6 -- the baseline is sent before the socket can be reached by a broadcast
# ---------------------------------------------------------------------------
class TestProjectionsWaitForTheBaseline:
    """F6. The baseline is a ceiling, so nothing may be applied before it lands.

    Ordering the two frames cannot close this: the socket is registered for
    broadcasts before the connect snapshot is even written, and `slots` has to
    stay the first frame a client reads. The gate is what closes it -- a socket
    between its `last_seqs` read and the baseline's arrival is withheld from
    `member_projection`.
    """

    def _state_with_one_owner_socket(self):
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.state import DashboardState

        store = {"_is_dashboard_user": True, "_app": "", "_allowed_events": frozenset()}
        ws = MagicMock()
        ws.closed = False
        ws.get.side_effect = lambda k, default=None: store.get(k, default)
        ws.__setitem__ = lambda k, v: store.__setitem__(k, v)

        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state._ws_clients = [ws]
        state._owner_ws_clients = set()
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        state._serialize_for_client = DashboardState._serialize_for_client.__get__(state)
        state._send_ws_all = DashboardState._send_ws_all.__get__(state)
        wire: list = []
        state._spawn_ws_send = lambda sock, payload: wire.append((sock, payload))
        return state, ws, store, wire

    def test_a_projection_is_withheld_while_the_baseline_is_pending(self):
        from kiro_crew.dashboard.websocket_hub import MEMBERS_BASELINE_PENDING

        state, ws, store, wire = self._state_with_one_owner_socket()
        store[MEMBERS_BASELINE_PENDING] = True
        state._send_ws_all(
            "member_projection",
            {"slug": "alice", "key": "roster", "value": {}, "seq": 7},
            json.dumps({"type": "member_projection"}),
        )
        assert wire == [], "a projection reached a socket that has no ceiling yet"

    def test_the_same_projection_is_delivered_once_the_baseline_has_landed(self):
        from kiro_crew.dashboard.websocket_hub import MEMBERS_BASELINE_PENDING

        state, ws, store, wire = self._state_with_one_owner_socket()
        store[MEMBERS_BASELINE_PENDING] = False
        state._send_ws_all(
            "member_projection",
            {"slug": "alice", "key": "roster", "value": {}, "seq": 7},
            json.dumps({"type": "member_projection"}),
        )
        assert len(wire) == 1

    def test_the_gate_withholds_projections_only(self):
        """A pending baseline must not silence the rest of the socket's traffic."""
        from kiro_crew.dashboard.websocket_hub import MEMBERS_BASELINE_PENDING

        state, ws, store, wire = self._state_with_one_owner_socket()
        store[MEMBERS_BASELINE_PENDING] = True
        state._send_ws_all("chat_chunk", {"slot": "s"}, json.dumps({"type": "chat_chunk"}))
        assert len(wire) == 1

    @pytest.mark.asyncio
    async def test_the_flag_is_set_before_the_read_and_cleared_after_the_send(self):
        """Set before the read, because the window it closes starts there."""
        from kiro_crew.dashboard.websocket_hub import MEMBERS_BASELINE_PENDING, WebSocketHub
        from kiro_crew.eventlog import service as service_mod

        store: dict = {}
        seen_during_read: list[bool] = []

        class _Ws:
            closed = False

            def get(self, k, default=None):
                return store.get(k, default)

            def __setitem__(self, k, v):
                store[k] = v

            async def send_str(self, msg):
                store["sent"] = msg

        class _Svc:
            def last_seqs(self):
                seen_during_read.append(store.get(MEMBERS_BASELINE_PENDING, False))
                return {"alice": 4}

        import unittest.mock as _mock

        hub = _hub()
        with _mock.patch.object(service_mod, "get_service", lambda: _Svc()):
            await hub.send_members_subscribed(_Ws())

        assert seen_during_read == [True], "the flag must cover the last_seqs read"
        assert store[MEMBERS_BASELINE_PENDING] is False
        assert "members_subscribed" in store["sent"]

    @pytest.mark.asyncio
    async def test_the_flag_is_cleared_even_when_the_baseline_cannot_be_read(self):
        """Withholding for the rest of the connection is worse than one truncate."""
        from kiro_crew.dashboard.websocket_hub import MEMBERS_BASELINE_PENDING, WebSocketHub
        from kiro_crew.eventlog import service as service_mod

        store: dict = {}

        class _Ws:
            closed = False

            def get(self, k, default=None):
                return store.get(k, default)

            def __setitem__(self, k, v):
                store[k] = v

            async def send_str(self, msg):  # pragma: no cover - never reached
                store["sent"] = msg

        class _Broken:
            def last_seqs(self):
                raise OSError("log unreadable")

        import unittest.mock as _mock

        hub = _hub()
        with _mock.patch.object(service_mod, "get_service", lambda: _Broken()):
            await hub.send_members_subscribed(_Ws())

        assert store[MEMBERS_BASELINE_PENDING] is False
        assert "sent" not in store

    def test_the_helper_uses_the_module_level_asyncio_import(self):
        """The advisory finding: a function-local ``import asyncio`` shadowed it."""
        import inspect

        from kiro_crew.dashboard.websocket_hub import WebSocketHub

        src = inspect.getsource(WebSocketHub.send_members_subscribed)
        assert "import asyncio" not in src
        assert "asyncio.to_thread" in src


# ---------------------------------------------------------------------------
# F3 -- a slot transition is marked seen only once its append lands
# ---------------------------------------------------------------------------
class TestSlotTransitionsAreCommittedNotAssumed:
    def _state(self, tmp_path):
        state = _make_state(tmp_path)
        state._member_driven_slots_seen = {}
        state._member_slot_emits_inflight = set()
        return state

    def test_a_landed_open_is_marked_seen(self, tmp_path):
        state = self._state(tmp_path)
        attempted = [("slot-1", CREW, types.SLOT_OPENED, {"slot_key": "slot-1"})]
        state._commit_slot_seen(attempted, [("slot-1", CREW, types.SLOT_OPENED)])
        assert state._member_driven_slots_seen == {"slot-1": CREW}
        assert state._member_slot_emits_inflight == set()

    def test_a_failed_open_is_not_marked_seen_so_it_is_retried(self, tmp_path):
        state = self._state(tmp_path)
        attempted = [("slot-1", CREW, types.SLOT_OPENED, {"slot_key": "slot-1"})]
        state._commit_slot_seen(attempted, [])
        assert state._member_driven_slots_seen == {}
        assert state._member_slot_emits_inflight == set(), "released, so the next diff retries it"

    def test_a_landed_close_clears_the_seen_entry(self, tmp_path):
        state = self._state(tmp_path)
        state._member_driven_slots_seen = {"slot-1": CREW}
        attempted = [("slot-1", CREW, types.SLOT_CLOSED, {"slot_key": "slot-1"})]
        state._commit_slot_seen(attempted, [("slot-1", CREW, types.SLOT_CLOSED)])
        assert state._member_driven_slots_seen == {}

    def test_a_failed_close_keeps_the_slot_seen_so_the_close_is_re_derived(self, tmp_path):
        state = self._state(tmp_path)
        state._member_driven_slots_seen = {"slot-1": CREW}
        attempted = [("slot-1", CREW, types.SLOT_CLOSED, {"slot_key": "slot-1"})]
        state._commit_slot_seen(attempted, [])
        assert state._member_driven_slots_seen == {"slot-1": CREW}

    def test_a_partial_batch_commits_only_what_landed(self, tmp_path):
        state = self._state(tmp_path)
        attempted = [
            ("slot-1", CREW, types.SLOT_OPENED, {"slot_key": "slot-1"}),
            ("slot-2", CREW, types.SLOT_OPENED, {"slot_key": "slot-2"}),
        ]
        state._commit_slot_seen(attempted, [("slot-2", CREW, types.SLOT_OPENED)])
        assert state._member_driven_slots_seen == {"slot-2": CREW}
