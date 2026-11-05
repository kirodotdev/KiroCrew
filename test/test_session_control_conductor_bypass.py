"""The Conductor in the owner's own tab dispatches workers without ``agent.session_control``.

Crew Mode's Conductor exists to open and drive worker sessions, so a Conductor the
person started in their own dashboard tab gets the same switch bypass a crew member
gets, bounded the same way: while it relies on the bypass it reaches only the
sessions it created. These tests pin that contract through the real
``create_session`` / ``authorize_target`` paths:

* the owner-tab Conductor creates a worker and sends to it with the switch off;
* it is refused (``not_creator``) on a session it did not create;
* a cron-, channel-linked or member-rooted Conductor gets no new bypass;
* a child the Conductor created gets no bypass and stays fenced;
* ``agent.member_dispatch = false`` withdraws the bypass, and so does a degraded
  ``agent`` config section.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from chat_test_helpers import _make_state
from member_session_control_helpers import (  # noqa: F401 -- the fixture is requested by name
    _fresh_create_budget_fixture,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key

_CONDUCTOR = "kirocrew-conductor"


def _owner_conductor(state, key: str = "chat-1-owner"):
    slot = state.get_or_create_slot(key)
    slot.agent = _CONDUCTOR
    return slot


@pytest.fixture
def _switch_off(monkeypatch):
    """Session control off, member_dispatch ceiling on (its default)."""
    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
    monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _conductor_resolves(_floor_monkeypatch):
    """Resolve the Conductor name the way the default agent resolves.

    The test config has no ``kirocrew-conductor`` binding, and a created child
    inherits its creator's agent; what is under test is the gate, not the binding.
    """
    real = sc.resolve_agent_bindings

    def _resolve(cfg, agent_name, *args, **kwargs):
        if agent_name in sc.CONDUCTOR_AGENTS:
            return real(cfg, "", *args, **kwargs)
        return real(cfg, agent_name, *args, **kwargs)

    _floor_monkeypatch.setattr(sc, "resolve_agent_bindings", _resolve)


def _create(state, caller):
    return asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))


def _authorize(state, caller, target_key, op="send"):
    return sc.authorize_target(
        state, caller_session_key=slot_history_key(caller), target=target_key, operation=op
    )


class TestOwnerTabConductorPredicate:
    """Positive identity only: every clause names what the caller IS."""

    def _state(self, slot):
        return SimpleNamespace(get_slot=lambda key: slot if key == slot.key else None)

    def _slot(self, **overrides):
        base = dict(
            key="chat-1-owner",
            agent=_CONDUCTOR,
            _created_by="",
            linked_session_key="",
            channel_origin=False,
            _app="",
            _origin="",
            memory_store="",
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_owner_tab_conductor_is_recognised(self):
        slot = self._slot()
        assert sc._owner_tab_conductor(self._state(slot), slot.key)

    def test_deprecated_alias_is_recognised(self):
        slot = self._slot(agent="kirocrew-ledger-conductor")
        assert sc._owner_tab_conductor(self._state(slot), slot.key)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"agent": "kirocrew"},
            {"agent": ""},
            {"_created_by": "chat-1-owner"},
            {"linked_session_key": "slack:T1:C1"},
            {"linked_session_key": "cron:job1"},
            {"channel_origin": True},
            {"_app": "some-app"},
            {"_origin": "cron"},
            {"_origin": "app"},
            {"key": "cron-job1"},
            {"key": "workflow-run1"},
            {"key": "member-radar"},
        ],
    )
    def test_anything_else_is_not(self, overrides):
        slot = self._slot(**overrides)
        assert not sc._owner_tab_conductor(self._state(slot), slot.key), overrides

    def test_member_bound_chat_is_not(self, monkeypatch):
        # A crewmate's chat runs on a conductor too; it keeps the MEMBER bypass.
        slot = self._slot(memory_store="member-radar-abc")
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: True)
        assert not sc._owner_tab_conductor(self._state(slot), slot.key)

    def test_missing_slot_is_not(self):
        state = SimpleNamespace(get_slot=lambda key: None)
        assert not sc._owner_tab_conductor(state, "chat-1-owner")


class TestOwnerTabConductorEndToEnd:
    def test_creates_and_sends_to_its_own_worker_with_the_switch_off(
        self, tmp_path, monkeypatch, _fresh_create_budget, _switch_off
    ):
        state = _make_state(tmp_path)
        caller = _owner_conductor(state)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)

        result = _create(state, caller)

        worker = state.get_slot(result["target"])
        assert worker is not None
        assert worker._created_by == caller.key
        for op in ("send", "read", "stop", "close"):
            assert _authorize(state, caller, worker.key, op) is worker, op

    def test_is_refused_on_a_session_it_did_not_create(
        self, tmp_path, monkeypatch, _fresh_create_budget, _switch_off
    ):
        state = _make_state(tmp_path)
        caller = _owner_conductor(state)
        other = state.get_or_create_slot("chat-7-user", workspace=caller.workspace)

        for op in ("send", "read", "stop", "close"):
            with pytest.raises(sc.SessionControlError) as exc:
                _authorize(state, caller, other.key, op)
            assert exc.value.code == "not_creator", op
            assert "Conductor" in exc.value.message

    def test_is_refused_on_a_peer_conductor_worker(
        self, tmp_path, monkeypatch, _fresh_create_budget, _switch_off
    ):
        # A worker some OTHER owner-tab conductor made is not this one's.
        state = _make_state(tmp_path)
        caller = _owner_conductor(state)
        peer = _owner_conductor(state, "chat-2-owner")
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: peer.workspace)
        peer_worker = state.get_slot(_create(state, peer)["target"])

        with pytest.raises(sc.SessionControlError) as exc:
            _authorize(state, caller, peer_worker.key)
        assert exc.value.code == "not_creator"

    def test_keeps_the_owner_reach_when_the_switch_is_on(
        self, tmp_path, monkeypatch, _fresh_create_budget
    ):
        # With session control on the Conductor is an ordinary owner caller, as before.
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        state = _make_state(tmp_path)
        caller = _owner_conductor(state)
        other = state.get_or_create_slot("chat-7-user", workspace=caller.workspace)
        assert _authorize(state, caller, other.key) is other

    def test_a_non_conductor_owner_tab_is_still_refused(
        self, tmp_path, monkeypatch, _fresh_create_budget, _switch_off
    ):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1-owner")
        caller.agent = "kirocrew"
        with pytest.raises(sc.SessionControlError) as exc:
            _create(state, caller)
        assert exc.value.code == "session_control_disabled"


class TestOtherConductorsKeepTodaysBehaviour:
    def test_channel_linked_conductor_is_refused(
        self, tmp_path, monkeypatch, _fresh_create_budget, _switch_off
    ):
        state = _make_state(tmp_path)
        caller = _owner_conductor(state)
        caller.linked_session_key = "slack:T1:C1:1700000000.000100"
        assert not sc._dispatch_bypass(state, caller.key)
        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(sc.create_session(state, caller_session_key=caller.key))
        assert exc.value.code == "session_control_disabled"

    def test_cron_conductor_is_refused(self, tmp_path, _switch_off):
        state = _make_state(tmp_path)
        cron = state.get_or_create_slot("cron-job1")
        cron.agent = _CONDUCTOR
        assert not sc._dispatch_bypass(state, cron.key)
        with patch.object(sc, "caller_slot_key", return_value=cron.key):
            with pytest.raises(sc.SessionControlError) as exc:
                asyncio.run(sc.create_session(state, caller_session_key="cron-job1"))
        assert exc.value.code == "session_control_disabled"

    def test_child_of_the_conductor_is_no_unfenced_deputy(
        self, tmp_path, monkeypatch, _fresh_create_budget, _switch_off
    ):
        state = _make_state(tmp_path)
        caller = _owner_conductor(state)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)
        child = state.get_slot(_create(state, caller)["target"])
        # The child inherits the Conductor agent; that must not carry the bypass.
        child.agent = _CONDUCTOR
        other = state.get_or_create_slot("chat-7-user", workspace=caller.workspace)

        assert not sc._dispatch_bypass(state, child.key)
        with pytest.raises(sc.SessionControlError) as exc:
            _create(state, child)
        assert exc.value.code == "session_control_disabled"
        with pytest.raises(sc.SessionControlError) as exc:
            _authorize(state, child, other.key)
        assert exc.value.code == "session_control_disabled"
        # And with the switch on, the child is still creator-fenced.
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        assert sc._caller_is_ownership_fenced(state, child.key)
        with pytest.raises(sc.SessionControlError) as exc:
            _authorize(state, child, other.key)
        assert exc.value.code == "not_creator"

    def test_child_cannot_dispatch_private_member_workers(
        self, tmp_path, monkeypatch, _fresh_create_budget, _switch_off
    ):
        # The lineage walk the private-member gate reads fences anything rooted in a
        # Conductor that is relying on the bypass.
        state = _make_state(tmp_path)
        caller = _owner_conductor(state)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)
        child = state.get_slot(_create(state, caller)["target"])
        assert sc._delegation_lineage_fenced(state, caller.key)
        assert sc._delegation_lineage_fenced(state, child.key)


class TestCeilingWithdrawsTheBypass:
    def test_member_dispatch_false_withdraws_it(self, tmp_path, monkeypatch, _fresh_create_budget):
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: False)
        state = _make_state(tmp_path)
        caller = _owner_conductor(state)
        with pytest.raises(sc.SessionControlError) as exc:
            _create(state, caller)
        assert exc.value.code == "session_control_disabled"

    @pytest.mark.parametrize("degraded", ["agent", "*"])
    def test_degraded_config_withdraws_it(
        self, tmp_path, monkeypatch, _fresh_create_budget, degraded
    ):
        # The real ceiling read: a discarded `agent` section (or whole file) falls
        # back to the permissive default, which must not grant the bypass.
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        cfg = SimpleNamespace(
            degraded_sections=frozenset({degraded}),
            agent=SimpleNamespace(member_dispatch=True),
        )
        state = _make_state(tmp_path)
        caller = _owner_conductor(state)
        with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
            assert sc.member_dispatch_enabled() is False
            assert not sc._dispatch_bypass(state, caller.key)
            with pytest.raises(sc.SessionControlError) as exc:
                _create(state, caller)
        assert exc.value.code == "session_control_disabled"

    def test_unreadable_config_withdraws_it(self, tmp_path, monkeypatch, _fresh_create_budget):
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        state = _make_state(tmp_path)
        caller = _owner_conductor(state)
        with patch.object(sc.KiroCrewConfig, "load", side_effect=RuntimeError("boom")):
            assert not sc._dispatch_bypass(state, caller.key)
