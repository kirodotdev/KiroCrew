"""Per-app WebSocket event scope: an open socket's scope and the fan-out that applies it.

A socket's scope shrinks while it stays open when its manifest narrows or its app is disabled,
the slots re-push and its envelope carry only what the scope admits, ``_send_ws_all`` composes
the gate, the payload filter and the fan-out, the subscribe handlers gate their replays, and
the ``exposeToApps`` cache keeps every disk read off the event loop.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import ws_event_scoping_helpers
from ws_event_scoping_helpers import _allowed, _make_slot, _make_state

from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.dashboard.ws_event_scope import build_allowed_event_set, ws_event_allowed


def _state_source() -> str:
    """state.py and the state_parts owners it composes, as one text."""
    import kiro_crew.dashboard.state as _st

    facade = Path(_st.__file__)
    owners = sorted((facade.parent / "state_parts").glob("[!_]*.py"))
    assert owners, "no state_parts owner found beside state.py"
    return "\n".join(path.read_text(encoding="utf-8") for path in [facade, *owners])


# The shared fixtures, bound here by name so pytest registers them for this module.
_clear_module_caches = ws_event_scoping_helpers._clear_module_caches
_clear_ws_scope_module_caches = ws_event_scoping_helpers._clear_ws_scope_module_caches


# ---------------------------------------------------------------------------
# Slots re-push payload filtering — an app that declared slots:own must NOT
# receive other apps' or users' slot metadata on ``push_slots_update`` /
# ``_broadcast`` re-pushes.  The filter runs inside DashboardState.
# _serialize_for_client on the send layer, so we assert that behaviour by
# reading the module source to guard against regression (constructing a real
# DashboardState in-process here would be over-engineered).
# ---------------------------------------------------------------------------


class TestSlotsRepushFilter:
    """Behavioral: an app that declared slots:own must NOT receive other
    apps' or users' slot metadata on ``push_slots_update`` re-pushes.  The
    filter runs inside ``DashboardState._serialize_for_client``.
    """

    def _make_state_with_slots(self, slot_map: dict) -> Any:
        # Wrap into a state that mimics DashboardState._serialize_for_client's
        # required surface.  We bind the real method to a MagicMock so it
        # runs against our slot map instead of a live DashboardState.
        from kiro_crew.dashboard.state import DashboardState

        state = MagicMock(spec=DashboardState)
        state._slots = slot_map
        state._serialize_for_client = DashboardState._serialize_for_client.__get__(state)
        return state

    def _ws(self, *, is_dashboard: bool, app: str = "", events=frozenset()):
        ws = MagicMock()
        store = {
            "_is_dashboard_user": is_dashboard,
            "_app": app,
            "_allowed_events": events,
        }
        ws.get.side_effect = lambda k, default=None: store.get(k, default)
        return ws

    def test_dashboard_user_receives_full_slots_list(self):
        import json

        slot_a = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="a")
        slot_b = _make_slot(owner_app="other", origin=SlotOrigin.APP, key="b")
        state = self._make_state_with_slots({"a": slot_a, "b": slot_b})
        ws = self._ws(is_dashboard=True)
        default_msg = json.dumps(
            {
                "type": "slots",
                "data": [{"key": "a"}, {"key": "b"}],
                "yolo": False,
                "channelTrusted": False,
            }
        )
        result = state._serialize_for_client(
            ws,
            "slots",
            {"slots": [{"key": "a"}, {"key": "b"}], "yolo": False, "channelTrusted": False},
            default_msg,
        )
        # Dashboard user: unchanged default msg (full list).
        assert result == default_msg

    def test_app_with_slots_own_only_sees_own_slot(self):
        import json

        slot_a = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="a")
        slot_b = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="b")
        state = self._make_state_with_slots({"a": slot_a, "b": slot_b})
        ws = self._ws(
            is_dashboard=False,
            app="mochi-pet",
            events=frozenset({"slots:own"}),
        )
        result_str = state._serialize_for_client(
            ws,
            "slots",
            {
                "slots": [{"key": "a"}, {"key": "b"}],
                "yolo": False,
                "channelTrusted": False,
            },
            default_msg="dummy",
        )
        result = json.loads(result_str)
        assert result["type"] == "slots"
        # Only the mochi-pet slot should survive per-app payload filtering.
        assert [s["key"] for s in result["data"]] == ["a"]

    def test_non_slots_event_passes_through_unchanged(self):
        # For any non-``slots`` event, _serialize_for_client returns default.
        state = self._make_state_with_slots({})
        ws = self._ws(is_dashboard=False, app="mochi-pet", events=frozenset())
        default = "OPAQUE"
        assert state._serialize_for_client(ws, "chat_chunk", {}, default) == default


# ---------------------------------------------------------------------------
# A socket's scope must be able to SHRINK while it stays open. `_allowed_events`
# is resolved at connect, so a narrowed or deleted manifest would otherwise keep
# granting revoked scopes until the connection happens to drop — and
# `disable_app` rewrites the registry without closing sockets.
# ---------------------------------------------------------------------------


class TestLiveScopeNarrowing:
    def setup_method(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        mod._declared_cache.clear()
        mod._declared_refreshing.clear()

    def _snapshot(self):
        return _allowed("slots:all", "notification")

    def test_narrowed_manifest_takes_effect_without_a_reconnect(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        mod._declared_cache["mochi-pet"] = (time.monotonic(), True, _allowed("slots:own"))
        eff = mod.effective_allowed_events("mochi-pet", self._snapshot())
        assert "slots:all" not in eff, "a revoked scope must stop being honoured"
        assert "notification" not in eff

    def test_deleted_manifest_collapses_to_tier0_only(self):
        """`app disable` / uninstall leaves the socket open; scopes must go."""
        from kiro_crew.dashboard import ws_event_scope as mod

        mod._declared_cache["mochi-pet"] = (time.monotonic(), False, frozenset())
        assert mod.effective_allowed_events("mochi-pet", self._snapshot()) == frozenset()
        assert mod.app_events_revoked("mochi-pet") is True

    def test_widened_manifest_does_not_escalate_an_open_socket(self):
        """Intersection, not replacement: widening needs a reconnect.

        Otherwise editing a manifest would grant a LIVE session scopes it was
        never authenticated for.
        """
        from kiro_crew.dashboard import ws_event_scope as mod

        mod._declared_cache["mochi-pet"] = (time.monotonic(), True, _allowed("slots:own", "log"))
        eff = mod.effective_allowed_events("mochi-pet", _allowed("slots:own"))
        assert eff == _allowed("slots:own")
        assert "log" not in eff, "a widened manifest must not reach an open socket"

    def test_cold_miss_keeps_the_connect_snapshot_and_schedules_a_refresh(self):
        """Fail-SAFE, not fail-closed: an empty fallback would withhold every
        event from every app on the first broadcast after a restart. The snapshot
        is itself an authenticated read, so leaning on it briefly widens nothing.
        """
        from kiro_crew.dashboard import ws_event_scope as mod

        snap = self._snapshot()
        with patch.object(mod, "_schedule_declared_refresh") as sched:
            assert mod.effective_allowed_events("mochi-pet", snap) == snap
            sched.assert_called_once_with("mochi-pet")

    def test_stale_entry_is_applied_and_refresh_scheduled(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        mod._declared_cache["mochi-pet"] = (
            time.monotonic() - (mod._MANIFEST_EXPOSE_TTL_SECS + 5),
            True,
            _allowed("notification"),
        )
        with patch.object(mod, "_schedule_declared_refresh") as sched:
            eff = mod.effective_allowed_events("mochi-pet", self._snapshot())
            sched.assert_called_once_with("mochi-pet")
        assert eff == _allowed("notification"), "stale-but-narrower still narrows"

    def test_never_reads_the_disk_on_the_decision_path(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        with (
            patch.object(mod, "get_app_manifest") as gm,
            patch.object(mod, "_schedule_declared_refresh"),
        ):
            mod.effective_allowed_events("mochi-pet", self._snapshot())
            gm.assert_not_called()

    def test_refresh_is_coalesced(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        loop = MagicMock()
        with patch.object(mod.asyncio, "get_running_loop", return_value=loop):
            for _ in range(20):
                mod._schedule_declared_refresh("mochi-pet")
        assert loop.run_in_executor.call_count == 1

    def test_disabled_app_declares_nothing_even_with_manifest_intact(self):
        """`disable_app` flips `enabled` and leaves `app.json` alone.

        Reading the manifest alone reports a disabled app's declarations
        unchanged, so the intersection would keep honouring them.
        """
        from kiro_crew.dashboard import ws_event_scope as mod

        fake = MagicMock()
        fake.permissions.events = ["slots:all", "log"]
        with (
            patch.object(mod, "is_app_enabled", return_value=False),
            patch.object(mod, "get_app_manifest", return_value=fake),
        ):
            assert mod._read_declared_events("mochi-pet") == (False, frozenset())

    def test_enabled_app_declares_its_manifest_events(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        fake = MagicMock()
        fake.permissions.events = ["slots:own"]
        with (
            patch.object(mod, "is_app_enabled", return_value=True),
            patch.object(mod, "get_app_manifest", return_value=fake),
        ):
            assert mod._read_declared_events("mochi-pet") == (True, _allowed("slots:own"))

    def test_enablement_is_checked_before_the_manifest_read(self):
        """A disabled app must not even cost a manifest parse."""
        from kiro_crew.dashboard import ws_event_scope as mod

        with (
            patch.object(mod, "is_app_enabled", return_value=False),
            patch.object(mod, "get_app_manifest") as gm,
        ):
            mod._read_declared_events("mochi-pet")
            gm.assert_not_called()

    def test_missing_manifest_reads_as_no_declarations_but_not_revoked(self):
        """A still-enabled app with an unreadable manifest declares nothing.

        It must NOT read as revoked: a corrupt or transient ``app.json`` read is
        not a disablement, and treating it as one would blank the app's own chat
        for a refresh interval over a filesystem hiccup. Revocation requires
        positive evidence from ``installed.json``.
        """
        from kiro_crew.dashboard import ws_event_scope as mod

        with (
            patch.object(mod, "is_app_enabled", return_value=True),
            patch.object(mod, "get_app_manifest", return_value=None),
        ):
            assert mod._read_declared_events("gone") == (True, frozenset())

    def test_log_subscriber_send_rechecks_the_live_scope(self):
        """The FOURTH read path: log fan-out bypasses the broadcast chokepoint.

        ``subscribe_logs`` grants once and the ring handler then writes straight
        to ``_ws_log_subscribers``, so revoking ``log`` has to be enforced at the
        send instead of at the subscribe. The recheck now goes through
        ``DashboardState._ws_client_allowed`` itself (the same chokepoint every
        other event uses), not a hand-rolled duplicate of its scope comparison,
        so this decision is SEL-audited too.
        """
        import asyncio as _aio

        from kiro_crew.dashboard import ws_event_scope as mod
        from kiro_crew.dashboard.handlers import updates as upd
        from kiro_crew.dashboard.state import DashboardState

        # Declaration set does not contain `log` (revoked).
        mod._declared_cache["mochi-pet"] = (time.monotonic(), True, _allowed("slots:own"))
        ws = MagicMock()
        store = {
            "_is_dashboard_user": False,
            "_app": "mochi-pet",
            "_allowed_events": _allowed("slots:own", "log"),
        }
        ws.get.side_effect = lambda k, default=None: store.get(k, default)
        sent: list[str] = []

        async def _send(msg):
            sent.append(msg)

        ws.send_str = _send
        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        state._ws_log_subscribers = {ws}

        _aio.run(upd._safe_ws_send(ws, '{"type":"log"}', state))
        assert sent == [], "a revoked log scope must stop the stream"
        assert ws not in state._ws_log_subscribers, "and drop the subscription"

    def test_log_subscriber_send_still_delivers_while_declared(self):
        import asyncio as _aio

        from kiro_crew.dashboard import ws_event_scope as mod
        from kiro_crew.dashboard.handlers import updates as upd
        from kiro_crew.dashboard.state import DashboardState

        mod._declared_cache["mochi-pet"] = (time.monotonic(), True, _allowed("log"))
        ws = MagicMock()
        store = {
            "_is_dashboard_user": False,
            "_app": "mochi-pet",
            "_allowed_events": _allowed("log"),
        }
        ws.get.side_effect = lambda k, default=None: store.get(k, default)
        sent: list[str] = []

        async def _send(msg):
            sent.append(msg)

        ws.send_str = _send
        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        state._ws_log_subscribers = {ws}

        _aio.run(upd._safe_ws_send(ws, '{"type":"log"}', state))
        assert sent == ['{"type":"log"}']

    def test_log_subscriber_send_takes_the_chokepoint_for_dashboard_users(self):
        """The dashboard user is delivered every log line -- and that grant is
        recorded, because the send goes through ``_ws_client_allowed`` for this
        socket kind too. The live stream's grant to the owner and the ring
        replay that admits the same socket leave the same SEL record.
        """
        import asyncio as _aio

        from kiro_crew.dashboard import ws_event_scope as mod
        from kiro_crew.dashboard.handlers import updates as upd
        from kiro_crew.dashboard.state import DashboardState

        mod._sel_last_audit.clear()
        ws = MagicMock()
        ws.get.side_effect = lambda k, default=None: (
            True if k == "_is_dashboard_user" else default
        )
        sent: list[str] = []

        async def _send(msg):
            sent.append(msg)

        ws.send_str = _send
        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        state._ws_log_subscribers = {ws}
        with patch("kiro_crew.sel.sel") as sel_mock:
            _aio.run(upd._safe_ws_send(ws, '{"type":"log"}', state))
        assert sent == ['{"type":"log"}'], "the owner must still receive the line"
        assert ws in state._ws_log_subscribers
        grants = [
            c.kwargs
            for c in sel_mock.return_value.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "granted"
        ]
        assert [(g["caller"], g["resources"]) for g in grants] == [
            (mod.DASHBOARD_USER_AUDITEE, "log")
        ], "the live log grant to the dashboard user must leave one SEL record"

    def test_source_guard_status_frame_withholds_checkout_identity(self):
        """Tier 0 holds only while the `dashboard` payload stays non-sensitive.

        `_push_status` writes straight to the socket every few seconds, so the
        frame's CONTENT is the control, not a gate. Counts and environment are
        fine; the checkout's branch and commit say what the operator is working
        on and have no consumer outside the owner surfaces, so they are stripped
        for app tokens instead of moving the whole frame behind a declaration —
        that would cut every existing app off from the version signal it uses to
        reload across a gateway upgrade.
        """
        src = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "ws.py"
        ).read_text(encoding="utf-8")
        assert (
            'for _owner_only in ("branch", "commit")' in src
        ), "the periodic dashboard frame must withhold checkout identity from apps"
        assert 'if not ws.get("_is_dashboard_user", False):' in src
        # And the owner surfaces must NOT be narrowed: /api/status and SSE run on
        # dashboard-user tokens and still need the full snapshot.
        state_src = _state_source()
        assert (
            "branch, commit = self._build_info" in state_src
        ), "status_snapshot itself must keep returning branch/commit"

    def test_source_guard_log_replay_uses_the_live_scope(self):
        """The replay half of the log path.

        Two gates read the log scope: the one-shot ring REPLAY in
        ``subscribe_logs`` and the per-frame send. Fixing only the send left the
        replay able to hand over the whole buffered ring on a revoked scope, so
        this guard covers ``ws.py`` too — the state.py-only guard below is why
        that half was missed.
        """
        src = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "ws.py"
        ).read_text(encoding="utf-8")
        assert (
            "effective_allowed_events(ws_app, allowed_events)" in src
        ), "subscribe_logs must resolve the live scope before replaying the ring"
        assert (
            '"log" in _live or "log:all" in _live' in src
        ), "the replay gate must test the LIVE set, not the connect snapshot"


#
# Narrowing the declaration set is not enough to revoke a disabled app, because
# the own-slot default grants an app its own slots WITHOUT consulting
# declarations at all -- `_slot_visible` returns True on the ownership check
# before `allowed_events` is ever read. `disable_app` does not invalidate the app
# token (`token_auth` has no enablement check; every app backend route gates on
# `is_app_enabled` itself), so a disabled app can keep an authenticated
# `/api/ws` socket open and keep streaming its own slot's chat content.
#
# There are THREE entry points into that default and only one of them passes
# through `ws_event_allowed`, so each is asserted separately here.
# ---------------------------------------------------------------------------


class TestDisabledAppLosesTheOwnSlotDefault:
    APP = "mochi-pet"

    def _revoke(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        mod._declared_cache[self.APP] = (time.monotonic(), False, frozenset())

    def _enable(self, events: frozenset[str] = frozenset()):
        from kiro_crew.dashboard import ws_event_scope as mod

        mod._declared_cache[self.APP] = (time.monotonic(), True, events)

    def _own_slot_state(self):
        slot = MagicMock()
        slot._app = self.APP
        slot.key = "s1"
        return _make_state({"s1": slot})

    def test_gate_denies_own_slot_chat_when_disabled(self):
        """Entry point 1: the per-event gate.

        This one is enforced by ``_slot_visible`` underneath, so it passes with or
        without the gate-level check -- see
        ``test_gate_denies_the_always_admitted_envelopes_when_disabled`` for the
        cases only the gate check covers.
        """
        state = self._own_slot_state()
        self._enable()
        assert (
            ws_event_allowed(
                "chat_delta",
                {"slot": "s1"},
                app=self.APP,
                allowed_events=frozenset(),
                state=state,
            )
            is True
        ), "an ENABLED app sees its own slot by documented default"

        self._revoke()
        assert (
            ws_event_allowed(
                "chat_delta",
                {"slot": "s1"},
                app=self.APP,
                allowed_events=frozenset(),
                state=state,
            )
            is False
        ), "a disabled app must not keep receiving its own chat"

    def test_gate_denies_the_always_admitted_envelopes_when_disabled(self):
        """What the gate-level revocation check uniquely buys.

        ``slots`` and the coalesced subagent batches return True at the gate
        WITHOUT consulting ``allowed_events`` -- they are admitted whole and
        narrowed in the payload. For a revoked app the payload filters empty them
        out, so without the gate check a disabled app receives a steady stream of
        empty envelopes instead of nothing. Denying at the gate is what makes
        "revocation collapses the socket to Tier 0" literally true.
        """
        from kiro_crew.dashboard import ws_event_scope as mod

        state = self._own_slot_state()
        batch = sorted(mod._SUBAGENT_BATCH_EVENTS)[0]

        self._enable()
        for etype in ("slots", batch):
            assert (
                ws_event_allowed(
                    etype,
                    {},
                    app=self.APP,
                    allowed_events=frozenset(),
                    state=state,
                )
                is True
            ), f"{etype} is admitted whole for an enabled app"

        self._revoke()
        for etype in ("slots", batch):
            assert (
                ws_event_allowed(
                    etype,
                    {},
                    app=self.APP,
                    allowed_events=frozenset(),
                    state=state,
                )
                is False
            ), f"{etype} must not be admitted for a revoked app"

    def test_slots_repush_filter_drops_own_slot_when_disabled(self):
        """Entry point 2: ``filter_slots_for_app`` never calls the gate."""
        from kiro_crew.dashboard.ws_event_scope import filter_slots_for_app

        state = self._own_slot_state()
        rows = [{"key": "s1"}]

        self._enable()
        assert filter_slots_for_app(rows, self.APP, frozenset(), state) == rows

        self._revoke()
        assert filter_slots_for_app(rows, self.APP, frozenset(), state) == [], (
            "the slots payload filter bypasses ws_event_allowed, so it needs its "
            "own revocation check"
        )

    def test_subagent_batch_filter_drops_own_items_when_disabled(self):
        """Entry point 3: ``filter_subagent_batch_for_app`` never calls the gate."""
        from kiro_crew.dashboard.ws_event_scope import filter_subagent_batch_for_app

        state = self._own_slot_state()
        items = [{"slot": "s1", "id": "a"}]

        self._enable()
        assert filter_subagent_batch_for_app(items, self.APP, frozenset(), state) == items

        self._revoke()
        assert filter_subagent_batch_for_app(items, self.APP, frozenset(), state) == []

    def test_slots_repush_filter_audits_each_item_decision(self):
        """The per-item slot filter is its own permission decision, not a
        detail of the frame-level grant already logged by ``ws_event_allowed``
        -- it needs an SEL record of its own for the same reason every other
        decision in this module does.
        """
        from kiro_crew.dashboard import ws_event_scope as mod

        self._enable()
        own = _make_slot(owner_app=self.APP, origin=SlotOrigin.APP, key="s1")
        foreign = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="s2")
        state = _make_state({"s1": own, "s2": foreign})
        rows = [{"key": "s1"}, {"key": "s2"}]

        with patch.object(mod, "_audit_decision") as audit:
            result = mod.filter_slots_for_app(rows, self.APP, frozenset(), state)

        assert [r["key"] for r in result] == ["s1"]
        outcomes = [c.args[2] for c in audit.call_args_list]
        assert "granted" in outcomes, "the visible own slot must be audited as a grant"
        assert any(
            o.startswith("denied:") for o in outcomes
        ), "the foreign slot must be audited as a denial"

    def test_subagent_batch_filter_audits_each_item_decision(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        self._enable()
        own = _make_slot(owner_app=self.APP, origin=SlotOrigin.APP, key="s1")
        foreign = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="s2")
        state = _make_state({"s1": own, "s2": foreign})
        items = [{"slot": "s1", "id": "own"}, {"slot": "s2", "id": "foreign"}]

        with patch.object(mod, "_audit_decision") as audit:
            result = mod.filter_subagent_batch_for_app(items, self.APP, frozenset(), state)

        assert [i["id"] for i in result] == ["own"]
        outcomes = [c.args[2] for c in audit.call_args_list]
        assert "granted" in outcomes
        assert any(o.startswith("denied:") for o in outcomes)

    def test_tier0_still_reaches_a_disabled_app(self):
        """Revocation collapses to Tier 0 -- it does not black out the socket.

        Tier 0 is counts-and-environment only; withholding it would leave a
        disabled-but-connected app unable to tell it had been turned off.
        """
        self._revoke()
        state = self._own_slot_state()
        from kiro_crew.dashboard import ws_event_scope as mod

        tier0 = sorted(mod._TIER0_ALWAYS)[0]
        assert (
            ws_event_allowed(
                tier0,
                {},
                app=self.APP,
                allowed_events=frozenset(),
                state=state,
            )
            is True
        )

    def test_cold_miss_does_not_revoke(self):
        """An unknown app must not read as revoked.

        The gate runs before the first off-loop refresh lands, so reporting
        "revoked" on a cold cache would blank every app's own slots on the first
        broadcast after a gateway restart. The connect path closes this window for
        real by PRIMING the cache -- see
        ``test_connect_load_primes_the_cache_so_the_first_frame_is_authoritative``.
        """
        from kiro_crew.dashboard import ws_event_scope as mod

        with patch.object(mod, "_schedule_declared_refresh") as sched:
            assert mod.app_events_revoked("never-seen") is False
            sched.assert_called_once_with("never-seen")

    def test_connect_load_primes_the_cache_so_the_first_frame_is_authoritative(self):
        """Why the connect read writes the cache instead of only returning.

        Without priming, a disabled app that RECONNECTS is judged by the cold-miss
        fallback (not revoked) for the initial slots push and the log replay --
        both of which run before any background refresh.
        """
        from kiro_crew.dashboard import ws_event_scope as mod

        fake = MagicMock()
        fake.permissions.events = ["slots:all"]

        with (
            patch.object(mod, "is_app_enabled", return_value=False),
            patch.object(mod, "get_app_manifest", return_value=fake),
        ):
            enabled, events = mod.load_declared_events_for_connect(self.APP)

        assert enabled is False, "the caller needs the flag to refuse the socket"
        assert events == frozenset()
        assert mod.app_events_revoked(self.APP) is True, (
            "the cache must be primed by the connect read, or the first frame is "
            "gated on the cold-miss fallback"
        )

    def test_connect_load_reports_an_enabled_app_and_its_scopes(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        fake = MagicMock()
        fake.permissions.events = ["slots:own", "log"]
        with (
            patch.object(mod, "is_app_enabled", return_value=True),
            patch.object(mod, "get_app_manifest", return_value=fake),
        ):
            enabled, events = mod.load_declared_events_for_connect(self.APP)
        assert enabled is True
        assert events == _allowed("slots:own", "log")
        assert mod.app_events_revoked(self.APP) is False

    def test_declaration_narrowing_alone_would_not_have_closed_this(self):
        """Pins WHY a separate predicate exists rather than reusing the scopes.

        A disabled app and an enabled app that declares no events both present an
        EMPTY effective scope set, so the gate cannot tell them apart from
        ``allowed_events`` -- and they must be treated differently, since the
        latter still gets its own slots.
        """
        from kiro_crew.dashboard import ws_event_scope as mod

        snap = _allowed("slots:own")

        self._enable()
        enabled_scopes = mod.effective_allowed_events(self.APP, snap)
        self._revoke()
        revoked_scopes = mod.effective_allowed_events(self.APP, snap)

        assert (
            enabled_scopes == revoked_scopes == frozenset()
        ), "indistinguishable by scope set -- hence app_events_revoked"


# ---------------------------------------------------------------------------
# The COMPOSITION seam: ``_send_ws_all`` is the chokepoint, and the three parts
# it wires together (``_ws_client_allowed``, ``_serialize_for_client``, the
# fan-out loop) are each covered in isolation above. Isolation is not enough
# here -- the defects this module exists to prevent live in the seam: a frame the
# gate admits whose payload is never filtered, or an event absent from the
# tables that the loop drops for an app while the dashboard keeps receiving it.
# These tests drive the REAL chokepoint and assert on the bytes that reach each
# socket, with an app token present -- every other ``_send_ws_all`` test in the
# suite flags every fake socket ``_is_dashboard_user=True`` and so only exercises
# the bypass branch.
# ---------------------------------------------------------------------------


class TestSendWsAllComposition:
    def _sockets_and_state(self, *, app_events: frozenset[str]):
        """A state with one dashboard socket and one app socket, real methods."""
        from kiro_crew.dashboard.state import DashboardState

        own = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="own")
        other = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="foreign")
        user = _make_slot(owner_app="", origin=SlotOrigin.USER, key="user-slot")

        def _ws(store: dict) -> MagicMock:
            ws = MagicMock()
            ws.closed = False
            ws.get.side_effect = lambda k, default=None: store.get(k, default)
            return ws

        dash = _ws({"_is_dashboard_user": True, "_app": "", "_allowed_events": frozenset()})
        app = _ws(
            {
                "_is_dashboard_user": False,
                "_app": "mochi-pet",
                "_allowed_events": app_events,
            }
        )

        state = MagicMock(spec=DashboardState)
        state._slots = {"own": own, "foreign": other, "user-slot": user}
        state._ws_clients = [dash, app]
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        state._serialize_for_client = DashboardState._serialize_for_client.__get__(state)
        state._send_ws_all = DashboardState._send_ws_all.__get__(state)

        wire: list[tuple[MagicMock, str]] = []
        state._spawn_ws_send = lambda ws, payload: wire.append((ws, payload))
        return state, dash, app, wire

    def _sent_to(self, wire, sock) -> list[str]:
        return [payload for ws, payload in wire if ws is sock]

    def test_foreign_slot_event_reaches_dashboard_but_not_app(self):
        """The seam's core job, asserted on the wire rather than on a bool."""
        state, dash, app, wire = self._sockets_and_state(app_events=_allowed("slots:own"))
        state._send_ws_all("chat_chunk", {"slot": "foreign"}, json.dumps({"type": "chat_chunk"}))
        assert self._sent_to(wire, dash), "dashboard user must still receive it"
        assert not self._sent_to(wire, app), "app must not see a foreign slot's chunk"

    def test_own_slot_event_reaches_both(self):
        """Own-slot delivery needs no declaration -- the documented default."""
        state, dash, app, wire = self._sockets_and_state(app_events=_allowed("slots:own"))
        state._send_ws_all("chat_chunk", {"slot": "own"}, json.dumps({"type": "chat_chunk"}))
        assert self._sent_to(wire, dash)
        assert self._sent_to(wire, app), "an app always sees its OWN slot's events"

    def test_slots_repush_is_admitted_but_payload_filtered_on_the_wire(self):
        """The admitted-frame-unfiltered-payload defect class, end to end.

        The gate lets ``slots`` through for everyone; only the serializer keeps
        an app from reading every slot. Asserting the boolean would pass while
        the wire still carried the full list.
        """
        state, dash, app, wire = self._sockets_and_state(app_events=_allowed("slots:own"))
        envelope = {
            "slots": [{"key": "own"}, {"key": "foreign"}, {"key": "user-slot"}],
            "yolo": True,
            "channelTrusted": True,
        }
        state._send_ws_all("slots", envelope, json.dumps({"type": "slots"}))

        app_frames = self._sent_to(wire, app)
        assert app_frames, "slots is always admitted; the payload is what narrows"
        payload = json.loads(app_frames[0])
        assert [s["key"] for s in payload["data"]] == ["own"]
        # Envelope posture fields are not slot data, so the slot filter cannot
        # narrow them -- they need their own decision.
        assert "yolo" not in payload
        assert "channelTrusted" not in payload

        dash_frames = self._sent_to(wire, dash)
        assert dash_frames == [
            json.dumps({"type": "slots"})
        ], "dashboard user gets the unmodified default message"

    def test_unknown_event_is_withheld_from_app_only(self):
        """Deny-by-default at the seam: an unclassified event never reaches an app.

        The payload deliberately carries NO ``slot``: a slot field routes the
        frame into the slot-visibility branch instead, where an own-slot event is
        allowed by default and the unknown-event floor is never consulted.
        """
        state, dash, app, wire = self._sockets_and_state(app_events=build_allowed_event_set(["*"]))
        state._send_ws_all("totally_new_event", {"detail": "x"}, json.dumps({"type": "x"}))
        assert self._sent_to(wire, dash)
        assert not self._sent_to(
            wire, app
        ), "even a wildcard manifest must not receive an unclassified event"

    def test_closed_socket_is_reaped_without_blocking_the_others(self):
        state, dash, app, wire = self._sockets_and_state(app_events=_allowed("slots:own"))
        dash.closed = True
        removed: list[MagicMock] = []
        state._remove_ws = removed.append
        state._send_ws_all("chat_chunk", {"slot": "own"}, json.dumps({"type": "chat_chunk"}))
        assert removed == [dash]
        assert self._sent_to(wire, app), "a dead peer must not stop live delivery"


