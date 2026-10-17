"""Dashboard chat: an empty or false-blocker turn — the replay guard and the empty-turn diagnostics."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.recovery_replays import LiveSlot, ReplayFamily
from kiro_crew.dashboard.state import _ChatSlot


class TestEmptyResponseRetry:
    """Verify _run_chat retries once on empty model response, then shows error."""

    def _make_state_and_slot(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("empty-resp-slot")
        slot.append("user", "hello", "msg msg-u")

        mock_client = state.sessions.get_or_create.return_value[0]
        mock_client.is_kiro_backend = True
        mock_client.context_usage_pct = MagicMock(return_value=50.0)
        mock_client.shutdown = AsyncMock()
        return state, slot, mock_client, _run_chat

    def _make_empty_stream(self, mock_client):
        """Stream that completes immediately with no text."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        mock_client.stream = _stream
        mock_client.stream_command = _stream

    @staticmethod
    async def _cancel_background_tasks(state) -> None:
        """Cancel AND await every task this isolated state spawned.

        `_run_chat` starts title/summary work that is irrelevant to these
        recovery assertions. A bare `task.cancel()` leaves the coroutine pending
        until the loop gets another tick, which surfaces as unawaited-coroutine
        and destroyed-pending-task warnings under xdist. Await the cancellation
        exactly; never sleep and never let one test's teardown spill into another.
        """
        tasks = list(state._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resumed", (False, True))
    async def test_read_then_false_tool_budget_claim_auto_continues(
        self, tmp_path: Path, resumed: bool
    ) -> None:
        """A false blocker after a read replays only the authenticated request."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat_utils import (
            FALSE_TOOL_BLOCKER_REPLAY_KIND,
            RecoveryPayload,
        )
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        state.sessions.get_or_create.return_value = (client, False, resumed)
        state.context_builder = MagicMock()
        state.context_builder.conversation_log = None
        state.context_builder.build_message.side_effect = lambda message, *_args, **_kwargs: (
            message,
            None,
        )
        hidden_context = "SECRET APP CONTEXT: never mirror this"
        slot._pending_context = [{"content": hidden_context, "source": "test-app"}]
        provider_prompts: list[str] = []

        async def _stream(msg):
            provider_prompts.append(msg)
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Reading CR workflow",
                tool_kind="read",
                tool_input="{}",
                tool_call_id="read-1",
                tool_name="fs_read",
                tool_identity_trusted=True,
            )
            yield LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="read-1",
                tool_output="workflow loaded",
                tool_final=True,
            )
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK,
                text=(
                    "I’m proceeding, but this turn’s tool budget was exhausted immediately "
                    "after loading the workflow. No publish or deployment has happened yet."
                ),
            )
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

        file_path = "/tmp/My Report.pdf"
        dir_path = "/tmp/My Reports/"
        request = (
            "Publish and deploy the demo\n"
            f"[attached_file 1] {file_path}\n"
            f"[attached_dir 1] {dir_path}"
        )
        slot.append(
            "user",
            request,
            "msg msg-u",
            meta={"files": [file_path], "dirs": [dir_path]},
        )
        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new=AsyncMock(return_value=False),
        ):
            await _run_chat(
                state,
                slot,
                request,
                _directive_user_origin=True,
            )
            await self._cancel_background_tasks(state)

        replay = next(item for item in slot._queue if item.get("content") == request)
        assert hidden_context in provider_prompts[0]
        assert replay["content"] == request
        assert hidden_context not in replay["content"]
        assert replay["kind"] == FALSE_TOOL_BLOCKER_REPLAY_KIND
        assert replay["payload"] == RecoveryPayload.ORIGINAL
        assert replay["meta"]["files"] == [file_path]
        assert replay["meta"]["dirs"] == [dir_path]
        assert replay["_directive_user_origin"] is True
        assert slot._promise_only_retries == 1
        state.sessions.record_success.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("result_shape", ["failed", "missing"])
    async def test_uncompleted_read_then_false_blocker_does_not_replay(
        self, tmp_path: Path, result_shape: str
    ) -> None:
        """Canonical read identity is insufficient without completed result proof."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream(msg):
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Reading workflow",
                tool_kind="read",
                tool_call_id="read-1",
                tool_name="fs_read",
                tool_identity_trusted=True,
            )
            if result_shape == "failed":
                yield LLMEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="read-1",
                    tool_output="read failed",
                    tool_final=False,
                )
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK,
                text=(
                    "I’m blocked from further tool execution in the resumed session: "
                    "the screenshot delivery tools are no longer callable here."
                ),
            )
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new=AsyncMock(return_value=False),
        ):
            await _run_chat(
                state,
                slot,
                "Publish and deploy the demo",
                _directive_user_origin=True,
            )
            await self._cancel_background_tasks(state)

        assert not slot._queue
        assert slot._promise_only_retries == 0
        state.sessions.record_success.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("identity_trusted", "kiro_backend"),
        ((False, True), (True, False)),
    )
    async def test_untrusted_or_non_kiro_identity_does_not_replay(
        self,
        tmp_path: Path,
        identity_trusted: bool,
        kiro_backend: bool,
    ) -> None:
        """A familiar name needs extractor provenance and a Kiro backend."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        client.is_kiro_backend = kiro_backend

        async def _stream(msg):
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="grep",
                tool_kind="read",
                tool_call_id="read-1",
                tool_name="grep",
                tool_identity_trusted=identity_trusted,
            )
            yield LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="read-1",
                tool_output="matches",
                tool_final=True,
            )
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK,
                text=(
                    "I’m blocked from further tool execution in the resumed session: "
                    "the screenshot delivery tools are no longer callable here."
                ),
            )
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new=AsyncMock(return_value=False),
        ):
            await _run_chat(
                state,
                slot,
                "Publish and deploy the demo",
                _directive_user_origin=True,
            )
            await self._cancel_background_tasks(state)

        assert not slot._queue
        assert slot._promise_only_retries == 0
        state.sessions.record_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_kiro_blocker_near_miss_logs_without_replay(self, tmp_path: Path, caplog) -> None:
        """Wording drift is observable but never grants replay authority."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        near_miss = "I'm blocked from further tool execution."

        async def _stream(msg):
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Reading workflow",
                tool_kind="read",
                tool_call_id="read-1",
                tool_name="fs_read",
                tool_identity_trusted=True,
            )
            yield LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="read-1",
                tool_output="workflow loaded",
                tool_final=True,
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=near_miss)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

        with (
            caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"),
            patch(
                "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
                new=AsyncMock(return_value=False),
            ),
        ):
            await _run_chat(
                state,
                slot,
                "Publish and deploy the demo",
                _directive_user_origin=True,
            )
            await self._cancel_background_tasks(state)

        assert not slot._queue
        assert slot._promise_only_retries == 0
        assert "False tool-blocker wording drift" in caplog.text
        assert near_miss not in caplog.text
        state.sessions.record_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_yolo_read_then_false_blocker_stays_notice_only(self, tmp_path: Path) -> None:
        """Auto-approve never dispatches even an authenticated blocker replay."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        state.is_yolo_active.return_value = True

        async def _stream(msg):
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Reading workflow",
                tool_kind="read",
                tool_input="{}",
                tool_call_id="read-1",
                tool_name="fs_read",
                tool_identity_trusted=True,
            )
            yield LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="read-1",
                tool_output="workflow loaded",
                tool_final=True,
            )
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK,
                text=(
                    "I’m blocked from further tool execution in the resumed session: "
                    "the screenshot delivery tools are no longer callable here."
                ),
            )
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new=AsyncMock(return_value=False),
        ):
            await _run_chat(
                state,
                slot,
                "Publish and deploy the demo",
                _directive_user_origin=True,
            )
            await self._cancel_background_tasks(state)

        assert not slot._queue
        assert slot._promise_only_retries == 0
        notices = [m.get("content", "") for m in slot.messages if m.get("role") == "notice"]
        assert any("Auto-continue is skipped under auto-approve mode" in m for m in notices)
        state.sessions.record_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_user_read_then_false_blocker_does_not_replay(self, tmp_path: Path) -> None:
        """App/system text cannot acquire authenticated-user replay provenance."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream(msg):
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Reading workflow",
                tool_kind="read",
                tool_input="{}",
                tool_call_id="read-1",
                tool_name="fs_read",
                tool_identity_trusted=True,
            )
            yield LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="read-1",
                tool_output="workflow loaded",
                tool_final=True,
            )
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK,
                text=(
                    "I’m blocked from further tool execution in the resumed session: "
                    "the screenshot delivery tools are no longer callable here."
                ),
            )
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new=AsyncMock(return_value=False),
        ):
            await _run_chat(state, slot, "Untrusted app injection")
            await self._cancel_background_tasks(state)

        assert not slot._queue
        assert slot._promise_only_retries == 0
        state.sessions.record_success.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("intervention", ["stop", "followup", "steer"])
    async def test_late_user_intervention_purges_false_blocker_replay(
        self, tmp_path: Path, intervention: str
    ) -> None:
        """A revocation landing after enqueue still wins at dispatch time."""
        from kiro_crew.dashboard.chat_runner import _start_next_queued_turn
        from kiro_crew.dashboard.chat_utils import (
            FALSE_TOOL_BLOCKER_REPLAY_KIND,
            RecoveryPayload,
        )

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot(f"late-{intervention}")
        request = "Delete the deployment"
        slot.queue_insert(
            0,
            request,
            kind=FALSE_TOOL_BLOCKER_REPLAY_KIND,
            payload=RecoveryPayload.ORIGINAL,
            directive_user_origin=True,
        )
        slot._promise_only_retries = 1
        slot.replays.arm(
            ReplayFamily.CONTINUATION,
            entry_id="",
            session_key="",
            stop_gen=slot._stop_generation,
            session_stop_gen=0,
        )

        if intervention == "stop":
            slot._stop_generation += 1
        elif intervention == "followup":
            slot.queue_append("Never mind", directive_user_origin=True)
            # A running child holds the user's follow-up after the purge.
            state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["child"]))
        else:
            slot._pending_steers = [{"content": "Stop; keep it"}]

        cfg = MagicMock()
        cfg.dashboard.merge_queued_messages = False
        with patch(
            "kiro_crew.dashboard.chat_runner.KiroCrewConfig.load",
            return_value=cfg,
        ):
            started = await _start_next_queued_turn(state, slot)

        assert started is False
        assert all(item.get("kind") != FALSE_TOOL_BLOCKER_REPLAY_KIND for item in slot._queue)
        assert slot._promise_only_retries == 0
        corrections = [
            msg.get("content", "")
            for msg in slot.messages
            if msg.get("role") == "notice" and "Auto-continue cancelled" in msg.get("content", "")
        ]
        assert len(corrections) == 1
        if intervention == "stop":
            assert "nothing was run" in corrections[0]
        else:
            assert "takes over" in corrections[0]

    @pytest.mark.asyncio
    async def test_session_scoped_stop_after_enqueue_purges_false_blocker_replay(
        self, tmp_path: Path
    ) -> None:
        """The false-blocker arm snapshots the SESSION stop counter at enqueue.

        A Stop issued on a linked channel surface moves only the session-scoped
        count (the slot's own ``_stop_generation`` never ticks), and the
        dispatch-point purge compares that count against its value AT ENQUEUE
        (the continuation record in ``slot.replays``). Without the arm's snapshot
        the comparison reads a count the arm never took, and the replay
        dispatches over the user's Stop.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat_runner import _start_next_queued_turn
        from kiro_crew.dashboard.chat_utils import (
            FALSE_TOOL_BLOCKER_REPLAY_KIND,
            effective_session_key,
        )
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        state.context_builder = MagicMock()
        state.context_builder.conversation_log = None
        state.context_builder.build_message.side_effect = lambda message, *_args, **_kwargs: (
            message,
            None,
        )
        # A session-scoped Stop counter the runner really reads: the arm's
        # enqueue snapshot and the purge's live read both go through
        # ``sessions.stop_generation(session_key)``.
        stop_counts: dict[str, int] = {}
        state.sessions.stop_generation = lambda key: stop_counts.get(key, 0)
        session_key = effective_session_key(slot)
        # Non-zero baseline: the record a slot starts with counts zero, so the
        # revalidation below reads "moved" unless the arm took the enqueue-time
        # count.
        stop_counts[session_key] = 5

        async def _stream(msg):
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Reading CR workflow",
                tool_kind="read",
                tool_input="{}",
                tool_call_id="read-1",
                tool_name="fs_read",
                tool_identity_trusted=True,
            )
            yield LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="read-1",
                tool_output="workflow loaded",
                tool_final=True,
            )
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK,
                text=(
                    "I’m proceeding, but this turn’s tool budget was exhausted "
                    "immediately after loading the workflow. No publish or "
                    "deployment has happened yet."
                ),
            )
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

        request = "Publish and deploy the demo"
        slot.append("user", request, "msg msg-u")
        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new=AsyncMock(return_value=False),
        ):
            await _run_chat(state, slot, request, _directive_user_origin=True)
            await self._cancel_background_tasks(state)

        replay = next(item for item in slot._queue if item.get("content") == request)
        assert replay["kind"] == FALSE_TOOL_BLOCKER_REPLAY_KIND
        # The arm must snapshot the ENQUEUE-time session count, exactly as the
        # sibling recovery arms do: with no Stop since, the record is not moved.
        assert not slot.replays.revalidate(
            ReplayFamily.CONTINUATION,
            LiveSlot(
                session_key=session_key,
                stop_generation=slot._stop_generation,
                stopping=False,
                user_input=False,
                session_stop_generation=lambda key: stop_counts.get(key, 0),
            ),
        ).stop_moved

        # A channel-side Stop lands while the replay waits: only the session
        # count moves. The dispatch-point purge must drop the replay.
        stop_counts[session_key] = 6
        cfg = MagicMock()
        cfg.dashboard.merge_queued_messages = False
        with patch(
            "kiro_crew.dashboard.chat_runner.KiroCrewConfig.load",
            return_value=cfg,
        ):
            started = await _start_next_queued_turn(state, slot)

        assert started is False
        assert all(item.get("kind") != FALSE_TOOL_BLOCKER_REPLAY_KIND for item in slot._queue)
        assert slot._promise_only_retries == 0
        assert slot._stop_generation == 0, "the slot's own Stop state was never touched"
        assert any(
            m.get("role") == "notice" and "nothing was run" in m.get("content", "")
            for m in slot.messages
        )


class TestProductiveTurnNeverReplaysVerbatim:
    """A turn that DID work must never have the user's message replayed verbatim.

    ``assistant_text`` is reset at every tool boundary, so a turn that streamed a
    preamble and then called a tool arrives at the terminal chain with an empty
    final segment and takes the empty-response branch. Rung 1 of that ladder
    re-queues the ORIGINAL message, which re-runs every tool call that already
    completed — a second ``send_message``, a second write, a second PR — and
    re-derives an answer the user has already read.

    The field incident these tests pin: two consecutive billed turns, each with an
    assistant preamble and successful tool calls and each ending on a clean
    ``end_turn``, were both classified empty; the first verbatim-replayed the
    user's message.

    Reuses ``TestEmptyResponseRetry``'s harness rather than a second one, so a
    change to how a slot is built cannot leave these tests driving a different
    runner than the ladder tests in ``TestEmptyResponseRetryLadder``.
    """

    _make_state_and_slot = TestEmptyResponseRetry._make_state_and_slot

    @staticmethod
    def _spy_queue(monkeypatch=None):
        """Record ``(index, content)`` for every queue insert."""
        calls: list[tuple] = []
        orig = _ChatSlot.queue_insert

        def spy(self_slot, *a, **kw):
            calls.append(a)
            return orig(self_slot, *a, **kw)

        return calls, spy

    _cancel_background_tasks = staticmethod(TestEmptyResponseRetry._cancel_background_tasks)

    @staticmethod
    def _text_then_tool_stream(client, executed):
        """TEXT -> TOOL_CALL -> TOOL_RESULT -> COMPLETE(end_turn).

        ``executed`` counts dispatches so a replay of the original message would
        be observable as a second execution rather than only as a queue entry.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="checking")
            executed.append(msg)
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-1",
                title="send_message",
            )
            yield LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", text="sent")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

    @staticmethod
    def _tool_only_stream(client):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", title="send_message")
            yield LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", text="sent")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

    @staticmethod
    def _thinking_only_stream(client):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_THINKING_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_THINKING_CHUNK, text="hmm")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

    @pytest.mark.asyncio
    async def test_text_then_tool_turn_continues_instead_of_replaying(self, tmp_path: Path) -> None:
        """The incident's own shape: preamble + a completed tool call, clean
        end_turn. The original message must NOT be re-queued; exactly one
        synthetic continuation may be, and the completed tool must not re-run."""
        from kiro_crew.dashboard.chat_utils import (
            _ACTIVITY_NO_REPLY_CONTINUE_MSG,
            _EMPTY_AUTO_CONTINUE_MSG,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        executed: list[str] = []
        self._text_then_tool_stream(client, executed)
        calls, spy = self._spy_queue()

        with patch.object(_ChatSlot, "queue_insert", spy):
            await _run_chat(state, slot, "send the summary")
            await self._cancel_background_tasks(state)

        # The verbatim replay is the defect. It must be absent.
        assert (0, "send the summary") not in calls, (
            "the ORIGINAL user message was re-queued after a turn that already "
            "ran a tool — the replay re-executes completed side effects"
        )
        # Exactly one continuation, and it is the one whose body does not claim
        # the turn produced nothing (which would invite the model to redo the
        # completed call).
        _continuations = [c for c in calls if c and c[1] == _ACTIVITY_NO_REPLY_CONTINUE_MSG]
        assert len(_continuations) == 1, f"expected one continuation, got {calls}"
        # The productive turn skipped rung 1 (verbatim replay), so this one
        # continuation consumes the whole bounded ladder. If the continuation
        # itself returns empty, it gives up rather than enqueueing a SECOND
        # continuation whose wording would again invite rework.
        assert slot._empty_response_retries == 2
        assert (0, _EMPTY_AUTO_CONTINUE_MSG) not in calls, (
            "the empty-response body tells the model its turn produced no "
            "output, which is false for a turn that ran a tool"
        )
        # The tool ran once. The queue was not drained in this test, so a second
        # execution could only come from a replay dispatched inside this turn.
        assert executed == ["send the summary"], f"tool dispatched more than once: {executed}"

    @pytest.mark.asyncio
    async def test_tool_only_turn_does_not_replay_verbatim(self, tmp_path: Path) -> None:
        """A turn with tool calls and no text at all still counts as productive."""
        from kiro_crew.dashboard.chat_utils import _ACTIVITY_NO_REPLY_CONTINUE_MSG

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._tool_only_stream(client)
        calls, spy = self._spy_queue()

        with patch.object(_ChatSlot, "queue_insert", spy):
            await _run_chat(state, slot, "post it")
            await self._cancel_background_tasks(state)

        assert (0, "post it") not in calls, "tool-only turn verbatim-replayed the prompt"
        assert (0, _ACTIVITY_NO_REPLY_CONTINUE_MSG) in calls

    @pytest.mark.asyncio
    async def test_thinking_only_turn_does_not_replay_verbatim(self, tmp_path: Path) -> None:
        """Thinking is work the replay would discard and re-charge for."""
        from kiro_crew.dashboard.chat_utils import _ACTIVITY_NO_REPLY_CONTINUE_MSG

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._thinking_only_stream(client)
        calls, spy = self._spy_queue()

        with patch.object(_ChatSlot, "queue_insert", spy):
            await _run_chat(state, slot, "think about it")
            await self._cancel_background_tasks(state)

        assert (0, "think about it") not in calls, "thinking-only turn verbatim-replayed the prompt"
        assert (0, _ACTIVITY_NO_REPLY_CONTINUE_MSG) in calls

    @pytest.mark.asyncio
    async def test_activity_free_turn_keeps_the_verbatim_replay_rung(self, tmp_path: Path) -> None:
        """The guard is scoped: a GENUINELY activity-free empty turn keeps rung 1.

        Without this, the fix could be 'never replay', which would silently
        remove the self-heal that a real provider-side empty depends on.
        """
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        TestEmptyResponseRetry._make_empty_stream(self, client)
        calls, spy = self._spy_queue()

        with (
            patch.object(_ChatSlot, "queue_insert", spy),
            patch(
                "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
                new=AsyncMock(return_value=False),
            ),
        ):
            await _run_chat(state, slot, "test message")
            await self._cancel_background_tasks(state)

        assert (0, "test message") in calls, "the activity-free replay rung was removed"
        assert slot._empty_response_retries == 1


class TestEmptyTurnDiagnostics:
    """The empty-response WARNING must name a CLOSED cause, and carry no content.

    The incident it exists for had three attempts with three different causes and
    one log line that could not tell them apart. These tests assert on the closed
    vocabulary only — never on a prompt, a response, a tool argument, a path, an
    identity, a token count or a cost, because asserting on those would pin
    exactly the leak the diagnostics are designed to avoid.
    """

    _make_state_and_slot = TestEmptyResponseRetry._make_state_and_slot
    _cancel_background_tasks = staticmethod(TestEmptyResponseRetry._cancel_background_tasks)

    @staticmethod
    def _causes(caplog):
        """The ``cause=`` values of every empty-response warning captured."""
        out = []
        for rec in caplog.records:
            msg = rec.getMessage()
            if "Empty model response" not in msg:
                continue
            for field in msg.split():
                if field.startswith("cause="):
                    out.append(field.split("=", 1)[1])
        return out

    @staticmethod
    def _fields(caplog):
        """``key=value`` pairs of the first empty-response warning captured."""
        for rec in caplog.records:
            msg = rec.getMessage()
            if "Empty model response" not in msg:
                continue
            return dict(
                tuple(f.split("=", 1)) for f in msg.split() if "=" in f and not f.startswith("%")
            )
        return {}

    def test_the_cause_and_rung_vocabularies_are_closed(self) -> None:
        """Pure-unit: every cause the classifier can return, from its own inputs.

        Kept as a unit test over ``classify_empty_turn`` rather than seven
        ``_run_chat`` drives: the ranking between overlapping causes is the part
        that can regress silently, and a stream fixture cannot express
        'synthesized terminal AND tool activity' as cleanly as the snapshot can.
        """
        from kiro_crew.dashboard.chat_utils import (
            EMPTY_CAUSE_NO_TERMINAL,
            EMPTY_CAUSE_OTHER,
            EMPTY_CAUSE_PROVIDER_EMPTY,
            EMPTY_CAUSE_SYNTHETIC,
            EMPTY_CAUSE_THINKING_ONLY,
            EMPTY_CAUSE_TOOL_ONLY,
            EMPTY_CAUSE_VISIBLE_PARTIAL,
            STOP_REASON_ABSENT,
            EmptyTurnActivity,
            classify_empty_turn,
        )

        # No terminal outranks everything, including activity.
        assert (
            classify_empty_turn(EmptyTurnActivity(saw_terminal=False, had_tools=True))
            == EMPTY_CAUSE_NO_TERMINAL
        )
        # A synthesized terminal outranks activity: the emptiness is ours.
        assert (
            classify_empty_turn(
                EmptyTurnActivity(saw_terminal=True, terminal_synthetic=True, had_tools=True)
            )
            == EMPTY_CAUSE_SYNTHETIC
        )
        # A flushed visible segment outranks tools: the user read an answer.
        assert (
            classify_empty_turn(
                EmptyTurnActivity(saw_terminal=True, flushed_visible=True, had_tools=True)
            )
            == EMPTY_CAUSE_VISIBLE_PARTIAL
        )
        assert (
            classify_empty_turn(EmptyTurnActivity(saw_terminal=True, had_tools=True))
            == EMPTY_CAUSE_TOOL_ONLY
        )
        assert (
            classify_empty_turn(EmptyTurnActivity(saw_terminal=True, had_thinking=True))
            == EMPTY_CAUSE_THINKING_ONLY
        )
        # A clean end_turn with nothing at all is the only genuine provider empty.
        assert (
            classify_empty_turn(EmptyTurnActivity(saw_terminal=True, stop_reason="end_turn"))
            == EMPTY_CAUSE_PROVIDER_EMPTY
        )
        # An OMITTED stop reason is NOT a provider empty — the distinction the
        # incident's third attempt needed and did not have.
        assert (
            classify_empty_turn(
                EmptyTurnActivity(saw_terminal=True, stop_reason=STOP_REASON_ABSENT)
            )
            == EMPTY_CAUSE_OTHER
        )

    def test_normalize_stop_reason_never_returns_a_raw_backend_string(self) -> None:
        """Closed output set, and the ACP spellings it mirrors are pinned.

        ``chat_utils`` may not import ``kiro_crew.acp.types`` (the agent-SDK
        boundary gate baselines it at one edge and a baselined file may not grow),
        so it spells the three stop reasons as literals. The test tree is outside
        that gate, so the pin lives here — a change to the backend's vocabulary
        reddens here instead of silently reclassifying every turn as ``other``.
        """
        from kiro_crew.acp.types import (
            STOP_REASON_CANCELLED,
            STOP_REASON_END_TURN,
            STOP_REASON_REFUSAL,
        )
        from kiro_crew.dashboard.chat_utils import (
            _STOP_CANCELLED_REASON,
            _STOP_END_TURN,
            _STOP_REFUSAL,
            STOP_REASON_ABSENT,
            STOP_REASON_OTHER,
            normalize_stop_reason,
        )

        assert _STOP_END_TURN == STOP_REASON_END_TURN
        assert _STOP_CANCELLED_REASON == STOP_REASON_CANCELLED
        assert _STOP_REFUSAL == STOP_REASON_REFUSAL

        assert normalize_stop_reason(None) == STOP_REASON_ABSENT
        assert normalize_stop_reason("") == STOP_REASON_ABSENT
        assert normalize_stop_reason(STOP_REASON_END_TURN) == STOP_REASON_END_TURN
        assert normalize_stop_reason("error: tool stall") == "error"
        # An invented backend reason is folded, never echoed.
        _invented = "the model said stop because of /home/someone/secret.txt"
        assert normalize_stop_reason(_invented) == STOP_REASON_OTHER

    @pytest.mark.asyncio
    async def test_provider_empty_and_no_terminal_event_report_distinct_causes(
        self, tmp_path: Path, caplog
    ) -> None:
        """Two turns that look identical to the old log line must not any more."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat_utils import (
            EMPTY_CAUSE_NO_TERMINAL,
            EMPTY_CAUSE_PROVIDER_EMPTY,
        )
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        # A clean end_turn with nothing in it.
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _clean(msg):
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _clean
        client.stream_command = _clean
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
            await _run_chat(state, slot, "a", _prompt_depth=1)
        await self._cancel_background_tasks(state)
        assert self._causes(caplog) == [EMPTY_CAUSE_PROVIDER_EMPTY]

        # A stream that ends without any terminal event at all.
        caplog.clear()
        state2, slot2, client2, _run_chat2 = self._make_state_and_slot(tmp_path)

        async def _no_terminal(msg):
            return
            yield  # pragma: no cover -- makes this an async generator

        client2.stream = _no_terminal
        client2.stream_command = _no_terminal
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
            await _run_chat2(state2, slot2, "a", _prompt_depth=1)
        await self._cancel_background_tasks(state2)
        assert self._causes(caplog) == [EMPTY_CAUSE_NO_TERMINAL]

    @pytest.mark.asyncio
    async def test_omitted_stop_reason_is_not_reported_as_provider_empty(
        self, tmp_path: Path, caplog
    ) -> None:
        """A terminal with no stopReason is its own observation."""
        from kiro_crew.dashboard.chat_utils import (
            EMPTY_CAUSE_OTHER,
            STOP_REASON_ABSENT,
        )
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE)  # no stop_reason

        client.stream = _stream
        client.stream_command = _stream
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
            await _run_chat(state, slot, "a", _prompt_depth=1)
        await self._cancel_background_tasks(state)

        assert self._causes(caplog) == [EMPTY_CAUSE_OTHER]
        assert self._fields(caplog).get("stop_reason") == STOP_REASON_ABSENT

    @pytest.mark.asyncio
    async def test_productive_turn_reports_visible_partial_and_the_continue_rung(
        self, tmp_path: Path, caplog
    ) -> None:
        """The incident's shape gets its own cause AND the rung it took.

        Asserting the rung is what makes the diagnostic answer 'what did the
        runner DO', not only 'what did it see'.
        """
        from kiro_crew.dashboard.chat_utils import (
            EMPTY_CAUSE_VISIBLE_PARTIAL,
            EMPTY_RUNG_CONTINUE,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        TestProductiveTurnNeverReplaysVerbatim._text_then_tool_stream(client, [])

        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
            await _run_chat(state, slot, "send the summary")
            await self._cancel_background_tasks(state)

        assert self._causes(caplog) == [EMPTY_CAUSE_VISIBLE_PARTIAL]
        _fields = self._fields(caplog)
        assert _fields.get("rung") == EMPTY_RUNG_CONTINUE
        assert _fields.get("flushed_visible") == "True"
        assert _fields.get("tools") == "True"

    @pytest.mark.asyncio
    async def test_the_warning_carries_no_content_and_no_billing_amounts(
        self, tmp_path: Path, caplog
    ) -> None:
        """The privacy floor, asserted against the rendered line.

        Every diagnostic value is a bool or a closed constant, so the prompt text,
        the streamed text, the tool title and the billed amounts must be absent
        from the line even though the turn carried all of them.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN, TurnUsage
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="SECRETPREAMBLE")
            yield LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", title="SECRETTOOL")
            yield LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason=STOP_REASON_END_TURN,
                usage=TurnUsage(credits=4.25, input_tokens=1234, output_tokens=99),
            )

        client.stream = _stream
        client.stream_command = _stream
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
            await _run_chat(state, slot, "SECRETPROMPT")
            await self._cancel_background_tasks(state)

        _line = next(
            rec.getMessage() for rec in caplog.records if "Empty model response" in rec.getMessage()
        )
        for forbidden in ("SECRETPROMPT", "SECRETPREAMBLE", "SECRETTOOL", "4.25", "1234", "99"):
            assert forbidden not in _line, f"{forbidden!r} leaked into {_line!r}"
        # Billing presence IS reported — as a bool, which is the point.
        assert "billed=True" in _line
