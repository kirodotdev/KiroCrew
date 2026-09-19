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
        member = DM_SLOT_KEY_PREFIX + "radar"
        state = _State({member: _slot(member)})
        assert sc._member_caller(state, member)

    def test_ordinary_and_unattended_slots_are_not(self):
        state = _State(
            {
                "chat-1-abc": _slot("chat-1-abc"),
                "cron-xyz": _slot("cron-xyz"),
            }
        )
        assert not sc._member_caller(state, "chat-1-abc")
        assert not sc._member_caller(state, "cron-xyz")
        assert not sc._member_caller(state, "")

    def test_chat_slot_bound_to_a_member_v2_store_is_a_member_caller(self, monkeypatch):
        # Case (b): the conductor's ORDINARY chat slot, whose bound memory store
        # is a crew member's private V2 store. The identity here is the STORE,
        # not the `member-` key prefix — so a plain `chat-` key still resolves
        # as a member caller.
        chat = _slot("chat-10-1789623359")
        chat.memory_store = "member-kirocrew-conductor-deadbeef"
        state = _State({chat.key: chat})
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: store.startswith("member-"))
        assert sc._member_caller(state, chat.key)

    def test_chat_slot_bound_to_a_non_member_store_is_not(self, monkeypatch):
        # A chat slot whose store is NOT a crew member's V2 store stays an
        # ordinary caller — the admission never widens past member stores.
        chat = _slot("chat-10-1789623359")
        chat.memory_store = "default"
        state = _State({chat.key: chat})
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: False)
        assert not sc._member_caller(state, chat.key)


class TestStoreIsMemberOwned:
    """The ONE config-record predicate the gate and the switch bypass share.

    A store is a crew member's store iff its config record carries a non-empty
    ``owner_member`` AND ``memory_version == 2`` AND that owner is still an active
    agent bound to exactly this store — read from the loaded config, never the
    on-disk manifest (that would be blocking IO at the sync fence). Every other
    answer is ``False``, and ``False`` fails CLOSED for what the predicate decides
    (admission and the switch bypass): an unreadable or degraded
    ``memory_stores`` section, a missing record, a retired or re-bound owner.
    """

    def _cfg(self, stores, *, degraded=frozenset(), agents=None):
        # By default, synthesize an active agent bound to each owned V2 store, so
        # the owner is the store's exclusive active binding. Pass `agents` to
        # model a retired/re-bound owner (a deleted crew leaves no agent).
        if agents is None:
            agents = {
                getattr(rec, "owner_member", ""): SimpleNamespace(memory_store=name)
                for name, rec in stores.items()
                if getattr(rec, "owner_member", "") and getattr(rec, "memory_version", 1) == 2
            }
        return SimpleNamespace(memory_stores=stores, degraded_sections=degraded, agents=agents)

    def test_member_owned_v2_store_is_true(self):
        stores = {"member-radar-abc": SimpleNamespace(owner_member="radar", memory_version=2)}
        with patch.object(sc.KiroCrewConfig, "load", return_value=self._cfg(stores)):
            assert sc._store_is_member_owned("member-radar-abc") is True

    def test_default_and_empty_are_false_without_reading_config(self):
        # Short-circuited before load(): the global store is never a member store.
        with patch.object(sc.KiroCrewConfig, "load", side_effect=AssertionError("loaded")):
            assert sc._store_is_member_owned("") is False
            assert sc._store_is_member_owned("default") is False

    def test_v1_or_unowned_store_is_false(self):
        stores = {
            "legacy": SimpleNamespace(owner_member="", memory_version=1),
            "ownerless-v2": SimpleNamespace(owner_member="", memory_version=2),
            "owned-v1": SimpleNamespace(owner_member="radar", memory_version=1),
        }
        with patch.object(sc.KiroCrewConfig, "load", return_value=self._cfg(stores)):
            assert sc._store_is_member_owned("legacy") is False
            assert sc._store_is_member_owned("ownerless-v2") is False
            assert sc._store_is_member_owned("owned-v1") is False

    def test_unknown_store_is_false(self):
        with patch.object(sc.KiroCrewConfig, "load", return_value=self._cfg({})):
            assert sc._store_is_member_owned("member-ghost-abc") is False

    def test_fails_closed_on_config_read_error(self):
        with patch.object(sc.KiroCrewConfig, "load", side_effect=RuntimeError("boom")):
            assert sc._store_is_member_owned("member-radar-abc") is False

    def test_fails_closed_on_degraded_memory_stores_section(self):
        stores = {"member-radar-abc": SimpleNamespace(owner_member="radar", memory_version=2)}
        for degraded in ("memory_stores", sc.DEGRADED_WHOLE_CONFIG):
            cfg = self._cfg(stores, degraded=frozenset({degraded}))
            with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
                assert sc._store_is_member_owned("member-radar-abc") is False, degraded

    def test_retired_owner_is_not_a_member_store(self):
        # A crew is DELETED while a chat slot bound to its store is still live.
        # The store record is RETAINED with `owner_member` still set, but no agent
        # is bound to it anymore (`cfg.agents` has none). It must NOT count as a
        # member store — that would hand its live workers the switch bypass past a
        # disabled `agent.session_control`.
        stores = {"member-radar-abc": SimpleNamespace(owner_member="radar", memory_version=2)}
        cfg = self._cfg(stores, agents={})  # owner agent deleted
        with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
            assert sc._store_is_member_owned("member-radar-abc") is False

    def test_owner_rebound_to_a_different_store_is_not_a_member_store(self):
        # The owner still exists but is now bound to a DIFFERENT store, so it is
        # not this store's exclusive active binding.
        stores = {"member-radar-abc": SimpleNamespace(owner_member="radar", memory_version=2)}
        agents = {"radar": SimpleNamespace(memory_store="some-other-store")}
        cfg = self._cfg(stores, agents=agents)
        with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
            assert sc._store_is_member_owned("member-radar-abc") is False


