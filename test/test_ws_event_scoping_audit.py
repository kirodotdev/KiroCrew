"""Per-app WebSocket event scope: grants reach the SEL trail, not just denials.

The ``ws_event_allowed`` wrapper audits what an app socket receives as well as what it is
refused, with every deny its per-window dedup suppresses still counted; the three sends in
``ws.py`` that bypass ``_send_ws_all`` audit their grants themselves; and a dashboard-user
socket's grants are recorded under its reserved auditee through the same path.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import ws_event_scoping_helpers
from ws_event_scoping_helpers import _allowed, _make_state

from kiro_crew.dashboard.ws_event_scope import ws_event_allowed

# The shared fixtures, bound here by name so pytest registers them for this module.
_clear_module_caches = ws_event_scoping_helpers._clear_module_caches
_clear_ws_scope_module_caches = ws_event_scoping_helpers._clear_ws_scope_module_caches


# ---------------------------------------------------------------------------
# Grants are audited, not just denials.
#
# AUTOSDE.yaml `backend-security-controls` (blocking) requires a SEL event for
# every permission decision. Auditing only the refusals leaves "what did this
# app actually receive" unanswerable, which is also the operator question the
# feature doc admits it could not answer.
#
# The audit lives in the `ws_event_allowed` WRAPPER rather than at each
# `return True`, so a branch added later is covered without remembering to
# report itself. The per-tier test below is what pins that: it drives one event
# from EACH tier and asserts all of them produce a record.
# ---------------------------------------------------------------------------


class TestGrantsAreAudited:
    APP = "mochi-pet"

    def _state_with_own_slot(self):
        slot = MagicMock()
        slot._app = self.APP
        slot.key = "s1"
        return _make_state({"s1": slot})

    def _enable(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        mod._declared_cache[self.APP] = (time.monotonic(), True, frozenset())

    def test_an_allowed_event_is_audited_as_granted(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        self._enable()
        state = self._state_with_own_slot()
        with patch.object(mod, "_audit_decision") as audit:
            assert (
                ws_event_allowed(
                    "chat_delta",
                    {"slot": "s1"},
                    app=self.APP,
                    allowed_events=frozenset(),
                    state=state,
                )
                is True
            )
        audit.assert_called_once()
        assert audit.call_args.args[2] == "granted", "outcome must read as a grant"

    def test_every_tier_produces_a_record(self):
        """Pins that the wrapper covers all return-True paths, not just one.

        Tier 0, the always-admitted envelopes and the slot-scoped branch each
        return True from a DIFFERENT place in the decision function. If the audit
        were attached per-branch instead of to the result, whichever branch was
        forgotten would show up here.
        """
        from kiro_crew.dashboard import ws_event_scope as mod

        self._enable()
        state = self._state_with_own_slot()
        batch = sorted(mod._SUBAGENT_BATCH_EVENTS)[0]
        cases = [
            (sorted(mod._TIER0_ALWAYS)[0], {}, frozenset()),  # Tier 0
            ("slots", {}, frozenset()),  # admitted whole
            (batch, {}, frozenset()),  # admitted whole
            ("chat_delta", {"slot": "s1"}, frozenset()),  # own slot
            ("log", {}, _allowed("log")),  # global, declared
        ]
        for event, payload, scopes in cases:
            with patch.object(mod, "_audit_decision") as audit:
                assert (
                    ws_event_allowed(
                        event,
                        payload,
                        app=self.APP,
                        allowed_events=scopes,
                        state=state,
                    )
                    is True
                ), f"{event} should be allowed in this setup"
            assert audit.call_count == 1, f"{event} was allowed without an audit record"

    def test_repeated_grants_are_deduplicated(self):
        self._enable()
        state = self._state_with_own_slot()

        # Count SEL emissions, not calls into the audit helper: the helper runs
        # every time and the window is what collapses the write.
        with patch("kiro_crew.sel.sel") as sel_factory:
            for _ in range(5):
                ws_event_allowed(
                    "chat_delta",
                    {"slot": "s1"},
                    app=self.APP,
                    allowed_events=frozenset(),
                    state=state,
                )
            calls = sel_factory.return_value.log_api_access.call_count
        assert calls == 1, (
            f"5 identical grants emitted {calls} SEL records; the dedup window "
            "must collapse them into one"
        )

    def test_a_grant_does_not_starve_the_deny_record(self):
        """Grants and denials must not share a dedup slot.

        Same app, same event: allowed on its own slot, denied on someone else's.
        If both used one key, whichever happened first would suppress the other
        for the whole window and the denial would go unrecorded.
        """
        self._enable()
        own = MagicMock()
        own._app = self.APP
        foreign = MagicMock()
        foreign._app = "other-app"
        foreign._origin = ""
        state = _make_state({"s1": own, "s2": foreign})

        with patch("kiro_crew.sel.sel") as sel_factory:
            ws_event_allowed(
                "chat_delta",
                {"slot": "s1"},
                app=self.APP,
                allowed_events=frozenset(),
                state=state,
            )
            ws_event_allowed(
                "chat_delta",
                {"slot": "s2"},
                app=self.APP,
                allowed_events=frozenset(),
                state=state,
            )
            outcomes = [
                c.kwargs.get("outcome")
                for c in sel_factory.return_value.log_api_access.call_args_list
            ]
        assert any(o == "granted" for o in outcomes), f"no grant recorded: {outcomes}"
        assert any(
            o and o.startswith("denied:") for o in outcomes
        ), f"no denial recorded: {outcomes}"


class TestSuppressedDenyCountIsReported:
    """The dedup comment claimed suppressed denies were "counted" while
    the map held only a timestamp — no counter existed. The tally must actually
    reach the trail, so a burst is visible as volume without one SEL write per
    frame (this gate runs per event PER CLIENT on the broadcast hot path)."""

    def _clear(self):
        from kiro_crew.dashboard import ws_event_scope as _wes

        _wes._sel_last_audit.clear()

    def test_next_emission_reports_the_suppressed_tally(self):
        from kiro_crew.dashboard import ws_event_scope as _wes

        self._clear()
        state = _make_state({})
        with patch("kiro_crew.sel.sel") as m:
            # First denial emits with no tally.
            _wes._audit_deny("app-x", "chat_chunk", "slot_missing")
            assert m.return_value.log_api_access.call_count == 1
            assert "suppressed" not in str(
                m.return_value.log_api_access.call_args.kwargs["resources"]
            )
            # Three more identical denials inside the window are collapsed...
            for _ in range(3):
                _wes._audit_deny("app-x", "chat_chunk", "slot_missing")
            assert m.return_value.log_api_access.call_count == 1
            # ...and the tally rides the next emission once the window passes.
            key = ("app-x", "chat_chunk", "slot_missing")
            ts, count = _wes._sel_last_audit[key]
            assert count == 3
            _wes._sel_last_audit[key] = (ts - _wes._SEL_DEDUP_WINDOW_SECS - 1, count)
            _wes._audit_deny("app-x", "chat_chunk", "slot_missing")
            assert m.return_value.log_api_access.call_count == 2
            assert "suppressed=3" in str(
                m.return_value.log_api_access.call_args.kwargs["resources"]
            )
        assert state is not None  # harness precondition
        self._clear()

    def test_tally_resets_after_emission(self):
        from kiro_crew.dashboard import ws_event_scope as _wes

        self._clear()
        with patch("kiro_crew.sel.sel"):
            _wes._audit_deny("app-y", "log", "unknown_event")
            key = ("app-y", "log", "unknown_event")
            assert _wes._sel_last_audit[key][1] == 0
        self._clear()


# ---------------------------------------------------------------------------
# Direct-send grant auditing in ws.py
#
# Three sends reach an app socket WITHOUT passing ``_send_ws_all``, so
# ``ws_event_allowed`` never records them: the initial slots push's ``yolo``
# envelope field, the periodic ``dashboard`` status frame, and the
# ``subscribe_logs`` ring replay. These are FUNCTIONAL tests that drive
# ``api_ws`` rather than asserting on the shape of the source, because the
# earlier spelling-pinned guards for two of them broke the moment the shared
# ``_audit_grant_quietly`` helper replaced the inlined ``try``/``except`` --
# the same failure mode the off-loop guard in this file already documents
# ("a guard that pins a spelling resists refactoring instead of catching bugs").
# ---------------------------------------------------------------------------


class TestDirectSendGrantsAreAudited:
    APP = "mochi-pet"

    def _manifest(self, events: list[str]):
        manifest = MagicMock()
        manifest.permissions.events = events
        manifest.permissions.exposeToApps = []
        return manifest

    def _fake_ws(self):
        """A socket that ends both loops immediately.

        ``closed = True`` stops ``_push_status`` before its first iteration and
        ``__anext__`` raising ends the message loop, so a test that wants either
        one drives it explicitly instead of racing the real timers.
        """

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = True
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
                # Connect snapshot sends a pre-dumped string (offender-note
                # seam); parse back so assertions keep reading dict frames.
                self.sent.append(json.loads(payload))

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        return FakeWebSocket()

    def _drive_connect(self, monkeypatch, *, declared: list[str], yolo: bool):
        """Run ``api_ws`` through connect + initial slots push for an app token."""
        import asyncio as _aio

        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard import ws_event_scope
        from kiro_crew.dashboard.handlers import source_providers

        monkeypatch.setattr(ws_event_scope, "is_app_enabled", lambda _n: True)
        monkeypatch.setattr(ws_event_scope, "get_app_manifest", lambda _n: self._manifest(declared))
        monkeypatch.setattr(ws_event_scope, "_declared_cache", {})

        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.side_effect = lambda **_kw: []
        state._yolo = yolo

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

        with patch.object(dashboard_ws, "_audit_grant_quietly") as audit:
            _aio.run(dashboard_ws.api_ws(Request()))  # type: ignore[arg-type]
        return fake_ws, audit

    def test_initial_yolo_grant_is_audited(self, monkeypatch):
        """An app declaring ``yolo`` receives the live override state on the
        initial push -- a direct ``send_json``, so nothing else records it.
        """
        fake_ws, audit = self._drive_connect(monkeypatch, declared=["yolo"], yolo=True)

        assert fake_ws.sent, "the initial slots push must still be delivered"
        assert fake_ws.sent[0].get("yolo") is True, (
            "an app declaring yolo must actually receive the field "
            "(otherwise this test would pass for the wrong reason)"
        )
        events = [c.args[1] for c in audit.call_args_list]
        assert "slots_yolo" in events, "the yolo envelope grant must be audited"

    def test_no_yolo_declaration_means_no_grant_and_no_record(self, monkeypatch):
        """The audit follows the GRANT, not the code path: an app without the
        scope never receives ``yolo``, so there is nothing to record.
        """
        fake_ws, audit = self._drive_connect(monkeypatch, declared=["slots:own"], yolo=True)

        assert fake_ws.sent
        assert (
            "yolo" not in fake_ws.sent[0]
        ), "withheld field must be ABSENT, not sent as a falsy default"
        events = [c.args[1] for c in audit.call_args_list]
        assert (
            "slots_yolo" not in events
        ), "auditing a grant that did not happen would make the trail lie"

    def test_subscribe_logs_grant_is_audited(self, monkeypatch):
        """The ring replay writes to the socket directly, so the grant that
        admits it needs its own record -- the deny side already had one.
        """
        import asyncio as _aio

        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard import ws_event_scope
        from kiro_crew.dashboard.handlers import source_providers

        monkeypatch.setattr(ws_event_scope, "is_app_enabled", lambda _n: True)
        monkeypatch.setattr(ws_event_scope, "get_app_manifest", lambda _n: self._manifest(["log"]))
        monkeypatch.setattr(ws_event_scope, "_declared_cache", {})

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

        # One `subscribe_logs` frame, then end the loop.
        class Msg:
            def __init__(self) -> None:
                from aiohttp import WSMsgType

                self.type = WSMsgType.TEXT
                self.data = json.dumps({"type": "subscribe_logs"})

        base = self._fake_ws()

        class LoopWs(type(base)):  # type: ignore[misc]
            def __init__(self) -> None:
                super().__init__()
                self._yielded = False

            async def __anext__(self):
                if self._yielded:
                    raise StopAsyncIteration
                self._yielded = True
                return Msg()

        fake_ws = LoopWs()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", MagicMock())

        with patch.object(dashboard_ws, "_audit_grant_quietly") as audit:
            _aio.run(dashboard_ws.api_ws(Request()))  # type: ignore[arg-type]

        state.subscribe_logs.assert_called_once_with(fake_ws)
        events = [c.args[1] for c in audit.call_args_list]
        assert "subscribe_logs" in events, "a granted log subscription must leave an SEL record"

    def test_audit_helper_records_the_grant(self):
        from kiro_crew.dashboard import ws as dashboard_ws

        with patch.object(dashboard_ws, "_audit_allow") as allow:
            dashboard_ws._audit_grant_quietly("mochi-pet", "dashboard")
        allow.assert_called_once_with("mochi-pet", "dashboard")

    def test_audit_helper_names_an_unknown_app_rather_than_sending_empty(self):
        from kiro_crew.dashboard import ws as dashboard_ws

        with patch.object(dashboard_ws, "_audit_allow") as allow:
            dashboard_ws._audit_grant_quietly("", "dashboard")
        allow.assert_called_once_with("<unknown>", "dashboard")

    def test_a_failing_audit_sink_never_breaks_delivery(self):
        """The swallow is the load-bearing part of the helper.

        Dropping a frame the app is entitled to because the SEL sink hiccuped
        would turn an observability fault into a functional outage. Having ONE
        copy of this branch is what makes it testable at all -- inlined at three
        call sites it was three never-executed paths.
        """
        from kiro_crew.dashboard import ws as dashboard_ws

        with patch.object(dashboard_ws, "_audit_allow", side_effect=RuntimeError("sink down")):
            # Must not raise.
            dashboard_ws._audit_grant_quietly("mochi-pet", "subscribe_logs")

    def test_periodic_dashboard_frame_grant_is_audited(self):
        """``_push_status`` sends the Tier-0 status frame straight to the socket
        every few seconds; Tier 0 admits it unconditionally but the admission is
        still a decision, and no other code path records this one.

        Asserted against the source rather than by driving the loop: the pusher
        is a closure inside ``api_ws`` spawned as a background task, so a
        functional test would have to win a race against its own cancellation
        on teardown -- and a test that reconstructed the loop body here would be
        asserting on the reconstruction, not on what ships.

        The check is deliberately REGION-scoped and whitespace-normalised rather
        than a literal spelling, so renaming the helper or reflowing the call
        cannot red it while the invariant holds. That is the correction the
        off-loop guard in this file already went through.
        """
        src = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "ws.py"
        ).read_text(encoding="utf-8")
        # The app-token narrowing block inside _push_status: it strips the
        # owner-only fields, and the grant must be recorded in the same branch.
        marker = 'for _owner_only in ("branch", "commit"):'
        assert marker in src, "the app-token narrowing block moved; re-anchor this guard"
        region = " ".join(src[src.index(marker) :].split())
        # Look only as far as the send that ends the block.
        region = region[: region.index('ws.send_json({"type": "dashboard"')]
        # ``.*`` rather than ``[^)]*``: the auditee is now resolved by a nested
        # call (``_grant_auditee(ws, ws_app)``), and the guard is about the
        # record existing between the narrowing and the send, not its arity.
        assert re.search(r'_audit\w*\(.*"dashboard"\)', region), (
            "the periodic dashboard frame's grant to an app socket must be "
            "SEL-audited between the payload narrowing and the send"
        )


# ---------------------------------------------------------------------------
# Dashboard-user grants reach the SEL trail
#
# ``_ws_client_allowed`` records the grant for a ``_is_dashboard_user`` socket
# at the chokepoint itself, under a reserved auditee that no app id can
# collide with. The three direct-send grant sites in ``ws.py`` (``slots_yolo``,
# ``dashboard``, ``subscribe_logs``) and the live log fan-out's per-send
# recheck audit the dashboard user through the same path, so one socket kind
# has one identity in the stream whichever path delivers a frame, and the
# trail shows what is handed to the operator as well as what is withheld from
# app tokens.
# ---------------------------------------------------------------------------


def _dashboard_user_ws() -> MagicMock:
    ws = MagicMock()
    ws.closed = False
    store = {"_is_dashboard_user": True, "_app": "", "_allowed_events": frozenset()}
    ws.get.side_effect = lambda k, default=None: store.get(k, default)
    return ws


def _granted(sel_mock) -> list[tuple[str, str]]:
    """(caller, resources) of every granted record the patched sink saw."""
    return [
        (c.kwargs["caller"], c.kwargs["resources"])
        for c in sel_mock.return_value.log_api_access.call_args_list
        if c.kwargs.get("outcome") == "granted"
    ]


class TestDashboardUserGrantsAreAudited:
    def _predicate(self):
        from kiro_crew.dashboard import ws_event_scope as mod
        from kiro_crew.dashboard.state import DashboardState

        mod._sel_last_audit.clear()
        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        return state._ws_client_allowed

    def test_chokepoint_records_the_dashboard_user_grant(self):
        """The pin for the short-circuit itself: restoring the bare
        ``return True`` ahead of the audit makes this red.

        The record carries the reserved auditee rather than the socket's empty
        app claim, so the operator can tell their own socket's grants from an
        unnamed app token's (``<unknown>``) or the fail-closed empty-app deny
        (``<empty>``).
        """
        from kiro_crew.dashboard.ws_event_scope import DASHBOARD_USER_AUDITEE

        allowed = self._predicate()
        with patch("kiro_crew.sel.sel") as sel_mock:
            assert allowed(_dashboard_user_ws(), "chat_chunk", {"slot": "chat-1"}) is True
        assert _granted(sel_mock) == [(DASHBOARD_USER_AUDITEE, "chat_chunk")]
        record = sel_mock.return_value.log_api_access.call_args.kwargs
        assert record["operation"] == "ws_event_scope"
        assert record["caller"] not in ("", "<unknown>", "<empty>")

    def test_dashboard_user_grants_share_the_dedup_window(self):
        """Volume contract: the per-frame decision on the owner's socket is the
        highest-volume class in the trail, and ``_audit_decision`` already
        promises one record per (auditee, event, reason) per window for grants.
        A chunk flood collapses to one record; a different event type is a
        different key and gets its own.
        """
        from kiro_crew.dashboard.ws_event_scope import DASHBOARD_USER_AUDITEE

        allowed = self._predicate()
        ws = _dashboard_user_ws()
        with patch("kiro_crew.sel.sel") as sel_mock:
            for _ in range(5):
                assert allowed(ws, "chat_chunk", {"slot": "chat-1"}) is True
            assert allowed(ws, "slots", []) is True
        assert _granted(sel_mock) == [
            (DASHBOARD_USER_AUDITEE, "chat_chunk"),
            (DASHBOARD_USER_AUDITEE, "slots"),
        ]

    def test_a_failing_audit_sink_never_withholds_a_dashboard_frame(self):
        """The audit must never become a gate: an entitled frame is delivered
        even when the sink raises. Patched at the module attribute the
        chokepoint resolves at call time, so this exercises the real swallow.
        """
        from kiro_crew.dashboard import ws_event_scope as mod

        allowed = self._predicate()
        with patch.object(mod, "_audit_allow", side_effect=RuntimeError("sink down")):
            assert allowed(_dashboard_user_ws(), "chat_chunk", {"slot": "chat-1"}) is True

    def test_reserved_auditee_is_outside_the_app_id_namespace(self):
        """No manifest can claim the label, so a record under it is always the
        dashboard user and never an app that picked a confusable id.
        """
        from kiro_crew.apps.manifest import KEBAB_RE
        from kiro_crew.dashboard.ws_event_scope import DASHBOARD_USER_AUDITEE

        assert KEBAB_RE.match(DASHBOARD_USER_AUDITEE) is None
        assert DASHBOARD_USER_AUDITEE not in ("", "<unknown>", "<empty>")

    def test_grant_auditee_names_the_socket_kind(self):
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.ws_event_scope import DASHBOARD_USER_AUDITEE

        assert dashboard_ws._grant_auditee(_dashboard_user_ws(), "") == DASHBOARD_USER_AUDITEE
        app_ws = MagicMock()
        app_ws.get.side_effect = lambda k, default=None: {"_is_dashboard_user": False}.get(
            k, default
        )
        assert dashboard_ws._grant_auditee(app_ws, "mochi-pet") == "mochi-pet"
        # An absent flag is an app socket, never inferred as the owner.
        bare = MagicMock()
        bare.get.side_effect = lambda k, default=None: default
        assert dashboard_ws._grant_auditee(bare, "mochi-pet") == "mochi-pet"

    # -- direct-send sites in ws.py, driven functionally like the app-token
    #    tests in TestDirectSendGrantsAreAudited -----------------------------

    def _dashboard_state(self):
        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.side_effect = lambda **_kw: []
        state._yolo = True
        state._folders = []
        state.folders_generation.return_value = 1
        return state

    def _dashboard_request(self, state):
        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"app": "", "user": "U_OWNER"})
                self["is_dashboard_user"] = True
                self.app = {"state": state}

        return Request()

    def _wire(self, monkeypatch, fake_ws):
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.handlers import source_providers

        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", MagicMock())
        monkeypatch.setattr(source_providers, "schedule_visibility_refresh", MagicMock())

    def test_connect_time_yolo_grant_to_the_dashboard_user_is_audited(self, monkeypatch):
        """The owner always receives the live blanket-approval override on the
        initial push; that is a grant of operator security posture and was the
        one socket kind recorded nowhere (the site was gated on the flag).
        """
        import asyncio as _aio

        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.ws_event_scope import DASHBOARD_USER_AUDITEE

        state = self._dashboard_state()
        fake_ws = TestDirectSendGrantsAreAudited()._fake_ws()
        self._wire(monkeypatch, fake_ws)
        with patch.object(dashboard_ws, "_audit_grant_quietly") as audit:
            _aio.run(dashboard_ws.api_ws(self._dashboard_request(state)))  # type: ignore[arg-type]

        assert (
            fake_ws.sent and fake_ws.sent[0].get("yolo") is True
        ), "the dashboard user must actually receive the field"
        assert (DASHBOARD_USER_AUDITEE, "slots_yolo") in [
            tuple(c.args) for c in audit.call_args_list
        ]

    def _drive_one_message(self, monkeypatch, state, payload: dict):
        """Run ``api_ws`` for a dashboard user whose socket yields ONE frame."""
        import asyncio as _aio

        from aiohttp import WSMsgType

        from kiro_crew.dashboard import ws as dashboard_ws

        class Msg:
            type = WSMsgType.TEXT
            data = json.dumps(payload)

        base = TestDirectSendGrantsAreAudited()._fake_ws()

        class LoopWs(type(base)):  # type: ignore[misc]
            def __init__(self) -> None:
                super().__init__()
                self._yielded = False

            async def __anext__(self):
                if self._yielded:
                    raise StopAsyncIteration
                self._yielded = True
                return Msg()

        fake_ws = LoopWs()
        self._wire(monkeypatch, fake_ws)
        _aio.run(dashboard_ws.api_ws(self._dashboard_request(state)))  # type: ignore[arg-type]
        return fake_ws

    def test_subscribe_logs_grant_to_the_dashboard_user_is_audited(self, monkeypatch):
        """The ring replay hands over the privileged gateway log history; the
        grant that admits the owner to it was gated out of the record.
        """
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.ws_event_scope import DASHBOARD_USER_AUDITEE

        state = self._dashboard_state()
        with patch.object(dashboard_ws, "_audit_grant_quietly") as audit:
            fake_ws = self._drive_one_message(monkeypatch, state, {"type": "subscribe_logs"})

        state.subscribe_logs.assert_called_once_with(fake_ws)
        assert (DASHBOARD_USER_AUDITEE, "subscribe_logs") in [
            tuple(c.args) for c in audit.call_args_list
        ]

    def test_reconnect_replay_to_the_dashboard_user_takes_the_chokepoint(self, monkeypatch):
        """The subagent reconnect replay writes to the socket directly but
        filters every frame through ``_ws_client_allowed``, so the record
        present after a gateway restart is the same record the live broadcast
        leaves -- the divergence the report worried about cannot open.
        """
        from kiro_crew.dashboard import ws_event_scope as mod
        from kiro_crew.dashboard.state import DashboardState
        from kiro_crew.dashboard.ws_event_scope import DASHBOARD_USER_AUDITEE

        mod._sel_last_audit.clear()
        state = self._dashboard_state()
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        state.native_subagent_snapshots.return_value = [
            {
                "id": "sa-1",
                "slot": "chat-1",
                "task": "t",
                "agent": "default",
                "streaming": "",
                "last_tool": "",
                "started": 1.0,
                "done": False,
            }
        ]
        state.subagents = None
        with patch("kiro_crew.sel.sel") as sel_mock:
            fake_ws = self._drive_one_message(monkeypatch, state, {"type": "subscribe_subagents"})

        assert any(
            f.get("type") == "subagent_snapshot" for f in fake_ws.sent
        ), "the replay frame must still be delivered"
        assert (DASHBOARD_USER_AUDITEE, "subagent_snapshot") in _granted(sel_mock)

    def test_periodic_dashboard_frame_grant_covers_both_socket_kinds(self):
        """Structural pin, same anchoring as the app-token guard above: the
        ``dashboard`` grant record must sit OUTSIDE the app-token narrowing
        block (at the indentation of the send that follows it), not inside
        the ``if not _is_dashboard_user`` branch where the owner's socket
        never reaches it.
        """
        src = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "ws.py"
        ).read_text(encoding="utf-8")
        marker = 'for _owner_only in ("branch", "commit"):'
        assert marker in src, "the app-token narrowing block moved; re-anchor this guard"
        # Start at the beginning of the marker's LINE so its indentation is kept.
        start = src.rfind("\n", 0, src.index(marker)) + 1
        region = src[start : src.index('ws.send_json({"type": "dashboard"')]
        lines = region.splitlines()
        narrowing_indent = len(lines[0]) - len(lines[0].lstrip())
        audit_lines = [ln for ln in lines if re.search(r'_audit\w*\(.*"dashboard"\)', ln)]
        assert len(audit_lines) == 1, "exactly one grant record for the periodic frame"
        audit_indent = len(audit_lines[0]) - len(audit_lines[0].lstrip())
        assert audit_indent < narrowing_indent, (
            "the periodic dashboard frame's grant must be recorded for the "
            "dashboard user too, i.e. outside the app-token-only branch"
        )
