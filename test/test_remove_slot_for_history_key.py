"""Tests for _remove_slot_for_history_key in handlers.py."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Collection, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew import cron as cron_module
from kiro_crew import session_ledger
from kiro_crew.cron import (
    CronService,
    CronStoreBusy,
    CronStoreUnreadable,
    cron_job_id_from_session_key,
    cron_owner_matches,
    cron_session_key_is_stable,
)
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.handlers import (
    _remove_slot_for_history_key as _remove_slot_for_history_key_impl,
)
from kiro_crew.dashboard.handlers import (
    api_session_delete,
    api_sessions_clear,
)
from kiro_crew.dashboard.handlers import sessions as sessions_module
from kiro_crew.dashboard.handlers.sessions import (
    _CRON_RELEASE_ATTEMPTS,
    CRON_OWNERSHIP_UNKNOWN_CODE,
    CRON_STORE_BUSY_CODE,
    CRON_STORE_UNREADABLE_CODE,
    _capture_history_delete_claim,
    _delete_history_session,
    _HistoryDeleteClaim,
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
    state.crons = None
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


async def _remove_slot_for_history_key(
    state: MagicMock,
    key: str,
    *,
    delete_claim: _HistoryDeleteClaim | None = None,
    exact_owner_keys: Collection[str] = (),
) -> None:
    """Drive teardown with the same exact owner claim production captures."""
    claim = delete_claim or _capture_history_delete_claim(state, key)
    owner_keys = set(claim.cron_owner_keys)
    owner_keys.update(owner for owner in exact_owner_keys if owner)
    crons = getattr(state, "crons", None)
    if crons is not None and not exact_owner_keys:
        swept = await sessions_module._owner_keys_bound_to_transcript(crons, (key,))
        owner_keys.update(swept.get(key, ()))
    if owner_keys != set(claim.cron_owner_keys):
        claim = replace(claim, cron_owner_keys=frozenset(owner_keys))
    await _remove_slot_for_history_key_impl(state, key, delete_claim=claim)


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
        assert reloaded.get_job(malformed.id).session_key == ["not", "a", "string"]

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


class TestAJobBornDuringTheDeleteIsNotStranded:
    """An add that commits mid-delete is caught by the post-delete pass.

    The pre-delete sweep sees only what exists when it scans, and a channel
    ``cron_add`` commits under its own store lock. The store's add-time guard
    resolves a ``cron:`` parent id, so a channel-keyed owner passes it untouched
    and the row lands stamped with the key this delete retires.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @pytest.mark.asyncio
    async def test_an_add_between_the_sweep_and_the_unlink_ends_ownerless(
        self, tmp_path, monkeypatch
    ):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        real_delete = sessions_module._delete_history_session
        born: dict[str, str] = {}

        def _add_then_delete(
            log_arg,
            key,
            claim,
            *,
            skip_pinned=False,
            exact_owner_keys=(),
        ):
            # The seam itself: the add commits after the sweep has read the store
            # and before the unlink retires the key.
            if not born:
                job = crons.add_job("late", "ping", every_secs=3600, session_key=channel_key)
                born["id"] = job.id
            return real_delete(
                log_arg,
                key,
                claim,
                skip_pinned=skip_pinned,
                exact_owner_keys=exact_owner_keys,
            )

        monkeypatch.setattr(sessions_module, "_delete_history_session", _add_then_delete)

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert born, "the interleaved add never ran, so the window was not exercised"
        assert CronService(base_dir=tmp_path).get_job(born["id"]).session_key == ""

    @pytest.mark.asyncio
    async def test_an_unreadable_store_after_the_delete_names_the_gap(
        self, tmp_path, monkeypatch, caplog
    ):
        """The row is gone, so the post-pass cannot refuse -- it must say so."""
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        calls = {"n": 0}
        real_sweep = sessions_module._owner_keys_bound_to_transcript

        async def _second_pass_blind(crons_arg, keys):
            calls["n"] += 1
            if calls["n"] == 1:
                return await real_sweep(crons_arg, keys)
            raise CronStoreUnreadable("crons.json is not readable")

        monkeypatch.setattr(sessions_module, "_owner_keys_bound_to_transcript", _second_pass_blind)

        with caplog.at_level(logging.WARNING):
            resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert calls["n"] == 2
        assert "post-delete owner scan could not read" in caplog.text
        assert "kirocrew cron adopt" in caplog.text

    @pytest.mark.asyncio
    async def test_the_bulk_clear_catches_an_add_between_its_sweep_and_the_unlink(
        self, tmp_path, monkeypatch
    ):
        """The bulk twin: ONE pre-sweep, a per-row unlink loop, then ONE re-sweep.

        The window is wider here than on the single delete (one scan fronts the
        whole loop), so an add that commits after the scan and before its row is
        unlinked is stamped with a key the clear retires. The batched post-unlink
        pass must release it -- leaving it to by-id recovery strands a job the
        clear itself opened the window for.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(history_key, "")
        log.list_sessions = lambda: [{"key": history_key}]  # type: ignore[method-assign]
        request = self._request(self._state(crons, log))

        real_delete = sessions_module._delete_history_session
        born: dict[str, str] = {}

        def _add_then_delete(
            log_arg,
            key,
            claim,
            *,
            skip_pinned=False,
            exact_owner_keys=(),
        ):
            # The seam itself: the batch sweep has read the store; the unlink has
            # not yet retired this row. Same interleaving as the single delete.
            if not born:
                job = crons.add_job("late", "ping", every_secs=3600, session_key=channel_key)
                born["id"] = job.id
            return real_delete(
                log_arg,
                key,
                claim,
                skip_pinned=skip_pinned,
                exact_owner_keys=exact_owner_keys,
            )

        monkeypatch.setattr(sessions_module, "_delete_history_session", _add_then_delete)

        resp = await api_sessions_clear(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (body["ok"], body["cleared"], body["failed"]) == (True, 1, 0)
        assert born, "the interleaved add never ran, so the window was not exercised"
        assert CronService(base_dir=tmp_path).get_job(born["id"]).session_key == ""

    @pytest.mark.asyncio
    async def test_the_bulk_clear_re_sweeps_once_for_the_whole_batch(self, tmp_path, monkeypatch):
        """Two lock acquisitions for N rows, not N + 1: the post-pass is batched too."""
        keys = [f"slack_178537013{i}.085469" for i in range(3)]
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(keys[0], "")
        for key in keys[1:]:
            log._meta[key] = {}
        log.list_sessions = lambda: [{"key": key} for key in keys]  # type: ignore[method-assign]
        request = self._request(self._state(crons, log))

        seen: list[list[str]] = []
        real_sweep = sessions_module._owner_keys_bound_to_transcript

        async def _counting_sweep(crons_arg, sweep_keys):
            seen.append(sorted(sweep_keys))
            return await real_sweep(crons_arg, sweep_keys)

        monkeypatch.setattr(sessions_module, "_owner_keys_bound_to_transcript", _counting_sweep)

        resp = await api_sessions_clear(request)

        assert json.loads(resp.body.decode("utf-8"))["cleared"] == 3
        # The pre-sweep takes the selection; the post-pass takes what was unlinked.
        assert seen == [sorted(keys), sorted(keys)]

    @pytest.mark.asyncio
    async def test_an_unreadable_store_after_the_bulk_clear_names_the_gap(
        self, tmp_path, monkeypatch, caplog
    ):
        """The rows are gone, so the post-pass cannot refuse -- it must say so."""
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, "")
        log.list_sessions = lambda: [{"key": history_key}]  # type: ignore[method-assign]
        request = self._request(self._state(crons, log))

        calls = {"n": 0}
        real_sweep = sessions_module._owner_keys_bound_to_transcript

        async def _second_pass_blind(crons_arg, sweep_keys):
            calls["n"] += 1
            if calls["n"] == 1:
                return await real_sweep(crons_arg, sweep_keys)
            raise CronStoreUnreadable("crons.json is not readable")

        monkeypatch.setattr(sessions_module, "_owner_keys_bound_to_transcript", _second_pass_blind)

        with caplog.at_level(logging.WARNING):
            resp = await api_sessions_clear(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"], body["cleared"], body["failed"]) == (200, True, 1, 0)
        assert calls["n"] == 2
        # What the pre-sweep found is still released; only the late arrival is named.
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""
        assert "post-delete owner scan could not read" in caplog.text
        assert history_key in caplog.text
        assert "cron adopt <id> --release" in caplog.text


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
        """A second parser in cron.py could drift from the MCP surface's reading."""
        source = inspect.getsource(cron_module)
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
    def test_dead_parent_keeps_private_memory_binding_before_owner_release(
        self, tmp_path, monkeypatch, append_path
    ):
        """A late child keeps its creator's private memory after becoming ownerless."""
        from kiro_crew import member_memory_auth
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.memory_stores import provision_member_memory

        config = KiroCrewConfig()
        config.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        store = provision_member_memory(config, "writer")
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: config))
        monkeypatch.setattr(member_memory_auth, "read_private_session_store", lambda _key: store)

        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("nightly", "sweep", every_secs=3600)
        live = crons.add_job(
            "live-follow-up",
            "ping",
            every_secs=3600,
            session_key=f"cron:{parent.id}",
        )
        live_binding = (live.memory_store, live.member_id)
        assert live_binding == (store, "writer")
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


