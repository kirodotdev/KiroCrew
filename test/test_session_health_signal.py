"""The ``session_health_changed`` refresh signal.

A frontend-only app (Board) shows four session lanes. It can read
``/api/chat/slots`` -- its manifest lists that path -- but not
``/api/sessions/health``, so polling health degrades into a visible "could not
load session health" warning. The host therefore SIGNALS a health change instead
of expecting every interested client to poll it.

What these tests pin is the signal's whole contract, because it is what makes the
mechanism safe to ship:

* it fires when the VERDICT moves, and NOT on every recomputation (otherwise the
  timer driver becomes a broadcast every interval);
* it carries NO session data -- no slot key, no session key, no classification,
  no count -- so a subscriber that cannot read health learns only *when* to
  refresh what it can already read;
* it is gated by the PRE-EXISTING ``sessions`` declaration, so no app permission
  is widened and an app holding nothing still receives nothing;
* the timer driver that feeds it runs ONLY for a connection that declared that
  scope, so a host where nobody asked for the signal recomputes nothing.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import session_health, ws_event_scope
from kiro_crew.dashboard.handlers import sessions


@pytest.fixture(autouse=True)
def _reset_scope_caches():
    """The scope module memoises per-app manifest state; isolate every test."""
    for cache in (
        ws_event_scope._declared_cache,
        ws_event_scope._exposeto_cache,
        ws_event_scope._sel_last_audit,
    ):
        cache.clear()
    yield
    for cache in (
        ws_event_scope._declared_cache,
        ws_event_scope._exposeto_cache,
        ws_event_scope._sel_last_audit,
    ):
        cache.clear()


@pytest.fixture(autouse=True)
def _reset_cache():
    sessions._health_cache = {}
    sessions._health_cache_ts = 0.0
    sessions._health_lock = sessions.LoopBoundLock()
    # The seam under test (refresh_session_health) publishes through the
    # process-global monitor, and any earlier health test on the same xdist
    # worker may already have seeded its baseline. Start every test from
    # "nothing published yet" and hand the prior baseline back afterwards.
    mon = session_health.default_monitor()
    with mon._lock:
        prior = mon._published_verdict
        mon._published_verdict = None
    yield
    sessions._health_cache = {}
    sessions._health_cache_ts = 0.0
    sessions._health_lock = sessions.LoopBoundLock()
    with mon._lock:
        mon._published_verdict = prior


class _RecordingState:
    """A state that records broadcasts, standing in for DashboardState."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, object]] = []
        self.subagents = None

    def broadcast_ws(self, msg_type: str, data: object) -> None:
        self.frames.append((msg_type, data))


def _health(*, running: int = 0, stalled: tuple[str, ...] = (), degrade: str | None = None) -> dict:
    return {
        "counts": {"running": running, "queued": 0, "stalled": len(stalled)},
        "stalled": {k: {"reason": "tool_stall", "since_ts": 1.0} for k in stalled},
        "degrade_reason": degrade,
        # Ages move on every sample; the fingerprint must ignore them.
        "slots": {"sess-1": {"classification": "running", "age_secs": 12.5}},
    }


# --- the fingerprint ------------------------------------------------------


