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

        class _Sock:
            """A real object, not a MagicMock: ``ws[key] = value`` is a TYPE-level
            lookup, so a ``__setitem__`` assigned onto a mock instance is never the
            one the broadcast path calls."""

            closed = False

            def get(self, k, default=None):
                return store.get(k, default)

            def __getitem__(self, k):
                return store[k]

            def __setitem__(self, k, v):
                store[k] = v

            async def send_str(self, msg):
                store.setdefault("_sent", []).append(msg)

            async def close(self):
                store["_closed"] = True

        ws = _Sock()

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
    async def test_a_withheld_projection_is_replayed_after_the_baseline(self):
        """Withholding alone LOSES the value, so the frame is held and replayed.

        ``last_seqs`` is read BEFORE the withheld append, so the baseline does not
        carry that seq: a discarded frame leaves the client stale until the same
        key next changes, which for a key that changes once is forever.
        """
        import unittest.mock as _mock

        from kiro_crew.dashboard.websocket_hub import (
            MEMBERS_BASELINE_HELD,
            MEMBERS_BASELINE_PENDING,
        )
        from kiro_crew.eventlog import service as service_mod

        state, ws, store, wire = self._state_with_one_owner_socket()
        store[MEMBERS_BASELINE_PENDING] = True
        state._send_ws_all(
            "member_projection",
            {"slug": "alice", "key": "roster", "value": {"n": 1}, "seq": 7},
            json.dumps({"type": "member_projection", "data": {"slug": "alice", "seq": 7}}),
        )
        assert wire == [], "a projection reached a socket that has no ceiling yet"
        assert len(store[MEMBERS_BASELINE_HELD]) == 1, "the frame was discarded, not held"

        class _Svc:
            def last_seqs(self):
                return {"alice": 4}

        hub = _hub()
        with _mock.patch.object(service_mod, "get_service", lambda: _Svc()):
            await hub.send_members_subscribed(ws)

        sent = store.get("_sent", [])
        assert len(sent) == 2, f"expected the baseline then the replay, got {len(sent)} frame(s)"
        assert "members_subscribed" in sent[0], "the baseline must land first"
        assert "member_projection" in sent[1], "the held projection was never replayed"
        assert store[MEMBERS_BASELINE_HELD] == {}

    @pytest.mark.asyncio
    async def test_repeats_for_one_identity_coalesce_to_the_last_value(self):
        """A projection frame carries a whole value, so only the last one matters."""
        import unittest.mock as _mock

        from kiro_crew.dashboard.websocket_hub import (
            MEMBERS_BASELINE_HELD,
            MEMBERS_BASELINE_PENDING,
        )
        from kiro_crew.eventlog import service as service_mod

        state, ws, store, wire = self._state_with_one_owner_socket()
        store[MEMBERS_BASELINE_PENDING] = True
        for seq in (7, 8, 9):
            state._send_ws_all(
                "member_projection",
                {"slug": "alice", "key": "roster", "value": {"n": seq}, "seq": seq},
                json.dumps({"type": "member_projection", "data": {"seq": seq}}),
            )
        assert len(store[MEMBERS_BASELINE_HELD]) == 1, "one identity must hold one payload"

        class _Svc:
            def last_seqs(self):
                return {"alice": 4}

        hub = _hub()
        with _mock.patch.object(service_mod, "get_service", lambda: _Svc()):
            await hub.send_members_subscribed(ws)

        sent = store.get("_sent", [])
        assert len(sent) == 2
        assert '"seq": 9' in sent[1] or '"seq":9' in sent[1], "the replay must be the last value"

    @pytest.mark.asyncio
    async def test_the_flag_is_set_before_the_read_and_cleared_after_the_send(self):
        """Set before the read, because the window it closes starts there."""
        from kiro_crew.dashboard.websocket_hub import MEMBERS_BASELINE_PENDING
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
        from kiro_crew.dashboard.websocket_hub import MEMBERS_BASELINE_PENDING
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

    def test_a_transition_suppressed_by_an_in_flight_append_is_re_derived(self, tmp_path):
        """Releasing the slot is not enough: the next broadcast may never come.

        A slot that closes while its own open is being written produces no further
        slot change, so nothing re-derives the close unless committing does it --
        and the durable log is then left saying the slot is still open.
        """
        state = self._state(tmp_path)
        state._member_slot_emits_recheck = True
        calls: list[int] = []
        state._do_slots_broadcast = lambda: calls.append(1)
        attempted = [("slot-1", CREW, types.SLOT_OPENED, {"slot_key": "slot-1"})]
        state._commit_slot_seen(attempted, [("slot-1", CREW, types.SLOT_OPENED)])
        assert calls == [1], "the suppressed transition was never re-derived"
        assert state._member_slot_emits_recheck is False

    def test_an_ordinary_commit_does_not_re_derive(self, tmp_path):
        """No transition was suppressed, so there is nothing owed."""
        state = self._state(tmp_path)
        calls: list[int] = []
        state._do_slots_broadcast = lambda: calls.append(1)
        attempted = [("slot-1", CREW, types.SLOT_OPENED, {"slot_key": "slot-1"})]
        state._commit_slot_seen(attempted, [("slot-1", CREW, types.SLOT_OPENED)])
        assert calls == []

    def test_the_re_derive_waits_until_the_whole_batch_has_drained(self, tmp_path):
        state = self._state(tmp_path)
        state._member_slot_emits_recheck = True
        state._member_slot_emits_inflight = {"slot-1", "slot-9"}
        calls: list[int] = []
        state._do_slots_broadcast = lambda: calls.append(1)
        attempted = [("slot-1", CREW, types.SLOT_OPENED, {"slot_key": "slot-1"})]
        state._commit_slot_seen(attempted, [("slot-1", CREW, types.SLOT_OPENED)])
        assert calls == [], "slot-9 is still writing; re-deriving now would race it"
        assert state._member_slot_emits_recheck is True


