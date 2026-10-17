"""Tests for _remove_slot_for_history_key in handlers.py.

The history-delete routes' cron-owner sweep lives in
``test_remove_slot_for_history_key_owner_sweep.py`` and the session ledger and crew-log
cleanup in ``test_remove_slot_for_history_key_crew_log.py``; the fakes they share live
in ``remove_slot_for_history_key_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Collection
from unittest.mock import AsyncMock, MagicMock

import pytest
from remove_slot_for_history_key_helpers import (
    _guard_work_ledger_cleanup,
    _make_slot,
    _make_state,
    _remove_slot_for_history_key,
)

from kiro_crew import cron as cron_module
from kiro_crew.cron import (
    CronService,
    CronStoreBusy,
    cron_job_id_from_session_key,
    cron_owner_matches,
    cron_session_key_is_stable,
)
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.handlers.sessions import (
    _CRON_RELEASE_ATTEMPTS,
    _capture_history_delete_claim,
    _delete_history_session,
    _resolve_history_delete_claim,
)
from kiro_crew.history import ConversationLog, HistoryLockTimeout


class TestRemoveSlotForHistoryKey:
    @pytest.mark.asyncio
    async def test_exact_key_match(self):
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "dashboard_chat-1-100" not in state._slots

    @pytest.mark.asyncio
    async def test_stripped_key_match(self):
        slot = _make_slot("chat-1-100")
        state = _make_state({"chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "chat-1-100" not in state._slots

    @pytest.mark.asyncio
    async def test_colon_prefix_stripped(self):
        slot = _make_slot("chat-2-200")
        state = _make_state({"chat-2-200": slot})
        await _remove_slot_for_history_key(state, "dashboard:chat-2-200")
        assert "chat-2-200" not in state._slots

    @pytest.mark.asyncio
    async def test_preunlink_expected_absence_refuses_later_successor(self):
        state = _make_state({})
        key = "slack:team:direct:42"
        claim = _capture_history_delete_claim(state, key)
        successor = _make_slot("slack_team_direct_42")
        successor.linked_session_key = key
        state._slots[successor.key] = successor

        await _remove_slot_for_history_key(state, key, delete_claim=claim)

        assert state._slots[successor.key] is successor
        state.sessions.destroy_if.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_bare_delete_finds_live_canonical_slack_slot(self):
        thread_ts = "1785370133.085469"
        target = _make_slot(f"slack_{thread_ts}")
        target.linked_session_key = f"slack:{thread_ts}"
        state = _make_state({target.key: target})
        legacy_path = Path("/history") / f"{thread_ts}.jsonl"
        state.conversation_log._path.side_effect = lambda _key: legacy_path
        claim = _resolve_history_delete_claim(
            state.conversation_log,
            thread_ts,
            _capture_history_delete_claim(state, thread_ts),
        )

        await _remove_slot_for_history_key(state, thread_ts, delete_claim=claim)

        assert target.key not in state._slots
        state.sessions.destroy.assert_awaited_once_with(target.linked_session_key)

    @pytest.mark.asyncio
    async def test_bare_delete_preserves_slot_resolved_to_coexisting_canonical_file(self):
        thread_ts = "1785370133.085469"
        canonical = f"slack:{thread_ts}"
        target = _make_slot(f"slack_{thread_ts}")
        target.linked_session_key = canonical
        state = _make_state({target.key: target})
        legacy_path = Path("/history") / f"{thread_ts}.jsonl"
        canonical_path = Path("/history") / f"slack_{thread_ts}.jsonl"
        state.conversation_log._path.side_effect = lambda history_key: (
            legacy_path if history_key == thread_ts else canonical_path
        )
        claim = _resolve_history_delete_claim(
            state.conversation_log,
            thread_ts,
            _capture_history_delete_claim(state, thread_ts),
        )

        await _remove_slot_for_history_key(state, thread_ts, delete_claim=claim)

        assert state._slots[target.key] is target
        state.sessions.destroy_if.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_match_is_noop(self):
        state = _make_state({"chat-9-999": _make_slot("chat-9-999")})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "chat-9-999" in state._slots
        state.sessions.destroy.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "deleted_key",
        ("slack:team:direct:42", "slack_team_direct_42"),
        ids=("logical-key", "dashboard-list-stem"),
    )
    async def test_lossy_fold_does_not_remove_foreign_running_slot(
        self, deleted_key: str, monkeypatch: pytest.MonkeyPatch
    ):
        """A channel key and dashboard slot key can fold to one spelling while
        their transcripts remain distinct because the dashboard one is prefixed."""
        _guard_work_ledger_cleanup(monkeypatch)
        slot = _make_slot("slack_team_direct_42", running=True)
        state = _make_state({slot.key: slot})

        try:
            await _remove_slot_for_history_key(state, deleted_key)

            assert state._slots[slot.key] is slot
            assert not slot.task.done()
            state.sessions.destroy.assert_not_awaited()

            state.remove_chat_pins_for_slots.assert_not_awaited()
            state.sessions.drop_autocompact_overrides_matching.assert_not_called()
        finally:
            if not slot.task.done():
                slot.task.cancel()
                await asyncio.gather(slot.task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_same_slot_object_rerouted_after_claim_is_preserved(self):
        target = _make_slot("slack_team_direct_42")
        target.linked_session_key = "slack:team:direct:42"
        state = _make_state({target.key: target})
        claim = _capture_history_delete_claim(state, target.linked_session_key)

        # Cron/workflow adoption mutates an existing slot instead of replacing it.
        # Object identity therefore still matches even though ownership does not.
        target.linked_session_key = "workflow:successor"

        await _remove_slot_for_history_key(
            state,
            "slack:team:direct:42",
            delete_claim=claim,
        )

        assert state._slots[target.key] is target
        state.sessions.destroy_if.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_slot_successor_task_after_claim_is_preserved(self):
        target = _make_slot("slack_team_direct_42")
        target.linked_session_key = "slack:team:direct:42"
        state = _make_state({target.key: target})
        claim = _capture_history_delete_claim(state, target.linked_session_key)

        async def successor_turn():
            await asyncio.sleep(999)

        target.running = True
        target.task = asyncio.create_task(successor_turn())
        try:
            await _remove_slot_for_history_key(
                state,
                target.linked_session_key,
                delete_claim=claim,
            )

            assert state._slots[target.key] is target
            assert not target.task.done()
            state.sessions.destroy_if.assert_not_awaited()
        finally:
            target.task.cancel()
            await asyncio.gather(target.task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_successful_teardown_preserves_independent_sidecars(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _guard_work_ledger_cleanup(monkeypatch)
        target = _make_slot("slack_team_direct_42")
        target.linked_session_key = "slack:team:direct:42"
        state = _make_state({target.key: target})

        await _remove_slot_for_history_key(state, target.linked_session_key)

        assert target.key not in state._slots
        state.sessions.destroy.assert_awaited_once_with(target.linked_session_key)
        state.remove_chat_pins_for_slots.assert_not_awaited()
        state.sessions.drop_autocompact_overrides_matching.assert_not_called()

    @pytest.mark.asyncio
    async def test_replacement_sharing_effective_key_is_not_destroyed(self):
        target = _make_slot("slack_team_direct_42")
        target.linked_session_key = "slack:team:direct:42"
        replacement = _make_slot("replacement")
        replacement.linked_session_key = target.linked_session_key
        state = _make_state({target.key: target, replacement.key: replacement})

        await _remove_slot_for_history_key(state, target.linked_session_key)

        assert state._slots[replacement.key] is replacement
        state.sessions.destroy_if.assert_awaited_once()
        state.sessions.destroy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claimant_generation_replaced_after_claim_is_not_destroyed(self):
        target = _make_slot("slack_team_direct_42")
        target.linked_session_key = "slack:team:direct:42"
        state = _make_state({target.key: target})
        original_generation = state.sessions.session_generation.return_value
        claim = _capture_history_delete_claim(state, target.linked_session_key)

        state.sessions.session_generation.return_value = original_generation + 1
        await _remove_slot_for_history_key(
            state,
            target.linked_session_key,
            delete_claim=claim,
        )

        assert state._slots[target.key] is target
        state.sessions.destroy_if.assert_not_awaited()
        state.sessions.destroy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slotless_delete_preserves_independent_sidecars(self):
        state = _make_state({})

        await _remove_slot_for_history_key(state, "dashboard_cron-42")

        state.sessions.destroy_if.assert_not_awaited()
        state.remove_chat_pins_for_slots.assert_not_awaited()
        state.sessions.drop_autocompact_overrides_matching.assert_not_called()

    @pytest.mark.asyncio
    async def test_legacy_bare_alias_blocks_canonical_session_destroy(self):
        thread_ts = "1785370133.085469"
        target = _make_slot(f"slack_{thread_ts}")
        target.linked_session_key = f"slack:{thread_ts}"
        replacement = _make_slot("replacement")
        replacement.linked_session_key = thread_ts
        state = _make_state({target.key: target, replacement.key: replacement})
        canonical_path = Path("/history") / f"slack_{thread_ts}.jsonl"
        state.conversation_log._path.return_value = canonical_path
        claim = _resolve_history_delete_claim(
            state.conversation_log,
            target.linked_session_key,
            _capture_history_delete_claim(state, target.linked_session_key),
        )

        await _remove_slot_for_history_key(
            state,
            target.linked_session_key,
            delete_claim=claim,
        )

        assert state._slots[replacement.key] is replacement
        state.sessions.destroy_if.assert_awaited_once()
        state.sessions.destroy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_running_task_cancelled(self):
        slot = _make_slot("dashboard_chat-1-100", running=True)
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert slot.task.cancelled()
        state.sessions.destroy.assert_awaited_once_with("dashboard:chat-1-100")

    @pytest.mark.asyncio
    async def test_pending_question_cancelled_before_running_task(self):
        """History deletion must not leave a DashboardState-owned question
        future alive after its slot task and provider have been destroyed."""
        slot = _make_slot("dashboard_chat-1-100", running=True)
        state = _make_state({"dashboard_chat-1-100": slot})
        task_was_done: list[bool] = []

        def cancel_questions(slot_key: str) -> int:
            assert slot_key == slot.key
            task_was_done.append(slot.task.done())
            return 1

        state.cancel_questions_for_slot = MagicMock(side_effect=cancel_questions)

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        state.cancel_questions_for_slot.assert_called_once_with(slot.key)
        assert task_was_done == [False]
        assert slot.task.cancelled()

    @pytest.mark.asyncio
    async def test_non_running_task_not_cancelled(self):
        slot = _make_slot("dashboard_chat-1-100", running=False)
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert slot.task is None
        state.sessions.destroy.assert_awaited_once_with("dashboard:chat-1-100")

    @pytest.mark.asyncio
    async def test_stacked_dashboard_file_does_not_own_canonical_slot(self, tmp_path):
        stacked_key = "dashboard_dashboard_chat-3-300"
        canonical_key = "dashboard:chat-3-300"
        log = ConversationLog(base_dir=tmp_path)
        log.append(stacked_key, "user", "stacked")
        log.append(canonical_key, "user", "canonical")
        slot = _make_slot("chat-3-300")
        state = _make_state({slot.key: slot})
        state.conversation_log = log
        claim = _capture_history_delete_claim(state, stacked_key)

        deleted, claim = await asyncio.to_thread(
            _delete_history_session,
            log,
            stacked_key,
            claim,
        )
        await _remove_slot_for_history_key(state, stacked_key, delete_claim=claim)

        assert deleted is True
        assert not log.has_log(stacked_key)
        assert log.has_log(canonical_key)
        assert state._slots[slot.key] is slot
        state.sessions.destroy_if.assert_not_awaited()

    def test_outer_delete_lock_timeout_returns_false(self):
        state = _make_state({})
        claim = _capture_history_delete_claim(state, "dashboard_chat-3-300")
        log = MagicMock(spec=ConversationLog)
        log.locked_stems.side_effect = HistoryLockTimeout("wedged")

        deleted, returned_claim = _delete_history_session(
            log,
            "dashboard_chat-3-300",
            claim,
        )

        assert deleted is False
        assert returned_claim is claim
        log.delete_session.assert_not_called()

    def test_canonical_slack_delete_locks_both_stems(self, tmp_path, monkeypatch):
        thread_ts = "1785370133.085469"
        canonical = f"slack:{thread_ts}"
        canonical_stem = f"slack_{thread_ts}"
        log = ConversationLog(base_dir=tmp_path)
        log.append(thread_ts, "user", "legacy transcript")
        claim = _capture_history_delete_claim(_make_state({}), canonical)
        active: set[str] = set()
        real_locked = log._locked_stem
        real_delete = log.delete_session

        @contextlib.contextmanager
        def recording_lock(key: str):
            already_active = key in active
            with real_locked(key):
                active.add(key)
                try:
                    yield
                finally:
                    if not already_active:
                        active.remove(key)

        def observed_delete(key: str, *, skip_pinned: bool = False):
            assert {thread_ts, canonical_stem} <= active
            return real_delete(key, skip_pinned=skip_pinned)

        monkeypatch.setattr(log, "_locked_stem", recording_lock)
        monkeypatch.setattr(log, "delete_session", observed_delete)

        deleted, _claim = _delete_history_session(log, canonical, claim)

        assert deleted is True

    def test_locked_route_capture_binds_exact_runtime_owner(self, tmp_path):
        key = "dashboard_chat-3-300"
        slot = _make_slot(key)
        state = _make_state({key: slot})
        claim = _capture_history_delete_claim(state, key)
        log = ConversationLog(base_dir=tmp_path)
        log.append(key, "user", "hello")

        deleted, resolved_claim = _delete_history_session(log, key, claim)

        assert deleted is True
        assert resolved_claim.cron_owner_keys == frozenset({"dashboard:chat-3-300"})

    @pytest.mark.asyncio
    async def test_batch_clear_removes_multiple_slots(self):
        """Verify batch clear removes matched slots and leaves unmatched."""
        slot_a = _make_slot("chat-1-100")
        slot_b = _make_slot("chat-2-200", running=True)
        slot_c = _make_slot("chat-9-999")
        state = _make_state(
            {
                "chat-1-100": slot_a,
                "chat-2-200": slot_b,
                "chat-9-999": slot_c,
            }
        )
        # Simulate batch clear for two keys (one matched, one running)
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        await _remove_slot_for_history_key(state, "dashboard_chat-2-200")
        assert "chat-1-100" not in state._slots
        assert "chat-2-200" not in state._slots
        assert "chat-9-999" in state._slots  # unmatched stays
        assert state.sessions.destroy.await_count == 2

    @pytest.mark.asyncio
    async def test_reverse_prefix_lookup(self):
        """A reverse-prefix alias is valid when the slot owns the bare history."""
        slot = _make_slot("dashboard_chat-1-100")
        slot.linked_session_key = "chat-1-100"
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "chat-1-100")
        assert "dashboard_chat-1-100" not in state._slots
        state.sessions.destroy.assert_awaited_once_with("chat-1-100")

    @pytest.mark.asyncio
    async def test_sessions_remove_exception_does_not_propagate(self):
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.destroy = AsyncMock(side_effect=RuntimeError("already gone"))
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "dashboard_chat-1-100" not in state._slots


class TestChannelSlotTeardown:
    """Deleting a channel history must tear down the CHANNEL's session.

    A channel-born slot runs the channel's own session, so a key derived from
    the history key names a session that does not exist: the provider survives
    the delete and its next inbound message recreates the transcript the user
    just removed.
    """

    @pytest.mark.asyncio
    async def test_destroys_the_slots_own_session_not_a_derived_key(self):
        slot = _make_slot("slack_1785370133.085469")
        slot.linked_session_key = "slack:1785370133.085469"
        state = _make_state({"slack_1785370133.085469": slot})
        canonical_path = Path("/history/slack_1785370133.085469.jsonl")
        state.conversation_log._path.return_value = canonical_path
        claim = _resolve_history_delete_claim(
            state.conversation_log,
            "slack_1785370133.085469",
            _capture_history_delete_claim(state, "slack_1785370133.085469"),
        )

        await _remove_slot_for_history_key(
            state,
            "slack_1785370133.085469",
            delete_claim=claim,
        )

        state.sessions.destroy.assert_awaited_once_with("slack:1785370133.085469")
        assert "slack_1785370133.085469" not in state._slots


class TestPermanentDeleteReleasesCronOwnership:
    """A permanently deleted session must not strand the jobs it scheduled.

    ``cron_add`` stamps the creating session's key on the job, and the MCP
    ownership gate is equality on that key -- so once the session is gone for
    good the row is manageable by nobody. Release collapses that invisible state
    into the documented ownerless one the CLI and Schedule page already manage.
    """

    def _service(self, tmp_path):
        return CronService(base_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_releases_the_deleted_sessions_job(self, tmp_path):
        crons = self._service(tmp_path)
        job = crons.add_job("repro", "ping", every_secs=3600, session_key="dashboard:chat-1-100")
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        reloaded = CronService(base_dir=tmp_path).get_job(job.id)
        assert reloaded is not None
        assert reloaded.session_key == ""

    @pytest.mark.asyncio
    async def test_released_job_keeps_its_schedule_and_stays_enabled(self, tmp_path):
        crons = self._service(tmp_path)
        job = crons.add_job(
            "repro", "ping", cron_expr="0 9 * * 1-5", session_key="dashboard:chat-1-100"
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        reloaded = CronService(base_dir=tmp_path).get_job(job.id)
        assert reloaded is not None
        assert reloaded.enabled is True
        assert reloaded.schedule.cron_expr == "0 9 * * 1-5"
        assert reloaded.message == "ping"

    @pytest.mark.asyncio
    async def test_leaves_another_live_sessions_job_owned(self, tmp_path):
        crons = self._service(tmp_path)
        mine = crons.add_job("mine", "ping", every_secs=3600, session_key="dashboard:chat-1-100")
        theirs = crons.add_job(
            "theirs", "ping", every_secs=3600, session_key="dashboard:chat-9-999"
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(mine.id).session_key == ""
        assert reloaded.get_job(theirs.id).session_key == "dashboard:chat-9-999"

    @pytest.mark.asyncio
    async def test_deleting_a_channel_transcript_leaves_the_lookalike_dashboard_owner(
        self, tmp_path
    ):
        """No ``dashboard:`` owner spelling is derived for a non-dashboard key.

        The stem of a channel transcript such as ``slack_<ts>`` is the
        ``dashboard:slack_<ts>`` of a DIFFERENT session; deriving that spelling
        would release the jobs of that live dashboard row when only the channel
        thread is deleted.
        """
        crons = self._service(tmp_path)
        theirs = crons.add_job(
            "theirs", "ping", every_secs=3600, session_key="dashboard:slack_1785370133.085469"
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "slack_1785370133.085469")

        reloaded = CronService(base_dir=tmp_path).get_job(theirs.id)
        assert reloaded is not None
        assert reloaded.session_key == "dashboard:slack_1785370133.085469"

    @pytest.mark.asyncio
    async def test_deleting_the_dashboard_transcript_still_releases_its_own_owner(self, tmp_path):
        """The same stem under a ``dashboard_`` key releases its ``dashboard:`` owner."""
        crons = self._service(tmp_path)
        mine = crons.add_job(
            "mine", "ping", every_secs=3600, session_key="dashboard:slack_1785370133.085469"
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "dashboard_slack_1785370133.085469")

        reloaded = CronService(base_dir=tmp_path).get_job(mine.id)
        assert reloaded is not None
        assert reloaded.session_key == ""

    @pytest.mark.asyncio
    async def test_leaves_an_ownerless_job_untouched(self, tmp_path):
        crons = self._service(tmp_path)
        job = crons.add_job("cli-made", "ping", every_secs=3600)
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        reloaded = CronService(base_dir=tmp_path).get_job(job.id)
        assert reloaded is not None
        assert reloaded.session_key == ""

    @pytest.mark.asyncio
    async def test_releases_a_channel_sessions_job_by_its_own_key(self, tmp_path):
        crons = self._service(tmp_path)
        job = crons.add_job(
            "channel", "ping", every_secs=3600, session_key="slack:1785370133.085469"
        )
        slot = _make_slot("slack_1785370133.085469")
        slot.linked_session_key = "slack:1785370133.085469"
        state = _make_state({"slack_1785370133.085469": slot})
        state.crons = crons

        claim = replace(
            _capture_history_delete_claim(state, "slack_1785370133.085469"),
            path_match_verified=True,
        )
        await _remove_slot_for_history_key(
            state,
            "slack_1785370133.085469",
            delete_claim=claim,
        )

        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    @pytest.mark.asyncio
    async def test_slotless_live_manager_session_keeps_its_job_owned(self, tmp_path):
        owner_key = "slack:1785370133.085469"
        crons = self._service(tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=owner_key)
        state = _make_state({})
        state.crons = crons
        state.sessions.session_keys.return_value = frozenset({owner_key})

        await _remove_slot_for_history_key(
            state,
            "slack_1785370133.085469",
            exact_owner_keys=(owner_key,),
        )

        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == owner_key

    @pytest.mark.asyncio
    async def test_a_released_job_is_then_adoptable(self, tmp_path):
        crons = self._service(tmp_path)
        job = crons.add_job("repro", "ping", every_secs=3600, session_key="dashboard:chat-1-100")
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        adopter = CronService(base_dir=tmp_path)
        assert adopter.get_job(job.id).session_key == ""
        assert adopter.adopt_job(job.id, "dashboard:chat-2-200") is True
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == (
            "dashboard:chat-2-200"
        )

    @pytest.mark.asyncio
    async def test_a_missing_cron_service_does_not_break_the_delete(self, tmp_path):
        state = _make_state({"dashboard_chat-1-100": _make_slot("dashboard_chat-1-100")})
        state.crons = None

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert "dashboard_chat-1-100" not in state._slots

    def test_tab_close_does_not_reach_the_permanent_delete_funnel(self):
        """Closing a tab archives a session, so it must not release anything."""
        source = inspect.getsource(chat_handlers.api_chat_slot_delete)
        assert "_remove_slot_for_history_key" not in source


class TestReleaseIsOwnerConditioned:
    """The release must clear the owner it was told about, not whoever owns the
    job by the time the write lands.

    Releasing by iterating a snapshot and calling ``adopt_job(id, "")``
    unconditionally is a lost-update race: another surface (the CLI's
    ``cron adopt``, a cron-injected slot stamping its own key) can hand the job
    to a DIFFERENT session in the gap, and the unconditional write then unbinds
    a job from a session that legitimately owns it. The store's
    ``release_jobs_owned_by`` selects AFTER its in-lock reload, making the
    release a compare-and-clear.
    """

    class _StaleRow:
        """What a pre-lock snapshot hands back: the owner as of the last cache
        refresh, which a cross-process write has already superseded."""

        def __init__(self, job_id: str, session_key: str) -> None:
            self.id = job_id
            self.session_key = session_key

    @pytest.mark.asyncio
    async def test_a_job_re_adopted_since_the_snapshot_keeps_its_new_owner(
        self, tmp_path, monkeypatch
    ):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("repro", "ping", every_secs=3600, session_key="dashboard:chat-1-100")
        # Another surface re-owns the job through its OWN service instance, so
        # the write is on disk while this instance's cache still names the old
        # owner -- the staleness any pre-lock snapshot is subject to.
        other = CronService(base_dir=tmp_path)
        assert other.adopt_job(job.id, "dashboard:chat-2-200") is True
        monkeypatch.setattr(
            crons,
            "list_jobs",
            lambda include_disabled=False: [self._StaleRow(job.id, "dashboard:chat-1-100")],
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == (
            "dashboard:chat-2-200"
        )

    def test_a_failed_save_leaves_the_cache_agreeing_with_disk(self, tmp_path, monkeypatch):
        """An unwritable store must not release the job in memory only.

        ``_save`` serializes the job list, so the release has to mutate before
        persisting -- but if that write fails, memory saying "ownerless" while
        disk still names the owner makes every in-process ownership decision read
        a state that never happened, and the old owner comes back on restart.
        """
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("repro", "ping", every_secs=3600, session_key="dashboard:chat-1-100")

        def _boom() -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(crons, "_save", _boom)

        with pytest.raises(OSError):
            crons._release_jobs_owned_by_locked({"dashboard:chat-1-100"})

        assert crons.get_job(job.id).session_key == "dashboard:chat-1-100"
        monkeypatch.undo()
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == (
            "dashboard:chat-1-100"
        )


class TestAFailedRemovalDoesNotLeakIntoTheNextSave:
    """A removal whose ``_save`` failed must not be persisted by a later one.

    The removal filters the row out of ``self._jobs`` BEFORE persisting, so a
    failed write leaves the cache missing a row that is still on disk. Rolling the
    children's ownership back is not enough: while the fingerprint still says the
    cache agrees with disk, the next ``_sync`` skips its reload and the next
    ``_save`` serializes that cache -- writing out the removal the caller was told
    had failed, and completing the parent's removal while the children keep the
    ownership just restored.
    """

    @staticmethod
    def _boom() -> None:
        raise OSError(28, "No space left on device")

    def test_malformed_persisted_owner_does_not_poison_unrelated_removal(self, tmp_path):
        # The loader type-guards record fields at deserialization, so a
        # non-string session_key reads back as the guarded unset value "",
        # not as the stored list. What this test owns is the removal half:
        # dropping an unrelated job must leave the malformed record present
        # (guarded, not deleted) rather than poisoning or dropping it.
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("parent", "ping", every_secs=3600)
        malformed = crons.add_job("malformed", "ping", every_secs=3600)
        setattr(malformed, "session_key", ["not", "a", "string"])
        with crons._file_lock():
            crons._save()

        worker = CronService(base_dir=tmp_path)
        assert worker.remove_job(parent.id, actor="cli", source="cli") is True

        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(parent.id) is None
        assert reloaded.get_job(malformed.id) is not None
        assert reloaded.get_job(malformed.id).session_key == ""

    def test_a_failed_removal_is_not_persisted_by_an_unrelated_later_save(
        self, tmp_path, monkeypatch
    ):
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("parent", "ping", every_secs=3600)
        child = crons.add_job("child", "ping", every_secs=3600, session_key=f"cron:{parent.id}")
        other = crons.add_job("other", "ping", every_secs=3600)

        monkeypatch.setattr(crons, "_save", self._boom)
        with pytest.raises(OSError):
            crons._remove_job_locked(parent.id)
        monkeypatch.undo()

        # The child's ownership is rolled back in the cache...
        assert crons.get_job(child.id).session_key == f"cron:{parent.id}"
        # ...and an unrelated later removal must not carry the parent's out with it.
        crons._remove_job_locked(other.id)

        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(parent.id) is not None
        assert reloaded.get_job(child.id).session_key == f"cron:{parent.id}"
        assert reloaded.get_job(other.id) is None

    def test_the_batch_and_owner_removal_paths_fail_the_same_way(self, tmp_path, monkeypatch):
        """The three removal paths share the rollback, so they share the reload."""
        for label, remove in (
            ("batch", lambda crons, job: crons._remove_jobs_locked([job.id])),
            ("owner", lambda crons, job: crons._remove_jobs_by_owner_locked("anapp")),
        ):
            crons = CronService(base_dir=tmp_path / label)
            job = crons.add_job("victim", "ping", every_secs=3600, created_by="anapp")
            other = crons.add_job("other", "ping", every_secs=3600)

            monkeypatch.setattr(crons, "_save", self._boom)
            with pytest.raises(OSError):
                remove(crons, job)
            monkeypatch.undo()

            crons._remove_job_locked(other.id)

            reloaded = CronService(base_dir=tmp_path / label)
            assert reloaded.get_job(job.id) is not None, label


class TestALiveCronPrincipalKeepsItsJobs:
    """A ``cron:<job id>`` owner is not retired by deleting a transcript.

    A cron's result conversation is linked to that key, so it reaches the funnel's
    owner candidates — but every future run of the job presents the same key, so
    releasing the jobs that cron created would leave a LIVE owner unable to list,
    update or remove its own work.
    """

    @pytest.mark.asyncio
    async def test_a_still_scheduled_cron_keeps_owning_the_jobs_it_created(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        owner = crons.add_job("nightly", "check the queue", every_secs=3600)
        # The nightly job's own turn scheduled a follow-up, stamped with the
        # session key a cron run presents.
        created = crons.add_job(
            "follow-up", "ping", every_secs=3600, session_key=f"cron:{owner.id}"
        )
        # Deleting the nightly job's RESULT transcript, whose slot is linked to
        # that same key.
        slot = _make_slot(f"cron-{owner.id}")
        slot.linked_session_key = f"cron:{owner.id}"
        state = _make_state({f"cron-{owner.id}": slot})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{owner.id}", exact_owner_keys=(f"cron:{owner.id}",)
        )

        assert CronService(base_dir=tmp_path).get_job(created.id).session_key == (
            f"cron:{owner.id}"
        )

    @pytest.mark.asyncio
    async def test_a_retired_crons_key_is_still_released(self, tmp_path):
        """Once the owning job is gone its key can never be presented again."""
        crons = CronService(base_dir=tmp_path)
        stranded = crons.add_job("follow-up", "ping", every_secs=3600, session_key="cron:deadbeef")
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, "cron_deadbeef", exact_owner_keys=("cron:deadbeef",)
        )

        assert CronService(base_dir=tmp_path).get_job(stranded.id).session_key == ""

    @pytest.mark.asyncio
    async def test_a_cron_the_cli_deleted_is_dead_even_while_the_cache_lists_it(self, tmp_path):
        """Liveness must be read from the store, not from the cache.

        ``list_jobs`` is cache-only with up to one timer-poll interval of
        cross-process staleness. A CLI ``cron remove`` inside that window leaves
        this service still listing the job, so a cached liveness check calls the
        dead principal live and skips releasing its children — and nothing re-runs
        the delete funnel, so they strand with no warning.
        """
        crons = CronService(base_dir=tmp_path)
        owner = crons.add_job("nightly", "check the queue", every_secs=3600)
        child = crons.add_job("follow-up", "ping", every_secs=3600, session_key=f"cron:{owner.id}")
        # Another process (the CLI) removes the owning job. This service's cache
        # has not observed it.
        cli = CronService(base_dir=tmp_path)
        assert cli.remove_job(owner.id, actor="cli", source="cli") is True
        assert {j.id for j in crons.list_jobs(include_disabled=True)} == {owner.id, child.id}

        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{owner.id}", exact_owner_keys=(f"cron:{owner.id}",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_per_run_execution_keys_resolve_to_their_job(self):
        """``cron:<job id>:<run id>`` names the job, not a principal of its own."""
        assert cron_job_id_from_session_key("cron:abc123") == "abc123"
        assert cron_job_id_from_session_key("cron:abc123:run-7") == "abc123"
        assert cron_job_id_from_session_key("dashboard:chat-1-100") == ""
        assert cron_job_id_from_session_key("slack:1785370133.085469") == ""

    def test_the_release_path_shares_the_stores_one_key_parser(self):
        """A second parser in cron.py or its service owners could drift from the MCP
        surface's reading."""
        import importlib
        import pkgutil

        from kiro_crew.cron_service import service

        source = "".join(
            inspect.getsource(module)
            for module in (
                cron_module,
                *(
                    importlib.import_module(f"{service.__name__}.{info.name}")
                    for info in pkgutil.iter_modules(service.__path__)
                ),
            )
        )
        assert source.count("def cron_job_id_from_session_key") == 1
        assert "def _cron_job_id_from_session_key" not in source