def _slot(key: str, *, created_by: str = "", workspace: str = "default") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        workspace=workspace,
        memory_mode="persistent",
        _app="",
        linked_session_key="",
        _created_by=created_by,
        memory_store="",
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
        state = _State({member: _slot(member), "chat-1-abc": _slot("chat-1-abc")})
        with patch.object(sc, "member_dispatch_enabled", return_value=True):
            assert sc._member_bypass(state, member) is True
            assert sc._member_bypass(state, "chat-1-abc") is False  # not a member
        with patch.object(sc, "member_dispatch_enabled", return_value=False):
            assert sc._member_bypass(state, member) is False  # ceiling off
            assert sc._member_bypass(state, "chat-1-abc") is False

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


class TestMemberChildExecutionContext:
    """Created workers retain canonical identity before their first turn."""

    def _prepare(self, tmp_path, monkeypatch, *, member=True):
        from pathlib import Path

        from member_memory_helpers import forget_declared_stores, write_member_home

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.sections import ResolvedBindings
        from kiro_crew.execution_context import bind_session_execution, resolve_member_execution
        from kiro_crew.history import ConversationLog

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        write_member_home(tmp_path, "radar", "peer")
        forget_declared_stores(monkeypatch)
        state = _make_state(tmp_path)
        state.conversation_log = ConversationLog()
        caller = _member_tab(state) if member else state.get_or_create_slot("chat-owner")
        cfg = KiroCrewConfig.load()
        execution = resolve_member_execution(cfg, "radar")
        if member:
            caller.agent = "radar"
            caller.memory_store = execution.store.legacy_name
            bind_session_execution(slot_history_key(caller), execution)

        def resolve(_cfg, name, *_args, **_kwargs):
            selected = resolve_member_execution(_cfg, name or "radar")
            return ResolvedBindings(
                workspace_dir=Path("workspace"),
                memory_store_name=selected.store.legacy_name,
                effective_memory_config={},
                kiro_agent=selected.template_id,
                selection_kind="member",
                resolved_alias=name or "radar",
                execution_context=selected,
            )

        monkeypatch.setattr(sc, "resolve_agent_bindings", resolve)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda *_: caller.workspace)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        return state, caller, execution

    @pytest.mark.parametrize("via_http", [False, True])
    def test_child_has_canonical_identity_before_first_turn(
        self, tmp_path, monkeypatch, _fresh_create_budget, via_http
    ):
        import json

        from member_memory_helpers import make_request

        from kiro_crew.dashboard.handlers.session_control import api_session_control_create
        from kiro_crew.execution_context import read_session_execution

        state, caller, execution = self._prepare(tmp_path, monkeypatch)

        async def create():
            if not via_http:
                return await sc.create_session(state, caller_session_key=slot_history_key(caller))
            response = await api_session_control_create(
                make_request(
                    state,
                    "/api/session-control/create",
                    body={},
                    internal=True,
                    session=slot_history_key(caller),
                )
            )
            assert response.status == 200, response.text
            return json.loads(response.text)

        result = asyncio.run(create())
        child = state.get_slot(result["target"])
        assert child is not None
        actual = read_session_execution(slot_history_key(child), required=True)
        assert actual.member_id == execution.member_id
        assert actual.store == execution.store
        assert actual.template_id == execution.template_id

    @pytest.mark.parametrize("caller_form", ["canonical", "slot", "stem"])
    def test_inheritance_uses_canonical_caller_key(
        self, tmp_path, monkeypatch, _fresh_create_budget, caller_form
    ):
        from kiro_crew.execution_context import read_session_execution
        from kiro_crew.history import transcript_stem

        state, caller, execution = self._prepare(tmp_path, monkeypatch)
        canonical = slot_history_key(caller)
        key = {"canonical": canonical, "slot": caller.key, "stem": transcript_stem(canonical)}[
            caller_form
        ]
        result = asyncio.run(sc.create_session(state, caller_session_key=key))
        child = state.get_slot(result["target"])
        assert read_session_execution(slot_history_key(child)).store == execution.store

    def test_inherited_route_survives_changed_alias_binding(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        from kiro_crew.execution_context import read_session_execution

        state, caller, execution = self._prepare(tmp_path, monkeypatch)
        real_resolve = sc.resolve_agent_bindings
        monkeypatch.setattr(
            sc, "resolve_agent_bindings", lambda cfg, *_a, **_k: real_resolve(cfg, "peer")
        )
        result = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        child = state.get_slot(result["target"])
        assert read_session_execution(slot_history_key(child)).store == execution.store

    @pytest.mark.parametrize("member", [False, True])
    def test_explicit_target_member_captures_selected_route(
        self, tmp_path, monkeypatch, _fresh_create_budget, member
    ):
        from kiro_crew.execution_context import read_session_execution

        state, caller, _ = self._prepare(tmp_path, monkeypatch, member=member)
        before = read_session_execution(slot_history_key(caller))
        result = asyncio.run(
            sc.create_session(state, caller_session_key=slot_history_key(caller), agent="peer")
        )
        child = state.get_slot(result["target"])
        assert read_session_execution(slot_history_key(child)).member_id == "peer"
        assert read_session_execution(slot_history_key(caller)) == before

    def test_malformed_caller_refuses_without_publishing_child(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        state, caller, _ = self._prepare(tmp_path, monkeypatch)
        state.conversation_log.update_metadata(
            slot_history_key(caller), {"execution_context": {"bad": "data"}}
        )
        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        assert error.value.code == "memory_unavailable"
        assert state.creator_slot_count(caller.key) == 0

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_canonical_restricted_mode_cannot_create_persistent_child(
        self, tmp_path, monkeypatch, _fresh_create_budget, mode
    ):
        from kiro_crew.execution_context import bind_session_execution

        state, caller, execution = self._prepare(tmp_path, monkeypatch)
        bind_session_execution(
            slot_history_key(caller), execution.with_mode(mode), replace_existing=True
        )
        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        assert error.value.code == "ephemeral_caller"
        assert state.creator_slot_count(caller.key) == 0

    def test_parent_privacy_change_during_resolution_retracts_child(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        from kiro_crew.execution_context import bind_session_execution

        state, caller, execution = self._prepare(tmp_path, monkeypatch)
        resolve = sc.resolve_agent_bindings

        def change_privacy(*args, **kwargs):
            selected = resolve(*args, **kwargs)
            bind_session_execution(
                slot_history_key(caller), execution.with_mode("temporary"), replace_existing=True
            )
            return selected

        monkeypatch.setattr(sc, "resolve_agent_bindings", change_privacy)
        with pytest.raises(sc.SessionControlError) as error:
            asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        assert error.value.code == "caller_memory_changed"
        assert state.creator_slot_count(caller.key) == 0

    @pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
    def test_binding_failure_retracts_child(
        self, tmp_path, monkeypatch, _fresh_create_budget, failure
    ):
        state, caller, _ = self._prepare(tmp_path, monkeypatch)

        def fail(*_args, **_kwargs):
            raise failure("binding failed")

        monkeypatch.setattr(sc, "bind_session_execution", fail)
        with pytest.raises(failure):
            asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        assert state.creator_slot_count(caller.key) == 0