# ---------------------------------------------------------------------------
# G3 -- per-member appends go through ONE ordered writer
# ---------------------------------------------------------------------------
class TestMemberLogAppendsAreOrdered:
    """The member log is order-bearing and its projections fold from log order.

    The default executor has many threads, so two appends for one member race for
    the log's lock and the one submitted second can land first -- after which the
    folded projection regresses to an older message preview, or shows a patrol
    still armed after it stopped.
    """

    def test_the_writer_has_exactly_one_worker(self):
        from kiro_crew.dashboard.state import _member_log_executor

        pool = _member_log_executor()
        assert pool._max_workers == 1
        assert _member_log_executor() is pool, "a second pool would serialise nothing"

    def test_no_member_log_append_uses_the_default_executor(self):
        """Source ratchet: `run_in_executor(None, ...)` is what reorders them."""
        import inspect

        from kiro_crew.dashboard import state as state_mod
        from kiro_crew.slack import gateway as gateway_mod

        for mod in (state_mod, gateway_mod):
            src = inspect.getsource(mod)
            assert (
                "run_in_executor(None, _emit" not in src
            ), f"{mod.__name__} submits a member-log append to the default executor"

    def test_both_append_sites_name_the_writer(self):
        import inspect

        from kiro_crew.dashboard import state as state_mod
        from kiro_crew.slack import gateway as gateway_mod

        assert inspect.getsource(state_mod).count("_member_log_executor()") >= 3
        assert "_member_log_executor()" in inspect.getsource(gateway_mod)