# ---------------------------------------------------------------------------
# The ``slots`` ENVELOPE (not its ``data``) carries global safety-posture
# booleans. Filtering the slot list does not narrow them, so they need their
# own decision or an app with only ``slots:own`` reads the operator's live
# blanket-approval state off every re-push.
# ---------------------------------------------------------------------------


class TestSlotsEnvelopePosture:
    def _state(self):
        from kiro_crew.dashboard.state import DashboardState

        slot = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="a")
        state = MagicMock(spec=DashboardState)
        state._slots = {"a": slot}
        state._serialize_for_client = DashboardState._serialize_for_client.__get__(state)
        return state

    def _ws(self, events):
        ws = MagicMock()
        store = {
            "_is_dashboard_user": False,
            "_app": "mochi-pet",
            "_allowed_events": events,
        }
        ws.get.side_effect = lambda k, default=None: store.get(k, default)
        return ws

    def _envelope(self, events):
        import json

        return json.loads(
            self._state()._serialize_for_client(
                self._ws(events),
                "slots",
                {"slots": [{"key": "a"}], "yolo": True, "channelTrusted": True},
                default_msg="dummy",
            )
        )

    def test_undeclared_app_gets_no_yolo_state(self):
        """The leak: slots:own alone must not disclose the override state."""
        env = self._envelope(frozenset({"slots:own"}))
        assert "yolo" not in env, env

    def test_undeclared_app_gets_no_channel_trust_state(self):
        env = self._envelope(frozenset({"slots:own"}))
        assert "channelTrusted" not in env, env

    def test_omitted_rather_than_defaulted_to_false(self):
        """``false`` is a factual claim about posture, so absence is required.

        A falsy default would still answer the question the app must not be
        able to ask -- and would answer it WRONGLY here (yolo is on).
        """
        env = self._envelope(frozenset({"slots:own"}))
        assert env.get("yolo") is None

    def test_declaring_yolo_scope_receives_it(self):
        """Gating must not lock out an app that legitimately declared it."""
        env = self._envelope(frozenset({"slots:own", "yolo"}))
        assert env["yolo"] is True

    def test_wildcard_manifest_receives_it(self):
        env = self._envelope(build_allowed_event_set(["*"]))
        assert env["yolo"] is True

    def test_channel_trust_withheld_even_from_wildcard(self):
        """No scope declares it and no app SDK consumer reads it."""
        env = self._envelope(build_allowed_event_set(["*"]))
        assert "channelTrusted" not in env, env

    def test_slot_data_still_filtered(self):
        env = self._envelope(frozenset({"slots:own", "yolo"}))
        assert [s["key"] for s in env["data"]] == ["a"]

    def test_source_guard_connect_push_uses_the_same_helper(self):
        """The connect-time push in ws.py builds the SAME envelope.

        It is a sibling emitter, so a fix applied only to the broadcast
        re-push would leave the first frame after connect leaking.
        """
        src = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "ws.py"
        ).read_text(encoding="utf-8")
        assert (
            "slots_envelope_extras(" in src
        ), "ws.py connect push must route the envelope through the gate helper"
        assert '"yolo": state._yolo,' not in src.replace(
            '{"yolo": state._yolo}', ""
        ), "ws.py must not put yolo on an app envelope unconditionally"

    def test_source_guard_yolo_scope_is_not_a_parallel_name(self):
        """The scope reuses the one that already gates ``yolo_expired``."""
        from kiro_crew.dashboard import ws_event_scope as mod

        assert mod._YOLO_SCOPE == mod._GLOBAL_EVENT_DECLARATIONS["yolo_expired"]