class TestOwnerSpellingsOfOneCronPrincipal:
    """A cron principal is stamped under several spellings, all naming one job.

    ``build_cron_session_context`` mints ``cron:<job>`` for a persistent job and
    ``cron:<job>:<run id>`` for a stateless one, and the sequential-agent path
    mints ``cron:<job>:<agent>``. A job the run creates carries whichever the run
    presented, so a release holding only the two-segment form must still reach it.
    """

    def test_matcher_folds_cron_spellings_and_keeps_others_exact(self):
        assert cron_owner_matches("cron:P", "cron:P") is True
        assert cron_owner_matches("cron:P:agentA", "cron:P") is True
        assert cron_owner_matches("cron:P:1f0e-uuid", "cron:P") is True
        assert cron_owner_matches("cron:P", "cron:P:agentA") is True
        assert cron_owner_matches("cron:P:agentA", "cron:P:agentB") is True
        # Different principals, and cron vs non-cron, never fold together.
        assert cron_owner_matches("cron:P", "cron:Q") is False
        assert cron_owner_matches("cron:P", "dashboard:chat-1-100") is False
        assert cron_owner_matches("dashboard:chat-1-100", "cron:P") is False
        # Non-cron owners keep ONE spelling each and compare exactly.
        assert cron_owner_matches("dashboard:chat-1-100", "dashboard:chat-1-100") is True
        assert cron_owner_matches("dashboard:chat-1-100", "dashboard:chat-1-101") is False

    @pytest.mark.asyncio
    async def test_an_agent_sequence_child_is_released_with_its_retired_parent(self, tmp_path):
        """``cron:<parent>:<agent>`` is a DURABLE spelling of the parent's key."""
        crons = CronService(base_dir=tmp_path)
        child = crons.add_job(
            "follow-up", "ping", every_secs=3600, session_key="cron:deadbeef:researcher"
        )
        state = _make_state({})
        state.crons = crons

        # The delete funnel only ever holds the two-segment form.
        await _remove_slot_for_history_key(
            state, "cron_deadbeef", exact_owner_keys=("cron:deadbeef",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    @pytest.mark.asyncio
    async def test_a_stateless_run_child_is_released_with_its_retired_parent(self, tmp_path):
        """``cron:<parent>:<run id>`` is the ephemeral spelling of the same key."""
        crons = CronService(base_dir=tmp_path)
        child = crons.add_job(
            "follow-up",
            "ping",
            every_secs=3600,
            session_key="cron:deadbeef:3f2a1c88-0000-4000-8000-000000000001",
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, "cron_deadbeef", exact_owner_keys=("cron:deadbeef",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    @pytest.mark.asyncio
    async def test_a_live_parent_keeps_its_three_segment_children_owned(self, tmp_path):
        """Spelling tolerance must not outrun the liveness check."""
        crons = CronService(base_dir=tmp_path)
        owner = crons.add_job("nightly", "check the queue", every_secs=3600)
        child = crons.add_job(
            "follow-up", "ping", every_secs=3600, session_key=f"cron:{owner.id}:researcher"
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{owner.id}", exact_owner_keys=(f"cron:{owner.id}",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == (
            f"cron:{owner.id}:researcher"
        )

    @pytest.mark.asyncio
    async def test_another_crons_children_are_left_alone(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        # Both parents must really exist: the add guard refuses to stamp an owner
        # whose cron is absent (see TestAChildCannotBeBornUnderADeadParent), and
        # the survivor here has to be a LIVE, stable principal to stay owned.
        mine_parent = crons.add_job(
            "mine-parent", "sweep", every_secs=3600, agent_sequence=["agentA", "agentB"]
        )
        theirs_parent = crons.add_job(
            "theirs-parent", "sweep", every_secs=3600, agent_sequence=["agentA", "agentB"]
        )
        mine = crons.add_job(
            "mine", "ping", every_secs=3600, session_key=f"cron:{mine_parent.id}:agentA"
        )
        theirs = crons.add_job(
            "theirs", "ping", every_secs=3600, session_key=f"cron:{theirs_parent.id}:agentA"
        )
        assert crons.remove_job(mine_parent.id, actor="cli", source="cli") is True
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{mine_parent.id}", exact_owner_keys=(f"cron:{mine_parent.id}",)
        )

        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(mine.id).session_key == ""
        assert reloaded.get_job(theirs.id).session_key == f"cron:{theirs_parent.id}:agentA"

    @pytest.mark.asyncio
    async def test_the_stranded_warning_names_three_segment_children(self, caplog):
        """A release that cannot land must still name the child it could not free."""
        crons = _BusyCrons("76ef369f", "cron:deadbeef:researcher")
        state = _make_state({})
        state.crons = crons

        with caplog.at_level(logging.WARNING):
            await _remove_slot_for_history_key(
                state, "cron_deadbeef", exact_owner_keys=("cron:deadbeef",)
            )

        assert crons.release_attempts == _CRON_RELEASE_ATTEMPTS
        assert "76ef369f" in caplog.text
        assert "FAILED" in caplog.text


class TestRemovingACronReleasesItsChildren:
    """Removing a cron retires its principal, so its children must be released.

    The history-delete funnel deliberately SKIPS a live cron owner, so a
    transcript deleted BEFORE the cron is removed leaves nothing behind to notice
    later — the removal itself is the only place that can close the gap. Without
    the cascade the child keeps firing under a key no session can present:
    `cron_list` omits it, `cron_update` / `cron_remove` answer "job not found".
    """

    def _parent_and_child(self, tmp_path, child_owner_suffix: str = ""):
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("nightly", "check the queue", every_secs=3600)
        child = crons.add_job(
            "follow-up",
            "ping",
            every_secs=3600,
            session_key=f"cron:{parent.id}{child_owner_suffix}",
        )
        return crons, parent, child

    @pytest.mark.asyncio
    async def test_transcript_delete_then_parent_removal_releases_the_child(self, tmp_path):
        """The exact ordering the funnel cannot cover on its own."""
        crons, parent, child = self._parent_and_child(tmp_path)
        state = _make_state({})
        state.crons = crons

        # Step 1: the cron's own transcript is deleted while the cron is LIVE.
        # The funnel correctly leaves the child owned.
        await _remove_slot_for_history_key(
            state, f"cron_{parent.id}", exact_owner_keys=(f"cron:{parent.id}",)
        )
        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == (f"cron:{parent.id}")

        # Step 2: the cron is removed. Its principal is now retired.
        assert crons.remove_job(parent.id, actor="cli", source="cli") is True

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_remove_job_releases_a_three_segment_child(self, tmp_path):
        crons, parent, child = self._parent_and_child(tmp_path, ":researcher")

        assert crons.remove_job(parent.id, actor="cli", source="cli") is True

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_batch_removal_releases_children(self, tmp_path):
        """`cron_remove_all` lands in the batch core, not the single-job one."""
        crons, parent, child = self._parent_and_child(tmp_path)
        removed, missing = crons.remove_jobs_sync([parent.id], actor="mcp", source="mcp")

        assert removed == [parent.id] and missing == []
        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_app_owner_teardown_releases_children(self, tmp_path):
        """An app's cron can own jobs outside the app; uninstall retires it."""
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("app-nightly", "sweep", every_secs=3600, created_by="app:demo")
        child = crons.add_job("follow-up", "ping", every_secs=3600, session_key=f"cron:{parent.id}")

        assert crons.remove_jobs_by_owner_sync("app:demo") == [parent.id]

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_deferred_one_shot_removal_releases_children(self, tmp_path):
        """A Done()/delete_after_run self-removal deferred to a tick still cascades."""
        crons, parent, child = self._parent_and_child(tmp_path)
        crons.defer_removal(parent.id)

        with crons._file_lock():
            crons._sync()
            drained = crons._drain_pending_removals_locked()

        assert drained == [parent.id]
        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_removal_leaves_an_unrelated_jobs_owner_alone(self, tmp_path):
        crons, parent, child = self._parent_and_child(tmp_path)
        other = crons.add_job("theirs", "ping", every_secs=3600, session_key="dashboard:chat-9-999")
        sibling_cron = crons.add_job("sibling", "tick", every_secs=3600)
        sibling_child = crons.add_job(
            "sibling-follow-up", "ping", every_secs=3600, session_key=f"cron:{sibling_cron.id}"
        )

        assert crons.remove_job(parent.id, actor="cli", source="cli") is True

        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(child.id).session_key == ""
        assert reloaded.get_job(other.id).session_key == "dashboard:chat-9-999"
        assert reloaded.get_job(sibling_child.id).session_key == f"cron:{sibling_cron.id}"

    def test_a_failed_save_rolls_the_child_release_back(self, tmp_path, monkeypatch):
        crons, parent, child = self._parent_and_child(tmp_path)

        def _boom() -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(crons, "_save", _boom)

        with pytest.raises(OSError):
            crons.remove_job(parent.id, actor="cli", source="cli")

        assert crons.get_job(child.id).session_key == f"cron:{parent.id}"
        monkeypatch.undo()
        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(parent.id) is not None
        assert reloaded.get_job(child.id).session_key == f"cron:{parent.id}"

    def test_a_failed_save_rolls_the_deferred_drain_back(self, tmp_path, monkeypatch):
        """The drain's rollback must cover a BARE OSError, not only unreadable.

        ``_save`` raises ``CronStoreUnreadable`` for a store it refuses to write
        over, but plain ``OSError`` for a store it cannot write AT ALL (ENOSPC,
        EROFS, EIO out of ``atomic_write``). A narrow ``except CronStoreUnreadable``
        skipped the rollback for that second class entirely: memory said
        "ownerless" while disk still named the owner, the queue stayed empty, and
        the fingerprint still matched the untouched file so nothing reloaded the
        truth back -- until the next successful save persisted the release.
        """
        crons, parent, child = self._parent_and_child(tmp_path)
        crons.defer_removal(parent.id)

        def _boom() -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(crons, "_save", _boom)

        with pytest.raises(OSError):
            with crons._file_lock():
                crons._sync()
                crons._drain_pending_removals_locked()

        # In-memory ownership is back to exactly the pre-removal state.
        assert crons.get_job(child.id).session_key == f"cron:{parent.id}"
        # And the delete intent is requeued, not dropped on the floor.
        assert parent.id in crons._pending_removals

        # The next SUCCESSFUL save must not persist the release the failed one
        # abandoned -- an unrelated add is the cheapest real writer to prove it.
        monkeypatch.undo()
        crons.add_job("unrelated", "ping", every_secs=3600)

        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(parent.id) is not None
        assert reloaded.get_job(child.id).session_key == f"cron:{parent.id}"

    def test_an_unreadable_store_still_holds_the_deferred_drain(self, tmp_path, monkeypatch):
        """The tolerated class keeps its old contract: no raise, requeued, rolled back."""
        crons, parent, child = self._parent_and_child(tmp_path)
        crons.defer_removal(parent.id)

        def _boom() -> None:
            raise cron_module.CronStoreUnreadable("crons.json is not readable")

        monkeypatch.setattr(crons, "_save", _boom)

        with crons._file_lock():
            crons._sync()
            drained = crons._drain_pending_removals_locked()

        assert drained == []
        assert crons.get_job(child.id).session_key == f"cron:{parent.id}"
        assert parent.id in crons._pending_removals


class TestOnlyASTABLELiveKeyRetainsOwnership:
    """Existence of the parent row is not enough to keep a child owned.

    A stateless job mints ``cron:<job id>:<uuid4>`` fresh per fire, so the key its
    last run stamped on a child is ALREADY unpresentable while the parent is still
    scheduled. Retaining that ownership strands the child exactly as a removed
    parent would, and the shape of the key cannot tell you which case you are in:
    a durable sequential-agent key and an ephemeral per-run key are both three
    segments.
    """

    @pytest.mark.asyncio
    async def test_a_stateless_parents_child_is_released(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job(
            "stateless-nightly", "sweep", every_secs=3600, persistent_session=False
        )
        assert cron_session_key_is_stable(parent) is False
        child = crons.add_job(
            "follow-up",
            "ping",
            every_secs=3600,
            session_key=f"cron:{parent.id}:3f2a1c88-0000-4000-8000-000000000001",
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{parent.id}", exact_owner_keys=(f"cron:{parent.id}",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    @pytest.mark.asyncio
    async def test_old_stateless_run_keeps_every_live_runs_child_owned(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job(
            "stateless-nightly", "sweep", every_secs=3600, persistent_session=False
        )
        deleted_key = f"cron:{parent.id}:deleted-run"
        pending_key = f"cron:{parent.id}:pending-run"
        current_key = f"cron:{parent.id}:current-run"
        deleted_child = crons.add_job(
            "deleted-child", "ping", every_secs=3600, session_key=deleted_key
        )
        pending_child = crons.add_job(
            "pending-child", "ping", every_secs=3600, session_key=pending_key
        )
        current_child = crons.add_job(
            "current-child", "ping", every_secs=3600, session_key=current_key
        )
        crons.register_active_session_key(parent.id, pending_key)
        crons.register_active_session_key(parent.id, current_key)

        released = await crons.release_jobs_owned_by({deleted_key})

        reloaded = CronService(base_dir=tmp_path)
        assert released == [deleted_child.id]
        assert reloaded.get_job(deleted_child.id).session_key == ""
        assert reloaded.get_job(pending_child.id).session_key == pending_key
        assert reloaded.get_job(current_child.id).session_key == current_key

    @pytest.mark.asyncio
    async def test_release_rechecks_live_keys_after_waiting_for_the_store_lock(
        self, tmp_path, monkeypatch
    ):
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job(
            "stateless-nightly", "sweep", every_secs=3600, persistent_session=False
        )
        deleted_key = f"cron:{parent.id}:deleted-run"
        deleted_child = crons.add_job(
            "deleted-child", "ping", every_secs=3600, session_key=deleted_key
        )
        live: dict[str, str] = {}
        real_release = crons._release_jobs_owned_by_locked

        def register_and_persist_before_release(*args, **kwargs):
            live_key = f"cron:{parent.id}:new-live-run"
            crons.register_active_session_key(parent.id, live_key)
            child = crons.add_job("live-child", "ping", every_secs=3600, session_key=live_key)
            live.update(key=live_key, child=child.id)
            return real_release(*args, **kwargs)

        monkeypatch.setattr(
            crons,
            "_release_jobs_owned_by_locked",
            register_and_persist_before_release,
        )

        released = await crons.release_jobs_owned_by({deleted_key})

        reloaded = CronService(base_dir=tmp_path)
        assert released == [deleted_child.id]
        assert reloaded.get_job(live["child"]).session_key == live["key"]

    @pytest.mark.asyncio
    async def test_a_persistent_parents_child_stays_owned(self, tmp_path):
        """The positive control: a stable key is still presentable, so keep it."""
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job(
            "persistent-nightly", "sweep", every_secs=3600, persistent_session=True
        )
        assert cron_session_key_is_stable(parent) is True
        child = crons.add_job("follow-up", "ping", every_secs=3600, session_key=f"cron:{parent.id}")
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{parent.id}", exact_owner_keys=(f"cron:{parent.id}",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == (f"cron:{parent.id}")

    @pytest.mark.asyncio
    async def test_an_agent_sequence_parents_child_stays_owned(self, tmp_path):
        """A sequential-agent key is three segments but DURABLE, so keep it."""
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job(
            "seq-nightly",
            "sweep",
            every_secs=3600,
            persistent_session=False,
            agent_sequence=["researcher", "writer"],
        )
        assert cron_session_key_is_stable(parent) is True
        child = crons.add_job(
            "follow-up", "ping", every_secs=3600, session_key=f"cron:{parent.id}:researcher"
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{parent.id}", exact_owner_keys=(f"cron:{parent.id}",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == (
            f"cron:{parent.id}:researcher"
        )


class TestAChildCannotBeBornUnderADeadParent:
    """The add side closes the window the removal cascade cannot see.

    The cascade only releases children that existed when it scanned. A run of the
    parent still in flight can call ``cron_add`` between that scan and its own
    teardown, so the guard has to live in the locked ADD transaction — resolved
    against the same reloaded store, which is what makes the two cover each other.
    """

    def test_add_drops_an_owner_whose_cron_is_gone(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("nightly", "sweep", every_secs=3600)
        assert crons.remove_job(parent.id, actor="cli", source="cli") is True

        child = crons.add_job(
            "late-follow-up", "ping", every_secs=3600, session_key=f"cron:{parent.id}"
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_add_drops_a_three_segment_owner_whose_cron_is_gone(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("nightly", "sweep", every_secs=3600)
        assert crons.remove_job(parent.id, actor="cli", source="cli") is True

        child = crons.add_job(
            "late-follow-up",
            "ping",
            every_secs=3600,
            session_key=f"cron:{parent.id}:researcher",
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_add_keeps_an_owner_whose_cron_is_alive(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("nightly", "sweep", every_secs=3600)

        child = crons.add_job("follow-up", "ping", every_secs=3600, session_key=f"cron:{parent.id}")

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == (f"cron:{parent.id}")

    def test_add_leaves_non_cron_owners_alone(self, tmp_path):
        """A dashboard or channel owner names no cron row to look up."""
        crons = CronService(base_dir=tmp_path)

        job = crons.add_job(
            "from-chat", "ping", every_secs=3600, session_key="dashboard:chat-1-100"
        )

        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == (
            "dashboard:chat-1-100"
        )

    @pytest.mark.parametrize("owner", [None, ""], ids=["none", "empty"])
    def test_add_accepts_a_falsy_owner_without_parsing_it(self, tmp_path, owner):
        """A FALSY owner is ownerless, not a malformed cron key.

        ``session_key`` is an optional caller-supplied field, so the create path's
        falsy-skip persists ``None`` on the row (``cron_add`` from the CLI, the
        onboarding importer, an explicit ``session_key=None``). The guard runs on
        every add, so parsing that value unconditionally makes the store's own
        ownerless case raise ``AttributeError: 'NoneType' has no attribute
        'startswith'`` -- which is what
        ``test_cron_string_field_validation.py::TestFalsySkipSemantics`` caught.
        """
        crons = CronService(base_dir=tmp_path)

        job = crons.add_job("unowned", "ping", every_secs=3600, session_key=owner)

        assert not CronService(base_dir=tmp_path).get_job(job.id).session_key

    @pytest.mark.parametrize("owner", [None, ""], ids=["none", "empty"])
    def test_the_shared_parser_reads_a_falsy_key_as_a_non_cron_owner(self, owner):
        """The guard belongs at the ONE parser, so every consumer inherits it.

        The add-guard, the release path's liveness check and
        ``cron_owner_matches`` all resolve a principal through this function; a
        per-call-site ``if not key`` would be three chances to miss one.
        """
        assert cron_job_id_from_session_key(owner) == ""
        assert cron_owner_matches(owner, "cron:76ef369f") is False

    @pytest.mark.parametrize("append_path", ["add", "add-if-absent"])
    def test_dead_parent_keeps_captured_member_memory_before_owner_release(
        self, tmp_path, monkeypatch, append_path
    ):
        """A late child keeps its creator's captured memory after becoming ownerless."""
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.execution_context import bind_session_execution, execution_from_record
        from kiro_crew.memory_stores import provision_member_memory

        config = KiroCrewConfig()
        config.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        store = provision_member_memory(config, "writer")
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: config))

        crons = CronService(base_dir=tmp_path)
        member_id = config.agents["writer"].member_id
        parent = crons.add_job("nightly", "sweep", every_secs=3600, member_id=member_id)
        execution = execution_from_record({"execution_context": parent.execution_context})
        bind_session_execution(f"cron:{parent.id}", execution)
        live = crons.add_job(
            "live-follow-up",
            "ping",
            every_secs=3600,
            session_key=f"cron:{parent.id}",
        )
        live_binding = (live.memory_store, live.member_id)
        assert live_binding == (store, member_id)
        assert crons.remove_job(parent.id, actor="cli", source="cli") is True

        kwargs = {
            "name": f"late-follow-up-{append_path}",
            "message": "ping",
            "every_secs": 3600,
            "session_key": f"cron:{parent.id}",
        }
        if append_path == "add":
            late = crons.add_job(**kwargs)
        else:
            late = crons.add_job_if_absent(lambda job: job.name == kwargs["name"], **kwargs)
            assert late is not None

        persisted = CronService(base_dir=tmp_path).get_job(late.id)
        assert persisted is not None
        # The binding is asserted first: releasing the dead owner must not turn
        # the creator's private schedule into a Global Memory V1 schedule.
        assert (persisted.memory_store, persisted.member_id) == live_binding
        assert persisted.execution_context == execution.to_record()
        assert persisted.session_key == ""


class TestDeferredRemovalTickWriteFault:
    def test_tick_keeps_scanning_after_deferred_removal_save_fault(self, tmp_path, monkeypatch):
        """A requeued ENOSPC removal must not starve every other scheduled job."""
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("parent", "sweep", every_secs=3600)
        child = crons.add_job("child", "ping", every_secs=3600, session_key=f"cron:{parent.id}")
        other = crons.add_job("other", "ping", every_secs=3600)
        crons.defer_removal(parent.id)

        def _disk_full() -> None:
            raise OSError(28, "No space left on device")

        with monkeypatch.context() as patched:
            patched.setattr(crons, "_save", _disk_full)
            snapshot = crons._tick_scan_locked()

        assert {job.id for job in snapshot} == {child.id, other.id}
        assert parent.id in crons._pending_removals
        assert crons.get_job(child.id).session_key == f"cron:{parent.id}"

        crons._tick_scan_locked()
        persisted = CronService(base_dir=tmp_path)
        assert persisted.get_job(parent.id) is None
        assert persisted.get_job(child.id).session_key == ""
        assert not crons._pending_removals


class _BusyCrons:
    """A cron store whose lock never frees, on both release surfaces.

    Answers ``CronStoreBusy`` for the batch release AND for a per-id
    ``adopt_job``, so the test discriminates on the funnel's BEHAVIOUR (does it
    retry, does it report) rather than on which method it happens to call.
    """

    def __init__(self, job_id: str, session_key: str, *, busy_for: int | None = None) -> None:
        self._job_id = job_id
        self._session_key = session_key
        self._busy_for = busy_for
        self.release_attempts = 0
        self.adopt_attempts = 0

    def list_jobs(self, include_disabled: bool = False) -> list[Any]:
        return [TestReleaseIsOwnerConditioned._StaleRow(self._job_id, self._session_key)]

    async def release_jobs_owned_by(self, owner_keys: Collection[str]) -> list[str]:
        self.release_attempts += 1
        if self._busy_for is not None and self.release_attempts > self._busy_for:
            self._session_key = ""
            return [self._job_id]
        raise CronStoreBusy("cron store lock held")

    def adopt_job(self, job_id: str, session_key: str) -> bool:
        self.adopt_attempts += 1
        raise CronStoreBusy("cron store lock held")


class TestReleaseSurvivesAndSurfacesLockContention:
    """``CronStoreBusy`` is ordinary lock contention, not a reason to give up.

    Nothing re-runs this funnel and the deleted session can never present its
    key again, so a dropped release strands the job permanently -- the exact
    state the release exists to prevent. Contention is retried, and a release
    that still will not land is reported loudly instead of swallowed.
    """

    @pytest.mark.asyncio
    async def test_transient_contention_is_retried_until_it_lands(self, caplog):
        crons = _BusyCrons("76ef369f", "dashboard:chat-1-100", busy_for=2)
        state = _make_state({})
        state.crons = crons

        with caplog.at_level(logging.WARNING):
            await _remove_slot_for_history_key(
                state,
                "dashboard_chat-1-100",
                exact_owner_keys=("dashboard:chat-1-100",),
            )

        assert crons.release_attempts == 3
        assert crons._session_key == ""
        assert "FAILED" not in caplog.text

    @pytest.mark.asyncio
    async def test_sustained_contention_is_surfaced_not_swallowed(self, caplog):
        crons = _BusyCrons("76ef369f", "dashboard:chat-1-100")
        state = _make_state({})
        state.crons = crons

        with caplog.at_level(logging.WARNING):
            await _remove_slot_for_history_key(
                state,
                "dashboard_chat-1-100",
                exact_owner_keys=("dashboard:chat-1-100",),
            )

        assert crons.release_attempts == 3
        # The warning has to name what is stranded and who owns it -- an operator
        # cannot release a job by hand from a message that names neither.
        assert "76ef369f" in caplog.text
        assert "dashboard:chat-1-100" in caplog.text
        assert "FAILED" in caplog.text
