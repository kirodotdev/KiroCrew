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

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import SlotOrigin
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

    def test_recent_session_restore_never_promotes_metadata_to_lineage(self, tmp_path, monkeypatch):
        # Transcript metadata is a file an agent's file tools can edit. The
        # attribution is restored for the ownership boundary, but a
        # `created_by_sid` found there is ignored and the slot carries no lineage
        # witness, so the child's first turn after a restart writes no
        # `session/opened.parent` -- a metadata edit cannot forge gateway lineage.
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
            "created_by_sid": "acp-sess-creator-at-mint",
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
        restored = state._slots["chat-1-worker"]
        assert restored._created_by == "member-autofix"
        assert restored._created_by_sid == ""
        assert restored._lineage_minted is False

    def test_save_and_rehydrate_keep_attribution_but_never_lineage(self, tmp_path, monkeypatch):
        # Round trip through the real serializer: `created_by` is written and
        # restored (ownership boundary); the frozen sid is never written, and the
        # rehydrated slot has no lineage witness, so nothing read back from the
        # transcript can become the crew-log `session/opened.parent` record.
        import json as _json
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )
        from kiro_crew.dashboard.state import DashboardState
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
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
        slot = state.get_or_create_slot("chat-1-worker")
        slot._created_by = "member-autofix"
        slot._created_by_sid = "acp-sess-creator-at-mint"
        slot._lineage_minted = True
        slot.append("user", "task")
        slot.drain()

        _save_slot_to_history(state, slot, force=True)
        written = [
            _json.loads(line)
            for line in (tmp_path / "dashboard_chat-1-worker.jsonl").read_text("utf-8").splitlines()
            if line.strip()
        ]
        meta = next(row for row in written if row.get("_type") == "metadata")
        assert meta.get("created_by") == "member-autofix"
        assert "created_by_sid" not in meta
        assert "_lineage_minted" not in meta

        del state._slots[slot.key]
        restored = _rehydrate_slot_from_history(state, slot.key)

        assert restored is not None
        assert restored._created_by == "member-autofix"
        assert restored._created_by_sid == ""
        assert restored._lineage_minted is False


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


_MEMBER = DM_SLOT_KEY_PREFIX + "radar"


@pytest.fixture
def _fresh_create_budget():
    """The per-caller create-rate window is process-wide module state."""
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


def _member_tab(state):
    """A crew member's own DM slot, as the member-thread endpoint mints it.

    ``mode="member"`` is the one path the slot registry admits a ``member-``
    key through; the agent is left to inherit so a name that does not resolve
    in the test config cannot pre-empt the gates under test.
    """
    return state.get_or_create_slot(_MEMBER, mode="member")