# ---------------------------------------------------------------------------
# ``ws_event_allowed`` is synchronous and runs inside ``_send_ws_all``'s
# per-client loop, so nothing it calls may touch the disk: a blocking read on
# the broadcast path stalls every other request and the heartbeat with it.
# ---------------------------------------------------------------------------


class TestExposeToCacheNeverBlocksTheLoop:
    def setup_method(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        mod._exposeto_cache.clear()
        mod._exposeto_refreshing.clear()

    def test_cold_miss_does_not_read_from_the_hot_path(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        with (
            patch.object(mod, "get_app_manifest") as gm,
            patch.object(mod, "_schedule_expose_to_refresh") as sched,
        ):
            assert mod._load_expose_to("other-app") == frozenset()
            gm.assert_not_called()
            sched.assert_called_once_with("other-app")

    def test_cold_miss_fails_closed(self):
        """Withholding one frame is the safe direction; granting is not."""
        from kiro_crew.dashboard import ws_event_scope as mod

        with patch.object(mod, "_schedule_expose_to_refresh"):
            assert mod._load_expose_to("other-app") == frozenset()

    def test_stale_entry_is_served_not_reloaded(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        stale = frozenset({"mochi-pet"})
        mod._exposeto_cache["other-app"] = (
            time.monotonic() - (mod._MANIFEST_EXPOSE_TTL_SECS + 5),
            stale,
        )
        with (
            patch.object(mod, "get_app_manifest") as gm,
            patch.object(mod, "_schedule_expose_to_refresh") as sched,
        ):
            assert mod._load_expose_to("other-app") == stale
            gm.assert_not_called()
            sched.assert_called_once_with("other-app")

    def test_fresh_entry_schedules_nothing(self):
        from kiro_crew.dashboard import ws_event_scope as mod

        fresh = frozenset({"mochi-pet"})
        mod._exposeto_cache["other-app"] = (time.monotonic(), fresh)
        with patch.object(mod, "_schedule_expose_to_refresh") as sched:
            assert mod._load_expose_to("other-app") == fresh
            sched.assert_not_called()

    def test_refresh_is_coalesced_across_a_broadcast_burst(self):
        """One frame x many clients must not queue one thread job each."""
        from kiro_crew.dashboard import ws_event_scope as mod

        loop = MagicMock()
        with patch.object(mod.asyncio, "get_running_loop", return_value=loop):
            for _ in range(25):
                mod._schedule_expose_to_refresh("other-app")
        assert loop.run_in_executor.call_count == 1

    def test_no_running_loop_falls_back_to_sync_load(self):
        """Off the loop (tests, CLI) blocking is fine -- and refusing to load
        at all would leave the cache permanently cold."""
        from kiro_crew.dashboard import ws_event_scope as mod

        with patch.object(mod, "_read_expose_to", return_value=frozenset({"x"})):
            mod._schedule_expose_to_refresh("other-app")
        assert mod._exposeto_cache["other-app"][1] == frozenset({"x"})

    def test_source_guard_connect_manifest_read_is_offloaded(self):
        """The connect path must never read the manifest ON the event loop.

        Pins the INVARIANT, not one spelling: whichever loader the connect path
        uses, every manifest-reading call in ``ws.py`` has to be wrapped in
        ``asyncio.to_thread``. An earlier version of this guard asserted the
        literal ``to_thread(get_app_manifest`` and so went red when the read was
        replaced by ``load_declared_events_for_connect`` — still off-loop, still
        correct. A guard that pins a spelling resists the refactor instead of
        catching the regression.
        """
        from kiro_crew.dashboard import ws_event_scope as mod  # noqa: F401

        src = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "ws.py"
        ).read_text(encoding="utf-8")

        # Drop the import block: a loader NAME appearing in an import is not a
        # call. Then collapse whitespace so a multi-line
        # ``to_thread(\n    loader, arg\n)`` reads the same as the one-line form --
        # line-by-line matching would miss exactly that shape.
        body = re.sub(r"from kiro_crew[^)]*?\)", "", src, flags=re.S)
        norm = re.sub(r"\s+", " ", body)

        blocking_loaders = ("get_app_manifest", "load_declared_events_for_connect")
        checked = 0
        for loader in blocking_loaders:
            for match in re.finditer(re.escape(loader), norm):
                window = norm[max(0, match.start() - 80) : match.start()]
                assert "to_thread" in window, (
                    f"{loader} reads the manifest from disk, so the call must be "
                    f"wrapped in asyncio.to_thread; context: "
                    f"{norm[max(0, match.start() - 80):match.start() + 40]!r}"
                )
                checked += 1
        assert checked, (
            "the connect path no longer resolves the app's scope at all -- if the "
            "loader was renamed, add it to blocking_loaders"
        )

    def test_source_guard_connect_refuses_a_disabled_app(self):
        """The connect read must drive the refusal, not just build a snapshot.

        Reading enablement and then ignoring it is the defect this replaced: the
        initial slots push and the log replay both run before any background
        refresh, so a disabled app that reconnects would be served from its intact
        manifest.
        """
        src = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "ws.py"
        ).read_text(encoding="utf-8")
        assert "if not app_enabled:" in src and "ws.close(" in src, (
            "ws.py must close the socket when the connect read reports the app " "disabled"
        )
        # The refusal has to precede registration, or there is no cleanup scope.
        assert src.index("if not app_enabled:") < src.index("state.register_ws("), (
            "refuse BEFORE register_ws -- refusing after leaves the socket "
            "registered with no cleanup scope to unregister it"
        )
        # And the read that feeds it must also precede registration.
        assert src.index("load_declared_events_for_connect, ws_app") < src.index(
            "state.register_ws("
        ), "resolve the app's scope before registering the socket"

    def test_source_guard_manifest_loader_has_no_internal_cache(self):
        """The reason this cache exists at all.

        If ``get_app_manifest`` ever gains its own cache this guard should be
        revisited rather than silently keeping a redundant layer.
        """
        import inspect

        from kiro_crew.apps import manager

        src = inspect.getsource(manager.get_app_manifest)
        assert "from_json_file" in src and "cache" not in src.lower()


# ---------------------------------------------------------------------------
# subscribe_logs / subscribe_subagents subscription-layer gate
# ---------------------------------------------------------------------------


class TestSubscribeHandlerGates:
    """The WS ``subscribe_logs`` and ``subscribe_subagents`` handlers replay
    initial snapshot data via ``ws.send_json`` — a path that bypasses the
    ``_send_ws_all`` chokepoint entirely.  Both must be gated at the
    subscription-handler level so an app without the right scope cannot
    receive log history or subagent snapshots merely by sending a subscribe
    message.
    """

    def _ws_source(self) -> str:
        import kiro_crew.dashboard.ws as _ws

        # encoding is explicit on every source read in this file: Path.read_text()
        # defaults to the LOCALE codepage, so on Windows (cp1252) reading a module
        # that contains an em dash or an arrow raises UnicodeDecodeError and the
        # guard fails for a reason that has nothing to do with what it asserts.
        return Path(_ws.__file__).read_text(encoding="utf-8")

    def test_subscribe_logs_is_gated_on_the_log_scope(self):
        """Guard the REAL gate, not a copy of it.

        A local ``gate_would_deny`` restating the predicate would pass no matter
        what ws.py does — the failure mode that let a sibling subagent test stay
        green after its gate was removed. Assert on the shipped source instead:
        log history is privileged and must stay declaration-gated.
        """
        src = self._ws_source()
        # Assert the GATE EXISTS and accepts both spellings, without pinning the
        # variable it reads: the set is resolved live (``_live``) rather than from
        # the connect snapshot, and a guard naming the old variable would resist
        # that fix instead of catching a missing gate.
        assert '"log" in _live' in src, "subscribe_logs must remain gated on the log scope"
        assert (
            '"log:all" in _live' in src
        ), "the replay gate must accept log:all, like the per-event chokepoint"
        assert "log_scope_not_declared" in src, "the log deny must stay audited"

    def test_subscribe_subagents_has_no_declaration_gate(self):
        """Owning your own slots is the default, not an opt-in.

        Rejecting the subscription when nothing matched ``subagent*`` /
        ``slots:*`` starved an app of its OWN slot's replay -- the one thing it
        is always entitled to. The per-frame check is the only place the scope
        decision belongs, so the declaration-level rejection is gone and must
        not come back.
        """
        src = self._ws_source()
        assert (
            "subagent_scope_not_declared" not in src
        ), "the declaration-level subscribe_subagents rejection must not return"
        assert "state.subscribe_subagents(ws)" in src

    def test_subagent_snapshot_replay_uses_per_item_scope_check(self):
        # Behavioral: subagent_snapshot is now in _SLOT_SCOPED_EVENTS and
        # _SUBAGENT_EVENTS, so ws_event_allowed routes it through
        # _subagent_visible — an app can be denied for a foreign slot's
        # snapshot even after passing the subscribe_subagents gate.
        slot = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="s1")
        state = _make_state({"s1": slot})
        assert (
            ws_event_allowed(
                "subagent_snapshot",
                {"slot": "s1", "id": "a1"},
                app="mochi-pet",
                allowed_events=_allowed("subagent"),  # own-only
                state=state,
            )
            is False
        )

    def test_subscribe_denials_emit_sel_audit(self):
        # Behavioral: driving the actual subscribe handler would require an
        # in-process aiohttp WS.  Instead, we exercise _audit_deny directly
        # with the two reasons the handlers use, and confirm the SEL call
        # is made.
        from kiro_crew.dashboard import ws_event_scope as _wes

        with patch("kiro_crew.sel.sel") as sel_mock:
            _wes._audit_deny("mochi-pet", "subscribe_logs", "log_scope_not_declared")
            outcomes = [
                c.kwargs.get("outcome", "")
                for c in sel_mock.return_value.log_api_access.call_args_list
            ]
            assert any("log_scope_not_declared" in o for o in outcomes)