class TestVerdictFingerprint:
    def test_ignores_ages_so_a_quiet_resample_is_not_a_change(self):
        """Without this the timer driver broadcasts once per interval forever."""
        a = _health(running=1)
        b = _health(running=1)
        b["slots"]["sess-1"]["age_secs"] = 999.0
        assert session_health.health_verdict_fingerprint(
            a
        ) == session_health.health_verdict_fingerprint(b)

    def test_counts_degrade_and_stall_identity_each_move_the_digest(self):
        base = session_health.health_verdict_fingerprint(_health(running=1))
        assert session_health.health_verdict_fingerprint(_health(running=2)) != base
        assert (
            session_health.health_verdict_fingerprint(_health(running=1, degrade="paused")) != base
        )
        assert (
            session_health.health_verdict_fingerprint(_health(running=1, stalled=("sess-9",)))
            != base
        )
        # A stall MOVING between slots is a change even at an equal count.
        one = session_health.health_verdict_fingerprint(_health(stalled=("sess-a",)))
        other = session_health.health_verdict_fingerprint(_health(stalled=("sess-b",)))
        assert one != other

    def test_a_slot_changing_classification_moves_the_digest_at_an_equal_count(self):
        """``counts`` folds every waiting kind into one number, so a slot going
        from waiting_input to waiting_permission -- or two slots swapping
        states -- leaves the counts identical. The per-slot classification is
        what makes that a change."""
        base = _health(running=1)
        base["slots"]["sess-2"] = {"classification": "waiting_input", "age_secs": 3.0}
        moved = _health(running=1)
        moved["slots"]["sess-2"] = {"classification": "waiting_permission", "age_secs": 3.0}
        assert session_health.health_verdict_fingerprint(
            base
        ) != session_health.health_verdict_fingerprint(moved)
        swapped = _health(running=1)
        swapped["slots"] = {
            "sess-1": {"classification": "waiting_input", "age_secs": 3.0},
            "sess-2": {"classification": "running", "age_secs": 12.5},
        }
        assert session_health.health_verdict_fingerprint(
            base
        ) != session_health.health_verdict_fingerprint(swapped)

    def test_an_effective_cap_change_moves_the_digest(self):
        base = _health(running=1)
        base["effective_caps"] = {"subagents": {"adaptive": 4, "ceiling": 8, "paused": False}}
        same = _health(running=1)
        same["effective_caps"] = {"subagents": {"paused": False, "ceiling": 8, "adaptive": 4}}
        lowered = _health(running=1)
        lowered["effective_caps"] = {"subagents": {"adaptive": 2, "ceiling": 8, "paused": False}}
        fp = session_health.health_verdict_fingerprint
        assert fp(base) == fp(same)  # key order is not a change
        assert fp(base) != fp(lowered)

    def test_a_balanced_task_row_swap_moves_the_digest(self):
        """Task rows reach ``counts`` only as ``len(waiting)``: task A leaving a
        wait state as task B enters one keeps every count still, yet the rows a
        subscriber holds are wrong. Row identity and state are what catch it;
        the row's age is still ignored."""
        fp = session_health.health_verdict_fingerprint

        def row(task_id: str, state: str, age: float) -> dict:
            return {"kind": "task", "id": task_id, "state": state, "reason": state, "age_secs": age}

        a = _health(running=1)
        a["waiting"] = [row("t-a", "waiting_dependency", 3.0)]
        a["recovering"] = [row("t-c", "retry_scheduled", 1.0)]
        same_older = _health(running=1)
        same_older["waiting"] = [row("t-a", "waiting_dependency", 99.0)]
        same_older["recovering"] = [row("t-c", "retry_scheduled", 50.0)]
        assert fp(a) == fp(same_older)
        swapped = _health(running=1)
        swapped["waiting"] = [row("t-b", "waiting_dependency", 3.0)]
        swapped["recovering"] = [row("t-c", "retry_scheduled", 1.0)]
        assert fp(a) != fp(swapped)
        restated = _health(running=1)
        restated["waiting"] = [row("t-a", "waiting_input", 3.0)]
        restated["recovering"] = [row("t-c", "retry_scheduled", 1.0)]
        assert fp(a) != fp(restated)
        # A recovering row moving between task ids at an equal count, too.
        other_recovering = _health(running=1)
        other_recovering["waiting"] = [row("t-a", "waiting_dependency", 3.0)]
        other_recovering["recovering"] = [row("t-d", "retry_scheduled", 1.0)]
        assert fp(a) != fp(other_recovering)

    def test_the_queue_by_state_tally_moves_the_digest_but_its_oldest_wait_does_not(self):
        fp = session_health.health_verdict_fingerprint
        a = _health(running=1)
        a["queued"] = {
            "available": True,
            "count": 2,
            "oldest_wait_secs": 4.0,
            "by_state": {"queued": 2},
        }
        aged = _health(running=1)
        aged["queued"] = {
            "available": True,
            "count": 2,
            "oldest_wait_secs": 40.0,
            "by_state": {"queued": 2},
        }
        moved = _health(running=1)
        moved["queued"] = {
            "available": True,
            "count": 2,
            "oldest_wait_secs": 4.0,
            "by_state": {"queued": 1, "waiting_dependency": 1},
        }
        assert fp(a) == fp(aged)
        assert fp(a) != fp(moved)

    def test_survives_a_missing_or_malformed_health_dict(self):
        for bad in (None, {}, {"counts": None, "stalled": 7, "degrade_reason": None}):
            assert isinstance(session_health.health_verdict_fingerprint(bad), str)