class TestMemberDispatchEndToEnd:
    """The member contract driven through the REAL create/authorize paths.

    ``TestAuthorizeTargetMemberPath`` above pins the gate order with a fake
    state; this pins the whole ``create_session`` / ``authorize_target``
    transaction against a real ``DashboardState`` — the child is actually
    minted, attributed, and then reached (or refused) by the same functions
    production runs. It is the member twin of ``test_cron_session_control``'s
    end-to-end classes.
    """

    def test_a_member_creates_an_attributed_user_origin_child(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        state = _make_state(tmp_path)
        caller = _member_tab(state)
        # The child inherits the caller's workspace; pin the binding's workspace
        # name to it so the agent-workspace check passes without a config fixture.
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)
        # Switch OFF on purpose: the member bypass is what admits the create.
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)

        result = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))

        child = state.get_slot(result["target"])
        assert child is not None
        # `created_by` is the fence's only input, so the create must write it.
        assert child._created_by == _MEMBER
        # USER, unlike a cron child: a member's worker is meant to be visible in
        # the sidebar and taken over by the person, so it must reach `slots:user`.
        assert child._origin == SlotOrigin.USER

    def test_the_global_switch_does_not_gate_a_member_create(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        state = _make_state(tmp_path)
        caller = _member_tab(state)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)

        result = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        assert state.get_slot(result["target"]) is not None

    def test_member_create_is_refused_when_the_ceiling_is_off(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # Switch off AND ceiling off: the member falls back under the switch and
        # is refused exactly like an ordinary caller — no bypass, no session.
        state = _make_state(tmp_path)
        caller = _member_tab(state)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: False)

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        assert exc.value.code == "session_control_disabled"

    def test_a_member_reaches_its_own_worker(self, tmp_path, monkeypatch, _fresh_create_budget):
        state = _make_state(tmp_path)
        caller = _member_tab(state)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)

        result = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        worker = state.get_slot(result["target"])
        for op in ("send", "read", "stop", "close"):
            resolved = sc.authorize_target(
                state,
                caller_session_key=slot_history_key(caller),
                target=worker.key,
                operation=op,
            )
            assert resolved is worker, op

    def test_a_member_cannot_reach_a_session_it_did_not_create(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The user's own conversation — protected by the ownership fence, not by
        # a blanket refusal of the member.
        state = _make_state(tmp_path)
        caller = _member_tab(state)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        state.get_or_create_slot("chat-7", workspace=caller.workspace)

        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(caller),
                target="chat-7",
                operation="send",
            )
        assert exc.value.code == "not_creator"
        assert "crew member" in exc.value.message

    def test_member_create_into_a_foreign_store_is_refused(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # A workspace is not a memory silo: a private V2 member could otherwise
        # mint a worker whose resolved agent is bound to `default`/global or a
        # peer's store, laundering work out of its own private memory. The
        # `require_memory_delegation` guard (the same one the private spawn path
        # uses) refuses that, and `create_session` maps it to the
        # `memory_delegation_denied` 403 -- an AUTHORIZATION refusal, because the
        # store is legal and the caller simply may not delegate into it. The child
        # agent-workspace check must pass first, so pin `_workspace_name_for_dir`
        # as the other end-to-end tests do; the delegation guard itself is stubbed
        # to reject, isolating this seam from the member-binding plumbing exercised
        # in test_member_memory_api.
        import kiro_crew.context as context
        from kiro_crew.memory_stores import UnknownMemoryStore

        state = _make_state(tmp_path)
        caller = _member_tab(state)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)

        def _reject(_log, _parent, _target_store):
            raise UnknownMemoryStore(
                "A Crew Member's tasks must retain that member's private memory."
            )

        monkeypatch.setattr(context, "require_memory_delegation", _reject)

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        assert exc.value.code == "memory_delegation_denied"
        assert exc.value.status == 403
        # The guard reads binding FILES, so its own text is not safe to echo: the
        # refusal carries a fixed message and nothing the guard said.
        assert "private memory" not in exc.value.message
        # No session is left behind on refusal.
        assert state.creator_slot_count(_MEMBER) == 0

    def test_member_create_maps_a_corrupt_binding_valueerror(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # require_memory_delegation reads the caller's binding from disk; a
        # corrupt/unreadable binding file surfaces as a bare ValueError, not
        # UnknownMemoryStore. It must map to the same memory_delegation_denied
        # refusal rather than escaping create_session as an unhandled 500.
        import kiro_crew.context as context

        state = _make_state(tmp_path)
        caller = _member_tab(state)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)

        def _corrupt(_log, _parent, _target_store):
            raise ValueError("The protected member session binding is missing or unreadable")

        monkeypatch.setattr(context, "require_memory_delegation", _corrupt)

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        assert exc.value.code == "memory_delegation_denied"
        assert exc.value.status == 403
        assert "binding" not in exc.value.message
        assert state.creator_slot_count(_MEMBER) == 0