# ---------------------------------------------------------------------------
# exposeToApps TTL cache — hot-path performance vs disk I/O
# ---------------------------------------------------------------------------


class TestExposeToAppsCache:
    """``_target_exposes_to`` runs on the WS broadcast hot path. Without a
    local cache, ``get_app_manifest`` re-reads the JSON manifest from disk on
    every cross-app slot event. A 30-second TTL cache keeps disk I/O bounded
    while still picking up manifest edits within one window.
    """

    def test_repeated_calls_hit_cache_and_do_not_reread_manifest(self):
        from kiro_crew.dashboard import ws_event_scope as _wes

        manifest = MagicMock()
        manifest.permissions.exposeToApps = ["monitor-app"]
        with patch(
            "kiro_crew.dashboard.ws_event_scope.get_app_manifest",
            return_value=manifest,
        ) as m:
            state = MagicMock()
            assert _wes._target_exposes_to("mochi-pet", "monitor-app", state) is True
            assert _wes._target_exposes_to("mochi-pet", "monitor-app", state) is True
            assert _wes._target_exposes_to("mochi-pet", "monitor-app", state) is True
            # Only one manifest load despite 3 checks (cached).
            assert m.call_count == 1

    def test_cache_denies_when_manifest_missing(self):
        from kiro_crew.dashboard import ws_event_scope as _wes

        with patch(
            "kiro_crew.dashboard.ws_event_scope.get_app_manifest",
            return_value=None,
        ):
            state = MagicMock()
            # Missing manifest — fail-closed even under caching.
            assert _wes._target_exposes_to("ghost", "monitor-app", state) is False

    def test_cache_denies_on_manifest_load_failure(self):
        from kiro_crew.dashboard import ws_event_scope as _wes

        with patch(
            "kiro_crew.dashboard.ws_event_scope.get_app_manifest",
            side_effect=RuntimeError("boom"),
        ):
            state = MagicMock()
            assert _wes._target_exposes_to("target", "requester", state) is False

    def test_disabled_target_exposes_to_nobody(self):
        """Disabling an app does not delete its manifest, so without this
        check an observer holding ``slots:app:<target>`` would keep seeing a
        disabled app's slots -- the cross-app mirror of the check
        ``app_events_revoked`` already applies to the target's own socket.
        """
        from kiro_crew.dashboard import ws_event_scope as _wes

        manifest = MagicMock()
        manifest.permissions.exposeToApps = ["monitor-app"]
        with (
            patch(
                "kiro_crew.dashboard.ws_event_scope.get_app_manifest",
                return_value=manifest,
            ) as m,
            patch("kiro_crew.dashboard.ws_event_scope.is_app_enabled", return_value=False),
        ):
            state = MagicMock()
            assert _wes._target_exposes_to("mochi-pet", "monitor-app", state) is False
            # The manifest is never even read once the app is known disabled.
            m.assert_not_called()
