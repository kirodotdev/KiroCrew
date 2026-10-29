"""A ``linked_session_key`` rebind may not move the key an arm is settling onto."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.paths import CWD_CLEARED


def _slot(key: str = "dashboard:test", linked: str = "", claim_cwd: str = ""):
    slot = SimpleNamespace()
    slot.key = key
    slot.linked_session_key = linked
    slot.claim_cwd = claim_cwd
    return slot


class TestTheSlotFactoryKeepsItsKey:
    def test_the_slot_factory_binds_its_keyword_only_when_it_mints_the_slot(self, tmp_path):
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slack:C123.456", linked_session_key="slack:C123.456")
        assert slot.linked_session_key == "slack:C123.456"

        again = state.get_or_create_slot(slot.key, linked_session_key="cron:job-9")
        assert again is slot
        assert slot.linked_session_key == "slack:C123.456"


class TestTheSettleCannotSuspend:
    def test_the_settle_completes_without_yielding_to_the_loop(self):
        """An await between the key this reads and the transfer is the whole defect class."""
        from kiro_crew.dashboard.chat_runner import _settle_and_transfer_arm

        settled = _settle_and_transfer_arm(
            MagicMock(),
            _slot(linked="slack:1111.0001"),
            "dashboard:test",
            None,
            publish=False,
        )

        assert not inspect.isawaitable(settled), "the settle handed back something to await"
        assert settled == (None, "slack:1111.0001")


class TestARebindCannotLaunderAppAuthorizationIntoARetry:
    @pytest.mark.asyncio
    async def test_a_newer_same_key_change_during_the_reset_is_kept_not_cleared(self):
        """The teardown awaits, so a second project change can land on the SAME key."""
        from kiro_crew.dashboard import chat_runner

        slot = SimpleNamespace(
            key="dashboard:test",
            _app=None,
            linked_session_key="",
            _pending_reset_history_key="dashboard:test",
            claim_cwd="/project/b",
            forget_session_model_state=MagicMock(),
            messages=[],
        )

        async def _reset(*_a, **_k):
            slot.claim_cwd = "/project/c"
            return True

        settle = MagicMock(return_value=(None, "dashboard:test"))
        with (
            patch.object(chat_runner, "_settle_and_transfer_arm", settle),
            patch.object(chat_runner, "subagents_attached_async", AsyncMock(return_value=False)),
            patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()) as retry,
        ):
            state = MagicMock()
            state.sessions.reset = _reset
            await chat_runner._consume_pending_reset(state, slot)

        assert slot._pending_reset_history_key == "dashboard:test"
        retry.assert_called_once()
        assert settle.call_args_list[-1].args[3] == "/project/c"

    @pytest.mark.asyncio
    async def test_a_refused_teardown_still_re_settles_the_superseded_arm(self):
        """A refused reset is the WORSE case:"""
        from kiro_crew.dashboard import chat_runner

        slot = SimpleNamespace(
            key="dashboard:test",
            _app=None,
            linked_session_key="",
            _pending_reset_history_key="dashboard:test",
            claim_cwd="/project/b",
            forget_session_model_state=MagicMock(),
        )

        async def _refuse(*_a, **_k):
            slot.claim_cwd = "/project/c"
            return False

        settle = MagicMock(return_value=(None, "dashboard:test"))
        with (
            patch.object(chat_runner, "_settle_and_transfer_arm", settle),
            patch.object(chat_runner, "subagents_attached_async", AsyncMock(return_value=False)),
            patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()) as retry,
        ):
            state = MagicMock()
            state.sessions.reset = _refuse
            torn_down = await chat_runner._consume_pending_reset(state, slot)

        assert torn_down is False, "a refused teardown must not report one"
        assert settle.call_args_list[-1].args[3] == "/project/c"
        assert slot._pending_reset_history_key == "dashboard:test"
        retry.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_rebound_foreign_key_is_dropped_rather_than_rearmed(self):
        from kiro_crew.dashboard import chat_runner

        slot = SimpleNamespace(
            key="dashboard:test",
            _app="spec-builder",
            _pending_reset_history_key="dashboard:test",
            claim_cwd=None,
        )
        rebound = MagicMock(return_value=(None, "slack:1111.0001"))
        with (
            patch.object(chat_runner, "_settle_and_transfer_arm", rebound),
            patch.object(chat_runner, "effective_session_key", lambda _s: "slack:1111.0001"),
            patch.object(chat_runner, "sel", MagicMock()),
            patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()) as retry,
        ):
            await chat_runner._consume_pending_reset(MagicMock(), slot)

        assert slot._pending_reset_history_key is None
        retry.assert_not_called()


def test_a_spent_arm_is_reaped_only_where_no_start_can_still_read_its_generation():
    from kiro_crew.session import SessionManager
    from kiro_crew.session_allocation import SessionRegistryState

    mgr = object.__new__(SessionManager)
    mgr._allocation_state = SessionRegistryState()
    alloc = mgr._allocation_boundary()
    alloc._allocation_reservations["busy"] = {object()}
    for key in ("idle", "busy"):
        alloc.supersede_arm_for_new_slot(key)
    assert "idle" not in alloc.state.retire_arms
    assert alloc.state.retire_arms["busy"].generation == 1


def test_a_cleared_project_resumes_its_conversation_under_the_default_directory():
    """A clear moves the DIRECTORY."""
    from kiro_crew.session_allocation import SessionAllocationService

    src = inspect.getsource(SessionAllocationService._get_or_create_impl)
    assert "cwd_blocks_pool = cwd == CWD_CLEARED" in src, "read the wrong function"
    after_read = src.split("resume_sid = owner._session_map.get(key)", 1)[1]
    guard = after_read.split("cwd_blocks_pool", 1)[0]
    assert "clear_sid" not in guard, "the cleared-claim resume guard discards the conversation"


def test_a_stale_start_does_not_drain_what_a_forced_stop_parked():
    """Adoption EMPTIES the parked store, and a stale start is evicted moments later."""
    from kiro_crew.session_allocation import SessionAllocationService

    src = inspect.getsource(SessionAllocationService._get_or_create_impl)
    guard = "if not stale_generation and adopt_parked_queue(session, key):"
    assert guard in src, "a stale start must be refused adoption at its registration"
    assert src.index("stale_generation = True") < src.index(guard)


def test_a_cleared_project_retires_the_stored_directory_not_just_the_arm():
    """The arm is in MEMORY."""
    from kiro_crew.session import SessionManager
    from kiro_crew.session_allocation import SessionRegistryState

    mgr = object.__new__(SessionManager)
    mgr._allocation_state = SessionRegistryState()
    mgr._session_map = MagicMock()
    alloc = mgr._allocation_boundary()

    alloc.mark_retire_on_next_claim("dashboard:test", "/project/b")
    assert mgr._session_map.clear_cwd.call_count == 0

    alloc.mark_retire_on_next_claim("dashboard:test", CWD_CLEARED)
    mgr._session_map.clear_cwd.assert_called_once_with("dashboard:test")


class TestTheChildrenProbeCoversTheKeyTheArmLandsOn:
    @pytest.mark.asyncio
    async def test_a_rebind_probes_children_on_the_key_the_arm_is_transferred_to(self):
        from kiro_crew.dashboard import chat_runner

        slot = SimpleNamespace(
            key="dashboard:test",
            _app=None,
            linked_session_key="cron:job-9",
            _pending_reset_history_key="dashboard:test",
            claim_cwd="/project/b",
            forget_session_model_state=MagicMock(),
        )
        probed: list[str] = []

        async def _attached(_state, _slot, probe_key, _why):
            probed.append(probe_key)
            return probe_key == "cron:job-9"

        settle = MagicMock(return_value=(None, "cron:job-9"))
        with (
            patch.object(chat_runner, "_settle_and_transfer_arm", settle),
            patch.object(chat_runner, "subagents_attached_async", _attached),
            patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()),
        ):
            await chat_runner._consume_pending_reset(MagicMock(), slot)

        assert "cron:job-9" in probed, "the key the arm lands on was never probed for children"
        assert settle.call_args_list[-1].kwargs["publish"] is False


def test_a_child_deferred_rebind_still_withdraws_the_abandoned_keys_arm():
    from kiro_crew.dashboard import chat_runner

    slot = SimpleNamespace(key="dashboard:test", _app=None, linked_session_key="cron:job-9")
    state = MagicMock()
    chat_runner._settle_and_transfer_arm(state, slot, "dashboard:test", "/b", publish=False)
    state.sessions.withdraw_retire_arm.assert_called_once_with("dashboard:test")
    state.sessions.transfer_retire_arm.assert_not_called()


def test_a_cleared_arm_stores_the_requirement_rather_than_a_guess_at_it():
    """``resolved_cwd(CWD_CLEARED, key)`` answers the per-session default, which is a guess."""
    from kiro_crew.session import SessionManager
    from kiro_crew.session_allocation import SessionRegistryState

    mgr = object.__new__(SessionManager)
    mgr._allocation_state = SessionRegistryState()
    mgr._session_map = MagicMock()
    alloc = mgr._allocation_boundary()
    alloc.mark_retire_on_next_claim("dashboard:test", CWD_CLEARED)

    assert alloc.state.retire_arms["dashboard:test"].cwd == CWD_CLEARED


def test_transferring_an_arm_away_withdraws_it_from_the_abandoned_key():
    """The abandoned key keeps its session and its stored directory."""
    from kiro_crew.session import SessionManager
    from kiro_crew.session_allocation import SessionRegistryState

    mgr = object.__new__(SessionManager)
    mgr._allocation_state = SessionRegistryState()
    mgr._session_map = MagicMock()
    mgr._session_map.get_cwd.return_value = "/old"
    alloc = mgr._allocation_boundary()
    waiting = SimpleNamespace(retire_on_arm=False)
    alloc._sessions["cron:job-9"] = waiting
    alloc.mark_retire_on_next_claim("cron:job-9", CWD_CLEARED)
    assert waiting.retire_on_arm is True

    alloc.transfer_retire_arm("cron:job-9", "dashboard:test", CWD_CLEARED)

    assert waiting.retire_on_arm is False
    mgr._session_map.restore_cwd.assert_called_once_with("cron:job-9", "/old")
    assert alloc.state.retire_arms["dashboard:test"].cwd == CWD_CLEARED


def test_a_discarded_conversation_keeps_its_arm_record_until_the_retry():
    from kiro_crew.session import SessionManager
    from kiro_crew.session_allocation import SessionRegistryState

    mgr = object.__new__(SessionManager)
    mgr._allocation_state = SessionRegistryState()
    alloc = mgr._allocation_boundary()
    alloc.note_conversation_discarded("dashboard:test")
    alloc._reap_arm_if_quiescent("dashboard:test")
    assert alloc.state.retire_arms["dashboard:test"].held_for_retry is True


def test_an_equivalent_cleared_rearm_does_not_supersede_an_in_flight_start():
    from kiro_crew.session import SessionManager
    from kiro_crew.session_allocation import SessionRegistryState

    mgr = object.__new__(SessionManager)
    mgr._allocation_state = SessionRegistryState()
    mgr._session_map = MagicMock()
    alloc = mgr._allocation_boundary()

    alloc.transfer_retire_arm("dashboard:test", "dashboard:test", CWD_CLEARED)
    armed = alloc.state.retire_arms["dashboard:test"].generation
    alloc.transfer_retire_arm("dashboard:test", "dashboard:test", CWD_CLEARED)
    assert alloc.state.retire_arms["dashboard:test"].generation == armed

    alloc.transfer_retire_arm("dashboard:test", "dashboard:test", "/a/b")
    assert alloc.state.retire_arms["dashboard:test"].generation > armed


def test_a_cleared_arm_is_satisfied_by_a_cleared_claim_not_by_a_resolved_path():
    """Only the cold start knows where a cleared project lands, so nothing earlier may guess."""
    from kiro_crew.session_allocation import SessionAllocationService

    src = inspect.getsource(SessionAllocationService._get_or_create_impl)
    anchor = "armed_cwd = claim_arm.cwd if claim_arm is not None else None"
    assert anchor in src, "read the wrong function"
    verdict = src.split(anchor, 1)[1].split("stale_generation = True", 1)[0]
    assert "cwd != CWD_CLEARED" in verdict, "a cleared arm is compared against a path resolved here"
