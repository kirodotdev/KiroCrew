"""A deferred project reset lands on the session the slot runs on, and never on one it left."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)

from kiro_crew.config.loader import _session_work_dir
from kiro_crew.config.paths import CWD_CLEARED, resolved_cwd
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.session import SessionManager


def _state_for(slot):
    state = MagicMock()
    state._slots = {slot.key: slot}
    state.get_slot = lambda key: state._slots.get(key)
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock(return_value=True)
    state.sessions.transfer_retire_arm = MagicMock()
    return state


class TestARebindDuringTheConsumeMovesTheReset:
    @pytest.mark.asyncio
    async def test_a_rebind_during_the_children_probe_leaves_the_reset_armed_for_the_live_key(
        self,
    ):
        slot = _ChatSlot("test")
        slot.linked_session_key = "slack:1111.0001"
        slot.project = "/workspace/new"
        slot._pending_reset_history_key = "slack:1111.0001"
        state = _state_for(slot)

        async def _probe_while_rebinding(_state, probed_slot, _key, _why):
            if probed_slot.linked_session_key == "slack:1111.0001":
                probed_slot.linked_session_key = "cron:job-9"
            return False

        with (
            patch.object(chat_runner, "subagents_attached_async", _probe_while_rebinding),
            patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()) as rearm,
        ):
            torn_down = await chat_runner._consume_pending_reset(state, slot)

        assert state.sessions.reset.await_count == 0
        assert not torn_down
        assert slot._pending_reset_history_key == "cron:job-9"
        rearm.assert_called_once()
        assert state.sessions.transfer_retire_arm.call_args.args[:2] == (
            "slack:1111.0001",
            "cron:job-9",
        ), "the arm stayed on the abandoned key instead of following the slot"

    @pytest.mark.asyncio
    async def test_a_consume_with_no_rebind_applies_the_reset_on_an_active_turn_refusal_only(
        self,
    ):
        """The control for the case above, and the reset's call shape:"""
        slot = _ChatSlot("test")
        slot.linked_session_key = "slack:1111.0001"
        slot.project = "/workspace/new"
        slot._pending_reset_history_key = "slack:1111.0001"
        state = _state_for(slot)

        with (
            patch.object(chat_runner, "subagents_attached_async", AsyncMock(return_value=False)),
            patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()),
        ):
            torn_down = await chat_runner._consume_pending_reset(state, slot)

        state.sessions.reset.assert_awaited_once_with(
            "slack:1111.0001", skip_if_busy=True, refuse_only_on_active_turn=True
        )
        assert torn_down
        assert slot._pending_reset_history_key is None


def _cwd_reporting_factory():
    inner = _mock_provider_factory()

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = inner(session_key, agent, channel_id, **kwargs)
        provider.cwd = kwargs.get("cwd") or "/workspace/default"
        return provider

    return factory


class TestAnArmDefersWhileChildrenAreAttached:
    @pytest.mark.asyncio
    async def test_an_arm_published_before_children_attached_does_not_retire_their_runtime(
        self, cfg
    ):
        mgr = SessionManager(cfg, provider_factory=_cwd_reporting_factory())
        attached = {"slack:1111.0001": True}
        mgr.set_subagent_probe(lambda key: attached.get(key, False))
        try:
            parent, *_ = await mgr.get_or_create("slack:1111.0001", cwd="/project/a")
            mgr.release("slack:1111.0001")
            mgr.transfer_retire_arm("slack:1111.0001", "slack:1111.0001", "/project/b")

            served, *_ = await mgr.get_or_create("slack:1111.0001", cwd="/project/b")
            mgr.release("slack:1111.0001")

            assert served is parent, "the claim retired the session its sub-agents run on"
            parent.shutdown.assert_not_awaited()

            attached["slack:1111.0001"] = False
            successor, *_ = await mgr.get_or_create("slack:1111.0001", cwd="/project/b")
            mgr.release("slack:1111.0001")

            assert successor is not parent, "the deferred arm was dropped instead of kept"
            assert successor.cwd == "/project/b"
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_attached_children_never_defer_a_signed_out_accounts_retirement(self, cfg):
        """Only the ARM defers."""
        mgr = SessionManager(cfg, provider_factory=_cwd_reporting_factory())
        mgr.set_subagent_probe(lambda _key: True)
        try:
            parent, *_ = await mgr.get_or_create("dashboard:x", cwd="/project/a")
            mgr.release("dashboard:x")
            mgr._sessions["dashboard:x"].retire_on_identity_change = True

            served, *_ = await mgr.get_or_create("dashboard:x", cwd="/project/a")
            mgr.release("dashboard:x")

            assert served is not parent
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_probe_that_raises_keeps_the_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_cwd_reporting_factory())

        def _broken(_key):
            raise RuntimeError("store unreadable")

        mgr.set_subagent_probe(_broken)
        try:
            parent, *_ = await mgr.get_or_create("dashboard:x", cwd="/project/a")
            mgr.release("dashboard:x")
            mgr.transfer_retire_arm("dashboard:x", "dashboard:x", "/project/b")

            served, *_ = await mgr.get_or_create("dashboard:x", cwd="/project/b")
            mgr.release("dashboard:x")

            assert served is parent
        finally:
            await mgr.close_all()


class TestAnArmRetirementKeepsWhatItRetires:
    @pytest.mark.asyncio
    async def test_queued_messages_on_an_arm_retired_session_reach_its_successor(self, cfg):
        """Acknowledged follow-ups queued behind a busy reply are parked, not unlinked."""
        mgr = SessionManager(cfg, provider_factory=_cwd_reporting_factory())
        try:
            parent, *_ = await mgr.get_or_create("discord:1", cwd="/project/a")
            assert mgr.enqueue("discord:1", "m-1", "follow-up", force=True)
            mgr.release("discord:1")
            mgr.transfer_retire_arm("discord:1", "discord:1", "/project/b")

            successor, *_ = await mgr.get_or_create("discord:1", cwd="/project/b")

            assert successor is not parent
            assert [e[0] for e in mgr._sessions["discord:1"].queue] == ["m-1"]
            mgr.release("discord:1")
        finally:
            await mgr.close_all()


class TestAClearedCwdResolvesWithNoMocks:
    def test_a_cleared_cwd_resolves_to_the_directory_the_provider_factory_binds(self):
        """Every cleared-project path reaches the real ``resolved_cwd("")``, unmocked here."""
        key = "dashboard:x"

        resolved = resolved_cwd(CWD_CLEARED, key)

        assert resolved == str(_session_work_dir(key))
        assert resolved != resolved_cwd(CWD_CLEARED, "dashboard:y")
