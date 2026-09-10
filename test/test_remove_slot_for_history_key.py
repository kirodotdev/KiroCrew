"""Tests for _remove_slot_for_history_key in handlers.py."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import session_ledger
from kiro_crew.dashboard.handlers import _remove_slot_for_history_key
from kiro_crew.dashboard.handlers.sessions import (
    _capture_history_delete_claim,
    _delete_history_session,
    _resolve_history_delete_claim,
)
from kiro_crew.history import ConversationLog, HistoryLockTimeout


def _make_state(slots: dict) -> MagicMock:
    state = MagicMock()
    state._slots = dict(slots)
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.destroy = AsyncMock()
    session_generation = 1
    state.sessions.session_generation = MagicMock(return_value=session_generation)
    state.sessions.session_keys = MagicMock(return_value=set())

    async def destroy_if(
        key: str,
        expected_generation: object,
        should_destroy,
        *,
        preserve_autocompact_override: bool = False,
    ) -> bool:
        assert preserve_autocompact_override is True
        if expected_generation != state.sessions.session_generation.return_value:
            return False
        if not should_destroy():
            return False
        await state.sessions.destroy(key)
        return True

    state.sessions.destroy_if = AsyncMock(side_effect=destroy_if)
    state.sessions.drop_autocompact_overrides_matching = MagicMock(return_value=0)
    state.remove_chat_pins_for_slots = AsyncMock()
    state.conversation_log = MagicMock()
    return state


def _make_slot(key: str, running: bool = False) -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.running = running
    # A real slot is unbound unless its conversation lives on another session.
    # Left unset, a bare MagicMock hands back a truthy Mock as the session key,
    # so the teardown would target something that is not a key at all.
    slot.linked_session_key = ""
    slot.channel_origin = False
    if running:

        async def _hang():
            await asyncio.sleep(999)

        slot.task = asyncio.ensure_future(_hang())
    else:
        slot.task = None
    return slot


def _guard_work_ledger_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("history deletion must preserve work ledgers")

    monkeypatch.setattr(session_ledger, "purge", unexpected)
    monkeypatch.setattr(session_ledger, "purge_matching", unexpected)


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