# ---------------------------------------------------------------------------
# L4 -- the durable append is sequenced BEFORE the frame is published
# ---------------------------------------------------------------------------
class TestPatrolAppendPrecedesItsBroadcast:
    """A published frame must not describe a transition the log does not hold.

    A subscriber acts on ``autonudge_state`` the moment it arrives. Publishing
    ahead of the append leaves a window where every client has the transition and
    the durable log does not, so if the process ends there the log is missing an
    event the whole fleet already saw, and the log is the record anyone later
    reconstructing the patrol reads.
    """

    def test_the_append_runs_before_the_publish(self):
        from kiro_crew.slack.gateway import _append_then_publish

        order: list[str] = []
        _append_then_publish(lambda: order.append("append"), lambda: order.append("publish"))

        assert order == ["append", "publish"], f"published out of order: {order}"

    def test_a_failed_append_withholds_the_publish(self):
        """A frame the log never accepted is never sent.

        Withholding one frame leaves every subscriber on the state it already
        holds, so the live view goes stale and the next successful append
        restores it. Publishing regardless would buy no availability -- nothing
        is blanked either way -- and would pay for it in a permanent
        disagreement between the log and what clients were told.
        """
        from kiro_crew.slack.gateway import _append_then_publish

        order: list[str] = []

        def _boom() -> None:
            order.append("append")
            raise OSError("no space left on device")

        _append_then_publish(_boom, lambda: order.append("publish"))

        assert order == ["append"], f"published a transition the log rejected: {order}"

    def test_the_withheld_append_is_reported_at_error(self, caplog):
        """A dropped durable write is not a debug detail.

        DEBUG is off in every deployment, so logging it there would make the one
        signal that a member's log is losing events invisible.
        """
        import logging

        from kiro_crew.slack.gateway import _append_then_publish

        def _boom() -> None:
            raise OSError("no space left on device")

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.slack.gateway"):
            _append_then_publish(_boom, lambda: None)

        loud = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert loud, "a failed durable append was not reported at error"

    def test_a_raising_publish_is_not_swallowed(self):
        """The publish is the caller's own contract, so its failure is not hidden.

        Only the emit is best-effort. Swallowing both would make the helper report
        success for a transition that reached neither the log nor any client.
        """
        import pytest as _pytest

        from kiro_crew.slack.gateway import _append_then_publish

        with _pytest.raises(RuntimeError):
            _append_then_publish(lambda: None, lambda: (_ for _ in ()).throw(RuntimeError("x")))

    def test_the_patrol_observer_publishes_only_through_the_ordered_helper(self):
        """Source ratchet: the observer holds no second, unordered publish.

        The fix is worthless if the frame also goes out directly, so the literal
        event name may appear exactly once in the module -- in the one publisher
        the helper drives. Anchored on the frame's own type string rather than on
        a function name, because renaming the closure would not move the defect.
        """
        import inspect

        from kiro_crew.slack import gateway as gateway_mod

        src = inspect.getsource(gateway_mod)
        assert (
            src.count('"autonudge_state"') == 1
        ), "a second autonudge_state publisher is a second ordering to reason about"
        assert "_append_then_publish(" in src, "the observer routes through the ordered helper"


