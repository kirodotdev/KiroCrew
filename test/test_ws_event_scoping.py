"""Tests for per-app WebSocket event scope enforcement.

Covers:
- ws_event_allowed: Tier 0 (always), Tier 1 (slot-scoped), Tier 2 (global)
- Slot visibility: own-slot, user-slot, cross-app opt-in, slots:all
- Subagent visibility: independent dimension from slots
- filter_slots_for_app: initial slots push filtering
- build_allowed_event_set: passthrough normalisation
- token_auth: implicit allow for /api/ws + /api/status with SEL audit
- broadcast_ws integration: app clients filtered, dashboard users unaffected
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.dashboard.token_auth import app_token_path_allowed
from kiro_crew.dashboard.ws_event_scope import (
    build_allowed_event_set,
    filter_slots_for_app,
    ws_event_allowed,
)


@pytest.fixture(autouse=True)
def _clear_module_caches():
    """Clear ws_event_scope module-level caches between tests.

    The exposeToApps TTL cache and the SEL audit dedup cache persist across
    test invocations at module scope; without an autouse fixture, a stale
    entry from an earlier test can suppress the behaviour under test in a
    later one (especially the cross-app tests that patch ``get_app_manifest``).
    """
    from kiro_crew.dashboard import ws_event_scope as _wes
    _wes._exposeto_cache.clear()
    _wes._sel_last_audit.clear()
    yield
    _wes._exposeto_cache.clear()
    _wes._sel_last_audit.clear()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_slot(*, owner_app: str = "", origin: str = SlotOrigin.USER, key: str = "s1") -> MagicMock:
    slot = MagicMock()
    slot._app = owner_app
    slot._origin = origin
    slot.key = key
    return slot


def _make_state(slots: dict[str, Any] | None = None) -> MagicMock:
    state = MagicMock()
    state._slots = slots or {}
    return state


def _allowed(*decls: str) -> frozenset[str]:
    return build_allowed_event_set(list(decls))


@pytest.fixture(autouse=True)
def _clear_ws_scope_module_caches(monkeypatch):
    """Isolate the module-level caches between tests.

    ``ws_event_scope`` keeps process-wide dicts (declaration refresh, exposeToApps,
    SEL dedup) keyed by app NAME. Tests share fixture app names, so one test
    seeding a narrowed declaration for ``mochi-pet`` would otherwise silently
    narrow every later test that uses the same name.

    Also pins ``is_app_enabled`` to True as the DEFAULT world, because that is what
    production looks like for any app able to reach the gate: a token is only
    minted for an installed app, so ``installed.json`` has an enabled entry. The
    suite's app names are synthetic and absent from the real ``installed.json``,
    which without this would read as REVOKED and withhold even the own-slot
    default. Tests that exercise revocation override this explicitly (patch it
    False, or seed ``_declared_cache`` with a disabled entry).
    """
    from kiro_crew.dashboard import ws_event_scope as mod
    monkeypatch.setattr(mod, "is_app_enabled", lambda _name: True)
    for cache in (mod._declared_cache, mod._exposeto_cache, mod._sel_last_audit):
        cache.clear()
    for pending in (mod._declared_refreshing, mod._exposeto_refreshing):
        pending.clear()
    yield
    for cache in (mod._declared_cache, mod._exposeto_cache, mod._sel_last_audit):
        cache.clear()
    for pending in (mod._declared_refreshing, mod._exposeto_refreshing):
        pending.clear()


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
    connecting no longer grants full event stream access. ``/api/status`` is
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


# ---------------------------------------------------------------------------
# _origin persistence — cron/system slots must survive a restart with the
# correct origin so the WS scope gate doesn't mis-classify them.
# ---------------------------------------------------------------------------

class TestOriginPersistence:
    """A cron- or system-initiated slot rehydrated from disk must keep its
    original origin so ``slots:user`` scopes don't inadvertently grant an app
    visibility into events it wasn't authorised for.

    These drive the REAL save/restore functions against a REAL
    ``ConversationLog``. An earlier version of this class rebuilt the
    ``meta_line`` assembly inside the test and asserted on its own local dict —
    so it stayed green while ``_save_slot_to_history`` never wrote ``origin``
    at all, and every slot silently rehydrated unattributed. Never restate the
    production logic here; call it.
    """

    @staticmethod
    def _state(tmp_path):
        from unittest.mock import AsyncMock

        from chat_test_helpers import _make_ready_kiro_prerequisite

        from kiro_crew.dashboard.state import DashboardState
        from kiro_crew.history import ConversationLog

        sessions = MagicMock(count=0)
        sessions.remove = AsyncMock()
        sessions.recycle_background = AsyncMock()
        sessions.get_pid = MagicMock(return_value=None)
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
        return state

    def test_slot_has_origin_attribute(self):
        from kiro_crew.dashboard.state import _ChatSlot

        assert hasattr(_ChatSlot("s1"), "_origin")

    def test_to_dict_includes_origin(self):
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s1")
        slot._origin = SlotOrigin.CRON
        assert slot.to_dict()["origin"] == SlotOrigin.CRON

    def test_save_writes_origin_to_meta(self, tmp_path, monkeypatch):
        """The REAL save path must persist origin — this is the write side that
        was missing, letting every restored slot come back unattributed."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("cron-1")
        slot._origin = SlotOrigin.CRON
        slot.append("user", "hi")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log.get_metadata("dashboard:cron-1")
        assert meta.get("origin") == SlotOrigin.CRON

    def test_untagged_origin_not_persisted(self, tmp_path, monkeypatch):
        """The fail-closed empty sentinel must not be written as a real value."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._origin = ""
        slot.append("user", "hi")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        assert "origin" not in state.conversation_log.get_metadata("dashboard:s1")

    @pytest.mark.parametrize("origin", [SlotOrigin.CRON, SlotOrigin.USER, SlotOrigin.APP])
    def test_origin_round_trips_through_rehydrate(self, tmp_path, monkeypatch, origin):
        """save -> _rehydrate_slot_from_history must return the SAME origin."""
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._origin = origin
        if origin == SlotOrigin.APP:
            slot._app = "mochi-pet"
        slot.append("user", "hi")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert restored._origin == origin

    def test_origin_round_trips_through_bulk_restore(self, tmp_path, monkeypatch):
        """The bulk restore path (gateway boot) must restore origin too — the
        two restore sites are separate code and both read ``meta["origin"]``."""
        from kiro_crew.dashboard.chat_persistence import (
            _save_slot_to_history,
            restore_recent_sessions,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("cron-1")
        slot._origin = SlotOrigin.CRON
        slot.append("user", "hi")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["cron-1"]

        restore_recent_sessions(state)

        assert state._slots["cron-1"]._origin == SlotOrigin.CRON

    def test_restored_cron_slot_still_hidden_from_slots_user(self, tmp_path, monkeypatch):
        """The security property the persistence exists for: a CRON slot that
        survived a restart must STILL be withheld from a ``slots:user`` app.
        Without the write side it rehydrated untagged and this gate flipped."""
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("cron-1")
        slot._origin = SlotOrigin.CRON
        slot.append("user", "hi")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["cron-1"]
        restored = _rehydrate_slot_from_history(state, "cron-1")
        assert restored is not None

        assert not ws_event_allowed(
            "chat_message", {"slot": "cron-1"},
            app="some-app", allowed_events=frozenset({"slots:user"}), state=state,
        )


# ---------------------------------------------------------------------------
# Positive is_dashboard_user flag from token_auth — a token that does not
# come through the auth middleware (or fails it) MUST NOT bypass the gate.
# ---------------------------------------------------------------------------

class TestAuthMiddlewareIsDashboardUser:
    """The auth middleware sets ``request["is_dashboard_user"]`` positively
    only for verified dashboard-user tokens.  App tokens and unauthenticated
    requests never see the flag flip to True.
    """

    def test_dashboard_user_bypasses_scope_gate(self):
        # Behavioral: a ws with positive _is_dashboard_user flag must bypass
        # the scope gate entirely (that's the CWE-269 fix's whole point).
        from kiro_crew.dashboard.state import DashboardState
        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        ws = MagicMock()
        ws.closed = False
        ws.get.side_effect = lambda k, default=None: {
            "_is_dashboard_user": True,
        }.get(k, default)
        # Even an unknown event that would deny app tokens passes for
        # dashboard users.
        assert state._ws_client_allowed(ws, "totally_unknown_event", {}) is True

    def test_missing_flag_treats_as_app_token(self):
        # Behavioral: without the positive flag, gate applies (deny-by-default).
        from kiro_crew.dashboard.state import DashboardState
        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state._ws_client_allowed = DashboardState._ws_client_allowed.__get__(state)
        ws = MagicMock()
        ws.closed = False
        ws.get.side_effect = lambda k, default=None: {
            # Deliberately missing _is_dashboard_user.
            "_app": "mochi-pet",
            "_allowed_events": frozenset(),
        }.get(k, default)
        assert state._ws_client_allowed(ws, "totally_unknown_event", {}) is False


# ---------------------------------------------------------------------------
# Missing _origin fails closed — a slot without the attribute must not be
# treated as USER by default.
# ---------------------------------------------------------------------------

class TestMissingOriginFailsClosed:
    """CWE-269 defense-in-depth: if ``_origin`` is somehow missing (pre-
    migration slot, race, bug), it must remain invisible to any ``:user``
    scope rather than being silently classified as ``SlotOrigin.USER``.
    """

    def _slot_without_origin(self, owner_app: str = "") -> MagicMock:
        # Deliberately DO NOT set _origin on the mock — spec=[] means
        # hasattr(slot, "_origin") is False.
        slot = MagicMock(spec=["_app"])
        slot._app = owner_app
        return slot

    def test_slot_visible_denied_when_origin_missing(self):
        slot = self._slot_without_origin()
        state = _make_state({"s1": slot})
        # slots:user must NOT grant visibility to a slot with no _origin.
        assert ws_event_allowed(
            "chat_chunk", {"slot": "s1"},
            app="mochi-pet",
            allowed_events=_allowed("slots:user"),
            state=state,
        ) is False

    def test_subagent_visible_denied_when_origin_missing(self):
        slot = self._slot_without_origin()
        state = _make_state({"s1": slot})
        assert ws_event_allowed(
            "subagent_chunk", {"slot": "s1"},
            app="mochi-pet",
            allowed_events=_allowed("subagent:user"),
            state=state,
        ) is False


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
        state._serialize_for_client = (
            DashboardState._serialize_for_client.__get__(state)
        )
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
        default_msg = json.dumps({
            "type": "slots",
            "data": [{"key": "a"}, {"key": "b"}],
            "yolo": False,
            "channelTrusted": False,
        })
        result = state._serialize_for_client(
            ws, "slots",
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
            is_dashboard=False, app="mochi-pet",
            events=frozenset({"slots:own"}),
        )
        result_str = state._serialize_for_client(
            ws, "slots",
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
        mod._declared_cache["mochi-pet"] = (
            time.monotonic(), True, _allowed("slots:own", "log")
        )
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
        with patch.object(mod, "get_app_manifest") as gm, \
                patch.object(mod, "_schedule_declared_refresh"):
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
        with patch.object(mod, "is_app_enabled", return_value=False), \
                patch.object(mod, "get_app_manifest", return_value=fake):
            assert mod._read_declared_events("mochi-pet") == (False, frozenset())

    def test_enabled_app_declares_its_manifest_events(self):
        from kiro_crew.dashboard import ws_event_scope as mod
        fake = MagicMock()
        fake.permissions.events = ["slots:own"]
        with patch.object(mod, "is_app_enabled", return_value=True), \
                patch.object(mod, "get_app_manifest", return_value=fake):
            assert mod._read_declared_events("mochi-pet") == (True, _allowed("slots:own"))

    def test_enablement_is_checked_before_the_manifest_read(self):
        """A disabled app must not even cost a manifest parse."""
        from kiro_crew.dashboard import ws_event_scope as mod
        with patch.object(mod, "is_app_enabled", return_value=False), \
                patch.object(mod, "get_app_manifest") as gm:
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
        with patch.object(mod, "is_app_enabled", return_value=True), \
                patch.object(mod, "get_app_manifest", return_value=None):
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

        # Declaration set no longer contains `log` (revoked).
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

    def test_log_subscriber_send_skips_the_check_for_dashboard_users(self):
        import asyncio as _aio

        from kiro_crew.dashboard.handlers import updates as upd

        ws = MagicMock()
        ws.get.side_effect = lambda k, default=None: (
            True if k == "_is_dashboard_user" else default
        )
        sent: list[str] = []

        async def _send(msg):
            sent.append(msg)

        ws.send_str = _send
        state = MagicMock()
        _aio.run(upd._safe_ws_send(ws, '{"type":"log"}', state))
        assert sent == ['{"type":"log"}']

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
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "dashboard" / "ws.py"
        ).read_text(encoding="utf-8")
        assert 'for _owner_only in ("branch", "commit")' in src, (
            "the periodic dashboard frame must withhold checkout identity from apps"
        )
        assert 'if not ws.get("_is_dashboard_user", False):' in src
        # And the owner surfaces must NOT be narrowed: /api/status and SSE run on
        # dashboard-user tokens and still need the full snapshot.
        state_src = (
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "dashboard" / "state.py"
        ).read_text(encoding="utf-8")
        assert "branch, commit = self._build_info" in state_src, (
            "status_snapshot itself must keep returning branch/commit"
        )

    def test_source_guard_log_replay_uses_the_live_scope(self):
        """The replay half of the log path.

        Two gates read the log scope: the one-shot ring REPLAY in
        ``subscribe_logs`` and the per-frame send. Fixing only the send left the
        replay able to hand over the whole buffered ring on a revoked scope, so
        this guard covers ``ws.py`` too — the state.py-only guard below is why
        that half was missed.
        """
        src = (
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "dashboard" / "ws.py"
        ).read_text(encoding="utf-8")
        assert "effective_allowed_events(ws_app, allowed_events)" in src, (
            "subscribe_logs must resolve the live scope before replaying the ring"
        )
        assert '"log" in _live or "log:all" in _live' in src, (
            "the replay gate must test the LIVE set, not the connect snapshot"
        )

    def test_source_guard_every_allowed_events_read_is_narrowed(self):
        """All three consumers must narrow, not just the gate.

        The payload filters (`slots`, subagent batches) decide with the same set;
        leaving one on the raw snapshot would keep handing back the rows a
        revoked scope selected.
        """
        src = (
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "dashboard" / "state.py"
        ).read_text(encoding="utf-8")
        raw_reads = src.count('ws.get("_allowed_events", frozenset())')
        narrowed = src.count("effective_allowed_events(ws_app, snapshot)")
        assert raw_reads == narrowed == 3, (
            f"{raw_reads} snapshot reads but {narrowed} narrowed — every read of "
            "_allowed_events in state.py must go through effective_allowed_events"
        )


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
            assert ws_event_allowed(
                "chat_delta", {"slot": "s1"},
                app=self.APP, allowed_events=frozenset(), state=state,
            ) is True
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
            (sorted(mod._TIER0_ALWAYS)[0], {}, frozenset()),          # Tier 0
            ("slots", {}, frozenset()),                                # admitted whole
            (batch, {}, frozenset()),                                  # admitted whole
            ("chat_delta", {"slot": "s1"}, frozenset()),               # own slot
            ("log", {}, _allowed("log")),                              # global, declared
        ]
        for event, payload, scopes in cases:
            with patch.object(mod, "_audit_decision") as audit:
                assert ws_event_allowed(
                    event, payload, app=self.APP, allowed_events=scopes, state=state,
                ) is True, f"{event} should be allowed in this setup"
            assert audit.call_count == 1, f"{event} was allowed without an audit record"

    def test_repeated_grants_are_deduplicated(self):
        self._enable()
        state = self._state_with_own_slot()

        # Count SEL emissions, not calls into the audit helper: the helper runs
        # every time and the window is what collapses the write.
        with patch("kiro_crew.sel.sel") as sel_factory:
            for _ in range(5):
                ws_event_allowed(
                    "chat_delta", {"slot": "s1"},
                    app=self.APP, allowed_events=frozenset(), state=state,
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
                "chat_delta", {"slot": "s1"},
                app=self.APP, allowed_events=frozenset(), state=state,
            )
            ws_event_allowed(
                "chat_delta", {"slot": "s2"},
                app=self.APP, allowed_events=frozenset(), state=state,
            )
            outcomes = [
                c.kwargs.get("outcome")
                for c in sel_factory.return_value.log_api_access.call_args_list
            ]
        assert any(o == "granted" for o in outcomes), f"no grant recorded: {outcomes}"
        assert any(
            o and o.startswith("denied:") for o in outcomes
        ), f"no denial recorded: {outcomes}"


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
        assert ws_event_allowed(
            "chat_delta", {"slot": "s1"},
            app=self.APP, allowed_events=frozenset(), state=state,
        ) is True, "an ENABLED app sees its own slot by documented default"

        self._revoke()
        assert ws_event_allowed(
            "chat_delta", {"slot": "s1"},
            app=self.APP, allowed_events=frozenset(), state=state,
        ) is False, "a disabled app must not keep receiving its own chat"

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
            assert ws_event_allowed(
                etype, {}, app=self.APP, allowed_events=frozenset(), state=state,
            ) is True, f"{etype} is admitted whole for an enabled app"

        self._revoke()
        for etype in ("slots", batch):
            assert ws_event_allowed(
                etype, {}, app=self.APP, allowed_events=frozenset(), state=state,
            ) is False, f"{etype} must not be admitted for a revoked app"

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
        assert any(o.startswith("denied:") for o in outcomes), (
            "the foreign slot must be audited as a denial"
        )

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
        assert ws_event_allowed(
            tier0, {}, app=self.APP, allowed_events=frozenset(), state=state,
        ) is True

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

        with patch.object(mod, "is_app_enabled", return_value=False), \
                patch.object(mod, "get_app_manifest", return_value=fake):
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
        with patch.object(mod, "is_app_enabled", return_value=True), \
                patch.object(mod, "get_app_manifest", return_value=fake):
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

        assert enabled_scopes == revoked_scopes == frozenset(), (
            "indistinguishable by scope set -- hence app_events_revoked"
        )


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
        app = _ws({
            "_is_dashboard_user": False,
            "_app": "mochi-pet",
            "_allowed_events": app_events,
        })

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
        state, dash, app, wire = self._sockets_and_state(
            app_events=_allowed("slots:own")
        )
        state._send_ws_all(
            "chat_chunk", {"slot": "foreign"}, json.dumps({"type": "chat_chunk"})
        )
        assert self._sent_to(wire, dash), "dashboard user must still receive it"
        assert not self._sent_to(wire, app), "app must not see a foreign slot's chunk"

    def test_own_slot_event_reaches_both(self):
        """Own-slot delivery needs no declaration -- the documented default."""
        state, dash, app, wire = self._sockets_and_state(
            app_events=_allowed("slots:own")
        )
        state._send_ws_all(
            "chat_chunk", {"slot": "own"}, json.dumps({"type": "chat_chunk"})
        )
        assert self._sent_to(wire, dash)
        assert self._sent_to(wire, app), "an app always sees its OWN slot's events"

    def test_slots_repush_is_admitted_but_payload_filtered_on_the_wire(self):
        """The admitted-frame-unfiltered-payload defect class, end to end.

        The gate lets ``slots`` through for everyone; only the serializer keeps
        an app from reading every slot. Asserting the boolean would pass while
        the wire still carried the full list.
        """
        state, dash, app, wire = self._sockets_and_state(
            app_events=_allowed("slots:own")
        )
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
        assert dash_frames == [json.dumps({"type": "slots"})], (
            "dashboard user gets the unmodified default message"
        )

    def test_unknown_event_is_withheld_from_app_only(self):
        """Deny-by-default at the seam: an unclassified event never reaches an app.

        The payload deliberately carries NO ``slot``: a slot field routes the
        frame into the slot-visibility branch instead, where an own-slot event is
        allowed by default and the unknown-event floor is never consulted.
        """
        state, dash, app, wire = self._sockets_and_state(
            app_events=build_allowed_event_set(["*"])
        )
        state._send_ws_all(
            "totally_new_event", {"detail": "x"}, json.dumps({"type": "x"})
        )
        assert self._sent_to(wire, dash)
        assert not self._sent_to(wire, app), (
            "even a wildcard manifest must not receive an unclassified event"
        )

    def test_closed_socket_is_reaped_without_blocking_the_others(self):
        state, dash, app, wire = self._sockets_and_state(
            app_events=_allowed("slots:own")
        )
        dash.closed = True
        removed: list[MagicMock] = []
        state._remove_ws = removed.append
        state._send_ws_all(
            "chat_chunk", {"slot": "own"}, json.dumps({"type": "chat_chunk"})
        )
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
        state._serialize_for_client = (
            DashboardState._serialize_for_client.__get__(state)
        )
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
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "dashboard" / "ws.py"
        ).read_text(encoding="utf-8")
        assert "slots_envelope_extras(" in src, (
            "ws.py connect push must route the envelope through the gate helper"
        )
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
        with patch.object(mod, "get_app_manifest") as gm, \
                patch.object(mod, "_schedule_expose_to_refresh") as sched:
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
            time.monotonic() - (mod._MANIFEST_EXPOSE_TTL_SECS + 5), stale
        )
        with patch.object(mod, "get_app_manifest") as gm, \
                patch.object(mod, "_schedule_expose_to_refresh") as sched:
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
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "dashboard" / "ws.py"
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
                window = norm[max(0, match.start() - 80):match.start()]
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
        """The connect read must be USED to refuse, not just to build a snapshot.

        Reading enablement and then ignoring it is the defect this replaced: the
        initial slots push and the log replay both run before any background
        refresh, so a disabled app that reconnects would be served from its intact
        manifest.
        """
        src = (
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "dashboard" / "ws.py"
        ).read_text(encoding="utf-8")
        assert "if not app_enabled:" in src and "ws.close(" in src, (
            "ws.py must close the socket when the connect read reports the app "
            "disabled"
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
        assert '"log" in _live' in src, (
            "subscribe_logs must remain gated on the log scope"
        )
        assert '"log:all" in _live' in src, (
            "the replay gate must accept log:all, like the per-event chokepoint"
        )
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
        assert "subagent_scope_not_declared" not in src, (
            "the declaration-level subscribe_subagents rejection must not return"
        )
        assert "state.subscribe_subagents(ws)" in src

    def test_subagent_snapshot_replay_uses_per_item_scope_check(self):
        # Behavioral: subagent_snapshot is now in _SLOT_SCOPED_EVENTS and
        # _SUBAGENT_EVENTS, so ws_event_allowed routes it through
        # _subagent_visible — an app can be denied for a foreign slot's
        # snapshot even after passing the subscribe_subagents gate.
        slot = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="s1")
        state = _make_state({"s1": slot})
        assert ws_event_allowed(
            "subagent_snapshot", {"slot": "s1", "id": "a1"},
            app="mochi-pet",
            allowed_events=_allowed("subagent"),  # own-only
            state=state,
        ) is False

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
        with patch(
            "kiro_crew.dashboard.ws_event_scope.get_app_manifest",
            return_value=manifest,
        ) as m, patch(
            "kiro_crew.dashboard.ws_event_scope.is_app_enabled", return_value=False
        ):
            state = MagicMock()
            assert _wes._target_exposes_to("mochi-pet", "monitor-app", state) is False
            # The manifest is never even read once the app is known disabled.
            m.assert_not_called()


class TestEventTableCompleteness:
    """Every event that can reach an app socket must be classified.

    ``ws_event_allowed`` DENIES unknown event types (deny-by-default, CWE-269).
    That is the right default, but it makes an unclassified event a *silent*
    loss of functionality for apps rather than a loud error -- the failure mode
    ``workflow_run_event`` (the one declaration that exists in the tree) sits
    closest to.

    This test closes that class of bug: it walks the source for literal event
    names published onto the WS fan-out and asserts each one is classified.
    New events therefore fail the build here instead of vanishing in
    production.
    """

    def test_fire_event_names_are_classified(self):
        """The emitter path a literal-scanning guard cannot see.

        ``SubagentManager._fire_event`` names its event, and the Slack gateway
        dispatcher forwards it with ``broadcast_ws(etype, ...)`` -- a VARIABLE, so
        scanning broadcast call sites for string literals finds none of these.
        Read the literals at the emitter instead. An unclassified name here hits
        the unknown-event floor and is denied to EVERY app regardless of what it
        declared, which is silent loss of the whole subagent stream rather than a
        loud error.
        """
        src = (
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "subagent.py"
        ).read_text(encoding="utf-8")
        # `_fire_event(` then the first string literal, across a line break.
        fired = set(re.findall(r'_fire_event\(\s*\n?\s*"([a-z_]+)"', src))
        assert len(fired) >= 8, f"emitter scan found too few names: {sorted(fired)}"
        unclassified = sorted(fired - self._classified())
        assert not unclassified, (
            "these events are fired to the WS fan-out but classified in no "
            f"ws_event_scope table, so every app token is denied them: {unclassified}"
        )

    def test_subagent_events_are_a_subset_of_slot_scoped(self):
        """A structural invariant the tables rely on but never state.

        The ``subagent:*`` vocabulary WIDENS slot visibility; it is not a second,
        independent tier. A member outside ``_SLOT_SCOPED_EVENTS`` falls to the
        unknown-event floor and is denied outright, and the SDK's slot-scoped
        drift loop stops covering it.
        """
        from kiro_crew.dashboard import ws_event_scope as mod
        assert mod._SUBAGENT_EVENTS <= mod._SLOT_SCOPED_EVENTS, (
            mod._SUBAGENT_EVENTS - mod._SLOT_SCOPED_EVENTS
        )

    @staticmethod
    def _classified() -> frozenset[str]:
        from kiro_crew.dashboard import ws_event_scope as m

        return frozenset(
            set(m._TIER0_ALWAYS)
            | set(m._SLOT_SCOPED_EVENTS)
            | set(m._SUBAGENT_EVENTS)
            | set(m._GLOBAL_EVENT_DECLARATIONS)
            # ``slots`` is short-circuited in the gate and filtered at the
            # payload layer instead (_serialize_for_client), so it is
            # deliberately absent from the tables.
            | {"slots"}
        )

    def test_every_broadcast_event_name_is_classified(self):
        import ast
        from pathlib import Path

        import kiro_crew

        root = Path(kiro_crew.__file__).parent
        # Owner-only fan-outs never reach an app socket, so their event names
        # need no classification.
        gated = {"broadcast_ws", "broadcast_ws_subagent_subscribers"}
        unclassified: list[tuple[str, int, str]] = []
        classified = self._classified()

        for path in root.rglob("*.py"):
            if "/builtins/" in str(path):
                continue  # app code, not gateway fan-out
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            # Module-level ``NAME = "literal"`` bindings, so a constant passed
            # as the event type is resolved rather than skipped.
            const_strs: dict[str, str] = {
                t.id: n.value.value
                for n in tree.body
                if isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
                for t in n.targets
                if isinstance(t, ast.Name)
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name not in gated or not node.args:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    event_name = first.value
                elif isinstance(first, ast.Name) and first.id in const_strs:
                    # Module-level string constant (e.g. SIDE_RESULT_EVENT) —
                    # resolving these matters: ``chat.side_result`` is passed
                    # this way and a literal-only scan silently misses it.
                    event_name = const_strs[first.id]
                else:
                    continue  # dynamic event type -- cannot be checked statically
                if event_name not in classified:
                    unclassified.append(
                        (str(path.relative_to(root)), first.lineno, event_name)
                    )

        assert not unclassified, (
            "These WS events are broadcast but not classified in "
            "ws_event_scope.py, so app tokens will silently never receive "
            f"them: {unclassified}. Add each to _TIER0_ALWAYS, "
            "_SLOT_SCOPED_EVENTS (+_SUBAGENT_EVENTS if applicable), or "
            "_GLOBAL_EVENT_DECLARATIONS."
        )

    def test_broadcast_type_map_events_are_classified(self):
        """``_broadcast`` translates internal ``_type`` values into WS events.

        That second dispatch path funnels through the same chokepoint, so its
        event names must be classified too -- a literal scan of
        ``broadcast_ws`` calls alone would miss them (chat_message and the
        slots re-push both travel this way).
        """
        for event in (
            "slots",
            "slot_title",
            "refresh",
            "update_progress",
            "artifact_update",
            "chat_message",
            "notification",
        ):
            assert event in self._classified(), (
                f"{event!r} is emitted by DashboardState._broadcast but is not "
                "classified in ws_event_scope.py"
            )


# ---------------------------------------------------------------------------
# Write-side origin: an undeclared slot must NOT be called USER
# ---------------------------------------------------------------------------

class TestUntaggedOriginIsNotUser:
    """The read side already failed closed on a missing ``_origin``; the WRITE
    side did not. ``origin or (APP if app else USER)`` labelled every untagged
    caller USER -- cron result injection, workflow inject, Slack, the
    OpenAI-compatible endpoint -- and an app holding ``slots:user`` received
    that private content."""

    def test_untagged_non_app_slot_is_not_user(self):
        from kiro_crew.dashboard.state import SlotOrigin, request_slot_origin

        src = Path(
            __import__("kiro_crew.dashboard.state", fromlist=["x"]).__file__
        ).read_text(encoding="utf-8")
        # The fail-open derivation must be gone from the CREATION path. It
        # still exists once, inside request_slot_origin -- that is the point:
        # the derivation moved to the layer that can actually make it.
        assert (
            "slot._origin = origin or (SlotOrigin.APP if app else SlotOrigin.USER)"
            not in src
        ), "get_or_create_slot must not infer USER; it cannot know"
        assert 'slot._origin = origin or (SlotOrigin.APP if app else "")' in src
        assert src.count("SlotOrigin.APP if app else SlotOrigin.USER") == 1, (
            "the USER derivation belongs only in request_slot_origin"
        )

        # USER is decided by the layer that knows: did a request carry a token?
        assert request_slot_origin("") == SlotOrigin.USER
        assert request_slot_origin("some-app") == SlotOrigin.APP

    def test_every_handler_slot_creation_declares_an_origin(self):
        """No slot-creation path in the handlers may fall through to the default.

        Two legitimate sources, and the distinction matters: a NEW slot's origin
        comes from the request (an app token means APP, its absence means the
        dashboard user), while RESUMING a persisted conversation must take the
        origin that conversation was stored with -- deriving it from the resumer
        relabels a cron slot as USER. Counting both together is the invariant;
        pinning one of them everywhere is what got the resume path wrong.
        """
        import kiro_crew.dashboard.chat_handlers as _ch

        src = Path(_ch.__file__).read_text(encoding="utf-8")
        creates = src.count("state.get_or_create_slot(")
        from_request = src.count("origin=request_slot_origin(")
        from_persisted = src.count('origin=str(meta.get("origin", ""))')
        assert creates == from_request + from_persisted, (
            f"{creates} slot creations but only {from_request} declare a "
            f"request-derived origin and {from_persisted} a persisted one"
        )

    def test_resume_takes_the_persisted_origin_not_the_resumer(self):
        """The resume endpoint must read metadata BEFORE creating the slot.

        It used to create the slot from the request identity and read the history
        metadata a dozen lines later, so resuming a persisted cron conversation
        from the dashboard produced a USER-tagged slot and `slots:user` handed its
        replayed content to any app holding that scope.
        """
        import kiro_crew.dashboard.chat_handlers as _ch

        src = Path(_ch.__file__).read_text(encoding="utf-8")
        meta_read = src.index("meta = state.conversation_log.get_metadata(history_key)")
        resume_create = src.index('origin=str(meta.get("origin", ""))')
        assert meta_read < resume_create, (
            "the resume path must read the persisted metadata before it creates "
            "the slot, or the origin it passes cannot come from that metadata"
        )

    def test_cron_injection_declares_cron(self):
        import kiro_crew.dashboard.cron_inject as _ci

        assert "origin=SlotOrigin.CRON" in Path(_ci.__file__).read_text(encoding="utf-8"), (
            "a cron result is the job's output, never something the user typed"
        )

    def test_rehydrate_restores_the_persisted_origin(self):
        """Re-deriving on restart would relabel a cron slot USER (leak) and a
        real user slot untagged (dropping a grant an app legitimately holds)."""
        import kiro_crew.dashboard.chat_persistence as _cp

        assert 'origin=str(meta.get("origin", ""))' in Path(_cp.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The legacy "*" declaration
# ---------------------------------------------------------------------------

class TestWildcardDeclaration:
    """``permissions.events: ["*"]`` predates this vocabulary and meant every
    event. Carried through as an opaque set member no gate recognises it, so
    such a manifest would keep its subscription and receive nothing."""

    def test_wildcard_expands_instead_of_passing_through(self):
        from kiro_crew.dashboard.ws_event_scope import build_allowed_event_set

        allowed = build_allowed_event_set(["*"])
        assert "*" not in allowed, "the wildcard must be expanded, not stored"
        assert "slots:all" in allowed
        assert "subagent:all" in allowed
        assert "notification:all" in allowed

    def test_wildcard_covers_every_global_declaration(self):
        from kiro_crew.dashboard.ws_event_scope import (
            _GLOBAL_EVENT_DECLARATIONS,
            build_allowed_event_set,
        )

        allowed = build_allowed_event_set(["*"])
        missing = sorted(
            d for d in set(_GLOBAL_EVENT_DECLARATIONS.values()) if d not in allowed
        )
        assert not missing, f"wildcard omits declarations: {missing}"

    def test_ordinary_declarations_pass_through_unchanged(self):
        from kiro_crew.dashboard.ws_event_scope import build_allowed_event_set

        assert build_allowed_event_set(["slots:own", "notification"]) == frozenset(
            {"slots:own", "notification"}
        )


# ---------------------------------------------------------------------------
# Notification source: canonical, prefixed, and system-only
# ---------------------------------------------------------------------------

class TestNotificationSourceParsing:
    """The note's field is ``source`` and an app push is ``app:<name>``.

    The gate used to read ``source_app``/``app`` -- keys no emitter writes -- so
    it saw "" for every note and accepted "" as the system stream: one app's
    private push reached any app holding ``notification:system``, and an app
    holding ``notification`` never saw its own.
    """

    def test_constants_match_the_real_emitters(self):
        """Pin the two producers, so a rename on either side fails here."""
        from kiro_crew.dashboard import ws_event_scope as wes
        from kiro_crew.notifications import bus

        # state.notify() -> payload_from_legacy() stamps this exact value.
        assert wes._SYSTEM_SOURCE == bus._SYSTEM_SOURCE

        # api_push_notification builds `source=f"app:{app_name}"`.
        push = Path(
            __import__(
                "kiro_crew.dashboard.handlers.notifications_push", fromlist=["x"]
            ).__file__
        ).read_text(encoding="utf-8")
        assert f'source=f"{wes._APP_SOURCE_PREFIX}{{app_name}}"' in push

    def test_parses_the_app_identity_out_of_the_prefix(self):
        from kiro_crew.dashboard.ws_event_scope import notification_source_app

        assert notification_source_app("app:mochi-pet") == "mochi-pet"
        # A bare name is NOT an app push: it never appears on the wire, and
        # treating it as one would accept a shape the server does not produce.
        assert notification_source_app("mochi-pet") == ""
        assert notification_source_app("system") == ""
        assert notification_source_app("") == ""

    def test_own_push_reaches_only_its_own_app(self):
        state = _make_state()
        note = {"source": "app:mochi-pet", "text": "hi"}
        assert ws_event_allowed(
            "notification", note, app="mochi-pet",
            allowed_events=_allowed("notification"), state=state,
        ) is True
        # The leak: a SECOND app holding the system scope must not receive it.
        assert ws_event_allowed(
            "notification", note, app="other-app",
            allowed_events=_allowed("notification:system"), state=state,
        ) is False
        assert ws_event_allowed(
            "notification", note, app="other-app",
            allowed_events=_allowed("notification"), state=state,
        ) is False

    def test_only_the_literal_system_source_is_the_system_stream(self):
        state = _make_state()
        for source in ("app:other-app", "notifications_api", "", "System", "sys"):
            assert ws_event_allowed(
                "notification", {"source": source}, app="mochi-pet",
                allowed_events=_allowed("notification:system"), state=state,
            ) is False, f"{source!r} must not read as the system stream"
        assert ws_event_allowed(
            "notification", {"source": "system"}, app="mochi-pet",
            allowed_events=_allowed("notification:system"), state=state,
        ) is True

    def test_ack_events_need_the_broad_scope(self):
        """`notification_ack`/`_unack` broadcast a bare `{"ts": ...}`.

        Nothing in that payload says WHOSE notification was acked, so own-only
        `notification` cannot be honoured for them. Letting them ride the plain
        declaration -- which is what removing them from the source filter did --
        handed an app the ack stream for every other app's and the system's
        notifications. Unattributable metadata takes the broad scope.
        """
        from kiro_crew.dashboard.ws_event_scope import (
            _SOURCE_FILTERED_EVENTS,
            _UNATTRIBUTED_NOTIFICATION_EVENTS,
        )

        state = _make_state()
        for event in ("notification_ack", "notification_unack"):
            # Not source-filtered (no `source` field to filter on) but still not
            # covered by the own-only declaration.
            assert event not in _SOURCE_FILTERED_EVENTS
            assert event in _UNATTRIBUTED_NOTIFICATION_EVENTS
            payload = {"ts": "2026-08-04T00:00:00Z"}
            assert ws_event_allowed(
                event, payload, app="mochi-pet",
                allowed_events=_allowed("notification"), state=state,
            ) is False
            assert ws_event_allowed(
                event, payload, app="mochi-pet",
                allowed_events=_allowed("notification:system"), state=state,
            ) is False
            assert ws_event_allowed(
                event, payload, app="mochi-pet",
                allowed_events=_allowed("notification:all"), state=state,
            ) is True
            assert ws_event_allowed(
                event, payload, app="mochi-pet",
                allowed_events=_allowed("log"), state=state,
            ) is False


# ---------------------------------------------------------------------------
# App-published events ride one fixed WS type
# ---------------------------------------------------------------------------

class TestAppEventFrames:
    """Every app event arrives as WS type ``app_event`` with the real name inside.

    The scope table is keyed by WS type, so the lookup could never match and each
    frame fell through to the global deny -- an app silently stopped receiving its
    OWN events the moment scoping landed.
    """

    def test_constant_matches_the_event_bus(self):
        """Pin the mirrored constant against the app layer's own definition."""
        from kiro_crew.apps.event_bus import APP_EVENT_WS_TYPE
        from kiro_crew.dashboard.ws_event_scope import _APP_EVENT_WS_TYPE

        assert _APP_EVENT_WS_TYPE == APP_EVENT_WS_TYPE

    def _frame(self, publisher: str, event: str) -> dict:
        # Shape produced by event_bus.build_broadcast_fn: the inner `type` is
        # renamed to `event` and rides beside `app` in the envelope.
        return {"app": publisher, "event": event, "data": {"n": 1}}

    def test_publisher_receives_its_own_event(self):
        state = _make_state()
        assert ws_event_allowed(
            "app_event", self._frame("mochi", "tick"),
            app="mochi", allowed_events=_allowed("slots:own"), state=state,
        ) is True

    def test_wildcard_app_still_receives_its_own_event(self):
        """`events: ["*"]` expands into CORE scopes, which cannot contain an
        app-chosen event name -- so ownership, not a second declaration lookup,
        has to decide, or every wildcard app loses its own events."""
        from kiro_crew.dashboard.ws_event_scope import build_allowed_event_set

        state = _make_state()
        assert ws_event_allowed(
            "app_event", self._frame("mochi", "tick"),
            app="mochi", allowed_events=build_allowed_event_set(["*"]), state=state,
        ) is True

    def test_another_apps_event_is_denied(self):
        """App events have no cross-app opt-in -- exposeToApps covers slots."""
        state = _make_state()
        assert ws_event_allowed(
            "app_event", self._frame("workflows", "tick"),
            app="mochi", allowed_events=_allowed("slots:all", "notification:all"),
            state=state,
        ) is False

    def test_unattributable_envelope_is_denied(self):
        state = _make_state()
        for frame in ({"event": "tick"}, {"app": "mochi"}, {}, {"app": "", "event": ""}):
            assert ws_event_allowed(
                "app_event", frame, app="mochi",
                allowed_events=_allowed("slots:own"), state=state,
            ) is False, f"{frame!r} must be denied"