class TestMemberChildPrivateBinding:
    """A member-created child on a V2 agent is bound to its private store BEFORE
    ``create_session`` returns.

    ``slot.memory_store`` alone is transcript metadata a save reads back; it is
    NOT the immutable per-session binding the turn path checks. Without a real
    binding record, a member-minted worker's first ``session_send`` runs
    ``_bind_private_slot_memory`` -> ``read_private_session_store`` -> ``None`` ->
    ``memory_unavailable``, so the worker a member just dispatched can never take
    a turn. These tests pin that ``create_session`` writes the binding the SAME
    read path (``read_private_session_store`` on the child's EFFECTIVE session
    key) will later find, and that a binding failure retracts the child rather
    than returning a doomed slot.

    Only agent resolution is stubbed. The caller's protected record, delegation
    guard and child's binding write use the real private-memory path.
    """

    def _pin_v2_bindings(self, monkeypatch, tmp_path, state, caller, store, *, private=True):
        """Declare *store* as a V2 store on disk and make create_session resolve to it."""
        from pathlib import Path

        from member_memory_helpers import forget_declared_stores, write_member_home

        from kiro_crew.config.sections import ResolvedBindings
        from kiro_crew.member_memory_auth import bind_private_session_store

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        # write_member_home lays down the manifest AND the config.json that names
        # the store, so require_memory_store (which the binding write calls)
        # recognizes it. `store` is `member-<member>`; derive the member back.
        write_member_home(tmp_path, store.removeprefix("member-"))
        # require_memory_store also validates the store's vector DB exists, so
        # initialize it the same way member_memory_helpers.env does.
        from kiro_crew import memory_stores as _ms
        from kiro_crew.vector_memory import VectorMemoryStore

        _tier = VectorMemoryStore(db_path=tmp_path / "memory_stores" / store / _ms.MEMORY_DB_FILE)
        _tier.init()
        _tier.close()
        forget_declared_stores(monkeypatch)

        bindings = ResolvedBindings(
            workspace_dir=Path("workspace"),
            memory_store_name=store,
            effective_memory_config={},
            kiro_agent="kiro",
        )
        monkeypatch.setattr(sc, "resolve_agent_bindings", lambda *a, **k: bindings)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: not private)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        if private:
            bind_private_session_store(slot_history_key(caller), store)
            state.conversation_log.update_metadata(
                slot_history_key(caller), {"memory_store": store}
            )
        return bindings

    @pytest.mark.parametrize("via_http", [False, True])
    def test_child_has_a_binding_readable_by_the_turn_path(
        self, tmp_path, monkeypatch, _fresh_create_budget, via_http
    ):
        import json
        import os

        from member_memory_helpers import make_request

        from kiro_crew import member_memory_auth, platform_compat
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.dashboard.handlers.session_control import api_session_control_create
        from kiro_crew.member_memory_auth import read_private_session_store

        state = _make_state(tmp_path)
        caller = _member_tab(state)
        self._pin_v2_bindings(monkeypatch, tmp_path, state, caller, "member-radar")
        caller_key = slot_history_key(caller)

        if via_http:
            monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: f"test-{pid}")
            member_memory_auth.publish_member_session_pid(
                os.getpid(), caller_key, memory_store="member-radar"
            )
            proof = member_memory_auth.issue_member_session_proof(caller_key, os.getpid())
            assert proof

            async def _create():
                response = await api_session_control_create(
                    make_request(
                        state,
                        "/api/session-control/create",
                        body={},
                        internal=True,
                        session=caller_key,
                        proof=proof,
                    )
                )
                assert response.status == 200, response.text
                return json.loads(response.text)

            result = asyncio.run(_create())
        else:
            result = asyncio.run(sc.create_session(state, caller_session_key=caller_key))
        child = state.get_slot(result["target"])
        assert child is not None

        # The turn path reads this exact protected key, not editable slot metadata.
        child_key = effective_session_key(child)
        assert read_private_session_store(child_key) == "member-radar"

    @pytest.mark.parametrize("caller_form", ["canonical", "slot", "stem"])
    def test_the_child_binding_ignores_how_the_caller_spelled_itself(
        self, tmp_path, monkeypatch, _fresh_create_budget, caller_form
    ):
        """A member's authority is its protected record, not the name it types.

        ``caller_session_key`` arrives as any of three spellings of the same
        session -- canonical history key, slot key, transcript stem -- and
        ``read_private_session_store`` recognizes only the canonical one. Reading
        the caller's protected record under the raw argument makes that caller's own
        authority depend on which spelling it chose: the slot and stem forms read
        back as unbound, the pre-birth binding does not happen, and the member's
        worker cannot take its first turn.
        """
        from kiro_crew.dashboard import chat_persistence
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.history import transcript_stem
        from kiro_crew.member_memory_auth import read_private_session_store

        state = _make_state(tmp_path)
        caller = _member_tab(state)
        self._pin_v2_bindings(monkeypatch, tmp_path, state, caller, "member-radar")
        canonical = slot_history_key(caller)
        identity = {
            "canonical": canonical,
            "slot": caller.key,
            "stem": transcript_stem(canonical),
        }[caller_form]

        bound_before_birth: list[bool] = []
        real_pin = chat_persistence._pin_private_agent_assignment

        def _watch(session_key, *args, **kwargs):
            bound_before_birth.append(read_private_session_store(session_key) is not None)
            return real_pin(session_key, *args, **kwargs)

        monkeypatch.setattr(sc, "_pin_private_agent_assignment", _watch)

        result = asyncio.run(sc.create_session(state, caller_session_key=identity))
        child = state.get_slot(result["target"])
        assert read_private_session_store(effective_session_key(child)) == "member-radar"
        # Written BEFORE the birth record, for every spelling. The birth pin would
        # otherwise be the only writer, and it derives its store from the selected
        # agent's config rather than from the caller's own record.
        assert bound_before_birth == [True]

    def test_global_caller_binds_a_child_only_to_the_member_it_selected(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # What needs guarding here is not whether the child has a binding but WHICH
        # store that binding names. The delegation gate authorizes the selected
        # member's own store, and that is the only store the child may land on.
        #
        # "No binding at all" is not the safe reading: this test also pins
        # `child.memory_store == "member-radar"`, and for a slot on a V2 store
        # `chat_runner._bind_private_slot_memory` raises `UnknownMemoryStore` ->
        # `memory_unavailable` when `read_private_session_store` returns None. A V2
        # store in the metadata with no binding on disk is a session that cannot
        # take a turn.
        #
        # A child aimed at a store the gate does NOT authorize is covered by
        # `test_a_v1_child_writes_no_private_binding`.
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.member_memory_auth import read_private_session_store

        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-owner")
        self._pin_v2_bindings(monkeypatch, tmp_path, state, caller, "member-radar", private=False)
        assert read_private_session_store(slot_history_key(caller)) is None

        result = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        child = state.get_slot(result["target"])
        assert child is not None
        assert child.memory_store == "member-radar"
        assert read_private_session_store(effective_session_key(child)) == "member-radar"
        # The creation did not write anything onto the caller's own identity.
        assert read_private_session_store(slot_history_key(caller)) is None

    def test_a_corrupt_caller_record_refuses_without_naming_it(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # The caller's own protected record is read before the slot is minted, to
        # decide whether the child may inherit private authority. A record that
        # cannot be read authorizes nothing, so the creation is refused -- and the
        # refusal must not echo the reader's own text, which names the file.
        from kiro_crew import member_memory_auth

        state = _make_state(tmp_path)
        caller = _member_tab(state)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)

        def _corrupt(_key):
            raise ValueError("/private/bindings/9f2c/memory.json is unreadable")

        monkeypatch.setattr(member_memory_auth, "read_private_session_store", _corrupt)

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        assert exc.value.code == "memory_delegation_denied"
        assert exc.value.status == 403
        assert "/private/bindings" not in exc.value.message
        assert "/private/bindings" not in str(exc.value)
        assert state.creator_slot_count(_MEMBER) == 0

    @pytest.mark.parametrize("target_store", ["default", "member-peer"])
    def test_private_caller_cannot_delegate_into_another_store(
        self, tmp_path, monkeypatch, _fresh_create_budget, target_store
    ):
        state = _make_state(tmp_path)
        caller = _member_tab(state)
        bindings = self._pin_v2_bindings(monkeypatch, tmp_path, state, caller, "member-radar")
        bindings.memory_store_name = target_store

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        # `memory_delegation_denied` 403, the one refusal `create_session` has for
        # this: the caller may not delegate into that store. Not a 4xx validation
        # code -- the store is a legal name and nothing about the request is
        # malformed, so a "fix your input" answer would be a lie to the caller and
        # would read as a client bug in the audit trail.
        assert exc.value.code == "memory_delegation_denied"
        assert exc.value.status == 403
        assert state.creator_slot_count(_MEMBER) == 0

    def test_a_v1_child_writes_no_private_binding(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.member_memory_auth import read_private_session_store

        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-owner")
        bindings = self._pin_v2_bindings(
            monkeypatch, tmp_path, state, caller, "member-radar", private=False
        )
        bindings.memory_store_name = "default"

        result = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        child = state.get_slot(result["target"])
        assert read_private_session_store(effective_session_key(child)) is None

    @pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
    @pytest.mark.parametrize("fail_at", ["version", "binding"])
    def test_a_binding_failure_retracts_the_child(
        self, tmp_path, monkeypatch, _fresh_create_budget, failure, fail_at
    ):
        import kiro_crew.member_memory_auth as member_memory_auth

        state = _make_state(tmp_path)
        caller = _member_tab(state)
        self._pin_v2_bindings(monkeypatch, tmp_path, state, caller, "member-radar")
        published = []
        monkeypatch.setattr(state, "_slots_broadcast_lock", None)
        monkeypatch.setattr(
            state, "_do_slots_broadcast", lambda: published.append(set(state._slots))
        )
        monkeypatch.setattr(
            state.conversation_log, "update_metadata", lambda *a: pytest.fail("persisted")
        )

        def _boom(*_args):
            raise failure("binding preparation failed")

        if fail_at == "binding":
            monkeypatch.setattr(member_memory_auth, "bind_private_session_store", _boom)
        else:
            monkeypatch.setattr(sc, "memory_store_version", _boom)

        expected = sc.SessionControlError if failure is RuntimeError else failure
        with pytest.raises(expected) as exc:
            asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        if failure is RuntimeError:
            assert exc.value.code == "agent_store_mismatch"
        assert state.creator_slot_count(_MEMBER) == 0
        assert published
        assert all(keys == {caller.key} for keys in published)