class _FakeTranscriptStore:
    """A conversation log whose metadata dies with the transcript.

    ``get_metadata_status`` answers ``({}, True)`` once the row is unlinked,
    exactly as the real store does when the file is gone -- so a funnel that
    reads ``linked_session_key`` after the delete gets nothing, which is the
    defect these tests guard.

    ``unreadable=True`` / ``raises=True`` model the OTHER failure: the row is
    still THERE but its metadata cannot be read (a partial write, a prior
    ENOSPC, corruption) -- the real store reports that as ``({}, False)`` after
    exhausting its retries, and raises outright on a decode it cannot even
    attempt. A falsy ``linked_session_key`` makes the row's metadata empty: a
    readable row that simply names no linked session.

    ``malformed=<payload>`` models the third failure: the read SUCCEEDS and
    reports itself readable, but hands back something that is not the mapping the
    store's contract promises (or a mapping whose ``linked_session_key`` is not a
    string). Reachable from a salvaged line, a partial write, or an agent-edited
    transcript -- the store file is agent-writable. It doubles as the way to model
    a FORGED metadata line, since "an agent wrote this" is the same fact either
    way.
    """

    def __init__(
        self,
        key: str,
        linked_session_key: str,
        *,
        unreadable: bool = False,
        raises: bool = False,
        malformed: Any = None,
    ) -> None:
        meta = {"linked_session_key": linked_session_key} if linked_session_key else {}
        self._meta: dict[str, Any] = {key: meta if malformed is None else malformed}
        self.calls: list[str] = []
        self._unreadable = unreadable
        self._raises = raises

    @contextlib.contextmanager
    def _locked(self, key: str) -> Iterator[None]:
        """Reentrant no-op stand-in for the real history lock."""
        self.calls.append(f"locked:{key}")
        yield

    @contextlib.contextmanager
    def locked_stems(self, stems: Collection[str]) -> Iterator[None]:
        """Stand in for ConversationLog's canonical-plus-legacy lock set."""
        ordered = tuple(stems)
        self.calls.append(f"locked_stems:{','.join(ordered)}")
        with self._locked(ordered[0] if ordered else ""):
            yield

    def get_metadata_status(self, key: str) -> tuple[Any, bool]:
        self.calls.append(f"get_metadata:{key}")
        if key in self._meta:
            if self._raises:
                raise json.JSONDecodeError("bad first line", "", 0)
            if self._unreadable:
                return {}, False
        payload = self._meta.get(key, {})
        # Copy only a mapping -- a malformed payload is handed back as-is,
        # exactly as a salvaged line would reach the caller.
        return (dict(payload) if isinstance(payload, dict) else payload), True

    def get_metadata(self, key: str) -> Any:
        return self.get_metadata_status(key)[0]

    def delete_session(self, key: str, *, skip_pinned: bool = False) -> bool:
        self.calls.append(f"delete_session:{key}")
        existed = key in self._meta
        self._meta.pop(key, None)
        return existed