class TestConstantValuedEventNamesAreClassified:
    """The completeness guard scans broadcast CALL SITES for literal names, so an
    event whose WS type comes from a CONSTANT is invisible to it -- which is how
    `app_event` shipped unclassified while the guard stayed green. Pin the known
    constant-valued types explicitly."""

    def test_app_event_is_classified(self):
        from kiro_crew.apps.event_bus import APP_EVENT_WS_TYPE
        from kiro_crew.dashboard import ws_event_scope as wes

        src = Path(wes.__file__).read_text(encoding="utf-8")
        assert APP_EVENT_WS_TYPE in src, (
            f"{APP_EVENT_WS_TYPE!r} is broadcast via a constant and must still be "
            "classified in ws_event_scope"
        )


# ---------------------------------------------------------------------------
# ``approval_resolved`` is slot-scoped, so the gate denies any frame it cannot
# attribute to a slot. The producers originally sent only {id, approved}, which
# meant an app received its approval REQUEST but never the RESOLUTION.
# ---------------------------------------------------------------------------

class TestApprovalResolvedCarriesSlot:
    @staticmethod
    def _state():
        from unittest.mock import AsyncMock

        from chat_test_helpers import _make_ready_kiro_prerequisite

        from kiro_crew.dashboard.state import DashboardState

        sessions = MagicMock(count=0)
        sessions.remove = AsyncMock()
        sessions.get_pid = MagicMock(return_value=None)
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=MagicMock(),
        )
        state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
        return state

    def test_slot_level_resolution_carries_owning_slot(self):
        """Drive the REAL resolve_approval and assert the broadcast payload."""
        import asyncio

        state = self._state()
        slot = state.get_or_create_slot("mochi-pet")
        slot._app = "mochi-pet"
        slot._origin = SlotOrigin.APP

        async def _run():
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            slot._approval_futures["a1"] = fut
            with patch.object(state, "broadcast_ws") as bws, patch(
                "kiro_crew.dashboard.state.sel"
            ):
                state.resolve_approval("a1", True)
            return [c for c in bws.call_args_list if c.args[0] == "approval_resolved"]

        calls = asyncio.run(_run())
        assert len(calls) == 1
        payload = calls[0].args[1]
        assert payload["id"] == "a1"
        assert payload["slot"] == "mochi-pet"

    def test_state_level_resolution_stays_unattributed(self):
        """A background (cron/subagent/gateway) approval owns no slot, so it
        must NOT claim one — the gate correctly withholds it from app tokens."""
        import asyncio

        state = self._state()

        async def _run():
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            state._approval_futures["b1"] = fut
            with patch.object(state, "broadcast_ws") as bws, patch(
                "kiro_crew.dashboard.state.sel"
            ):
                state.resolve_state_approval("b1", True)
            return [c for c in bws.call_args_list if c.args[0] == "approval_resolved"]

        calls = asyncio.run(_run())
        assert len(calls) == 1
        assert "slot" not in calls[0].args[1]

    def test_owning_app_receives_its_own_resolution(self):
        """End-to-end: the payload the real producer emits must pass the gate
        for the slot's owning app with NO extra declaration."""
        import asyncio

        state = self._state()
        slot = state.get_or_create_slot("mochi-pet")
        slot._app = "mochi-pet"
        slot._origin = SlotOrigin.APP

        async def _run():
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            slot._approval_futures["a1"] = fut
            with patch.object(state, "broadcast_ws") as bws, patch(
                "kiro_crew.dashboard.state.sel"
            ):
                state.resolve_approval("a1", True)
            return next(
                c.args[1] for c in bws.call_args_list if c.args[0] == "approval_resolved"
            )

        payload = asyncio.run(_run())
        assert ws_event_allowed(
            "approval_resolved", payload,
            app="mochi-pet", allowed_events=frozenset(), state=state,
        )
        # ...and a DIFFERENT app still must not see it.
        assert not ws_event_allowed(
            "approval_resolved", payload,
            app="other-app", allowed_events=frozenset(), state=state,
        )

    def test_every_approval_resolved_producer_sends_a_slot(self):
        """Source guard: a new producer that omits the slot key would silently
        withhold the resolution from the owning app. Pin every broadcast site."""
        import re

        roots = [
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / f
            for f in ("state.py", "chat_handlers.py", "chat_runner.py")
        ]
        found = 0
        for path in roots:
            src = path.read_text(encoding="utf-8")
            for m in re.finditer(r'"approval_resolved"', src):
                # Take the enclosing broadcast call: from the preceding
                # ``broadcast_ws`` to the matching close paren region.
                head = src.rfind("broadcast_ws", max(0, m.start() - 300), m.start())
                if head == -1:
                    continue
                found += 1
                block = src[head : m.start() + 400]
                assert '"slot"' in block or "payload" in block, (
                    f"{path.name}: approval_resolved broadcast near offset {m.start()} "
                    "does not carry a slot key"
                )
        assert found >= 4, f"expected >=4 approval_resolved producers, found {found}"