# --- when the signal fires -----------------------------------------------


class TestPublishOnChange:
    def test_first_computation_is_a_baseline_not_a_change(self):
        state = _RecordingState()
        mon = session_health.SessionHealthMonitor()
        assert session_health.publish_health_change(state, _health(running=1), monitor=mon) is False
        assert state.frames == []

    def test_a_moved_verdict_publishes_once_and_an_unchanged_one_stays_silent(self):
        state = _RecordingState()
        mon = session_health.SessionHealthMonitor()
        session_health.publish_health_change(state, _health(running=1), monitor=mon)  # baseline
        assert session_health.publish_health_change(state, _health(running=2), monitor=mon) is True
        assert [t for t, _ in state.frames] == [session_health.SESSION_HEALTH_EVENT]
        # Re-publishing the same verdict must not re-broadcast.
        for _ in range(3):
            assert (
                session_health.publish_health_change(state, _health(running=2), monitor=mon)
                is False
            )
        assert len(state.frames) == 1

    def test_a_failed_broadcast_leaves_the_change_pending(self):
        """A dropped frame must not be recorded as delivered."""

        class _Broken(_RecordingState):
            def __init__(self) -> None:
                super().__init__()
                self.attempts = 0

            def broadcast_ws(self, msg_type: str, data: object) -> None:
                self.attempts += 1
                raise RuntimeError("transport gone")

        state = _Broken()
        mon = session_health.SessionHealthMonitor()
        session_health.publish_health_change(state, _health(running=1), monitor=mon)
        assert session_health.publish_health_change(state, _health(running=2), monitor=mon) is False
        # Same verdict, retried: still pending, so it is attempted again.
        assert session_health.publish_health_change(state, _health(running=2), monitor=mon) is False
        assert state.attempts == 2

    def test_a_state_without_a_transport_is_not_an_error(self):
        mon = session_health.SessionHealthMonitor()
        session_health.publish_health_change(object(), _health(running=1), monitor=mon)
        assert (
            session_health.publish_health_change(object(), _health(running=2), monitor=mon) is False
        )


# --- the payload carries no session data ---------------------------------


class TestSignalCarriesNoSessionData:
    def test_payload_is_a_bare_timestamp(self):
        state = _RecordingState()
        mon = session_health.SessionHealthMonitor()
        session_health.publish_health_change(state, _health(running=1), monitor=mon)
        session_health.publish_health_change(
            state, _health(running=4, stalled=("sess-secret",), degrade="paused"), monitor=mon
        )
        _kind, payload = state.frames[0]
        assert isinstance(payload, dict)
        # A CLOSED key set: a future field carrying session state fails here.
        assert set(payload) == {"ts"}
        assert isinstance(payload["ts"], float)

    def test_no_slot_key_classification_or_count_appears_in_the_frame(self):
        state = _RecordingState()
        mon = session_health.SessionHealthMonitor()
        session_health.publish_health_change(state, _health(running=1), monitor=mon)
        session_health.publish_health_change(
            state,
            _health(running=4, stalled=("sess-secret",), degrade="paused"),
            monitor=mon,
        )
        blob = json.dumps(state.frames[0][1])
        for leak in ("sess-secret", "sess-1", "running", "stalled", "paused", "classification"):
            assert leak not in blob


# --- the scope gate is unchanged -----------------------------------------


