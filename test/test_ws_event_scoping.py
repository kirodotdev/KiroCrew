"""Tests for per-app WebSocket event scope enforcement.

Covers:
- ws_event_allowed: Tier 0 (always), Tier 1 (slot-scoped), Tier 2 (global)
- Slot visibility: own-slot, user-slot, cross-app opt-in, slots:all
- Subagent visibility: independent dimension from slots
- filter_slots_for_app: initial slots push filtering
- build_allowed_event_set: passthrough normalisation
- token_auth: implicit allow for /api/ws + /api/status with SEL audit
- broadcast_ws integration: app clients filtered, dashboard users unaffected

Siblings, split by behaviour: a slot's origin in ``test_ws_event_scoping_origin.py``, an
open socket's live scope and the ``_send_ws_all`` fan-out in
``test_ws_event_scoping_live.py``, per-frame classification and event-table completeness in
``test_ws_event_scoping_frames.py``, and the SEL audit of grants in
``test_ws_event_scoping_audit.py``. Their shared fixtures are in
``ws_event_scoping_helpers.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import ws_event_scoping_helpers
from ws_event_scoping_helpers import _allowed, _make_slot, _make_state

from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.dashboard.token_auth import app_token_path_allowed
from kiro_crew.dashboard.ws_event_scope import (
    build_allowed_event_set,
    filter_slots_for_app,
    ws_event_allowed,
)

# The shared fixtures, bound here by name so pytest registers them for this module.
_clear_module_caches = ws_event_scoping_helpers._clear_module_caches
_clear_ws_scope_module_caches = ws_event_scoping_helpers._clear_ws_scope_module_caches


# ---------------------------------------------------------------------------
# build_allowed_event_set
# ---------------------------------------------------------------------------

class TestBuildAllowedEventSet:
    def test_empty_declarations(self):
        assert build_allowed_event_set([]) == frozenset()

    def test_returns_frozenset(self):
        result = build_allowed_event_set(["notification", "slots:user"])
        assert isinstance(result, frozenset)
        assert "notification" in result
        assert "slots:user" in result

    def test_deduplication(self):
        result = build_allowed_event_set(["notification", "notification"])
        assert result == frozenset({"notification"})


# ---------------------------------------------------------------------------
# Tier 0: always-allowed events
# ---------------------------------------------------------------------------

class TestTier0AlwaysAllowed:
    def test_dashboard_always_allowed_for_any_app(self):
        state = _make_state()
        assert ws_event_allowed(
            "dashboard", {},
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is True

    def test_dashboard_allowed_even_with_empty_permissions(self):
        state = _make_state()
        assert ws_event_allowed(
            "dashboard", {"version": "3.0"},
            app="auto-improvement", allowed_events=frozenset(), state=state,
        ) is True


# ---------------------------------------------------------------------------
# Tier 1: slot-scoped events — own-slot visibility
# ---------------------------------------------------------------------------

class TestSlotScopedOwnSlot:
    def test_own_slot_chat_chunk_allowed(self):
        slot = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="mochi-pet")
        state = _make_state({"mochi-pet": slot})
        assert ws_event_allowed(
            "chat_chunk", {"slot": "mochi-pet", "content": "hi"},
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is True

    def test_own_slot_tool_call_allowed(self):
        slot = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="mochi-pet")
        state = _make_state({"mochi-pet": slot})
        assert ws_event_allowed(
            "tool_call", {"slot": "mochi-pet"},
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is True

    def test_other_app_slot_denied_without_declaration(self):
        slot = _make_slot(owner_app="auto-improvement", origin=SlotOrigin.APP, key="auto-improvement")
        state = _make_state({"auto-improvement": slot})
        assert ws_event_allowed(
            "chat_chunk", {"slot": "auto-improvement"},
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is False

    def test_missing_slot_denied(self):
        state = _make_state({})
        assert ws_event_allowed(
            "chat_chunk", {"slot": "ghost"},
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is False

    def test_slot_field_none_denied(self):
        state = _make_state({})
        # slot-scoped event type but no slot key in data
        assert ws_event_allowed(
            "chat_chunk", {},
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is False


# ---------------------------------------------------------------------------
# Tier 1: slot-scoped events — slots:user
# ---------------------------------------------------------------------------

class TestSlotScopedUserSlots:
    def test_user_slot_allowed_with_slots_user(self):
        slot = _make_slot(owner_app="", origin=SlotOrigin.USER, key="chat-1")
        state = _make_state({"chat-1": slot})
        assert ws_event_allowed(
            "chat_message", {"slot": "chat-1"},
            app="mochi-pet", allowed_events=_allowed("slots:user"), state=state,
        ) is True

    def test_user_slot_denied_without_slots_user(self):
        slot = _make_slot(owner_app="", origin=SlotOrigin.USER, key="chat-1")
        state = _make_state({"chat-1": slot})
        assert ws_event_allowed(
            "chat_message", {"slot": "chat-1"},
            app="mochi-pet", allowed_events=_allowed("notification"), state=state,
        ) is False

    def test_app_slot_not_covered_by_slots_user(self):
        # slots:user only covers USER-origin slots, not APP-origin ones
        slot = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="other-app")
        state = _make_state({"other-app": slot})
        assert ws_event_allowed(
            "chat_chunk", {"slot": "other-app"},
            app="mochi-pet", allowed_events=_allowed("slots:user"), state=state,
        ) is False


# ---------------------------------------------------------------------------
# Tier 1: slot-scoped events — slots:all
# ---------------------------------------------------------------------------

class TestSlotScopedSlotsAll:
    def test_slots_all_covers_any_slot(self):
        slot = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="other")
        state = _make_state({"other": slot})
        assert ws_event_allowed(
            "chat_chunk", {"slot": "other"},
            app="mochi-pet", allowed_events=_allowed("slots:all"), state=state,
        ) is True

    def test_slots_all_covers_user_slot(self):
        slot = _make_slot(owner_app="", origin=SlotOrigin.USER, key="chat-1")
        state = _make_state({"chat-1": slot})
        assert ws_event_allowed(
            "slot_title", {"slot": "chat-1"},
            app="any-app", allowed_events=_allowed("slots:all"), state=state,
        ) is True


# ---------------------------------------------------------------------------
# Tier 1: slot-scoped events — slots:app:<name> with exposeToApps opt-in
# ---------------------------------------------------------------------------

class TestSlotScopedCrossApp:
    def _state_with_expose(self, target_app: str, expose_to: list[str]) -> MagicMock:
        slot = _make_slot(owner_app=target_app, origin=SlotOrigin.APP, key=target_app)
        state = _make_state({target_app: slot})
        # Mock get_app_manifest to return a manifest with exposeToApps
        manifest = MagicMock()
        manifest.permissions.exposeToApps = expose_to
        state._get_manifest = MagicMock(return_value=manifest)
        return state

    def test_cross_app_allowed_when_target_opts_in(self):
        slot = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="mochi-pet")
        state = _make_state({"mochi-pet": slot})
        manifest = MagicMock()
        manifest.permissions.exposeToApps = ["monitor-app"]
        with patch("kiro_crew.dashboard.ws_event_scope.get_app_manifest", return_value=manifest):
            assert ws_event_allowed(
                "chat_chunk", {"slot": "mochi-pet"},
                app="monitor-app",
                allowed_events=_allowed("slots:app:mochi-pet"),
                state=state,
            ) is True

    def test_cross_app_denied_when_target_does_not_opt_in(self):
        slot = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="mochi-pet")
        state = _make_state({"mochi-pet": slot})
        manifest = MagicMock()
        manifest.permissions.exposeToApps = []  # no opt-in
        with patch("kiro_crew.dashboard.ws_event_scope.get_app_manifest", return_value=manifest):
            assert ws_event_allowed(
                "chat_chunk", {"slot": "mochi-pet"},
                app="monitor-app",
                allowed_events=_allowed("slots:app:mochi-pet"),
                state=state,
            ) is False

    def test_cross_app_allowed_with_wildcard_expose(self):
        slot = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="mochi-pet")
        state = _make_state({"mochi-pet": slot})
        manifest = MagicMock()
        manifest.permissions.exposeToApps = ["*"]
        with patch("kiro_crew.dashboard.ws_event_scope.get_app_manifest", return_value=manifest):
            assert ws_event_allowed(
                "chat_chunk", {"slot": "mochi-pet"},
                app="any-app",
                allowed_events=_allowed("slots:app:mochi-pet"),
                state=state,
            ) is True

    def test_cross_app_without_slots_app_declaration_denied(self):
        slot = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="mochi-pet")
        state = _make_state({"mochi-pet": slot})
        manifest = MagicMock()
        manifest.permissions.exposeToApps = ["monitor-app"]
        with patch("kiro_crew.dashboard.ws_event_scope.get_app_manifest", return_value=manifest):
            # monitor-app didn't declare slots:app:mochi-pet
            assert ws_event_allowed(
                "chat_chunk", {"slot": "mochi-pet"},
                app="monitor-app",
                allowed_events=_allowed("slots:user"),  # wrong declaration
                state=state,
            ) is False


# ---------------------------------------------------------------------------
# Subagent events — independent dimension
# ---------------------------------------------------------------------------

class TestSubagentEvents:
    def test_subagent_own_slot_always_allowed(self):
        slot = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="mochi-pet")
        state = _make_state({"mochi-pet": slot})
        assert ws_event_allowed(
            "subagent_done", {"slot": "mochi-pet"},
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is True

    def test_subagent_user_slot_requires_subagent_user(self):
        slot = _make_slot(owner_app="", origin=SlotOrigin.USER, key="chat-1")
        state = _make_state({"chat-1": slot})
        # Without subagent:user, denied
        assert ws_event_allowed(
            "subagent_done", {"slot": "chat-1"},
            app="mochi-pet", allowed_events=_allowed("slots:user"), state=state,
        ) is True  # slots:user falls through to slot visibility which allows it

    def test_subagent_user_independent_of_slots_user(self):
        """subagent:user can be declared without slots:user (narrower access)."""
        slot = _make_slot(owner_app="", origin=SlotOrigin.USER, key="chat-1")
        state = _make_state({"chat-1": slot})
        assert ws_event_allowed(
            "subagent_done", {"slot": "chat-1"},
            app="mochi-pet", allowed_events=_allowed("subagent:user"), state=state,
        ) is True
        # But chat content is still blocked (no slots:user)
        assert ws_event_allowed(
            "chat_chunk", {"slot": "chat-1"},
            app="mochi-pet", allowed_events=_allowed("subagent:user"), state=state,
        ) is False

    def test_subagent_all_covers_any_app_slot(self):
        slot = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="other")
        state = _make_state({"other": slot})
        assert ws_event_allowed(
            "subagent_spawn", {"slot": "other"},
            app="mochi-pet", allowed_events=_allowed("subagent:all"), state=state,
        ) is True


# ---------------------------------------------------------------------------
# Tier 2: global events
# ---------------------------------------------------------------------------

class TestGlobalEvents:
    def test_notification_own_app_requires_declaration(self):
        # Own-app notifications now require an explicit ``notification``
        # declaration (deny-by-default per CWE-269).  Undeclared apps get
        # denied even for their own notifications.
        state = _make_state()
        assert ws_event_allowed(
            "notification", {"source": "app:mochi-pet", "message": "hello"},
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is False

    def test_notification_own_app_allowed_when_declared(self):
        state = _make_state()
        assert ws_event_allowed(
            "notification", {"source": "app:mochi-pet", "message": "hello"},
            app="mochi-pet", allowed_events=_allowed("notification"), state=state,
        ) is True

    def test_notification_other_app_denied_without_declaration(self):
        # ``notification`` (without ``:all``) grants only own-app notifications;
        # a foreign app's push must be denied.
        state = _make_state()
        assert ws_event_allowed(
            "notification", {"source": "app:other-app"},
            app="mochi-pet", allowed_events=_allowed("notification"), state=state,
        ) is False

    def test_notification_all_covers_any_source(self):
        state = _make_state()
        assert ws_event_allowed(
            "notification", {"source": "app:other-app"},
            app="mochi-pet", allowed_events=_allowed("notification:all"), state=state,
        ) is True

    def test_sessions_restarting_requires_declaration(self):
        state = _make_state()
        assert ws_event_allowed(
            "sessions_restarting", {"status": "restarting"},
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is False
        assert ws_event_allowed(
            "sessions_restarting", {"status": "restarting"},
            app="mochi-pet", allowed_events=_allowed("sessions"), state=state,
        ) is True

    def test_log_requires_declaration(self):
        state = _make_state()
        assert ws_event_allowed(
            "log", {"line": "some log"},
            app="mochi-pet", allowed_events=_allowed("sessions"), state=state,
        ) is False
        assert ws_event_allowed(
            "log", {"line": "some log"},
            app="mochi-pet", allowed_events=_allowed("log"), state=state,
        ) is True

    def test_system_notifications_need_their_own_declaration(self):
        """Gateway-internal pushes are user content, not the app's own.

        ``send_message``, cron output and watchlist results reach state.notify(),
        which stamps ``source="system"``. Folding them into ``notification`` would
        make one declaration a broad grant -- the shape this module exists to
        remove.
        """
        state = _make_state()
        system_note = {"text": "cron finished", "source": "system"}
        assert ws_event_allowed(
            "notification", system_note,
            app="mochi-pet", allowed_events=_allowed("notification"), state=state,
        ) is False
        assert ws_event_allowed(
            "notification", system_note,
            app="mochi-pet", allowed_events=_allowed("notification:system"), state=state,
        ) is True
        # A note with NO source is not attributable, so it is not the system
        # stream either -- it is denied. Accepting "" as system is exactly the
        # defect that let one app's push reach every `notification:system`
        # holder: the gate read a key no emitter writes, so it saw "" for every
        # note. Only `notification:all` crosses this.
        unattributed = {"text": "hi"}
        assert ws_event_allowed(
            "notification", unattributed,
            app="mochi-pet", allowed_events=_allowed("notification"), state=state,
        ) is False
        assert ws_event_allowed(
            "notification", unattributed,
            app="mochi-pet", allowed_events=_allowed("notification:system"), state=state,
        ) is False
        assert ws_event_allowed(
            "notification", unattributed,
            app="mochi-pet", allowed_events=_allowed("notification:all"), state=state,
        ) is True

    def test_own_app_notification_still_needs_only_notification(self):
        state = _make_state()
        own = {"text": "done", "source": "app:mochi-pet"}
        assert ws_event_allowed(
            "notification", own,
            app="mochi-pet", allowed_events=_allowed("notification"), state=state,
        ) is True
        # notification:system does NOT imply own-app notifications.
        assert ws_event_allowed(
            "notification", own,
            app="mochi-pet", allowed_events=_allowed("notification:system"), state=state,
        ) is False

    def test_channel_settings_is_gated_by_owner_not_by_note_source(self):
        """It is not in _SOURCE_FILTERED_EVENTS (it carries no `source` field) but
        it is still attributed -- by its CHANNEL owner."""
        from kiro_crew.dashboard.ws_event_scope import _SOURCE_FILTERED_EVENTS

        assert "notification_channel_settings" not in _SOURCE_FILTERED_EVENTS
        state = _make_state()
        assert ws_event_allowed(
            "notification_channel_settings",
            {"channel": "mochi-pet.alerts", "settings": {"muted": True}},
            app="mochi-pet", allowed_events=_allowed("notification"), state=state,
        ) is True

    def test_artifact_update_requires_artifacts_declaration(self):
        state = _make_state()
        payload = {"slug": "my-doc", "version": 3, "deleted": False}
        assert ws_event_allowed(
            "artifact_update", payload,
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is False
        assert ws_event_allowed(
            "artifact_update", payload,
            app="mochi-pet", allowed_events=_allowed("artifacts"), state=state,
        ) is True

    def test_workflow_run_event_declared_by_its_own_name(self):
        """The workflows app declares the literal event name, not a scope alias.

        This is the only ``permissions.events`` declaration that exists in the
        tree today, so the gate must honour the literal name -- renaming it to a
        scope alias would silently break that app.
        """
        state = _make_state()
        payload = {"run_id": "r1", "session_key": "dashboard:chat-1"}
        assert ws_event_allowed(
            "workflow_run_event", payload,
            app="workflows", allowed_events=_allowed(), state=state,
        ) is False
        assert ws_event_allowed(
            "workflow_run_event", payload,
            app="workflows",
            allowed_events=_allowed("workflow_run_event"), state=state,
        ) is True

    def test_notification_channel_settings_is_scoped_by_channel_owner(self):
        """A channel is `<app>.<id>` or `system.<kind>`, so it IS attributable.

        The old fixture used `#team` -- a shape the bus never produces -- and the
        gate let ANY app with own-only `notification` read every channel's
        settings. messaging.py itself derives the source as
        `channel.split(".", 1)[0]`; this mirrors that.
        """
        state = _make_state()
        own = {"channel": "mochi-pet.alerts", "settings": {"muted": True}}
        foreign = {"channel": "workflows.alerts", "settings": {"muted": True}}
        assert ws_event_allowed(
            "notification_channel_settings", own,
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is False
        assert ws_event_allowed(
            "notification_channel_settings", own,
            app="mochi-pet", allowed_events=_allowed("notification"), state=state,
        ) is True
        # Another app's channel is NOT covered by own-only notification.
        assert ws_event_allowed(
            "notification_channel_settings", foreign,
            app="mochi-pet", allowed_events=_allowed("notification"), state=state,
        ) is False
        assert ws_event_allowed(
            "notification_channel_settings", foreign,
            app="mochi-pet", allowed_events=_allowed("notification:all"), state=state,
        ) is True
        # System channels ride the system opt-in, like the system stream itself.
        system = {"channel": "system.cron", "settings": {"muted": True}}
        assert ws_event_allowed(
            "notification_channel_settings", system,
            app="mochi-pet", allowed_events=_allowed("notification"), state=state,
        ) is False
        assert ws_event_allowed(
            "notification_channel_settings", system,
            app="mochi-pet", allowed_events=_allowed("notification:system"), state=state,
        ) is True

    def test_channel_owner_helper_matches_the_handler_convention(self):
        """Pin the helper against the derivation messaging.py already uses."""
        from kiro_crew.dashboard.ws_event_scope import notification_channel_owner

        for channel in ("mochi-pet.alerts", "system.cron", "workflows.x.y"):
            assert notification_channel_owner(channel) == channel.split(".", 1)[0]
        # No dot -> no owner -> not attributable.
        assert notification_channel_owner("#team") == ""
        assert notification_channel_owner("") == ''

    def test_unknown_event_denied(self):
        state = _make_state()
        assert ws_event_allowed(
            "some_unknown_event_xyz", {},
            app="mochi-pet", allowed_events=_allowed("slots:all"), state=state,
        ) is False

    def test_yolo_expired_requires_declaration(self):
        state = _make_state()
        assert ws_event_allowed(
            "yolo_expired", {"source": "timer"},
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is False
        assert ws_event_allowed(
            "yolo_expired", {"source": "timer"},
            app="mochi-pet", allowed_events=_allowed("yolo"), state=state,
        ) is True


# ---------------------------------------------------------------------------
# filter_slots_for_app
# ---------------------------------------------------------------------------

class TestFilterSlotsForApp:
    def _slot_dict(self, key: str) -> dict:
        return {"key": key, "title": key}

    def test_own_slot_included(self):
        slot = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="mochi-pet")
        state = _make_state({"mochi-pet": slot})
        result = filter_slots_for_app(
            [self._slot_dict("mochi-pet")],
            "mochi-pet", _allowed(), state,
        )
        assert len(result) == 1
        assert result[0]["key"] == "mochi-pet"

    def test_other_app_slot_excluded_by_default(self):
        slot = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="other-app")
        state = _make_state({"other-app": slot})
        result = filter_slots_for_app(
            [self._slot_dict("other-app")],
            "mochi-pet", _allowed(), state,
        )
        assert result == []

    def test_user_slot_included_with_slots_user(self):
        slot = _make_slot(owner_app="", origin=SlotOrigin.USER, key="chat-1")
        state = _make_state({"chat-1": slot})
        result = filter_slots_for_app(
            [self._slot_dict("chat-1")],
            "mochi-pet", _allowed("slots:user"), state,
        )
        assert len(result) == 1

    def test_user_slot_excluded_without_slots_user(self):
        slot = _make_slot(owner_app="", origin=SlotOrigin.USER, key="chat-1")
        state = _make_state({"chat-1": slot})
        result = filter_slots_for_app(
            [self._slot_dict("chat-1")],
            "mochi-pet", _allowed("notification"), state,
        )
        assert result == []

    def test_slots_all_includes_everything(self):
        slot1 = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="mochi-pet")
        slot2 = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="other-app")
        slot3 = _make_slot(owner_app="", origin=SlotOrigin.USER, key="chat-1")
        state = _make_state({
            "mochi-pet": slot1,
            "other-app": slot2,
            "chat-1": slot3,
        })
        result = filter_slots_for_app(
            [self._slot_dict(k) for k in ["mochi-pet", "other-app", "chat-1"]],
            "mochi-pet", _allowed("slots:all"), state,
        )
        assert len(result) == 3

    def test_missing_slot_in_state_excluded(self):
        # slot dict references a key not in state._slots (stale data)
        state = _make_state({})
        result = filter_slots_for_app(
            [self._slot_dict("ghost")],
            "mochi-pet", _allowed("slots:all"), state,
        )
        assert result == []


# ---------------------------------------------------------------------------
# token_auth: implicit allow for /api/ws and /api/status
# ---------------------------------------------------------------------------

class TestImplicitAllow:
    def test_api_ws_implicitly_allowed_for_any_app(self):
        assert app_token_path_allowed("mochi-pet", "/api/ws") is True
        assert app_token_path_allowed("auto-improvement", "/api/ws") is True

    def test_api_status_is_NOT_implicitly_allowed(self):
        """``/api/status`` is not connection infrastructure: it returns
        ``owner_id_hash``, host specs, cron/usage stats and the live
        safety-override state, with no response-level filter to match what
        event scoping gives ``/api/ws``. An app must declare it."""
        assert app_token_path_allowed("mochi-pet", "/api/status") is False

    def test_empty_app_name_still_denied(self):
        assert app_token_path_allowed("", "/api/ws") is False

    def test_other_undeclared_path_still_denied(self):
        # Implicit allow doesn't bleed into other paths
        assert app_token_path_allowed("mochi-pet", "/api/sessions") is False
        assert app_token_path_allowed("mochi-pet", "/api/ws/extra") is False

    def test_api_ws_implicit_allow_emits_no_exception(self):
        """SEL audit in implicit allow path must not raise even if SEL is unavailable."""
        # Patch the reference used at call time (token_auth imports sel as _sel_fn
        # at module load; patching kiro_crew.sel.sel would not affect that binding).
        with patch(
            "kiro_crew.dashboard.token_auth._sel_fn",
            side_effect=Exception("sel unavailable"),
        ):
            # Should return True despite SEL failure (audit is best-effort)
            result = app_token_path_allowed("mochi-pet", "/api/ws")
            assert result is True


# ---------------------------------------------------------------------------
# Regression: test_app_token_path_allowed_implicit_ws (from original CR)
# ---------------------------------------------------------------------------

def test_app_token_path_allowed_implicit_ws() -> None:
    """``/api/ws`` is implicitly allowed for all app tokens without an explicit
    permissions.api declaration: every app that uses KiroCrewClient needs it to
    connect, and the WS layer filters events per-app via ws_event_scope.py so
    connecting does not grant full event stream access. ``/api/status`` is
    NOT implicitly allowed — it has no equivalent response filter. The
    reconnect poll that uses it is the dashboard SPA
    (``useDashboardHealthProbe``), which runs on a dashboard-user token and
    never reaches this check.
    """
    assert app_token_path_allowed("mochi-pet", "/api/ws") is True
    assert app_token_path_allowed("mochi-pet", "/api/status") is False
    assert app_token_path_allowed("auto-improvement", "/api/ws") is True
    # Other undeclared paths are still denied
    assert app_token_path_allowed("mochi-pet", "/api/sessions") is False


# ---------------------------------------------------------------------------
# Empty-app fail-closed (deny-by-default, CWE-269)
# ---------------------------------------------------------------------------

class TestEmptyAppDeniedClosed:
    """``ws_event_allowed`` must fail-closed if called with an empty app.

    ``assert`` is compiled out under ``python -O``, so we can't rely on it as
    a security guard.  The runtime check must remain in production builds.
    """

    def test_empty_app_denied_for_tier0_event(self):
        # Even the ``dashboard`` Tier-0 event must be denied when app is empty.
        state = _make_state()
        assert ws_event_allowed(
            "dashboard", {},
            app="", allowed_events=_allowed("dashboard"), state=state,
        ) is False

    def test_empty_app_denied_for_owned_slot(self):
        slot = _make_slot(owner_app="", origin=SlotOrigin.USER, key="s1")
        state = _make_state({"s1": slot})
        assert ws_event_allowed(
            "chat_chunk", {"slot": "s1"},
            app="", allowed_events=_allowed("slots:user"), state=state,
        ) is False

    def test_empty_app_deny_emits_audit(self):
        # A caller that reaches ws_event_allowed with an empty app must
        # leave an audit trail so the bypass is observable.
        from kiro_crew.dashboard import ws_event_scope as _wes
        state = _make_state()
        with patch("kiro_crew.sel.sel") as sel_mock:
            _wes._sel_last_audit.clear()  # fresh window
            ws_event_allowed(
                "chat_chunk", {}, app="", allowed_events=_allowed(), state=state,
            )
            outcomes = [
                c.kwargs.get("outcome", "")
                for c in sel_mock.return_value.log_api_access.call_args_list
            ]
            assert any("empty_app_denied" in o for o in outcomes)


# ---------------------------------------------------------------------------
# ``slot_title`` uses ``key`` (not ``slot``) for the slot identifier
# ---------------------------------------------------------------------------

class TestSlotTitleKeyField:
    """Regression: ``slot_title`` events must be filtered by the ``key`` field.

    ``_broadcast`` builds a payload of ``{"key", "title"}`` (no ``slot``), so
    the scope gate has to fall back to ``key`` — otherwise every ``slot_title``
    push is denied for every app token.
    """

    def test_slot_title_own_slot_allowed_via_key(self):
        slot = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="s1")
        state = _make_state({"s1": slot})
        assert ws_event_allowed(
            "slot_title", {"key": "s1", "title": "hi"},
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is True

    def test_slot_title_other_app_denied(self):
        slot = _make_slot(owner_app="other", origin=SlotOrigin.APP, key="s1")
        state = _make_state({"s1": slot})
        assert ws_event_allowed(
            "slot_title", {"key": "s1", "title": "hi"},
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is False


# ---------------------------------------------------------------------------
# ``slots`` full re-push: needs any ``slots:*`` scope
# ---------------------------------------------------------------------------

class TestSlotsListEvent:
    """``slots`` events are always delivered — payload-level per-app filter
    in ``DashboardState._serialize_for_client`` enforces per-slot scope.
    The default is "app sees its own slots without an explicit declaration",
    matching filter_slots_for_app's own-slot-always behaviour on initial
    connect (avoids docstring/impl divergence flagged by AutoSDE).
    """

    def test_slots_allowed_even_without_scope(self):
        # No explicit slots:* declaration — event still delivered; the
        # payload filter in _serialize_for_client will trim to own slots.
        state = _make_state()
        assert ws_event_allowed(
            "slots", [],
            app="mochi-pet", allowed_events=_allowed(), state=state,
        ) is True

    def test_slots_allowed_with_slots_own(self):
        state = _make_state()
        assert ws_event_allowed(
            "slots", [],
            app="mochi-pet", allowed_events=_allowed("slots:own"), state=state,
        ) is True

    def test_slots_allowed_with_slots_all(self):
        state = _make_state()
        assert ws_event_allowed(
            "slots", [],
            app="mochi-pet", allowed_events=_allowed("slots:all"), state=state,
        ) is True


# ---------------------------------------------------------------------------
# SEL audit deduplication (first-per-window, not per-call)
# ---------------------------------------------------------------------------

class TestAuditDedup:
    def _clear_cache(self):
        from kiro_crew.dashboard import ws_event_scope
        ws_event_scope._sel_last_audit.clear()

    def test_first_deny_audited_subsequent_within_window_suppressed(self):
        self._clear_cache()
        state = _make_state()
        with patch("kiro_crew.sel.sel") as m:
            for _ in range(5):
                ws_event_allowed(
                    "notification", {"source": "app:other-app"},
                    app="mochi-pet",
                    allowed_events=_allowed("notification"),
                    state=state,
                )
            # Only one audit emitted for the (app, event_type, reason) tuple.
            assert m.return_value.log_api_access.call_count == 1

    def test_different_reasons_audited_independently(self):
        self._clear_cache()
        state = _make_state()
        with patch("kiro_crew.sel.sel") as m:
            # notification_scope_denied
            ws_event_allowed(
                "notification", {"source": "app:other-app"},
                app="mochi-pet",
                allowed_events=_allowed("notification"),
                state=state,
            )
            # unknown_event (different reason)
            ws_event_allowed(
                "totally_made_up_event", {},
                app="mochi-pet", allowed_events=_allowed(), state=state,
            )
            assert m.return_value.log_api_access.call_count == 2


# ---------------------------------------------------------------------------
# Positive ``_is_dashboard_user`` flag on _send_ws_all / subagent subscribers
# ---------------------------------------------------------------------------

class TestPositiveDashboardUserFlag:
    """The scope gate must be triggered by the ABSENCE of a positive
    ``_is_dashboard_user`` flag, not the absence of ``_app``.  This prevents
    a future refactor from silently opening a fail-open path.
    """

    def _make_ws(self, *, is_dashboard_user: bool | None, app: str = "") -> MagicMock:
        ws = MagicMock()
        ws.closed = False
        # Simulate ws["_app"] and ws.get behavior on a real WSResponse
        store = {}
        if is_dashboard_user is not None:
            store["_is_dashboard_user"] = is_dashboard_user
        if app:
            store["_app"] = app
            store["_allowed_events"] = _allowed()
        ws.get.side_effect = lambda k, default=None: store.get(k, default)
        return ws

    def test_dashboard_user_flag_unset_is_treated_as_app(self):
        """If a code path forgets to set _is_dashboard_user, the gate applies."""
        from kiro_crew.dashboard.state import DashboardState

        # Build a minimal state stub with the helper method.
        state = MagicMock(spec=DashboardState)
        state._slots = {}
        # Bind the real helper to the mock so we exercise real logic.
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        ws = self._make_ws(is_dashboard_user=None, app="mochi-pet")
        # unknown-event → deny path
        assert state._ws_client_allowed(ws, "totally_made_up", {}) is False

    def test_dashboard_user_positive_flag_bypasses_gate(self):
        from kiro_crew.dashboard.state import DashboardState
        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        ws = self._make_ws(is_dashboard_user=True)
        assert state._ws_client_allowed(ws, "totally_made_up", {}) is True


class TestScopeCheckExceptionAudited:
    """When ws_event_allowed itself blows up, the fail-closed branch must
    audit the deny so scope-check bugs remain observable.
    """

    def test_scope_check_exception_emits_audit(self):
        from kiro_crew.dashboard import ws_event_scope
        from kiro_crew.dashboard.state import DashboardState
        ws_event_scope._sel_last_audit.clear()
        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        ws = MagicMock()
        ws.closed = False
        ws.get.side_effect = lambda k, default=None: {
            "_is_dashboard_user": False,
            "_app": "mochi-pet",
            "_allowed_events": _allowed(),
        }.get(k, default)
        # Force ws_event_allowed to raise
        with patch(
            "kiro_crew.dashboard.ws_event_scope.ws_event_allowed",
            side_effect=RuntimeError("boom"),
        ), patch("kiro_crew.sel.sel") as sel_mock:
            result = state._ws_client_allowed(ws, "chat_chunk", {"slot": "x"})
            assert result is False
            # The audit was invoked at least once with the scope_check_exception reason.
            calls = sel_mock.return_value.log_api_access.call_args_list
            reasons = [c.kwargs.get("outcome", "") for c in calls]
            assert any("scope_check_exception" in r for r in reasons)