# ---------------------------------------------------------------------------
# ``/api/status`` after removal from the implicit-allow list: an app that
# DECLARES it still reaches it, so the tightening does not lock out the
# shipped consumers (``design_critique`` declares it in app.json).
# ---------------------------------------------------------------------------

class TestApiStatusRequiresDeclaration:
    def test_declared_app_still_reaches_api_status(self):
        """Removing the implicit grant must not break apps that declare it."""
        with patch(
            "kiro_crew.dashboard.token_auth._app_api_allowlist",
            return_value=("/api/status",),
        ):
            assert app_token_path_allowed("design-critique", "/api/status") is True

    def test_undeclared_app_is_denied_api_status(self):
        with patch(
            "kiro_crew.dashboard.token_auth._app_api_allowlist", return_value=()
        ):
            assert app_token_path_allowed("mochi-pet", "/api/status") is False

    def test_shipped_manifests_that_use_api_status_declare_it(self):
        """Source guard: no shipped builtin may RELY on the implicit allow.

        Derived, not a hardcoded name list: a name list goes stale the moment a
        builtin's permissions change (``crew_companion`` legitimately dropped
        ``/api/status`` from its manifest once it stopped calling it, which
        silently reds a hardcoded assertion instead of catching a real break).
        The invariant that actually matters is that any builtin whose shipped
        code reaches ``/api/status`` declares it in ``permissions.api`` -- with
        the implicit grant gone, an undeclared caller gets a 403.
        """
        import json

        builtins = (
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "apps" / "builtins"
        )
        frontends = (
            Path(__file__).resolve().parents[1] / "website" / "src" / "apps"
        )

        declared: list[str] = []
        for manifest in builtins.glob("*/app.json"):
            perms = json.loads(
                manifest.read_text(encoding="utf-8")
            ).get("permissions", {})
            api = perms.get("api") or []
            if isinstance(api, list) and "/api/status" in api:
                declared.append(manifest.parent.name)

        # Declaring it must remain a live, exercised path -- otherwise this
        # whole class could pass against a build where nothing can reach the
        # endpoint at all.
        assert declared, "no builtin declares /api/status; guard is vacuous"

        undeclared_callers: list[str] = []
        for app_dir in sorted(builtins.glob("*/app.json")):
            name = app_dir.parent.name
            if name in declared:
                continue
            roots = [app_dir.parent]
            fe = frontends / name.replace("_", "-")
            if fe.is_dir():
                roots.append(fe)
            for root in roots:
                for src in root.rglob("*"):
                    if not src.is_file():
                        continue
                    if src.suffix not in (".py", ".ts", ".tsx", ".js", ".jsx"):
                        continue
                    try:
                        text = src.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    if "/api/status" in text:
                        undeclared_callers.append(f"{name}:{src.name}")
                        break

        assert not undeclared_callers, (
            "these builtins reach /api/status without declaring it in "
            f"permissions.api, so they now 403: {undeclared_callers}"
        )