# ---------------------------------------------------------------------------
# The held queue's own failure paths: overflow, and a replay that cannot send
# ---------------------------------------------------------------------------
class TestHeldProjectionQueueEdges:
    """The queue exists so a withheld frame is not lost, so its OWN failures have
    to be the safe kind: over the cap the socket is closed rather than handed a
    partial replay, and a send fault abandons the queue instead of spinning on it.
    """

    def _sock(self):
        store: dict = {"_is_dashboard_user": True, "_app": "", "_allowed_events": frozenset()}

        class _Sock:
            closed = False

            def get(self, k, default=None):
                return store.get(k, default)

            def __getitem__(self, k):
                return store[k]

            def __setitem__(self, k, v):
                store[k] = v

            async def send_str(self, msg):
                store.setdefault("_sent", []).append(msg)

            async def close(self):
                store["_closed"] = True

        return _Sock(), store

    def _hub_and_sock(self):
        ws, store = self._sock()
        return _hub(), ws, store

    def test_over_the_cap_the_queue_is_abandoned_and_marked(self):
        from kiro_crew.dashboard.websocket_hub import (
            MEMBERS_BASELINE_HELD,
            MEMBERS_BASELINE_HELD_MAX,
        )

        hub, ws, store = self._hub_and_sock()
        for i in range(MEMBERS_BASELINE_HELD_MAX + 3):
            hub._hold_projection(ws, {"slug": f"s{i}", "key": "roster"}, f"payload-{i}")
        held = store[MEMBERS_BASELINE_HELD]
        assert len(held) == 1, "a partial replay is worse than a re-baseline"
        assert list(held) != [("s0", "roster")], "the overflow marker must replace the queue"

    def test_once_marked_further_frames_are_not_queued(self):
        from kiro_crew.dashboard.websocket_hub import (
            MEMBERS_BASELINE_HELD,
            MEMBERS_BASELINE_HELD_MAX,
        )

        hub, ws, store = self._hub_and_sock()
        for i in range(MEMBERS_BASELINE_HELD_MAX + 1):
            hub._hold_projection(ws, {"slug": f"s{i}", "key": "k"}, "p")
        before = dict(store[MEMBERS_BASELINE_HELD])
        hub._hold_projection(ws, {"slug": "later", "key": "k"}, "p")
        assert store[MEMBERS_BASELINE_HELD] == before

    @pytest.mark.asyncio
    async def test_an_overflowed_queue_closes_the_socket_instead_of_replaying(self):
        from kiro_crew.dashboard.websocket_hub import (
            MEMBERS_BASELINE_HELD,
            MEMBERS_BASELINE_HELD_MAX,
        )

        hub, ws, store = self._hub_and_sock()
        for i in range(MEMBERS_BASELINE_HELD_MAX + 1):
            hub._hold_projection(ws, {"slug": f"s{i}", "key": "k"}, "p")
        await hub._flush_held_projections(ws)
        assert store.get("_closed") is True, "the socket must re-baseline from scratch"
        assert store[MEMBERS_BASELINE_HELD] == {}
        assert store.get("_sent") is None, "nothing may be replayed after an overflow"

    @pytest.mark.asyncio
    async def test_a_close_that_itself_fails_is_swallowed(self):
        from kiro_crew.dashboard.websocket_hub import MEMBERS_BASELINE_HELD_MAX

        hub, ws, store = self._hub_and_sock()

        async def _bad_close():
            raise OSError("socket already gone")

        ws.close = _bad_close  # type: ignore[method-assign]
        for i in range(MEMBERS_BASELINE_HELD_MAX + 1):
            hub._hold_projection(ws, {"slug": f"s{i}", "key": "k"}, "p")
        await hub._flush_held_projections(ws)

    @pytest.mark.asyncio
    async def test_a_failing_replay_abandons_the_queue(self):
        from kiro_crew.dashboard.websocket_hub import MEMBERS_BASELINE_HELD

        hub, ws, store = self._hub_and_sock()

        async def _bad_send(_msg):
            raise ConnectionResetError("peer went away")

        ws.send_str = _bad_send  # type: ignore[method-assign]
        hub._hold_projection(ws, {"slug": "alice", "key": "roster"}, "payload")
        hub._hold_projection(ws, {"slug": "bob", "key": "roster"}, "payload")
        await hub._flush_held_projections(ws)
        assert store[MEMBERS_BASELINE_HELD] == {}, "a dead socket must not keep a queue"

    @pytest.mark.asyncio
    async def test_flushing_an_empty_queue_does_nothing(self):
        hub, ws, store = self._hub_and_sock()
        await hub._flush_held_projections(ws)
        assert store.get("_sent") is None
        assert store.get("_closed") is None


class TestServingLoopDoesNotIterateTheLiveRegistry:
    """The loop lookup runs on the appending thread; the registry is the serving
    loop's. A Python-level iterator over it raises RuntimeError the moment the
    other thread adds or drops a socket, and the sink's caller swallows a raise,
    so the cost was a committed delta no subscriber ever received.
    """

    def test_a_registry_mutated_during_the_lookup_does_not_raise(self):
        from types import SimpleNamespace

        from kiro_crew.dashboard.eventlog_ws import EventLogHub

        hub = EventLogHub()

        registry: dict = {}

        class _MutatesWhenRead:
            """Adds a socket the instant the scan reads its first state.

            The mutation has to land BETWEEN two steps of the iteration, not
            before it starts: a dict whose size changes before the first step
            raises nothing, so a test built that way passes over the live
            iterator too and proves nothing. Reading ``pump`` is the loop body,
            so mutating there is exactly mid-iteration.
            """

            @property
            def pump(self):
                registry[object()] = SimpleNamespace(pump=None)
                return None

        registry[object()] = _MutatesWhenRead()
        registry[object()] = SimpleNamespace(pump=None)

        hub._sockets = registry  # type: ignore[assignment]
        hub._captured_loop = None

        # Must not raise. None is a correct answer here (no pump, no captured
        # loop); RuntimeError is the defect.
        assert hub._serving_loop() is None

    def test_the_captured_loop_answers_without_touching_the_registry(self):
        import asyncio

        from kiro_crew.dashboard.eventlog_ws import EventLogHub

        hub = EventLogHub()
        loop = asyncio.new_event_loop()
        try:
            hub._captured_loop = loop

            touched: list[str] = []

            class _Tripwire(dict):
                def values(self):
                    touched.append("values")
                    return super().values()

                def __iter__(self):
                    touched.append("iter")
                    return super().__iter__()

            hub._sockets = _Tripwire()  # type: ignore[assignment]

            assert hub._serving_loop() is loop
            assert touched == [], f"the common path walked the registry: {touched}"
        finally:
            loop.close()


