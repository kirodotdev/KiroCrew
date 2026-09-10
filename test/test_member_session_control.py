"""Member sessions get session control automatically, bounded by ownership.

The crew-member operating model — the DM thread dispatches real work into
worker sessions it creates and patrols — holds with ZERO configuration: a
member caller passes the session-control gates without the global
``agent.session_control`` opt-in, and is bounded to the workers it created
itself instead. These tests pin the three halves of that contract:

* the gate bypass (member caller passes with the switch off; an ordinary
  caller still needs it),
* the ownership boundary (a member cannot touch a slot it did not create,
  even when the global switch is ON),
* the persistence of the boundary's input (``created_by`` written at birth
  and restored on rehydrate — without it every worker a member dispatched
  would come back unowned after a restart and the fail-closed check would
  strand them).

The session_* kirocrew-dashboard tools ride this same server-side
authorization: mounting them into a member session (per-session, over the
wire) grants nothing an ordinary caller could not already reach, because
every verb terminates in these gates.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.dashboard import session_control as sc
from kiro_crew.members import DM_SLOT_KEY_PREFIX


class TestMemberCallerPredicate:
    def test_member_slot_key_is_a_member_caller(self):
        assert sc._member_caller(DM_SLOT_KEY_PREFIX + "radar")

    def test_ordinary_and_unattended_slots_are_not(self):
        assert not sc._member_caller("chat-1-abc")
        assert not sc._member_caller("cron-xyz")
        assert not sc._member_caller("")


def _slot(key: str, *, created_by: str = "", workspace: str = "default") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        workspace=workspace,
        memory_mode="persistent",
        _app="",
        linked_session_key="",
        _created_by=created_by,
        mode="",
        running=False,
        messages=[],
    )


class _State:
    def __init__(self, slots: dict[str, SimpleNamespace]):
        self._slots = slots

    def get_slot(self, key: str):
        return self._slots.get(key)


class TestAuthorizeTargetMemberPath:
    """Drive authorize_target through the real gate order with a fake state."""

    def _authorize(self, state, caller_key, target_key):
        # caller_slot_key maps a session key to an open slot; the member path
        # is exercised below the identity resolution, so pin the mapping and
        # the workspace reads to keep the fixture at the authorization layer.
        # member_dispatch_enabled is pinned True here — these tests assert the
        # DEFAULT (bypass on) behaviour; the ceiling-off case has its own class.
        with (
            patch.object(sc, "caller_slot_key", return_value=caller_key),
            patch.object(sc, "session_control_enabled", return_value=False),
            patch.object(sc, "member_dispatch_enabled", return_value=True),
            patch.object(sc, "_resolve_slot", return_value=state._slots.get(target_key)),
        ):
            return sc.authorize_target(
                state,
                caller_session_key="dashboard:whatever",
                target=target_key,
                operation="send",
            )

    def test_member_controls_its_own_worker_with_switch_off(self):
        member = DM_SLOT_KEY_PREFIX + "radar"
        worker = _slot("chat-1-w1", created_by=member)
        state = _State({member: _slot(member), "chat-1-w1": worker})
        try:
            self._authorize(state, member, "chat-1-w1")
        except sc.SessionControlError as exc:
            # Workspace plumbing differs per deployment; the pin is that the
            # member path got PAST the config gate and the ownership check.
            assert exc.code not in ("session_control_disabled", "not_creator"), exc.code

    def test_member_cannot_touch_a_slot_it_did_not_create(self):
        member = DM_SLOT_KEY_PREFIX + "radar"
        foreign = _slot("chat-1-user", created_by="")
        state = _State({member: _slot(member), "chat-1-user": foreign})
        with pytest.raises(sc.SessionControlError) as exc_info:
            self._authorize(state, member, "chat-1-user")
        assert exc_info.value.code == "not_creator"

    def test_ownership_binds_even_when_globally_enabled(self):
        member = DM_SLOT_KEY_PREFIX + "radar"
        foreign = _slot("chat-1-user", created_by="")
        state = _State({member: _slot(member), "chat-1-user": foreign})
        with (
            patch.object(sc, "caller_slot_key", return_value=member),
            patch.object(sc, "session_control_enabled", return_value=True),
            patch.object(sc, "_resolve_slot", return_value=foreign),
        ):
            with pytest.raises(sc.SessionControlError) as exc_info:
                sc.authorize_target(
                    _State(state._slots),
                    caller_session_key="dashboard:whatever",
                    target="chat-1-user",
                    operation="send",
                )
        assert exc_info.value.code == "not_creator"

    def test_ordinary_caller_still_needs_the_switch(self):
        state = _State({"chat-1-a": _slot("chat-1-a"), "chat-1-b": _slot("chat-1-b")})
        with pytest.raises(sc.SessionControlError) as exc_info:
            self._authorize(state, "chat-1-a", "chat-1-b")
        assert exc_info.value.code == "session_control_disabled"


class TestCreatedByRecentSessionRestore:
    """created_by must survive the bulk recent-session restore path too.

    _rehydrate_slot_from_history restores it, but the startup path is
    _apply_recent_session — a member-created worker restored there without
    created_by comes back unowned, and authorize_target then refuses the
    legitimate creator with not_creator.
    """

    def test_recent_session_restore_rehydrates_created_by(self, tmp_path, monkeypatch):
        import json as _json
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard.chat import restore_recent_sessions
        from kiro_crew.dashboard.state import DashboardState
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        meta_line = {
            "_type": "metadata",
            "created_at": "2026-03-23T10:00:00",
            "last_consolidated": 0,
            "title": "Worker",
            "agent": "kirocrew",
            "created_by": "member-autofix",
        }
        rows = [
            _json.dumps(meta_line),
            _json.dumps({"role": "user", "content": "task", "ts": "2026-03-23T10:00:00"}),
        ]
        path = tmp_path / "dashboard_chat-1-worker.jsonl"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        path.touch()

        sessions = MagicMock(count=0)
        sessions.get_pid = MagicMock(return_value=None)
        sessions.remove = AsyncMock()
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        assert restore_recent_sessions(state, window_minutes=60) == 1
        assert state._slots["chat-1-worker"]._created_by == "member-autofix"


class TestCreatedByProjection:
    """``created_by`` rides the slot payload the WS ``slots`` frames carry.

    The Crew Members drawer lists the sessions a member is driving by filtering
    the live slots on this field, so a payload that dropped it would render the
    empty state for a member with ten workers in flight. Because a member caller
    is ownership-fenced to the slots it created (``authorize_target``), the
    created set IS the driven set -- no separate provenance field is needed.
    """

    def test_to_dict_carries_the_creator_slot_key(self):
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("chat-1-worker")
        slot._created_by = DM_SLOT_KEY_PREFIX + "autofix"
        assert slot.to_dict()["created_by"] == "member-autofix"

    def test_unattributed_slot_reports_empty_string_not_absent(self):
        from kiro_crew.dashboard.state import _ChatSlot

        # "" rather than a missing key: the frontend must be able to tell "a
        # person's own tab" from "an older gateway that never sent the field".
        assert _ChatSlot("chat-1-own").to_dict()["created_by"] == ""


class TestMemberDispatchCeiling:
    """The operator ceiling `agent.member_dispatch` on the member switch bypass.

    Default true reproduces today's behaviour (member bypasses the switch); set
    false, a member caller stops bypassing and falls back under
    `session_control`. The bypass condition is `_member_bypass` = member caller
    AND dispatch enabled, and the ceiling read fails CLOSED (withdraws the
    bypass on an unreadable config) the same direction `session_control` does.
    """

    def test_bypass_requires_member_and_ceiling_on(self):
        member = DM_SLOT_KEY_PREFIX + "radar"
        with patch.object(sc, "member_dispatch_enabled", return_value=True):
            assert sc._member_bypass(member) is True
            assert sc._member_bypass("chat-1-abc") is False  # not a member
        with patch.object(sc, "member_dispatch_enabled", return_value=False):
            assert sc._member_bypass(member) is False  # ceiling off
            assert sc._member_bypass("chat-1-abc") is False

    def test_member_dispatch_enabled_reads_the_config_field(self):
        cfg = SimpleNamespace(
            agent=SimpleNamespace(member_dispatch=True), degraded_sections=frozenset()
        )
        with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
            assert sc.member_dispatch_enabled() is True
        cfg_off = SimpleNamespace(
            agent=SimpleNamespace(member_dispatch=False), degraded_sections=frozenset()
        )
        with patch.object(sc.KiroCrewConfig, "load", return_value=cfg_off):
            assert sc.member_dispatch_enabled() is False

    def test_member_dispatch_enabled_fails_closed_on_read_error(self):
        with patch.object(sc.KiroCrewConfig, "load", side_effect=RuntimeError("boom")):
            # An unreadable config withdraws the bypass rather than granting it.
            assert sc.member_dispatch_enabled() is False

    def test_member_dispatch_enabled_fails_closed_on_degraded_section(self):
        # load() does not raise on a discarded `agent` section: it falls back to
        # the permissive default (member_dispatch=True) and records the loss in
        # degraded_sections. A stored `member_dispatch: false` would otherwise
        # silently revert to the bypass -- so a degraded `agent` or whole-config
        # (`*`) marker must withdraw it.
        for degraded in ("agent", sc.DEGRADED_WHOLE_CONFIG):
            cfg = SimpleNamespace(
                agent=SimpleNamespace(member_dispatch=True),
                degraded_sections=frozenset({degraded}),
            )
            with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
                assert sc.member_dispatch_enabled() is False, degraded

    def test_member_dispatch_enabled_trusts_value_when_not_degraded(self):
        # An unrelated degraded section does not withdraw the bypass -- only the
        # agent section or the whole config does.
        cfg = SimpleNamespace(
            agent=SimpleNamespace(member_dispatch=True),
            degraded_sections=frozenset({"dashboard"}),
        )
        with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
            assert sc.member_dispatch_enabled() is True

    def test_member_falls_back_under_switch_when_ceiling_off(self):
        # Switch off AND ceiling off: the member's exemption does not apply, so it hits
        # the same session_control_disabled refusal an ordinary caller gets.
        member = DM_SLOT_KEY_PREFIX + "radar"
        worker = _slot("chat-1-w1", created_by=member)
        state = _State({member: _slot(member), "chat-1-w1": worker})
        with (
            patch.object(sc, "caller_slot_key", return_value=member),
            patch.object(sc, "session_control_enabled", return_value=False),
            patch.object(sc, "member_dispatch_enabled", return_value=False),
            patch.object(sc, "_resolve_slot", return_value=worker),
        ):
            with pytest.raises(sc.SessionControlError) as exc_info:
                sc.authorize_target(
                    state,
                    caller_session_key="dashboard:whatever",
                    target="chat-1-w1",
                    operation="send",
                )
        assert exc_info.value.code == "session_control_disabled"

    def test_member_still_bypasses_when_ceiling_on_and_switch_off(self):
        # Default behaviour preserved: ceiling on, switch off -> member passes
        # the config gate (may still be bounded by ownership, but not by the
        # switch). Pin an owned worker so ownership does not intervene.
        member = DM_SLOT_KEY_PREFIX + "radar"
        worker = _slot("chat-1-w1", created_by=member)
        state = _State({member: _slot(member), "chat-1-w1": worker})
        with (
            patch.object(sc, "caller_slot_key", return_value=member),
            patch.object(sc, "session_control_enabled", return_value=False),
            patch.object(sc, "member_dispatch_enabled", return_value=True),
            patch.object(sc, "_resolve_slot", return_value=worker),
        ):
            try:
                sc.authorize_target(
                    state,
                    caller_session_key="dashboard:whatever",
                    target="chat-1-w1",
                    operation="send",
                )
            except sc.SessionControlError as exc:
                assert exc.code not in ("session_control_disabled", "not_creator"), exc.code

    def test_config_default_is_true(self):
        # The knob's default IS today's behaviour, so installing the change
        # alters nothing until an operator opts in.
        from kiro_crew.config.sections import AgentConfig

        assert AgentConfig().member_dispatch is True