class TestSignalRidesTheExistingSessionsDeclaration:
    APP = "board-under-test"

    def _allowed(self, declared: list[str]) -> bool:
        # Seed the declaration cache as an INSTALLED, enabled app: a cold miss
        # takes the "unknown app, refresh scheduled" path and would decide this
        # on cache timing rather than on the scope.
        allowed = ws_event_scope.build_allowed_event_set(declared)
        ws_event_scope._declared_cache[self.APP] = (time.monotonic(), True, allowed)
        state = MagicMock()
        state._slots = {}
        return ws_event_scope.ws_event_allowed(
            session_health.SESSION_HEALTH_EVENT,
            {"ts": 1.0},
            app=self.APP,
            allowed_events=allowed,
            state=state,
        )

    def test_an_app_declaring_nothing_is_denied(self):
        """The signal must not be a free Tier-0 frame."""
        assert self._allowed([]) is False

    def test_the_existing_sessions_declaration_already_covers_it(self):
        # `sessions` is the declaration that already gates `sessions_restarting`
        # and `GET /api/sessions/health`; nothing new is granted by adding this
        # event to it.
        assert self._allowed(["sessions"]) is True
        assert self._allowed(["sessions:all"]) is True

    def test_an_unrelated_declaration_does_not_reach_it(self):
        assert self._allowed(["slots:all"]) is False
        assert self._allowed(["notification:all"]) is False


# --- the driver seam -----------------------------------------------------


class TestRefreshSeamSignals:
    @pytest.mark.asyncio
    async def test_refresh_session_health_signals_when_the_verdict_moves(self):
        """The seam the WS timer driver calls is what emits the signal."""
        verdicts = [_health(running=1), _health(running=1), _health(running=3)]

        def fake_compute(*args, **kwargs):
            return verdicts.pop(0)

        state = _RecordingState()
        with patch(
            "kiro_crew.dashboard.session_health.compute_session_health", side_effect=fake_compute
        ):
            for _ in range(3):
                sessions._health_cache_ts = 0.0  # expire the TTL between ticks
                await sessions.refresh_session_health(state)

        kinds = [t for t, _ in state.frames]
        assert kinds == [session_health.SESSION_HEALTH_EVENT]

    @pytest.mark.asyncio
    async def test_the_endpoint_still_serves_the_full_payload(self):
        """The refactor must not change what /api/sessions/health returns."""
        with patch(
            "kiro_crew.dashboard.session_health.compute_session_health",
            return_value={"stalled": {"sess-1": {"reason": "tool_stall"}}},
        ):
            resp = await sessions.api_sessions_health(
                make_mocked_request("GET", "/api/sessions/health")
            )
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["stalled"] == {"sess-1": {"reason": "tool_stall"}}
        assert set(body) >= {
            "stalled",
            "slots",
            "waiting",
            "recovering",
            "queued",
            "effective_caps",
            "degrade_reason",
            "counts",
        }


# --- the timer driver only runs for a connection that asked for the signal ---


class TestDriverGate:
    """The driver exists solely to feed this event, so it follows the declaration.

    Unit level first: the predicate the gate reads. The functional test below
    drives ``api_ws`` and watches whether the recompute actually happens, because
    a predicate that is right while nothing consults it buys nothing.
    """

    def test_a_connection_declaring_nothing_holds_no_declaration(self):
        assert (
            ws_event_scope.global_event_declared(session_health.SESSION_HEALTH_EVENT, frozenset())
            is False
        )

    def test_the_sessions_declaration_holds_it_in_both_spellings(self):
        for declared in ({"sessions"}, {"sessions:all"}):
            assert (
                ws_event_scope.global_event_declared(
                    session_health.SESSION_HEALTH_EVENT, frozenset(declared)
                )
                is True
            )

    def test_an_unrelated_declaration_does_not_hold_it(self):
        for declared in ({"slots:all"}, {"notification:all"}, {"artifacts"}):
            assert (
                ws_event_scope.global_event_declared(
                    session_health.SESSION_HEALTH_EVENT, frozenset(declared)
                )
                is False
            )

    def test_an_unknown_event_is_denied_rather_than_defaulted(self):
        """Matches the gate's own deny-by-default for an event not in the table."""
        assert (
            ws_event_scope.global_event_declared("no_such_event", frozenset({"sessions"})) is False
        )

    def test_the_wildcard_declaration_carries_it(self):
        """``["*"]`` expands to every scope, so it must reach this one too."""
        assert (
            ws_event_scope.global_event_declared(
                session_health.SESSION_HEALTH_EVENT,
                ws_event_scope.build_allowed_event_set(["*"]),
            )
            is True
        )


