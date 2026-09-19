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


# The conductor's ORDINARY chat slot, bound to its member V2 store — case (b).
_CONDUCTOR_STORE = "member-kirocrew-conductor-deadbeef"
_CHAT_CALLER = "chat-10-1789623359"


class TestMemberChatSlotCallerFence:
    """A crew member acting through an ORDINARY chat slot (case (b)).

    A member agent (``kirocrew-conductor``) also runs in a plain dashboard chat
    slot (key ``chat-<n>-<ts>``) whose bound memory store is that member's
    private V2 store — its whole operating model (``session_create`` /
    ``session_send`` / ...) runs from there. Before this change such a slot was
    refused exactly like any private V2 caller, because the fence keyed only on
    the ``member-`` KEY prefix. These tests pin that the STORE now grants the
    same bypass and the same ownership fence a ``member-`` DM slot gets, driven
    through the real ``authorize_target`` gate order with a fake state.

    ``_store_is_member_owned`` is pinned rather than a config fixture: it has its
    own record-level unit tests above; here the interest is the fence order.
    """

    def _chat_caller_slot(self):
        s = _slot(_CHAT_CALLER)
        s.memory_store = _CONDUCTOR_STORE
        return s

    def _member_store(self, monkeypatch):
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: store == _CONDUCTOR_STORE)

    def test_chat_slot_member_controls_its_own_worker_with_switch_off(self, monkeypatch):
        self._member_store(monkeypatch)
        worker = _slot("chat-1-w1", created_by=_CHAT_CALLER)
        state = _State({_CHAT_CALLER: self._chat_caller_slot(), "chat-1-w1": worker})
        with (
            patch.object(sc, "caller_slot_key", return_value=_CHAT_CALLER),
            patch.object(sc, "session_control_enabled", return_value=False),
            patch.object(sc, "member_dispatch_enabled", return_value=True),
            patch.object(sc, "_resolve_slot", return_value=worker),
        ):
            resolved = sc.authorize_target(
                state,
                caller_session_key="dashboard:whatever",
                target="chat-1-w1",
                operation="send",
            )
        assert resolved is worker

    def test_chat_slot_member_cannot_touch_a_slot_it_did_not_create(self, monkeypatch):
        # The ownership fence follows the member's AUTHORITY (the store), so a
        # case-(b) caller is bounded to the workers it created just like a DM
        # slot — the user's own conversation stays out of reach.
        self._member_store(monkeypatch)
        foreign = _slot("chat-1-user", created_by="")
        state = _State({_CHAT_CALLER: self._chat_caller_slot(), "chat-1-user": foreign})
        with (
            patch.object(sc, "caller_slot_key", return_value=_CHAT_CALLER),
            patch.object(sc, "session_control_enabled", return_value=True),
            patch.object(sc, "_resolve_slot", return_value=foreign),
        ):
            with pytest.raises(sc.SessionControlError) as exc_info:
                sc.authorize_target(
                    state,
                    caller_session_key="dashboard:whatever",
                    target="chat-1-user",
                    operation="send",
                )
        assert exc_info.value.code == "not_creator"
        assert "crew member" in exc_info.value.message

    def test_chat_slot_member_falls_back_under_switch_when_ceiling_off(self, monkeypatch):
        # member_dispatch off withdraws the case-(b) bypass exactly as it does
        # for a DM slot: with the switch also off, it is refused like an ordinary
        # caller.
        self._member_store(monkeypatch)
        worker = _slot("chat-1-w1", created_by=_CHAT_CALLER)
        state = _State({_CHAT_CALLER: self._chat_caller_slot(), "chat-1-w1": worker})
        with (
            patch.object(sc, "caller_slot_key", return_value=_CHAT_CALLER),
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

    def test_non_member_chat_slot_still_needs_the_switch(self, monkeypatch):
        # A chat slot whose store is NOT a member store is unchanged: no bypass.
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: False)
        caller = _slot(_CHAT_CALLER)
        caller.memory_store = "default"
        state = _State({_CHAT_CALLER: caller, "chat-1-b": _slot("chat-1-b")})
        with (
            patch.object(sc, "caller_slot_key", return_value=_CHAT_CALLER),
            patch.object(sc, "session_control_enabled", return_value=False),
            patch.object(sc, "member_dispatch_enabled", return_value=True),
            patch.object(sc, "_resolve_slot", return_value=state._slots["chat-1-b"]),
        ):
            with pytest.raises(sc.SessionControlError) as exc_info:
                sc.authorize_target(
                    state,
                    caller_session_key="dashboard:whatever",
                    target="chat-1-b",
                    operation="send",
                )
        assert exc_info.value.code == "session_control_disabled"

    def test_legacy_v1_named_store_chat_tab_keeps_its_prior_reach(self, monkeypatch):
        # A plain chat tab bound to a legacy V1 NAMED (non-member) store. Its
        # private scope resolves to owner (None), so the HTTP gate admits it with
        # FULL reach and carries NO fence verdict (`precomputed_ownership_fenced`
        # stays None). The inline fence must not narrow it to creator-only just
        # because its store is not `default`: with `agent.session_control` ON, an
        # ordinary same-workspace target it did not create is authorized, exactly
        # as before member dispatch existed.
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: False)
        caller = _slot(_CHAT_CALLER)
        caller.memory_store = "team-shared-v1"  # a non-default, non-member store
        peer = _slot("chat-1-peer", created_by="")  # a session the caller did NOT create
        state = _State({_CHAT_CALLER: caller, "chat-1-peer": peer})
        # Precondition: this caller is NOT a member, and NOT ownership-fenced.
        assert not sc._member_caller(state, _CHAT_CALLER)
        assert not sc._caller_is_ownership_fenced(state, _CHAT_CALLER)
        with (
            patch.object(sc, "caller_slot_key", return_value=_CHAT_CALLER),
            patch.object(sc, "session_control_enabled", return_value=True),
            patch.object(sc, "_resolve_slot", return_value=peer),
        ):
            resolved = sc.authorize_target(
                state,
                caller_session_key="dashboard:whatever",
                target="chat-1-peer",
                operation="send",
            )
        assert resolved is peer

    # The ways an operator's own config writer can change a member store's record
    # between the HTTP gate's admission and the inner authorization. Each one makes
    # `_store_is_member_owned` answer False for a store it answered True for a beat
    # earlier — and each is a legitimate write, not corruption to guard against.
    _RECORD_FLIPS = {
        "member un-assigned (owner_member cleared, memory_version stays 2)": (
            lambda: {_CONDUCTOR_STORE: SimpleNamespace(owner_member="", memory_version=2)}
        ),
        "memory_version dropped (loader coerces a missing key to 1)": (
            lambda: {_CONDUCTOR_STORE: SimpleNamespace(owner_member="conductor", memory_version=1)}
        ),
        "record dropped (a malformed entry is silently discarded)": (lambda: {}),
    }

    def _flipped_cfg(self, stores, *, degraded=frozenset()):
        agents = {
            rec.owner_member: SimpleNamespace(memory_store=name)
            for name, rec in stores.items()
            if rec.owner_member
        }
        return SimpleNamespace(memory_stores=stores, degraded_sections=degraded, agents=agents)

    @pytest.mark.parametrize("flip", sorted(_RECORD_FLIPS))
    def test_record_flipped_after_admission_cannot_unfence_an_admitted_member(self, flip):
        # GPT-flagged window: a member's chat slot is admitted at the HTTP gate on
        # its VERIFIED scope, then the config record flips before the inner
        # authorization runs. The gate carried its admission as
        # `precomputed_ownership_fenced=True`, so the fence holds: a foreign
        # same-workspace session the caller did not create stays out of reach even
        # with the global switch ON. Driven through the real config-record
        # predicate (not a stub) so the flip is the actual record, whatever shape
        # the writer gave it.
        stores = self._RECORD_FLIPS[flip]()
        foreign = _slot("chat-1-user", created_by="")
        state = _State({_CHAT_CALLER: self._chat_caller_slot(), "chat-1-user": foreign})
        with (
            patch.object(sc.KiroCrewConfig, "load", return_value=self._flipped_cfg(stores)),
            patch.object(sc, "caller_slot_key", return_value=_CHAT_CALLER),
            patch.object(sc, "session_control_enabled", return_value=True),
            patch.object(sc, "_resolve_slot", return_value=foreign),
        ):
            # With the flipped record the store does not classify as a member store,
            # so the INLINE fence does not bind this caller — that is the window the
            # carried verdict closes, and what a regression here would silently reopen.
            assert not sc._member_caller(state, _CHAT_CALLER), flip
            assert not sc._caller_is_ownership_fenced(state, _CHAT_CALLER), flip
            with pytest.raises(sc.SessionControlError) as exc_info:
                sc.authorize_target(
                    state,
                    caller_session_key="dashboard:whatever",
                    target="chat-1-user",
                    operation="send",
                    precomputed_ownership_fenced=True,
                )
        assert exc_info.value.code == "not_creator", flip
        # A carried verdict cannot see the caller's class, so the text names the rule.
        assert "can only control sessions it created itself" in exc_info.value.message

    def test_degraded_config_after_admission_cannot_unfence_an_admitted_member(self):
        # Same window, config side: the `memory_stores` section degrades (or the
        # read raises) between admission and authorization. The predicate fails
        # closed to "not a member" — which withdraws the switch BYPASS, the safe
        # direction — while the carried verdict keeps the FENCE in place.
        stores = {_CONDUCTOR_STORE: SimpleNamespace(owner_member="conductor", memory_version=2)}
        degraded_cfg = self._flipped_cfg(stores, degraded=frozenset({"memory_stores"}))
        foreign = _slot("chat-1-user", created_by="")
        state = _State({_CHAT_CALLER: self._chat_caller_slot(), "chat-1-user": foreign})
        for load in (
            patch.object(sc.KiroCrewConfig, "load", return_value=degraded_cfg),
            patch.object(sc.KiroCrewConfig, "load", side_effect=RuntimeError("boom")),
        ):
            with (
                load,
                patch.object(sc, "caller_slot_key", return_value=_CHAT_CALLER),
                patch.object(sc, "session_control_enabled", return_value=True),
                patch.object(sc, "_resolve_slot", return_value=foreign),
            ):
                assert not sc._member_caller(state, _CHAT_CALLER)
                with pytest.raises(sc.SessionControlError) as exc_info:
                    sc.authorize_target(
                        state,
                        caller_session_key="dashboard:whatever",
                        target="chat-1-user",
                        operation="send",
                        precomputed_ownership_fenced=True,
                    )
            assert exc_info.value.code == "not_creator"