# ---------------------------------------------------------------------------
# A GLOBAL event must never be judged by a slot field that appears in its
# payload. ``notify()`` merges meta keys FLAT into the note, so a real caller
# (slack/gateway.py heartbeat: meta={"slot": slot.key}) puts a top-level
# ``slot`` on a ``notification`` frame.
# ---------------------------------------------------------------------------

class TestGlobalEventWithSlotDoesNotBypassScope:
    def _state_with_own_slot(self):
        slot = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="chat-1")
        return _make_state({"chat-1": slot})

    def test_notification_with_slot_still_needs_notification_scope(self):
        """The bypass: slots:all alone must NOT deliver a notification body."""
        state = self._state_with_own_slot()
        payload = {
            "kind": "heartbeat",
            "source": "system",
            "channel": "system.heartbeat",
            "title": "Still working",
            "body": "secret progress detail",
            "slot": "chat-1",  # flat-merged from notify(meta={"slot": ...})
        }
        assert not ws_event_allowed(
            "notification", payload,
            app="mochi-pet", allowed_events=frozenset({"slots:all"}), state=state,
        )

    def test_notification_with_slot_delivered_when_scope_declared(self):
        """With the real scope it is delivered — the fix only strips the
        smuggled inference, it does not break legitimate delivery."""
        state = self._state_with_own_slot()
        payload = {
            "kind": "heartbeat",
            "source": "system",
            "channel": "system.heartbeat",
            "title": "Still working",
            "body": "progress",
            "slot": "chat-1",
        }
        assert ws_event_allowed(
            "notification", payload,
            app="mochi-pet",
            allowed_events=frozenset({"notification:system"}),
            state=state,
        )

    def test_slack_heartbeat_really_passes_a_slot_in_meta(self):
        """Source guard: this whole class is only meaningful because a shipped
        caller flat-merges a slot onto a global notification frame."""
        gw = (
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "slack" / "gateway.py"
        ).read_text(encoding="utf-8")
        assert 'meta={"slot": slot.key}' in gw

    def test_meta_merge_is_flat_so_slot_lands_top_level(self):
        """Source guard on the mechanism: bus.push merges meta keys flat."""
        bus = (
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "notifications" / "bus.py"
        ).read_text(encoding="utf-8")
        assert "note[key] = value" in bus