class TestDriverGateIsFunctional:
    """Drive ``api_ws`` and observe whether the recompute runs.

    Modelled on ``test_ws_event_scoping.TestDirectSendGrantsAreAudited``: a fake
    socket through the real connect path, rather than asserting on the shape of
    the source. The observable is ``refresh_session_health`` being called, which
    is the work the gate exists to avoid.
    """

    APP = "board-under-test"

    def _manifest(self, events: list[str]):
        manifest = MagicMock()
        manifest.permissions.events = events
        manifest.permissions.exposeToApps = []
        return manifest

    def _fake_ws(self):
        class FakeWebSocket:
            def __init__(self) -> None:
                # OPEN, unlike the connect-only fake in test_ws_event_scoping:
                # the driver loop is `while not ws.closed`, so a closed socket
                # would make both the gated and ungated cases look identical.
                self.closed = False
                self.sent: list[dict] = []
                self._flags: dict = {}

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
                # Hold the connection open long enough for a driver -- if one was
                # started -- to reach its first tick, then end the message loop.
                await asyncio.sleep(0.15)
                raise StopAsyncIteration

        return FakeWebSocket()

    def _drive_connect(
        self, monkeypatch, *, declared: list[str], interval: float = 0.01
    ) -> list[object]:
        """Connect an app token declaring *declared*; return the refresh calls."""
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.handlers import source_providers

        monkeypatch.setattr(ws_event_scope, "is_app_enabled", lambda _n: True)
        monkeypatch.setattr(ws_event_scope, "get_app_manifest", lambda _n: self._manifest(declared))
        monkeypatch.setattr(ws_event_scope, "_declared_cache", {})

        calls: list[object] = []

        async def _recording_refresh(state):
            calls.append(state)
            return {}

        # ws.py imports both of these INSIDE the loop body, so patching the
        # module attributes here is what the driver will pick up when it runs.
        monkeypatch.setattr(sessions, "refresh_session_health", _recording_refresh)
        monkeypatch.setattr(sessions, "_HEALTH_REFRESH_SECS", interval)

        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.side_effect = lambda **_kw: []
        state._yolo = False

        app_name = self.APP

        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"app": app_name})
                self["is_dashboard_user"] = False
                self.app = {"state": state}

        fake_ws = self._fake_ws()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", MagicMock())

        asyncio.run(dashboard_ws.api_ws(Request()))  # type: ignore[arg-type]
        return calls

    def test_a_connection_that_declared_nothing_drives_nothing(self, monkeypatch):
        """No possible recipient, so no recomputation -- not even one tick."""
        assert self._drive_connect(monkeypatch, declared=[]) == []

    def test_an_unrelated_declaration_drives_nothing(self, monkeypatch):
        assert self._drive_connect(monkeypatch, declared=["slots:all"]) == []

    def test_a_connection_declaring_sessions_does_drive_the_refresh(self, monkeypatch):
        """The negative control: without this, the test above passes vacuously."""
        assert self._drive_connect(monkeypatch, declared=["sessions"]), (
            "a socket that declared `sessions` must still drive the refresh, "
            "or the gate has turned the mechanism off entirely"
        )

    def test_the_first_tick_is_at_connect_not_after_an_interval(self, monkeypatch):
        """The first computation in a process is the silent baseline. A driver
        that slept before its first tick would let a verdict that moved during
        that sleep BECOME the baseline and never signal it -- so the baseline is
        pinned at connect time, before any interval has elapsed."""
        # An interval far longer than the fake socket stays open: a driver that
        # sleeps first never reaches refresh; one that refreshes first does.
        calls = self._drive_connect(monkeypatch, declared=["sessions"], interval=60.0)
        assert len(calls) == 1