class TestMemberChatSlotCallerEndToEnd:
    """Case (b) through the REAL create/authorize transaction.

    A plain ``chat-`` slot bound to a member V2 store creates a child, the child
    is attributed to it and reachable by it, and it is fenced off the user's own
    sessions — the same contract ``TestMemberDispatchEndToEnd`` pins for a DM
    slot, now for the chat-slot caller.
    """

    def _chat_member_tab(self, state):
        slot = state.get_or_create_slot(_CHAT_CALLER)
        slot.memory_store = _CONDUCTOR_STORE
        return slot

    def test_chat_slot_member_creates_and_reaches_its_worker(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        state = _make_state(tmp_path)
        caller = self._chat_member_tab(state)
        # Recognise the caller's store as a member store without a config fixture;
        # keep the child on the default store so the private-binding plumbing
        # (exercised in TestMemberChildPrivateBinding) stays out of this test.
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: store == _CONDUCTOR_STORE)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)
        # Switch OFF: the case-(b) member bypass is what admits the create.
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)

        result = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        child = state.get_slot(result["target"])
        assert child is not None
        assert child._created_by == _CHAT_CALLER
        assert child._origin == SlotOrigin.USER
        for op in ("send", "read", "stop", "close"):
            resolved = sc.authorize_target(
                state,
                caller_session_key=slot_history_key(caller),
                target=child.key,
                operation=op,
            )
            assert resolved is child, op

    def test_chat_slot_member_cannot_reach_a_session_it_did_not_create(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        state = _make_state(tmp_path)
        caller = self._chat_member_tab(state)
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: store == _CONDUCTOR_STORE)
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


