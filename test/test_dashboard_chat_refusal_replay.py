"""Dashboard chat: the refusal fallback's queued replay — drain, consume, abort, supersede."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import await_successor, chat_done_frames, run_as_slot_task
from test_dashboard_chat_refusal import TestRunChatRefusalFallback as _RefusalFallbackSuite

from kiro_crew.dashboard.recovery_replays import LiveSlot, ReplayFamily
from kiro_crew.testing.wait import await_until


class TestRunChatRefusalFallbackReplay:
    """TestRunChatRefusalFallback: the queued replay's drain, consume and abort paths."""

    _make_state_for_run_chat = staticmethod(_RefusalFallbackSuite._make_state_for_run_chat)
    _make_refusing_client = staticmethod(_RefusalFallbackSuite._make_refusing_client)
    _refusal_events = staticmethod(_RefusalFallbackSuite._refusal_events)
    _answering_events = staticmethod(_RefusalFallbackSuite._answering_events)

    @pytest.mark.asyncio
    async def test_drain_purges_replay_after_stop(self, tmp_path, monkeypatch):
        """A Stop landing between the replay's enqueue and the drain kills the
        replay: the index-0 entry must never dispatch superseded work."""
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_runner import (
            SYNTHETIC_RECOVERY_KIND,
            _start_next_queued_turn,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        qid = slot.queue_insert(0, "retry me", kind=SYNTHETIC_RECOVERY_KIND)
        slot.replays.arm(
            ReplayFamily.CONTENT_FILTER,
            entry_id=qid,
            session_key=slot._refusal_fallback_session_key,
            stop_gen=slot._stop_generation,
            session_stop_gen=0,
        )
        # The Stop that landed while the replay waited.
        slot._stop_generation += 1
        _dispatched = MagicMock()
        monkeypatch.setattr(chat_runner, "_run_chat", _dispatched)

        assert await _start_next_queued_turn(state, slot) is False
        _dispatched.assert_not_called()
        assert all(q.get("id") != qid for q in slot._queue)
        assert not slot.replays.armed(ReplayFamily.CONTENT_FILTER)
        assert any(
            "Content-filter retry cancelled" in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "notice"
        ), "the transcript must say the retry was cancelled"

    @pytest.mark.asyncio
    async def test_drain_purges_replay_when_user_correction_queued(self, tmp_path, monkeypatch):
        """A user correction queued behind the index-0 replay supersedes it:
        the correction dispatches, the replay dies."""
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_runner import (
            SYNTHETIC_RECOVERY_KIND,
            _start_next_queued_turn,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        qid = slot.queue_insert(0, "retry me", kind=SYNTHETIC_RECOVERY_KIND)
        slot.replays.arm(
            ReplayFamily.CONTENT_FILTER,
            entry_id=qid,
            session_key=slot._refusal_fallback_session_key,
            stop_gen=slot._stop_generation,
            session_stop_gen=0,
        )
        # The correction the refusal card asked for, queued while the replay waited.
        slot.queue_insert(1, "actually, do this instead")

        _seen: list[str] = []

        def _fake_run_chat(_state, _slot, message, **kwargs):
            _seen.append(message)

            async def _noop():
                return None

            return _noop()

        monkeypatch.setattr(chat_runner, "_run_chat", _fake_run_chat)

        assert await _start_next_queued_turn(state, slot) is True
        assert _seen == [
            "actually, do this instead"
        ], "the correction must dispatch, never the purged replay"
        assert all(q.get("id") != qid for q in slot._queue)
        assert not slot.replays.armed(ReplayFamily.CONTENT_FILTER)

    @pytest.mark.asyncio
    async def test_replay_carries_original_turn_attachments(self, tmp_path, monkeypatch):
        """The replay is the SAME turn again: a refused message with files
        retries with its files riding the queue entry's meta."""
        from kiro_crew.dashboard.state import _ChatSlot

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        _recovery_meta: list[dict] = []
        _orig_insert = _ChatSlot.queue_insert

        def _spy_insert(self_slot, index, content, kind="", payload="", meta=None, **kw):
            if kind:
                _recovery_meta.append(dict(meta or {}))
            return _orig_insert(
                self_slot, index, content, kind=kind, payload=payload, meta=meta, **kw
            )

        monkeypatch.setattr(_ChatSlot, "queue_insert", _spy_insert)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello", _attachments=["/tmp/evidence.png"])
        if slot.task is not None:
            await slot.task

        assert _recovery_meta, "the refusal replay was never queued"
        assert _recovery_meta[0].get("files") == [
            "/tmp/evidence.png"
        ], "the replay entry must carry the refused turn's attachment list"

    @pytest.mark.asyncio
    async def test_replay_preserves_folder_attachment_typing(self, tmp_path, monkeypatch):
        """A folder attachment replays under ``dirs``: rebucketing the flat
        list under ``files`` would retype it and resolve its
        ``[attached_dir N]`` marker against the wrong list."""
        from kiro_crew.dashboard.state import _ChatSlot

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        _recovery_meta: list[dict] = []
        _orig_insert = _ChatSlot.queue_insert

        def _spy_insert(self_slot, index, content, kind="", payload="", meta=None, **kw):
            if kind:
                _recovery_meta.append(dict(meta or {}))
            return _orig_insert(
                self_slot, index, content, kind=kind, payload=payload, meta=meta, **kw
            )

        monkeypatch.setattr(_ChatSlot, "queue_insert", _spy_insert)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(
            state,
            slot,
            "hello",
            _attachments=["/tmp/report.pdf", "/tmp/project-dir"],
            _attachment_meta={
                "files": ["/tmp/report.pdf"],
                "dirs": ["/tmp/project-dir"],
            },
        )
        if slot.task is not None:
            await slot.task

        assert _recovery_meta, "the refusal replay was never queued"
        assert _recovery_meta[0].get("dirs") == [
            "/tmp/project-dir"
        ], "the folder attachment must replay under dirs, not be retyped as a file"
        assert _recovery_meta[0].get("files") == [
            "/tmp/report.pdf"
        ], "the file attachment must stay under files with no dir mixed in"

    @pytest.mark.asyncio
    async def test_drain_purges_replay_when_binding_changes(self, tmp_path, monkeypatch):
        """A relink moving the slot to a DIFFERENT session between the
        replay's enqueue and the drain kills the replay: it belongs to the
        session the refused turn ran on, never the newly bound one. (The
        unbound->bound case is already dropped by the admission sweep's
        fail-closed booleans; a bound->bound relink is invisible to them —
        this branch is what catches it and clears the episode record.)"""
        from kiro_crew.dashboard import chat_runner, session_control
        from kiro_crew.dashboard.chat_runner import (
            SYNTHETIC_RECOVERY_KIND,
            _start_next_queued_turn,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        # The turn ran on binding A; stamp admission containment as the real
        # producer (_queue_recovery) does, so the admission sweep sees an
        # unchanged boolean set and lets the entry through to the replay gate.
        slot.linked_session_key = "session-a"
        slot._refusal_fallback_session_key = "session-a"
        qid = slot.queue_insert(
            0,
            "retry me",
            kind=SYNTHETIC_RECOVERY_KIND,
            meta=session_control.containment_meta(state, slot),
        )
        slot.replays.arm(
            ReplayFamily.CONTENT_FILTER,
            entry_id=qid,
            session_key=slot._refusal_fallback_session_key,
            stop_gen=slot._stop_generation,
            session_stop_gen=0,
        )
        # The relink that landed while the replay waited.
        slot.linked_session_key = "session-b"
        _dispatched = MagicMock()
        monkeypatch.setattr(chat_runner, "_run_chat", _dispatched)

        assert await _start_next_queued_turn(state, slot) is False
        _dispatched.assert_not_called()
        assert all(q.get("id") != qid for q in slot._queue)
        assert not slot.replays.armed(ReplayFamily.CONTENT_FILTER), (
            "the dispatch-gate record must die with the replay — a later "
            "identical message on session-b is a genuine turn, not a retry"
        )
        assert any(
            "this chat moved to another session" in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "notice"
        ), "the transcript must say the retry died with the old binding"

    @pytest.mark.asyncio
    async def test_swept_replay_clears_dispatch_gate_record(self, tmp_path, monkeypatch):
        """A replay entry the admission sweep removed leaves the queue-id
        pointing at nothing. The drain's replay gate must clear the
        dispatch-gate text with it: otherwise an identical later resend
        matches as the retry — running on the fallback with the restore
        probe skipped and the one-retry allowance kept spent."""
        from kiro_crew.dashboard import chat_runner, session_control
        from kiro_crew.dashboard.chat_runner import _start_next_queued_turn

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        # Episode state as the swap left it — but the replay entry itself is
        # absent (the admission sweep dropped it on a containment change).
        slot.replays.arm(
            ReplayFamily.CONTENT_FILTER,
            entry_id="qid-swept-away",
            session_key="",
            stop_gen=0,
            session_stop_gen=0,
        )
        slot._refusal_fallback_attempted = True
        # The user's identical resend queued behind the (now gone) replay.
        slot.queue_insert(
            0,
            "retry me",
            meta=session_control.containment_meta(state, slot),
        )
        _dispatched = MagicMock()
        monkeypatch.setattr(chat_runner, "_run_chat", _dispatched)

        await _start_next_queued_turn(state, slot)
        assert not slot.replays.armed(ReplayFamily.CONTENT_FILTER), (
            "the dispatch-gate record must die with the swept replay — the "
            "identical resend is a genuine turn, not a mistaken retry"
        )

    @pytest.mark.asyncio
    async def test_swap_locks_the_turns_captured_binding(self, tmp_path, monkeypatch):
        """The swap serializes on the TURN's captured key and stamps it: a
        binding that moved before the refusal arrived must not re-route the
        lock domain."""
        import asyncio as _asyncio

        from kiro_crew.dashboard.chat_runner import _refusal_fallback_swap
        from kiro_crew.llm_helpers import slot_switch_session_lock

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        # The slot was bound AFTER the turn started: live derivation now
        # disagrees with the turn's captured key.
        slot.linked_session_key = "cron:job-123"
        client = self._make_refusing_client([])
        client.served_model = "fable-5"

        async def _apply_set_model(model: str) -> None:
            client.served_model = model

        client.set_model = AsyncMock(side_effect=_apply_set_model)

        _turn_key = "dash:turn-key"
        _held = slot_switch_session_lock(_turn_key)
        await _held.acquire()
        try:
            swap_task = _asyncio.create_task(
                _refusal_fallback_swap(slot, client, "opus-test", session_key=_turn_key)
            )
            for _ in range(10):
                await _asyncio.sleep(0)
            assert not swap_task.done(), (
                "the swap must serialize on the turn's captured key, "
                "not the newly bound session's"
            )
        finally:
            _held.release()
        assert await await_until(swap_task, what="the swap finishing") == "fable-5"
        assert (
            slot._refusal_fallback_session_key == _turn_key
        ), "the swap must stamp the binding it ran under"

    @pytest.mark.asyncio
    async def test_restore_locks_the_swap_recorded_binding(self, tmp_path, monkeypatch):
        """The restore serializes on the binding the SWAP recorded — a rebind
        between swap and restore must not split the two seams into disjoint
        lock domains."""
        import asyncio as _asyncio

        from kiro_crew.dashboard.chat_runner import _restore_refusal_fallback
        from kiro_crew.llm_helpers import slot_switch_session_lock

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        client.served_model = "opus-test"
        client.set_model = AsyncMock()

        slot._refusal_fallback_primary = "fable-5"
        slot._refusal_fallback_candidate = "opus-test"
        slot._refusal_pick_gen = slot._model_pick_gen
        slot._refusal_client_pick_epoch = 0
        slot._refusal_fallback_session_key = "dash:turn-key"
        # The rebind that landed after the swap.
        slot.linked_session_key = "cron:job-123"

        _held = slot_switch_session_lock("dash:turn-key")
        await _held.acquire()
        try:
            restore_task = _asyncio.create_task(_restore_refusal_fallback(slot, client))
            for _ in range(10):
                await _asyncio.sleep(0)
            assert not restore_task.done(), (
                "the restore must serialize on the swap's recorded key, "
                "not the rebound session's"
            )
        finally:
            _held.release()
        await await_until(restore_task, what="the restore finishing")
        # Once inside the lock, the rebind guard rules: the record belongs to
        # the recorded session, and ``client`` now serves the rebound one —
        # the restore must DROP the record, never move the rebound session's
        # model to a primary it never chose.
        client.set_model.assert_not_awaited()
        assert slot._refusal_fallback_primary == ""
        assert slot._refusal_fallback_candidate == ""

    @pytest.mark.asyncio
    async def test_replay_recognized_after_credential_redaction(self, tmp_path, monkeypatch):
        """Replay identity is the queue entry, not the text: the drain redacts
        credentials AFTER the swap records the raw message, so a refused
        message carrying a token must still be recognized as the one retry —
        exactly one swap, no second 'retrying once' notice, no restore probe
        running the replay back on the primary that just refused."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        events = self._refusal_events(category="CYBER")
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        secret_msg = "use token ghp_" + "A" * 36 + " to fetch the repo"
        await _run_chat(state, slot, secret_msg)

        assert slot.task is not None, "queued replay was not dispatched"
        await slot.task

        retry_notices = [
            m
            for m in slot.messages
            if m.get("role") == "error" and "retrying once on 'opus-test'" in m.get("content", "")
        ]
        assert len(retry_notices) == 1, (
            "the redacted replay must be recognized as the retry — a second "
            f"notice means it re-armed as a genuine turn: {retry_notices}"
        )
        # Exactly one model move: the swap. No restore probe ran inside the
        # replay (unrecognized, it would move the model back to the primary
        # mid-retry), and no second swap happened.
        client.set_model.assert_awaited_once_with("opus-test")
        assert slot._refusal_fallback_attempted is True, "allowance must stay spent"

    @staticmethod
    def _arm_refusal_replay(slot, *, session_key: str | None = None) -> None:
        """The slot state a refusal swap leaves behind before its replay runs."""
        from kiro_crew.dashboard.chat_utils import effective_session_key

        slot._refusal_fallback_primary = "fable-5"
        slot._refusal_fallback_candidate = "opus-test"
        slot._refusal_fallback_attempted = True
        slot._refusal_fallback_session_key = (
            effective_session_key(slot) if session_key is None else session_key
        )
        slot.replays.arm(
            ReplayFamily.CONTENT_FILTER,
            entry_id="q-retry",
            session_key=slot._refusal_fallback_session_key,
            stop_gen=getattr(slot, "_stop_generation", 0),
            session_stop_gen=0,
        )

    @pytest.mark.asyncio
    async def test_replay_consume_aborts_after_stop(self, tmp_path, monkeypatch):
        """A Stop completing between the drain's dequeue validation and the
        replay turn's consume must abort the replay: the current stop
        generation differs from the recorded snapshot, so the stale content
        never runs. With nothing queued, the exit guard ends the cycle: one
        serializable chat_done, the done row, and the slot's task released."""
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        # Snapshot taken at enqueue…
        self._arm_refusal_replay(slot)
        # …then a Stop pressed AND resolved in the spawn→consume window.
        slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1

        await run_as_slot_task(
            slot,
            _run_chat(state, slot, "retry me", _replay=frozenset({ReplayFamily.CONTENT_FILTER})),
        )

        cancelled = [
            m
            for m in slot.messages
            if m.get("role") == "notice"
            and "Content-filter retry cancelled" in m.get("content", "")
            and "the turn was stopped." in m.get("content", "")
        ]
        assert len(cancelled) == 1, f"expected the consume-time abort notice, got {slot.messages}"
        client.set_model.assert_not_awaited()
        state.sessions.get_or_create.assert_not_awaited()
        assert not slot.replays.armed(ReplayFamily.CONTENT_FILTER)
        assert slot._refusal_fallback_attempted is True, "allowance stays spent on abort"
        frames = chat_done_frames(state)
        assert len(frames) == 1 and isinstance(frames[0], dict), frames
        assert slot.messages[-1].get("role") == "done"
        assert slot.task is None

    @pytest.mark.asyncio
    async def test_replay_consume_aborts_after_rebind(self, tmp_path, monkeypatch):
        """A session rebind landing between dequeue and consume must abort the
        replay — the stale content belongs to the OLD session and must not
        enter the newly bound one."""
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        # The binding the swap recorded differs from the slot's live binding.
        self._arm_refusal_replay(slot, session_key="dash:old-session")

        await _run_chat(state, slot, "retry me", _replay=frozenset({ReplayFamily.CONTENT_FILTER}))

        cancelled = [
            m
            for m in slot.messages
            if m.get("role") == "notice"
            and "Content-filter retry cancelled" in m.get("content", "")
            and "this chat moved to another session." in m.get("content", "")
        ]
        assert len(cancelled) == 1, f"expected the rebind abort notice, got {slot.messages}"
        client.set_model.assert_not_awaited()
        assert not slot.replays.armed(ReplayFamily.CONTENT_FILTER)

    @pytest.mark.asyncio
    async def test_synthetic_recovery_dispatch_does_not_rearm(self, tmp_path, monkeypatch):
        """A kind-tagged recovery requeue of the user's OWN words is the same
        turn retried, not a genuine new message: it must not re-arm the
        one-retry allowance (re-arming lets a crashing fallback replay cycle
        primary-refusal → swap → crash indefinitely)."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([LLMEvent(kind=EVENT_COMPLETE)])
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        slot._refusal_fallback_attempted = True

        await _run_chat(state, slot, "my own words again", _synthetic_recovery_turn=True)

        assert slot._refusal_fallback_attempted is True, (
            "a synthetic recovery requeue must not re-arm the refusal-retry "
            "allowance — only a genuine user-origin dispatch does"
        )

    @pytest.mark.asyncio
    async def test_synthetic_recovery_dispatch_skips_restore_probe(self, tmp_path, monkeypatch):
        """A kind-tagged recovery requeue of the user's OWN words must finish
        on the model that produced the original attempt: the restore probe at
        turn start must not move the session back to the primary mid-recovery.
        The fixed-text membership check cannot recognize a requeued user
        message, so the gate needs the structural signal."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([LLMEvent(kind=EVENT_COMPLETE)])
        client.served_model = "opus-test"  # still on the fallback candidate
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_utils import effective_session_key

        slot._refusal_fallback_primary = "fable-5"
        slot._refusal_fallback_candidate = "opus-test"
        slot._refusal_fallback_attempted = True
        slot._refusal_pick_gen = slot._model_pick_gen
        slot._refusal_client_pick_epoch = 0
        slot._refusal_fallback_session_key = effective_session_key(slot)

        await _run_chat(state, slot, "my own words again", _synthetic_recovery_turn=True)

        client.set_model.assert_not_awaited()
        assert slot._refusal_fallback_primary == "fable-5", (
            "the restore record must survive a recovery dispatch — the next "
            "genuine turn owns the restore"
        )

    @pytest.mark.asyncio
    async def test_restore_dropped_when_slot_rebound(self, tmp_path, monkeypatch):
        """A restore record made under session A must never be applied through
        the provider of a session the slot has since rebound to: the record is
        dropped without touching the model."""
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        client.served_model = "opus-test"
        client.set_model = AsyncMock()

        slot._refusal_fallback_primary = "fable-5"
        slot._refusal_fallback_candidate = "opus-test"
        slot._refusal_pick_gen = slot._model_pick_gen
        slot._refusal_client_pick_epoch = 0
        # Swap recorded session A; the slot has since rebound elsewhere.
        slot._refusal_fallback_session_key = "dash:session-a"

        from kiro_crew.dashboard.chat_runner import _restore_refusal_fallback

        await _restore_refusal_fallback(slot, client)

        client.set_model.assert_not_awaited()
        assert slot._refusal_fallback_primary == ""
        assert slot._refusal_fallback_candidate == ""

    @pytest.mark.asyncio
    async def test_recovery_requeue_of_retry_turn_carries_replay_identity(
        self, tmp_path, monkeypatch
    ):
        """A pre-output process failure during the retry turn requeues the
        user's message verbatim through a GENERIC recovery family. The requeue
        must carry the refusal-replay identity and fresh stop snapshots onto
        the new entry — otherwise the requeued entry escapes the drain's
        refusal-specific validation, and a correction queued in the
        requeue-to-dispatch window loses to superseded work."""
        from kiro_crew.acp.client import AcpError

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.reset = AsyncMock()

        async def _raise_dead(msg):
            raise AcpError("ACP process exited (code=-15)")
            yield  # make it an async generator

        client.stream = _raise_dead
        client.stream_command = _raise_dead
        client.shutdown = AsyncMock()

        # Keep the requeued entry IN the queue: the post-turn drain would
        # otherwise dequeue and spawn it immediately, racing the assertions.
        async def _no_drain(_state, _slot, **_kwargs) -> bool:
            return False

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", _no_drain)

        from kiro_crew.dashboard.chat import _run_chat

        self._arm_refusal_replay(slot)

        await _run_chat(state, slot, "retry me", _replay=frozenset({ReplayFamily.CONTENT_FILTER}))

        requeued = [q for q in slot._queue if q.get("content") == "retry me"]
        assert requeued, f"expected the verbatim recovery requeue, queue={slot._queue}"
        assert slot.replays.entry_id(ReplayFamily.CONTENT_FILTER) == requeued[0]["id"], (
            "the requeued entry must carry the refusal-replay identity so the "
            "drain applies the refusal-specific validation at dispatch"
        )

        # Re-armed at the requeue's Stop counts: no Stop since, so it may run on
        # the session the swap ran under, and reads as moved anywhere else.
        def _view(session_key: str) -> LiveSlot:
            return LiveSlot(
                session_key=session_key,
                stop_generation=getattr(slot, "_stop_generation", 0),
                stopping=False,
                user_input=False,
                session_stop_generation=lambda _key: 0,
            )

        replay = ReplayFamily.CONTENT_FILTER
        assert not slot.replays.revalidate(
            replay, _view(slot._refusal_fallback_session_key)
        ).revoked
        assert slot.replays.revalidate(replay, _view("dash:other")).rebound

    @pytest.mark.asyncio
    async def test_swap_during_throttle_fallback_records_true_primary(self, tmp_path, monkeypatch):
        """A refusal landing while a THROTTLE fallback is serving must record
        the walk's true primary, not the wire model: the replay turn's
        throttle probe clears the walk's sticky state via moved-off, so a
        record naming the throttle candidate would strand the session on a
        model the user never chose, with no machinery left to recover."""
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        client.served_model = "haiku-throttle"  # wire = the throttle candidate

        # The throttle walk is actively serving and knows the true primary.
        slot._active_fallback_model = "haiku-throttle"
        slot._fallback_primary_model = "fable-5"

        from kiro_crew.dashboard.chat_runner import _refusal_fallback_swap
        from kiro_crew.dashboard.chat_utils import effective_session_key

        result = await _refusal_fallback_swap(
            slot, client, "opus-test", session_key=effective_session_key(slot)
        )

        assert result == "haiku-throttle", (
            "the return feeds the 'declined on X' notice — it must name the "
            "model that actually refused (the wire model)"
        )
        assert (
            slot._refusal_fallback_primary == "fable-5"
        ), "the record must name the user's model, not the throttle candidate"
        assert slot._refusal_fallback_candidate == "opus-test"

    @pytest.mark.asyncio
    async def test_replay_aborts_when_correction_queued_during_preparation(
        self, tmp_path, monkeypatch
    ):
        """The consume-seam supersession check runs at turn ENTRY, but turn
        preparation awaits (session acquisition, prompt assembly) before the
        stream opens. A correction queued in that window must abort the
        replay before any model call — a stale replay dispatching first could
        run destructive tools the correction cancelled."""
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(
            [
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="stale replay output"),
                LLMEvent(kind=EVENT_COMPLETE),
            ]
        )

        _orig_get_or_create = AsyncMock(return_value=(client, True, False))

        async def _acquire_and_inject(*args, **kwargs):
            # A user correction lands DURING preparation — after the
            # consume-seam check, before the stream opens.
            slot.queue_insert(0, "actually do the other thing", kind="")
            return await _orig_get_or_create(*args, **kwargs)

        state.sessions.get_or_create = _acquire_and_inject

        from kiro_crew.dashboard.chat import _run_chat

        self._arm_refusal_replay(slot)

        await _run_chat(state, slot, "retry me", _replay=frozenset({ReplayFamily.CONTENT_FILTER}))

        cancelled = [
            m
            for m in slot.messages
            if m.get("role") == "notice"
            and "Content-filter retry cancelled" in m.get("content", "")
        ]
        assert len(cancelled) == 1, f"expected the pre-stream abort notice, got {slot.messages}"
        assert not slot.replays.armed(ReplayFamily.CONTENT_FILTER)
        assert not any(
            m.get("role") == "assistant" for m in slot.messages
        ), "the stale replay must never reach the model"

    @staticmethod
    def _arm_model_access_replay(slot, entry_id: str = "q-replay") -> None:
        from kiro_crew.dashboard.chat_utils import effective_session_key

        slot.replays.arm(
            ReplayFamily.MODEL_ACCESS,
            entry_id=entry_id,
            session_key=effective_session_key(slot),
            stop_gen=getattr(slot, "_stop_generation", 0),
            session_stop_gen=0,
        )

    @staticmethod
    def _arm_image_recovery(slot) -> None:
        from kiro_crew.dashboard.chat_utils import effective_session_key

        slot.replays.arm(
            ReplayFamily.IMAGE_HISTORY,
            entry_id="q-image",
            session_key=effective_session_key(slot),
            stop_gen=getattr(slot, "_stop_generation", 0),
            session_stop_gen=0,
        )

    _ARM = {
        "refusal": "_arm_refusal_replay",
        "model-access": "_arm_model_access_replay",
        "image": "_arm_image_recovery",
    }

    @pytest.mark.parametrize(
        ("family", "message", "kwargs", "notice"),
        [
            (
                "refusal",
                "retry me",
                {"_replay": frozenset({ReplayFamily.CONTENT_FILTER})},
                "Content-filter retry cancelled",
            ),
            (
                "model-access",
                "original prompt",
                {"_replay": frozenset({ReplayFamily.MODEL_ACCESS})},
                "Model-fallback retry cancelled",
            ),
            (
                "image",
                "original prompt",
                {"_replay": frozenset({ReplayFamily.IMAGE_HISTORY})},
                "Image-history recovery cancelled",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_superseded_replay_abort_runs_the_queued_followup(
        self, tmp_path, monkeypatch, family, message, kwargs, notice
    ):
        """A replay cancelled at its consume seam returns before ``_run_chat``'s
        try, so the exit guard hands off the queue: the newer message that
        superseded the replay is dispatched once, and only its turn sends the
        cycle's one terminal chat_done."""
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        prompts: list[str] = []
        client = self._make_refusing_client(self._answering_events(), prompts)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        getattr(self, self._ARM[family])(slot)
        slot.queue_insert(0, "actually do the other thing", kind="")

        replay = await run_as_slot_task(slot, _run_chat(state, slot, message, **kwargs))

        assert any(
            m.get("role") == "notice" and notice in m.get("content", "") for m in slot.messages
        ), f"expected the consume-time abort notice, got {slot.messages}"
        assert chat_done_frames(state) == [], "the cycle ended before the follow-up ran"
        await await_successor(slot, replay)

        assert not any(message in p for p in prompts), "the cancelled replay must not run"
        assert sum("actually do the other thing" in p for p in prompts) == 1, prompts
        assert not slot._queue
        assert slot.task is None
        frames = chat_done_frames(state)
        assert len(frames) == 1 and isinstance(frames[0], dict), frames

    @pytest.mark.asyncio
    async def test_real_refusal_replay_dropped_after_relink(self, tmp_path, monkeypatch):
        """End to end through the real refuse -> swap -> enqueue path: a relink that
        lands before the drain moves the replay's session away, so the drain drops
        it with the move notice instead of dispatching it onto the new binding."""
        from kiro_crew.acp.types import STOP_REASON_REFUSAL
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.linked_session_key = "session-a"
        prompts: list[str] = []
        client = self._make_refusing_client([])

        async def _stream(msg):
            prompts.append(str(msg))
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_REFUSAL)

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        real_drain = chat_runner._start_next_queued_turn

        async def _relink_then_drain(drain_state, drain_slot, **kwargs):
            # The relink that lands while the replay waits in the queue.
            drain_slot.linked_session_key = "session-b"
            return await real_drain(drain_state, drain_slot, **kwargs)

        monkeypatch.setattr(chat_runner, "_start_next_queued_turn", _relink_then_drain)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        client.set_model.assert_awaited_once_with("opus-test")
        assert len(prompts) == 1, "the replay was dispatched onto the new binding"
        assert slot.task is None and slot._queue == []
        assert not slot.replays.armed(ReplayFamily.CONTENT_FILTER)
        assert any(
            m.get("role") == "notice"
            and m.get("content")
            == "ℹ️ Content-filter retry cancelled — this chat moved to another session."
            for m in slot.messages
        ), slot.messages

    @pytest.mark.asyncio
    async def test_real_refusal_replay_superseded_at_consume_runs_the_followup(
        self, tmp_path, monkeypatch
    ):
        """End to end through the real refuse -> swap -> enqueue -> drain path: a
        correction queued after the drain spawned the replay cancels it at the
        consume seam, and the correction runs next."""
        from kiro_crew.acp.types import STOP_REASON_REFUSAL
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        prompts: list[str] = []
        client = self._make_refusing_client([])
        replies = [
            [LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_REFUSAL)],
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), LLMEvent(kind=EVENT_COMPLETE)],
        ]

        async def _stream(msg):
            prompts.append(str(msg))
            for ev in replies[min(len(prompts), len(replies)) - 1]:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")
        replay = slot.task
        assert replay is not None, "the refusal replay was not dispatched"
        # The correction lands after the drain spawned the replay, before it runs.
        slot.queue_insert(0, "actually do the other thing", kind="")
        await await_until(replay, what="the refusal replay finishing")
        await await_successor(slot, replay)

        assert [p for p in prompts if "hello" in p] == [prompts[0]], "the replay ran"
        assert sum("actually do the other thing" in p for p in prompts) == 1, prompts
        assert any("Content-filter retry cancelled" in m.get("content", "") for m in slot.messages)
        assert not slot._queue and slot.task is None
        assert len(chat_done_frames(state)) == 1

    @pytest.mark.asyncio
    async def test_aborted_replay_starts_a_pending_synthesis(self, tmp_path, monkeypatch):
        """The consume-seam exit never reached the turn's own memory-preparation
        barrier, and no shared preparation holds turns back, so an armed
        synthesis starts, as it would after any finished turn, and the cycle's
        chat_done is left to it."""
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        from kiro_crew.dashboard.chat_utils import SYNTHESIS_CLEAR

        state.subagents = MagicMock()
        slot = state.get_or_create_slot("s1")
        state.sessions.get_or_create = AsyncMock()
        synthesis = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_pending_synthesis", synthesis)
        # No child holds the synthesis back (the fire gate's own reads are
        # covered by its tests).
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.synthesis_fire_verdict",
            AsyncMock(return_value=SYNTHESIS_CLEAR),
        )

        from kiro_crew.dashboard.chat import _run_chat

        self._arm_refusal_replay(slot)
        slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1
        slot._pending_synthesis = True

        await _run_chat(state, slot, "retry me", _replay=frozenset({ReplayFamily.CONTENT_FILTER}))
        await await_until(slot.task, what="the slot's turn finishing")

        synthesis.assert_awaited_once()
        assert chat_done_frames(state) == []

    @pytest.mark.asyncio
    async def test_the_drain_names_the_model_access_replay_by_its_queue_id(
        self, tmp_path, monkeypatch
    ):
        """The swap records its replay's queue id, and the drain passes the
        replay identity only for that entry: the replay keeps the spent one-shot,
        while an ordinary entry queued behind it refreshes it."""
        from kiro_crew.dashboard import chat_runner

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        prompts: list[str] = []
        client = self._make_refusing_client(self._answering_events(), prompts)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        self._arm_model_access_replay(
            slot,
            slot.queue_insert(0, "original prompt", kind=chat_runner.SYNTHETIC_RECOVERY_KIND),
        )
        slot._model_access_fallback_used = True
        seen: list[bool] = []
        real_stream = client.stream

        async def _stream(msg):
            seen.append(slot._model_access_fallback_used)
            async for event in real_stream(msg):
                yield event

        client.stream = _stream
        client.stream_command = _stream

        assert await chat_runner._start_next_queued_turn(state, slot)
        await await_successor(slot, None)

        assert seen == [True], "the replay turn must keep the one-shot it was queued under"
        assert not slot.replays.armed(ReplayFamily.MODEL_ACCESS)

        # A genuine entry is not the replay, so its turn refreshes the one-shot.
        slot.queue_insert(0, "a fresh message", kind="")
        assert await chat_runner._start_next_queued_turn(state, slot)
        await await_successor(slot, None)
        assert seen == [True, False]

    @pytest.mark.parametrize("caller", ["direct-send", "nudge-wake"])
    @pytest.mark.asyncio
    async def test_a_stale_model_access_record_does_not_cancel_the_next_turn(
        self, tmp_path, monkeypatch, caller
    ):
        """A record whose entry left the queue another way (a hard kill's clear,
        the stop counter moved since) is not what identifies a replay: only the
        drain's queue-id match does. So the next turn, a direct send or a nudge
        wake, runs normally."""
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        prompts: list[str] = []
        client = self._make_refusing_client(self._answering_events(), prompts)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        self._arm_model_access_replay(slot, "q-cleared-by-the-kill")
        slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1

        extra = (
            {"_directive_self_wake": True, "_turn_actor": "autonudge"}
            if caller == "nudge-wake"
            else {}
        )
        await _run_chat(state, slot, "the next message", **extra)

        assert any("the next message" in p for p in prompts), prompts
        assert not any("retry cancelled" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_a_synthetic_text_replay_consumes_the_model_access_record(
        self, tmp_path, monkeypatch
    ):
        """A swap during a synthetic recovery turn replays the synthetic text. The
        replay is still recognized (by queue id, not text), so it consumes the
        record instead of leaving it for the next turn."""
        from kiro_crew.dashboard.chat_runner import _CONN_RECOVER_MSG

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._answering_events())
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        self._arm_model_access_replay(slot)

        await _run_chat(
            state, slot, _CONN_RECOVER_MSG, _replay=frozenset({ReplayFamily.MODEL_ACCESS})
        )

        assert not slot.replays.armed(ReplayFamily.MODEL_ACCESS)