# ---------------------------------------------------------------------------
# Coalesced subagent batches: above SubagentEventCoalescer's threshold ONE
# frame carries MANY subagents' rows, so there is no top-level slot. They must
# reach the app (they were falling through to the unknown-event deny, losing
# ALL subagent status/output) but be filtered per item.
# ---------------------------------------------------------------------------

class TestSubagentBatchFrames:
    def _state(self):
        mine = _make_slot(owner_app="mochi-pet", origin=SlotOrigin.APP, key="mine")
        theirs = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="theirs")
        return _make_state({"mine": mine, "theirs": theirs})

    @pytest.mark.parametrize(
        "event", ["subagent_batch_update", "subagent_batch_chunks"]
    )
    def test_batch_frame_is_not_denied_as_unknown(self, event):
        """The frame must pass the gate — denying it starved apps of their OWN
        subagent events once the coalescer engaged."""
        state = self._state()
        assert ws_event_allowed(
            event, {"updates": [], "chunks": []},
            app="mochi-pet", allowed_events=frozenset(), state=state,
        )

    def test_filter_keeps_only_own_items(self):
        from kiro_crew.dashboard.ws_event_scope import filter_subagent_batch_for_app

        state = self._state()
        items = [
            {"id": "a1", "slot": "mine", "text": "mine"},
            {"id": "a2", "slot": "theirs", "text": "secret"},
        ]
        kept = filter_subagent_batch_for_app(items, "mochi-pet", frozenset(), state)
        assert [i["slot"] for i in kept] == ["mine"]

    def test_filter_drops_items_with_unresolvable_slot(self):
        from kiro_crew.dashboard.ws_event_scope import filter_subagent_batch_for_app

        state = self._state()
        items = [{"id": "a1", "slot": "gone"}, {"id": "a2"}, "notadict"]
        assert filter_subagent_batch_for_app(items, "mochi-pet", frozenset(), state) == []

    def test_subagent_all_sees_every_item(self):
        from kiro_crew.dashboard.ws_event_scope import filter_subagent_batch_for_app

        state = self._state()
        items = [{"id": "a1", "slot": "mine"}, {"id": "a2", "slot": "theirs"}]
        kept = filter_subagent_batch_for_app(
            items, "mochi-pet", frozenset({"subagent:all"}), state
        )
        assert len(kept) == 2

    def test_batch_events_really_exist_with_per_item_slots(self):
        """Source guard: this class is only meaningful while the coalescer emits
        these two frames AND seeds every buffered row with its slot."""
        src = (
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "subagent_scale.py"
        ).read_text(encoding="utf-8")
        assert '"subagent_batch_update"' in src
        assert '"subagent_batch_chunks"' in src
        # every buffered update row is seeded with a slot
        assert '{"slot": payload.get("slot", "")}' in src
        # every chunk row carries its slot
        assert '"slot": buf["slot"]' in src

    def test_batch_item_key_map_matches_the_emitter(self):
        """The payload keys the filter reads must be the ones actually sent."""
        from kiro_crew.dashboard.ws_event_scope import _SUBAGENT_BATCH_ITEM_KEY

        src = (
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "subagent_scale.py"
        ).read_text(encoding="utf-8")
        assert '"subagent_batch_update", {"updates": updates}' in src
        assert '"subagent_batch_chunks", {"chunks": chunks}' in src
        assert _SUBAGENT_BATCH_ITEM_KEY == {
            "subagent_batch_update": "updates",
            "subagent_batch_chunks": "chunks",
        }