class TestMemberCreatedWorkerCanDispatch:
    """F3 — the nested-conductor design, stated and pinned.

    A member's created child is bound to the member's own V2 store at birth, so
    ``_member_caller`` case (b) is true for the WORKER too: a member-created
    worker is itself member-bound and can dispatch its own children
    (grandchildren of the original member) under ``agent.member_dispatch``,
    WITHOUT the global ``session_control`` switch. This is INTENDED — the
    conductor operating model is recursive, depth-capped by the conductor agent
    rather than by this fence — and every node stays bounded by the same
    ownership fence: it reaches only what IT created, never the user's own
    sessions and never a sibling's. These tests pin that intended behaviour so a
    future reader does not mistake it for an accidental widening.
    """

    def test_a_member_bound_worker_is_itself_a_member_caller(self, monkeypatch):
        # The worker's slot is bound to the member store (as birth-time private
        # binding leaves it), so case (b) recognises it as a member caller even
        # though its key is a plain `chat-` key created BY another agent.
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: store == _CONDUCTOR_STORE)
        worker = _slot("chat-1-w1", created_by=_CHAT_CALLER)
        worker.memory_store = _CONDUCTOR_STORE
        state = _State({"chat-1-w1": worker})
        assert sc._member_caller(state, "chat-1-w1")

    def test_member_worker_dispatches_a_grandchild_with_switch_off(self, monkeypatch):
        # The worker (member-bound) creates and reaches its OWN child — a
        # grandchild of the original member — with the global switch OFF, admitted
        # by the case-(b) member bypass. Depth is capped by the conductor agent,
        # not here; this fence only bounds reach.
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: store == _CONDUCTOR_STORE)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        worker = _slot("chat-1-w1", created_by=_CHAT_CALLER)
        worker.memory_store = _CONDUCTOR_STORE
        grandchild = _slot("chat-2-g1", created_by="chat-1-w1")  # created BY the worker
        grandchild.memory_store = _CONDUCTOR_STORE
        state = _State({"chat-1-w1": worker, "chat-2-g1": grandchild})
        with (
            patch.object(sc, "caller_slot_key", return_value="chat-1-w1"),
            patch.object(sc, "_resolve_slot", return_value=grandchild),
        ):
            resolved = sc.authorize_target(
                state,
                caller_session_key="dashboard:worker",
                target="chat-2-g1",
                operation="send",
            )
        assert resolved is grandchild

    def test_member_worker_is_still_fenced_off_what_it_did_not_create(self, monkeypatch):
        # The recursion carries the operating model DOWN without carrying reach
        # ACROSS it: the member-bound worker cannot reach the user's own session
        # (or a sibling worker's), only its own children.
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: store == _CONDUCTOR_STORE)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        worker = _slot("chat-1-w1", created_by=_CHAT_CALLER)
        worker.memory_store = _CONDUCTOR_STORE
        user_session = _slot("chat-9-user", created_by="")  # the user's own tab
        state = _State({"chat-1-w1": worker, "chat-9-user": user_session})
        with (
            patch.object(sc, "caller_slot_key", return_value="chat-1-w1"),
            patch.object(sc, "_resolve_slot", return_value=user_session),
        ):
            with pytest.raises(sc.SessionControlError) as exc:
                sc.authorize_target(
                    state,
                    caller_session_key="dashboard:worker",
                    target="chat-9-user",
                    operation="send",
                )
        assert exc.value.code == "not_creator"