class TestThePatrolAppendCanActuallyFail:
    """The withhold is only real if the append it guards can raise.

    ``_append_then_publish`` skips the publish when the append raises. Handed a
    best-effort callable that logs at debug and returns, that branch is
    unreachable and every transition publishes whatever the log did -- the guard
    is present and inert. So the callable's error policy is part of the fix, not
    an implementation detail of it.
    """

    def test_emit_strict_propagates_a_service_failure(self, monkeypatch):
        from kiro_crew import eventlog_hooks

        def _boom():
            raise OSError("no space left on device")

        monkeypatch.setattr("kiro_crew.eventlog.service.get_service", _boom)

        import pytest as _pytest

        with _pytest.raises(OSError):
            eventlog_hooks.emit_strict("slug", "Name", "member/message", {})

    def test_emit_still_swallows_for_its_best_effort_callers(self, monkeypatch):
        """The other callers' contract is unchanged: an activity row is additive."""
        from kiro_crew import eventlog_hooks

        def _boom():
            raise OSError("no space left on device")

        monkeypatch.setattr("kiro_crew.eventlog.service.get_service", _boom)

        # Must not raise.
        eventlog_hooks.emit("slug", "Name", "member/message", {})

    def test_the_patrol_closure_hands_over_the_non_swallowing_append(self):
        """Source pin, anchored on the defect itself: which callable is passed.

        The defect was not a missing guard but the wrong callable reaching one,
        so the call site's choice is the thing to hold. A behavioural pin cannot
        reach it -- the closure lives inside an observer built during gateway
        start-up -- and this stays true under any rename of the guard.
        """
        import inspect

        from kiro_crew.slack import gateway as gateway_mod

        src = inspect.getsource(gateway_mod)
        # Every CALL, not the definition: a bare index() finds `def
        # _append_then_publish(` first and would read the helper's own body.
        calls = [
            i
            for i in range(len(src))
            if src.startswith("_append_then_publish(", i) and "def " not in src[max(0, i - 4) : i]
        ]
        assert calls, "no call to the ordered helper was found"
        for i in calls:
            window = src[i : i + 400]
            assert "emit_strict(" in window, (
                "a patrol append is not the non-swallowing one, so the withhold "
                f"cannot fire at offset {i}"
            )


class TestAnUpdateDoesNotLeaveSubscriptionsAuthorized:
    """Invalidating the grant cache stops the NEXT subscribe, not a live one.

    A subscription is authorized once, at subscribe time. An update may narrow
    permissions.contributions, so a socket that subscribed under the old manifest
    keeps streaming a unit the app has lost the right to read. Disabling the app
    closes its sockets; an update that changes the same permissions must too.
    """

    def test_the_update_route_closes_the_apps_event_log_sockets(self):
        """Source pin on the defect itself: a call that has to be present.

        Anchored on the update handler's own body, so it stays true under any
        rename of the hub method's caller, and on `close_app` because that is the
        established teardown verb -- the same one disabling an app uses.
        """
        import inspect

        from kiro_crew.apps import routes as routes_mod

        src = inspect.getsource(routes_mod.handle_update_app)
        assert "close_app" in src, (
            "a successful app update does not close the app's event-log sockets, "
            "so a subscription authorized under the old manifest keeps streaming"
        )