class TestSuppressedDenyCountIsReported:
    """The dedup comment used to claim suppressed denies were "counted" while
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
        monkeypatch.setattr(
            ws_event_scope, "get_app_manifest", lambda _n: self._manifest(declared)
        )
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
        monkeypatch.setattr(
            dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws
        )
        monkeypatch.setattr(source_providers, "schedule_check_refresh", MagicMock())

        with patch.object(dashboard_ws, "_audit_grant_quietly") as audit:
            _aio.run(dashboard_ws.api_ws(Request()))  # type: ignore[arg-type]
        return fake_ws, audit

    def test_initial_yolo_grant_is_audited(self, monkeypatch):
        """An app declaring ``yolo`` receives the live override state on the
        initial push -- a direct ``send_json``, so nothing else records it.
        """
        fake_ws, audit = self._drive_connect(
            monkeypatch, declared=["yolo"], yolo=True
        )

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
        fake_ws, audit = self._drive_connect(
            monkeypatch, declared=["slots:own"], yolo=True
        )

        assert fake_ws.sent
        assert "yolo" not in fake_ws.sent[0], (
            "withheld field must be ABSENT, not sent as a falsy default"
        )
        events = [c.args[1] for c in audit.call_args_list]
        assert "slots_yolo" not in events, (
            "auditing a grant that did not happen would make the trail lie"
        )

    def test_subscribe_logs_grant_is_audited(self, monkeypatch):
        """The ring replay writes to the socket directly, so the grant that
        admits it needs its own record -- the deny side already had one.
        """
        import asyncio as _aio

        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard import ws_event_scope
        from kiro_crew.dashboard.handlers import source_providers

        monkeypatch.setattr(ws_event_scope, "is_app_enabled", lambda _n: True)
        monkeypatch.setattr(
            ws_event_scope, "get_app_manifest", lambda _n: self._manifest(["log"])
        )
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
        monkeypatch.setattr(
            dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws
        )
        monkeypatch.setattr(source_providers, "schedule_check_refresh", MagicMock())

        with patch.object(dashboard_ws, "_audit_grant_quietly") as audit:
            _aio.run(dashboard_ws.api_ws(Request()))  # type: ignore[arg-type]

        state.subscribe_logs.assert_called_once_with(fake_ws)
        events = [c.args[1] for c in audit.call_args_list]
        assert "subscribe_logs" in events, (
            "a granted log subscription must leave an SEL record"
        )

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

        with patch.object(
            dashboard_ws, "_audit_allow", side_effect=RuntimeError("sink down")
        ):
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
            Path(__file__).resolve().parents[1]
            / "src" / "kiro_crew" / "dashboard" / "ws.py"
        ).read_text(encoding="utf-8")
        # The app-token narrowing block inside _push_status: it strips the
        # owner-only fields, and the grant must be recorded in the same branch.
        marker = 'for _owner_only in ("branch", "commit"):'
        assert marker in src, "the app-token narrowing block moved; re-anchor this guard"
        region = " ".join(src[src.index(marker):].split())
        # Look only as far as the send that ends the block.
        region = region[: region.index('ws.send_json({"type": "dashboard"')]
        assert re.search(r'_audit\w*\([^)]*"dashboard"\)', region), (
            "the periodic dashboard frame's grant to an app socket must be "
            "SEL-audited in the same branch that narrows the payload"
        )