class TestCloseReauthorizationDoesNotLoadConfig:
    """F1 — the close critical section carries the fence verdict, never reloads it.

    ``close_target`` re-runs ``authorize_target`` SYNCHRONOUSLY at
    ``close_slot``'s point of no return, and the ownership fence there
    (``_caller_is_ownership_fenced`` -> ``_member_caller`` ->
    ``_store_is_member_owned``) can load config on a cache miss — blocking IO on
    the event loop inside a no-suspension window. The fix resolves the fence
    verdict ONCE up front and threads it through ``precomputed_ownership_fenced``,
    so the re-check consults a carried boolean instead. This pins that the
    synchronous re-check performs NO ``KiroCrewConfig.load()``.
    """

    def test_reassert_closeable_reads_no_config(self, tmp_path, monkeypatch, _fresh_create_budget):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot(_CHAT_CALLER)
        caller.memory_store = _CONDUCTOR_STORE
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: store == _CONDUCTOR_STORE)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        result = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
        child_key = result["target"]

        # Count config loads while the SYNCHRONOUS pre-pop re-check runs. It is
        # invoked from close_target after the initial authorization; assert it is
        # called (proving the guard runs) yet loads no config (proving the verdict
        # is carried, not recomputed).
        real_authorize = sc.authorize_target
        loads: list[int] = []

        def _counting_load(*a, **k):
            loads.append(1)
            raise AssertionError("config must not be loaded during the sync close re-check")

        seen: dict[str, object] = {"precomputed": None}

        def _spy_authorize(*args, **kwargs):
            if kwargs.get("skip_enabled_check"):
                # This is the synchronous pre-pop re-check. From here on, any
                # config load is a regression; record what verdict it carries.
                seen["precomputed"] = kwargs.get("precomputed_ownership_fenced")
                monkeypatch.setattr(sc.KiroCrewConfig, "load", _counting_load)
            return real_authorize(*args, **kwargs)

        monkeypatch.setattr(sc, "authorize_target", _spy_authorize)
        # Close the member's own worker: the caller is a member (fenced), the
        # child is owned by it, so the close is authorized and the pre-pop
        # re-check runs its full identity/containment path with a carried verdict.
        asyncio.run(
            sc.close_target(state, caller_session_key=slot_history_key(caller), target=child_key)
        )
        assert seen["precomputed"] is True, "close must carry the member fence verdict"
        assert not loads, "the synchronous close re-check loaded config"