class TestALinkedKeyMustNameItsOwnTranscript:
    """The metadata link is untrusted INPUT to a privileged action.

    ``slot_history_key`` returns ``linked_session_key`` verbatim as the slot's
    transcript key, so a row carrying a link must BE that session's transcript.
    The transcript store is agent-writable, and whatever the link names is handed
    to ``release_jobs_owned_by`` as a retired owner -- so an unvalidated link lets
    an agent point its OWN row at a victim session's key, delete its own row, and
    have the funnel clear the victim's cron ownership. The history key comes from
    the route, not from the file, so the claim is checkable against the row it is
    stored on.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @pytest.mark.asyncio
    async def test_a_foreign_linked_key_is_dropped_not_honoured(self, tmp_path, caplog):
        """The attack: attacker's own row, victim's key in its metadata."""
        victim_key = "slack:1785370133.085469"
        attacker_history_key = "dashboard_chat-9-999"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=victim_key)
        log = _FakeTranscriptStore(attacker_history_key, victim_key)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": attacker_history_key}

        with caplog.at_level(logging.WARNING):
            resp = await api_session_delete(request)

        # The attacker deletes its OWN row -- always within its reach -- but the
        # claim is dropped rather than honoured: the victim keeps its job.
        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert victim_key in caplog.text and "not honoured" in caplog.text
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == victim_key
        assert f"delete_session:{attacker_history_key}" in log.calls

    @pytest.mark.asyncio
    async def test_another_channel_threads_key_is_dropped_too(self, tmp_path):
        """Valid-SHAPED is not valid: a real channel key for a DIFFERENT thread."""
        victim_key = "slack:1785370133.085469"
        attacker_history_key = "slack_9999999999.000001"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=victim_key)
        log = _FakeTranscriptStore(attacker_history_key, victim_key)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": attacker_history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{attacker_history_key}" in log.calls
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == victim_key

    @pytest.mark.parametrize(
        "history_key,linked",
        [
            pytest.param(
                "slack_1785370133.085469", "slack:1785370133.085469", id="canonical-channel-stem"
            ),
            pytest.param(
                "1785370133.085469", "slack:1785370133.085469", id="legacy-bare-thread-ts"
            ),
            pytest.param("cron_76ef369f", "cron:76ef369f", id="cron-born-tab"),
            pytest.param(
                "discord_kirocrew_direct_123",
                "discord:kirocrew:direct:123",
                id="multi-colon-channel-key",
            ),
            pytest.param(
                "dashboard_chat-1-100", "dashboard:chat-1-100", id="dashboard-session-own-key"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_link_naming_its_own_transcript_proceeds(self, tmp_path, history_key, linked):
        """Every spelling the store legitimately produces for THIS row still deletes.

        The rule is a stem match, not string equality: ``_safe_key`` folds every
        ``:`` to ``_`` (so a multi-colon channel key must fold whole, not on its
        first colon), and a Slack thread older than the canonical key logs under
        its bare ``thread_ts``. Each case is a spelling of the row's OWN
        transcript; a name that merely LOOKS like another session's is
        :meth:`test_a_leftover_shaped_name_cannot_claim_the_session_it_resembles`.
        """
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("owned", "ping", every_secs=3600, session_key=linked)
        log = _FakeTranscriptStore(history_key, linked)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls
        # Proceeding means the link was HONOURED, not merely tolerated.
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    @pytest.mark.parametrize(
        "history_key",
        [
            pytest.param("dashboard_slack_1785370133.085469", id="pre-migration-leftover-tab-file"),
            pytest.param(
                "dashboard_dashboard_slack_1785370133.085469", id="stacked-dashboard-prefix"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_leftover_shaped_name_cannot_claim_the_session_it_resembles(
        self, tmp_path, history_key
    ):
        """A leftover-shaped row is not the channel session it is spelled like.

        ``dashboard_slack_<ts>`` really is the shape of a pre-migration leftover
        tab whose conversation IS ``slack:<ts>`` -- and that is exactly why the
        claim cannot be honoured: the transcript is agent-writable, so a forged
        ``linked_session_key`` on a lookalike file is indistinguishable from the
        genuine leftover, and honouring either releases a LIVE session's jobs. The
        row keeps the strand instead, recoverable by id.
        """
        linked = "slack:1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=linked)
        log = _FakeTranscriptStore(history_key, linked)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        # The row DELETES (200): a leftover must not be permanently undeletable,
        # and the mismatch reads identically on every retry, so a refusal here
        # could never be cleared by the remedy the 409 names. The claim is still
        # not honoured -- the session it resembles keeps its job.
        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == linked

    @pytest.mark.asyncio
    async def test_a_dashboard_rows_stripped_stem_is_not_a_linkable_target(self, tmp_path, caplog):
        """No stem survives a ``dashboard_`` strip, channel-shaped or not.

        ``dashboard_chat-1-100`` does not accept a link to ``chat-1-100``: the
        linkable set is the canonical stem of the row alone, so an ordinary
        dashboard row is covered by the same rule as a channel-shaped one rather
        than by a separate gate that could be reasoned about independently. The
        claim is dropped with a warning and the row deletes; whatever the delete
        then releases comes from the keys derived from the row itself
        (``chat-1-100`` IS the slot key of this row), never from the claim.
        """
        history_key = "dashboard_chat-1-100"
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(history_key, "chat-1-100")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with caplog.at_level(logging.WARNING):
            resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls
        assert "not honoured" in caplog.text and "chat-1-100" in caplog.text

    @pytest.mark.asyncio
    async def test_the_bulk_clear_deletes_a_leftover_shaped_row_without_releasing(self, tmp_path):
        """The bulk twin of the leftover case: cleared, not skipped, nothing released."""
        linked = "slack:1785370133.085469"
        history_key = "dashboard_slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=linked)
        log = _FakeTranscriptStore(history_key, linked)
        log.list_sessions = lambda: [{"key": history_key}]  # type: ignore[method-assign]
        request = self._request(self._state(crons, log))

        resp = await api_sessions_clear(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (body["cleared"], body["skipped"], body["undeletable"]) == (1, 0, [])
        assert f"delete_session:{history_key}" in log.calls
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == linked

    @pytest.mark.asyncio
    async def test_the_bulk_clear_names_an_unreadable_row_by_id_and_code(self, tmp_path):
        """A refused row is reported, not folded into ``skipped`` beside pinned rows.

        The single delete answers 409 ``cron_ownership_unknown`` for this row; the
        clear cannot refuse one row of a batch, so it names the row with the same
        code and leaves it intact -- a bare ``skipped: N`` hid which rows could
        never be cleared, and why.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key, unreadable=True)
        log.list_sessions = lambda: [{"key": history_key}]  # type: ignore[method-assign]
        request = self._request(self._state(crons, log))

        resp = await api_sessions_clear(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"], body["cleared"], body["skipped"]) == (200, True, 0, 0)
        assert body["undeletable"] == [{"id": history_key, "code": CRON_OWNERSHIP_UNKNOWN_CODE}]
        assert f"delete_session:{history_key}" not in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key


class _SweepBlockedCrons:
    """A store whose strict owner scan never lands.

    ``owner_keys_async`` is the only read the owner sweep makes, so answering
    ``CronStoreBusy`` from it is what an unseeable store looks like from the
    funnel. Counts attempts so the test can assert the shared bounded backoff was
    spent before the refusal rather than the first failure being fatal.

    The method it answers on matters: this fake is only faithful because the REAL
    ``owner_keys_async`` raises here too. ``list_jobs_async``, which the sweep
    used before, degrades instead — it swallows ``CronStoreBusy`` and returns the
    cache — so a fake raising from THAT method tested a refusal the store could
    never trigger. See ``TestTheOwnerSweepMustReadTheStoreStrictly``, which drives
    the same refusal through a real ``CronService`` under a real held flock.
    """

    def __init__(self) -> None:
        self.attempts = 0

    async def owner_keys_async(self) -> set[str]:
        self.attempts += 1
        raise CronStoreBusy("cron store lock held")

    def list_jobs(self, include_disabled: bool = False) -> list[Any]:
        return []


class TestAnUnrecordedChannelOwnerIsFoundFromTheStore:
    """Absent ``linked_session_key`` does not mean "no owner to lose".

    Two states have a channel-owned job and no such metadata, and neither is an
    error: the ORDERING WINDOW (``cron_add`` stamps the channel key in one
    transaction, the slot save publishes ``linked_session_key`` in another, and a
    delete can land between them) and a LEGACY transcript written before the
    metadata existed. Absent is also the ordinary state of every dashboard
    session, so refusing is not an available answer -- the binding is resolved
    from the STORE side instead, where each job carries the exact owner key and
    ``transcript_stems`` says which transcript filenames that key occupies.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @pytest.mark.asyncio
    async def test_an_unrecorded_channel_owner_is_still_released(self, tmp_path):
        """The gap: the job is owned, the transcript never recorded by whom.

        The row matched here is the channel transcript itself, whose own stem IS
        the owner's stem -- an identity, not an inference. The leftover TAB file
        (``dashboard_slack_<ts>``) is deliberately NOT covered: reaching the owner
        from that name means stripping a prefix to name a DIFFERENT session, which
        nothing durable can license (see
        :class:`TestAStrippedNameCannotReleaseAnotherSessionsJobs`).
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        # No linked_session_key on the row -- the ordering window, or a legacy row.
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    @pytest.mark.asyncio
    async def test_the_sweep_finds_the_owner_of_a_legacy_bare_thread_ts_row(self, tmp_path):
        """A Slack thread older than the canonical key logs under its bare stem.

        ``ConversationLog._path`` falls back to the bare ``thread_ts`` filename, so
        ``slack:<ts>`` legitimately occupies ``<ts>.jsonl`` -- an identity, not an
        inference, and the reason this matches on every stem the OWNER occupies
        rather than its canonical one alone. Matching canonically only would answer
        "no owner" for a row that has one, and the delete would then destroy the
        last record of the binding and strand the job silently.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""
        """The stem cannot be un-folded, so the exact key must come off the job.

        ``_safe_key`` maps EVERY ``:`` to ``_``, so ``discord_kirocrew_direct_123``
        has several possible session keys and the funnel cannot reconstruct the
        right one. The store already holds it.
        """
        channel_key = "discord:kirocrew:direct:123"
        history_key = "discord_kirocrew_direct_123"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("dm", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    @pytest.mark.asyncio
    async def test_an_ordinary_dashboard_delete_releases_nothing_extra(self, tmp_path):
        """The sweep must not widen what a delete touches.

        Absent metadata is the NORMAL state here, and the jobs in the store belong
        to other conversations. A sweep that matched loosely would clear ownership
        the delete has no claim on -- which is the defect it exists to prevent,
        pointed the other way.
        """
        history_key = "dashboard_chat-1-100"
        crons = CronService(base_dir=tmp_path)
        theirs = crons.add_job(
            "theirs", "ping", every_secs=3600, session_key="dashboard:chat-9-999"
        )
        channel = crons.add_job(
            "channel", "ping", every_secs=3600, session_key="slack:1785370133.085469"
        )
        unowned = crons.add_job("cli", "ping", every_secs=3600)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(theirs.id).session_key == "dashboard:chat-9-999"
        assert reloaded.get_job(channel.id).session_key == "slack:1785370133.085469"
        assert reloaded.get_job(unowned.id).session_key == ""

    @pytest.mark.asyncio
    async def test_a_sweep_that_cannot_run_refuses_the_delete(self, tmp_path, caplog):
        """An unseeable store is not an empty one.

        The transcript is the only place the ownership could still be noticed, so
        deleting it while the store cannot be read strands the job silently. A 409
        naming contention, and the row survives to retry.
        """
        history_key = "slack_1785370133.085469"
        crons = _SweepBlockedCrons()
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with caplog.at_level(logging.WARNING):
            resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert body["ok"] is False
        assert (resp.status, body["code"]) == (409, CRON_STORE_BUSY_CODE)
        assert "retry" in body["error"]
        # NOT unlinked -- the transcript is still the only record of the binding.
        assert f"delete_session:{history_key}" not in log.calls
        assert crons.attempts == _CRON_RELEASE_ATTEMPTS

    @pytest.mark.asyncio
    async def test_a_missing_cron_service_still_deletes(self, tmp_path):
        """No store to sweep is not a failed sweep."""
        history_key = "slack_1785370133.085469"
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(None, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls


class TestAStrippedNameCannotReleaseAnotherSessionsJobs:
    """Reaching a FOREIGN session's owner from this row's NAME is never done.

    The store-side sweep matches the row's own transcript stem. It does NOT strip
    the ``dashboard_`` prefix to reach ``slack_<ts>``, even though
    ``dashboard_slack_<ts>`` is a real shape for a pre-migration leftover tab that
    IS the ``slack:<ts>`` conversation -- because deciding that from the name
    alone releases a LIVE channel session's jobs when an unrelated dashboard row
    is deleted for merely being spelled like it (``POST /api/chat/slots`` takes a
    client-chosen slot name).

    Gating the fold on a marker in the transcript does not fix it. Every candidate
    marker -- ``linked_session_key``, ``channel_origin`` -- lives on the metadata
    line of an AGENT-WRITABLE file, so an agent that can forge the lookalike
    transcript can forge the marker on it and the attack returns unchanged. And no
    server-side record can stand in: the absent-metadata case IS the slotless one,
    so there is no live slot whose provenance could be consulted.

    So the terminal rule: provenance for a destructive CROSS-SESSION action must
    come from outside agent-writable storage, and where none exists the action is
    not inferred. The cost is real and bounded -- a genuinely channel-born
    leftover with no metadata keeps its job owned by a session that is gone -- and
    it is RECOVERABLE by id through ``kirocrew cron adopt <id> --release``, which
    the funnel names in a warning. A forged release is not recoverable.

    Validating a PRESENT ``linked_session_key`` is a different question, and that
    check stands -- but on the CANONICAL stem only, for the reason two paragraphs
    up: admitting the stripped form would make the claim a marker-gated fold, and
    a marker-gated fold is as reachable as an ungated one. Compared against the
    route-derived history key alone, the check can only ever NARROW what a row
    reaches.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @pytest.mark.parametrize(
        "history_key",
        [
            pytest.param("dashboard_slack_1785370133.085469", id="one-prefix"),
            pytest.param("dashboard_dashboard_slack_1785370133.085469", id="stacked-prefix"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_lookalike_dashboard_row_cannot_release_a_channels_jobs(
        self, tmp_path, history_key
    ):
        """The attack: a dashboard session NAMED like the victim's thread."""
        victim_key = "slack:1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=victim_key)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        # The attacker's OWN row goes -- it owns nothing, so there is nothing to
        # refuse. What must not happen is the victim losing its job.
        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == victim_key

    @pytest.mark.parametrize(
        "forged",
        [
            pytest.param({"channel_origin": True}, id="forged-channel-origin"),
            pytest.param(
                {"channel_origin": True, "tab_id": "tab-7"}, id="forged-marker-with-tab-id"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_forged_provenance_marker_does_not_re_enable_the_attack(self, tmp_path, forged):
        """The reason no transcript marker can gate this.

        The metadata line lives in the same agent-writable file as the row's name.
        An agent that can create ``dashboard_slack_<ts>.jsonl`` can write
        ``channel_origin`` onto it, so a marker-gated fold is exactly as reachable
        as an ungated one -- it only adds a step. Nothing here may consult it.
        """
        victim_key = "slack:1785370133.085469"
        history_key = "dashboard_slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=victim_key)
        log = _FakeTranscriptStore(history_key, "", malformed=dict(forged))
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == victim_key

    @pytest.mark.asyncio
    async def test_the_bulk_clear_is_gated_the_same_way(self, tmp_path):
        """One selector, one sweep -- and the same refusal to infer on both."""
        victim_key = "slack:1785370133.085469"
        history_key = "dashboard_slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=victim_key)
        log = _FakeTranscriptStore(history_key, "")
        log.list_sessions = lambda: [{"key": history_key}]  # type: ignore[method-assign]
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}

        resp = await api_sessions_clear(request)

        assert json.loads(resp.body.decode("utf-8"))["cleared"] == 1
        assert CronService(base_dir=tmp_path).get_job(victim_job.id).session_key == victim_key

    @pytest.mark.asyncio
    async def test_a_genuinely_channel_born_leftover_strands_loudly(self, tmp_path, caplog):
        """The price of the rule, paid where an operator can see it.

        This row really is the ``slack:<ts>`` conversation's leftover tab file, and
        its job really is orphaned by the delete. The funnel cannot tell it from
        the forged case above -- that indistinguishability is the point -- so it
        declines to release and says so, naming the owner, the candidate job id,
        and the command that finishes the job by hand. Silence here would be the
        actual defect: an unrecoverable strand is one nobody was told about.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "dashboard_slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with caplog.at_level(logging.WARNING):
            resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key
        assert channel_key in caplog.text
        assert job.id in caplog.text
        assert "kirocrew cron adopt" in caplog.text

    @pytest.mark.asyncio
    async def test_a_present_link_cannot_release_the_session_it_resembles(self, tmp_path):
        """A claim is checked, and a leftover-shaped claim does not pass the check.

        The value is compared against the route-derived history key on the
        CANONICAL stem only, so a row named like a channel thread cannot reach
        that thread's session by carrying a link to it -- which is what makes the
        agent-writable metadata unable to widen anything.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "dashboard_slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        # Deleted, not refused -- but the claim is dropped, so the session it
        # resembles keeps its job.
        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key

    @pytest.mark.asyncio
    async def test_a_lookalike_row_still_releases_its_own_owner(self, tmp_path):
        """Dropping the fold must not narrow the row's own stem.

        A dashboard row named like a channel thread still owns whatever is stamped
        with ITS key. Only the victim's job is withheld.
        """
        history_key = "dashboard_slack_1785370133.085469"
        own_key = "dashboard:slack_1785370133.085469"
        victim_key = "slack:1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        mine = crons.add_job("mine", "ping", every_secs=3600, session_key=own_key)
        victim_job = crons.add_job("victim", "ping", every_secs=3600, session_key=victim_key)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(mine.id).session_key == ""
        assert reloaded.get_job(victim_job.id).session_key == victim_key


def _break_the_owner_read(crons: CronService) -> None:
    """Make the strict owner scan raise something the funnel never named.

    Not ``CronStoreBusy`` and not ``CronStoreUnreadable`` -- an ``OSError`` off
    the read, the shape a defect or an unanticipated environment failure takes.
    Only ``owner_keys_async`` is replaced; the store underneath is real, so the
    release path still runs and the test reads what stayed owned off the disk.
    """

    async def _broken() -> set[str]:
        raise OSError("EIO reading cron store")

    crons.owner_keys_async = _broken  # type: ignore[method-assign]


class _SweepUnreadableCrons:
    """A store whose strict owner scan answers the store's OWN unreadable verdict."""

    async def owner_keys_async(self) -> set[str]:
        raise cron_module.CronStoreUnreadable("refusing: the last load could not read crons.json")

    def list_jobs(self, include_disabled: bool = False) -> list[Any]:
        return []


class TestTheOwnerSweepFailsByCause:
    """Each way the sweep cannot answer has its own outcome, and each is visible.

    The store's own two ways of saying "you cannot see me" REFUSE, 409 with a
    code and remedy specific to the cause: a busy store wants a retry, an
    unreadable one wants the file repaired, and unreadable transcript metadata
    wants the by-id release. The dashboard client keys its message on the code,
    so the codes are the contract, not the English.

    An UNEXPECTED exception is different in kind. It is not the store's verdict
    but a defect or an environment failure the sweep never named, on a read that
    fronts EVERY history delete and the bulk clear -- refusing on it would let one
    cron-read fault block all history deletion. So the unknown case proceeds
    LOUDLY: the delete goes ahead, nothing is released on the strength of a read
    that did not happen, and the WARNING names the session, the exception and the
    by-id recovery. A job left owned by a departed session is bounded and
    recoverable by id; the alternative is unbounded.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @pytest.mark.asyncio
    async def test_an_unexpected_reader_exception_deletes_and_names_the_recovery(
        self, tmp_path, caplog
    ):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        _break_the_owner_read(crons)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with caplog.at_level(logging.WARNING):
            resp = await api_session_delete(request)

        assert (resp.status, json.loads(resp.body.decode("utf-8"))) == (200, {"ok": True})
        assert f"delete_session:{history_key}" in log.calls
        # Nothing is released on a read that did not happen: the job stays owned
        # by the departed session -- bounded, and recoverable by id.
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        loud = [w for w in warnings if "owner sweep failed unexpectedly" in w]
        assert loud, warnings
        assert history_key in loud[0] and "OSError" in loud[0]
        assert "kirocrew cron adopt <id> --release" in loud[0]

    @pytest.mark.asyncio
    async def test_a_busy_store_refuses_with_its_own_code(self, tmp_path):
        history_key = "slack_1785370133.085469"
        crons = _SweepBlockedCrons()
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"], body["code"]) == (409, False, CRON_STORE_BUSY_CODE)
        assert "retry" in body["error"]
        assert f"delete_session:{history_key}" not in log.calls

    @pytest.mark.asyncio
    async def test_an_unreadable_store_refuses_with_its_own_code(self, tmp_path):
        history_key = "slack_1785370133.085469"
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(_SweepUnreadableCrons(), log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"], body["code"]) == (409, False, CRON_STORE_UNREADABLE_CODE)
        # The store's own wording rides along; the by-id CLI reads the same file,
        # so it is deliberately not the remedy offered here.
        assert "could not read crons.json" in body["error"]
        assert "adopt" not in body["error"]
        assert f"delete_session:{history_key}" not in log.calls

    @pytest.mark.asyncio
    async def test_unreadable_metadata_refuses_with_its_own_code_and_the_job_ids(self, tmp_path):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key, unreadable=True)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"], body["code"]) == (409, False, CRON_OWNERSHIP_UNKNOWN_CODE)
        # Here the remedy IS the CLI, and the store-side sweep already bound the
        # row to its owner, so the refusal can name the job to release.
        assert "kirocrew cron adopt <id> --release" in body["error"]
        assert job.id in body["error"]
        assert f"delete_session:{history_key}" not in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key

    @pytest.mark.asyncio
    async def test_the_bulk_clear_refuses_the_whole_batch_by_cause(self, tmp_path):
        """The batch takes ONE sweep, so a store that cannot be seen refuses all of it."""
        history_key = "slack_1785370133.085469"
        crons = _SweepBlockedCrons()
        log = _FakeTranscriptStore(history_key, "")
        log.list_sessions = lambda: [{"key": history_key}]  # type: ignore[method-assign]
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}

        resp = await api_sessions_clear(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"], body["code"]) == (409, False, CRON_STORE_BUSY_CODE)
        assert (body["cleared"], body["failed"]) == (0, 0)
        assert f"delete_session:{history_key}" not in log.calls

    @pytest.mark.asyncio
    async def test_the_bulk_clear_proceeds_loudly_on_an_unexpected_error(self, tmp_path, caplog):
        """One cron-read fault does not block the clear; it is named instead."""
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        _break_the_owner_read(crons)
        log = _FakeTranscriptStore(history_key, "")
        log.list_sessions = lambda: [{"key": history_key}]  # type: ignore[method-assign]
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}

        with caplog.at_level(logging.WARNING):
            resp = await api_sessions_clear(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"], body["cleared"]) == (200, True, 1)
        assert f"delete_session:{history_key}" in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key
        assert "owner sweep failed unexpectedly" in caplog.text
        assert "kirocrew cron adopt <id> --release" in caplog.text


class TestTheOwnerSweepMustReadTheStoreStrictly:
    """The sweep's read has to RAISE on a store it cannot see, not answer empty.

    The defect class is a read that flattens an unreadable store into an empty
    answer. The sweep's whole premise is that the store knows an owner the
    transcript never recorded — so a read that reports "no owners" for a store it
    could not read hands the funnel the one answer it must not accept, and the
    delete then destroys the last record of a binding nobody checked.

    Both cache-shaped reads do exactly that, and neither raises:

    * ``list_jobs`` is cache-only, up to a timer poll behind a cross-process write.
    * ``list_jobs_async`` locks, but ``_synced_snapshot`` swallows
      ``CronStoreBusy`` and returns the cache, and it syncs through ``_sync()``,
      whose ``_load`` flattens an unreadable store to an empty job list.

    So these drive a REAL ``CronService`` into each store failure — a real held
    flock, a real corrupt store file — rather than a fake that raises from a method
    the real store never raises from. ``_file_lock``'s bounded spin is shortened to
    keep the three retries quick; the contention, the ``CronStoreBusy``, and the
    refusal are the store's own.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @staticmethod
    @contextlib.contextmanager
    def _store_lock_held(base_dir) -> Iterator[None]:
        """Hold the cron store's flock from a separate open description.

        The same technique ``test_file_lock_is_bounded_when_contended`` uses: flock
        on a second fd conflicts within one process too, so this is the real
        cross-process contention a CLI ``cron adopt`` or a large atomic save
        produces, not a patched-out lock.
        """
        from kiro_crew import platform_compat

        holder = (base_dir / ".crons.lock").open("w")
        platform_compat.acquire_lock(holder.fileno(), exclusive=True)
        try:
            yield
        finally:
            platform_compat.release_lock(holder.fileno())
            holder.close()

    @staticmethod
    def _with_short_lock_spin(crons: CronService) -> None:
        """Shrink the store lock's bounded wait so three retries stay quick.

        Only the timeout changes -- ``_file_lock``'s own non-blocking spin still
        runs and still raises the real ``CronStoreBusy``. The default 10 s wait
        would make each retry round a ten-second stall.
        """
        real_lock = crons._file_lock

        def _short(*, timeout: float = 0.3, poll: float = 0.02):
            return real_lock(timeout=timeout, poll=poll)

        crons._file_lock = _short  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_a_stale_cache_over_a_contended_store_does_not_strand_the_owner(self, tmp_path):
        """The miss this guards: stale-empty cache, owner on disk.

        A second process wrote the channel-owned job after this service loaded, so
        its cache says the store is empty; the lock is held, so the freshening read
        cannot land. ``list_jobs_async`` answers the stale ``[]`` -- the sweep finds
        nothing, the delete proceeds, and the job stays owned by a session that no
        longer exists with nothing left to notice. A strict read raises instead, and
        the funnel refuses.

        Asserts the INVARIANT rather than one branch: the job may be released, or
        the delete may be refused with the row intact. What is forbidden is the
        silent third outcome -- unlinked while still owned.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        # Another process (CLI, gateway, MCP) stamps the owner AFTER this service
        # loaded, so `crons` holds a cache that predates the job.
        job = CronService(base_dir=tmp_path).add_job(
            "channel", "ping", every_secs=3600, session_key=channel_key
        )
        assert crons.list_jobs(include_disabled=True) == [], "cache should be stale-empty"
        self._with_short_lock_spin(crons)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with self._store_lock_held(tmp_path):
            resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        still_owned = CronService(base_dir=tmp_path).get_job(job.id).session_key
        if still_owned:
            assert body["ok"] is False, (
                f"job {job.id} is still owned by {still_owned!r} and the delete reported "
                f"success: the sweep read a stale-empty cache over a contended store"
            )
            assert (resp.status, body["code"]) == (409, CRON_STORE_BUSY_CODE)
            assert (
                f"delete_session:{history_key}" not in log.calls
            ), "refused, so the transcript -- the last record of the binding -- must survive"

    @pytest.mark.asyncio
    async def test_a_persistently_contended_store_refuses_loudly(self, tmp_path, caplog):
        """Busy past the retries is a loud refusal, never a silent proceed.

        Same held lock, but asserted from the operator side: a 409 whose code names
        contention and whose text asks for a retry (the by-id CLI cannot help with a
        held lock), the row survives to retry, and the job is untouched.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        self._with_short_lock_spin(crons)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with caplog.at_level(logging.WARNING):
            with self._store_lock_held(tmp_path):
                resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert body["ok"] is False, "a store that cannot be read is not an empty one"
        assert resp.status == 409
        assert body["code"] == CRON_STORE_BUSY_CODE
        assert "retry" in body["error"]
        assert f"delete_session:{history_key}" not in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key
        assert any(
            "owner sweep not performed" in r.message or "cron store busy" in r.message
            for r in caplog.records
        ), "the refusal must be logged, not just returned"

    @pytest.mark.asyncio
    async def test_an_unreadable_store_refuses_instead_of_reading_it_empty(self, tmp_path):
        """``_load`` flattens a corrupt store to ``[]``; the sweep must not believe it.

        ``_sync()`` does not raise on an unparseable store -- it degrades to an
        empty job list -- so the pre-fix sweep reads "no owners" off a store that
        may hold several, and unlinks the only other record of them.
        ``_sync_for_write`` refuses instead.
        """
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        crons.add_job("channel", "ping", every_secs=3600, session_key="slack:1785370133.085469")
        store = next(p for p in tmp_path.rglob("*.json") if "cron" in p.name)
        store.write_text("{ not json at all", encoding="utf-8")
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert body["ok"] is False, "an unreadable store is not an ownerless one"
        assert resp.status == 409
        assert body["code"] == CRON_STORE_UNREADABLE_CODE
        # The store wording travels with the refusal; the by-id CLI reads the same
        # file, so it is deliberately NOT offered as the remedy here.
        assert "could not read" in body["error"] and "adopt" not in body["error"]
        assert f"delete_session:{history_key}" not in log.calls

    @pytest.mark.asyncio
    async def test_the_strict_scan_sees_a_cross_process_write_the_cache_missed(self, tmp_path):
        """Positive control: uncontended, the strict scan is FRESHER than the cache.

        Without it the refusals above could be satisfied by a scan that always
        fails. Same stale-cache setup, lock free: the locked reload picks up the
        other process's job and the release lands.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = CronService(base_dir=tmp_path).add_job(
            "channel", "ping", every_secs=3600, session_key=channel_key
        )
        assert crons.list_jobs(include_disabled=True) == [], "cache should be stale-empty"
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    @pytest.mark.asyncio
    async def test_the_store_scan_includes_a_disabled_jobs_owner(self, tmp_path):
        """A paused job's owner is stamped on disk and strands just the same."""
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        crons.enable_job(job.id, False)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""


class TestSlotlessChannelSessionReleasesItsExactOwnerKey:
    """A channel session's cron is owned under the channel's EXACT key.

    After a gateway restart such a session has no live slot, so the funnel's
    ``effective_session_key`` branch never runs and the derived candidates are
    only the folded transcript name and a ``dashboard:`` spelling of it --
    neither of which a channel job is stamped with. The exact key survives only
    in the transcript's ``linked_session_key``, which the delete itself destroys,
    so it has to be read first.
    """

    @pytest.mark.asyncio
    async def test_delete_releases_the_cron_and_reads_the_key_before_unlinking(self, tmp_path):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key)
        # Post-restart: the transcript is on disk, the slot is not.
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert log.calls.index(f"get_metadata:{history_key}") < log.calls.index(
            f"delete_session:{history_key}"
        )
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    @pytest.mark.asyncio
    async def test_bulk_clear_releases_the_same_cron(self, tmp_path):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key)
        log.list_sessions = lambda: [{"key": history_key}]  # type: ignore[method-assign]
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}

        resp = await api_sessions_clear(request)

        assert json.loads(resp.body.decode("utf-8"))["cleared"] == 1
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""


class TestUnreadableMetadataAbortsTheDelete:
    """A row whose metadata cannot be READ must not be unlinked.

    ``linked_session_key`` is the only string that matches a channel session's
    cron owner, and the unlink destroys the only copy. When the read FAILS --
    a partial write, a prior ENOSPC, corruption -- proceeding deletes the last
    thing that could ever name the owner, and nothing downstream notices:
    ``_warn_stranded_cron_ownership`` fires when a release FAILS, never when the
    key was never read at all. The transcript is recoverable (backups, and the
    user can delete it again); the ownership is not, so the delete loses.

    An ABSENT metadata line is a different answer, not a quieter version of this
    one: there is no owner key to lose, so it deletes exactly as before. So is an
    empty dict, which is what the store returns for a row with no metadata line.
    A payload that is not a mapping at all, or one whose ``linked_session_key``
    is not a string, is MALFORMED -- read successfully, trustworthy not at all --
    and refuses with the unreadable case.
    """

    def _request(self, state) -> MagicMock:
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}
        return request

    def _state(self, crons, log) -> MagicMock:
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        return state

    @pytest.mark.asyncio
    async def test_metadata_that_raises_refuses_the_delete(self, tmp_path, caplog):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key, raises=True)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        with caplog.at_level(logging.ERROR):
            resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert body["ok"] is False
        assert resp.status == 409
        assert body["code"] == CRON_OWNERSHIP_UNKNOWN_CODE
        # The refusal has to say what to do about it: an operator cannot release
        # ownership by hand from a bare "ok": false.
        assert "kirocrew cron adopt" in body["error"]
        assert history_key in caplog.text and "kirocrew cron adopt" in caplog.text
        # NOT unlinked -- the row still holds the only copy of the owner key, so
        # the ownership is recoverable rather than stranded.
        assert f"delete_session:{history_key}" not in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key

    @pytest.mark.asyncio
    async def test_metadata_reported_unreadable_refuses_the_delete(self, tmp_path):
        """``({}, False)`` -- the real store's answer after its read retries."""
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key, unreadable=True)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert (resp.status, body["ok"]) == (409, False)
        assert body["code"] == CRON_OWNERSHIP_UNKNOWN_CODE
        assert f"delete_session:{history_key}" not in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key

    @pytest.mark.asyncio
    async def test_absent_metadata_still_deletes(self, tmp_path):
        """No metadata line means no owner key to lose. Unchanged behaviour."""
        history_key = "dashboard_chat-1-100"
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(history_key, "")
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls

    @pytest.mark.asyncio
    async def test_an_empty_metadata_dict_still_deletes(self, tmp_path):
        """``{}`` is ABSENT, not malformed -- the store's own no-metadata answer.

        ``_read_metadata_status`` returns ``({}, True)`` for a row with no
        metadata line AND for a first line that is not metadata-typed, so
        refusing on it would refuse the ordinary dashboard session.
        """
        history_key = "dashboard_chat-1-100"
        crons = CronService(base_dir=tmp_path)
        log = _FakeTranscriptStore(history_key, "", malformed={})
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert f"delete_session:{history_key}" in log.calls

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("slack:1785370133.085469", id="bare-string"),
            pytest.param(["slack:1785370133.085469"], id="list"),
            pytest.param(17, id="scalar"),
            pytest.param({"linked_session_key": ["slack:1785370133.085469"]}, id="non-str-value"),
            pytest.param({"linked_session_key": {"key": "slack:1785"}}, id="mapping-value"),
        ],
    )
    @pytest.mark.asyncio
    async def test_malformed_metadata_refuses_the_delete(self, tmp_path, payload):
        """A READABLE read is not a TRUSTWORTHY one.

        The read succeeds and reports ``readable=True``, but the payload is not
        the mapping the store promises (or its ``linked_session_key`` is not a
        string). Folding that into the absent case answers ``""`` -- or, for a
        non-string value, ``str()``-mints a key matching no job -- and the delete
        proceeds on a row that may name a real owner: the exact strand the abort
        exists to prevent, reached through the one door it left open.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key, malformed=payload)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        body = json.loads(resp.body.decode("utf-8"))
        assert body["ok"] is False
        assert resp.status == 409
        assert body["code"] == CRON_OWNERSHIP_UNKNOWN_CODE
        assert "kirocrew cron adopt" in body["error"]
        assert f"delete_session:{history_key}" not in log.calls
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == channel_key

    @pytest.mark.asyncio
    async def test_the_owner_key_is_read_inside_the_delete_lock(self, tmp_path):
        """One lock hold, not two: a writer cannot land a key in between.

        Two separate holds leave a window in which a concurrent
        ``update_metadata`` publishes the ``linked_session_key`` this funnel just
        read as absent -- and the unlink then destroys exactly the key it needed.
        """
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key)
        request = self._request(self._state(crons, log))
        request.match_info = {"key": history_key}

        await api_session_delete(request)

        assert log.calls[0].startswith(f"locked_stems:{history_key},")
        assert log.calls.index(f"locked:{history_key}") < log.calls.index(
            f"get_metadata:{history_key}"
        )
        assert log.calls.index(f"get_metadata:{history_key}") < log.calls.index(
            f"delete_session:{history_key}"
        )
