"""Dashboard chat: ``_run_chat`` recovering from a busy prompt and a dead ACP process."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from test_dashboard_chat import TestAcpProcessDiedRecovery as _AcpDiedSuite


class TestPromptBusyRecovery:
    """When kiro-cli returns 'Prompt already in progress', _run_chat must
    reset the session and re-queue the message so the next attempt cold-starts."""

    @pytest.mark.asyncio
    async def test_prompt_busy_resets_session_and_requeues(self, tmp_path: Path) -> None:
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("busy-slot")
        slot.append("user", "hello", "msg msg-u")

        # Make client.stream raise "already in progress"
        mock_client = state.sessions.get_or_create.return_value[0]

        async def _raise_busy(msg):
            raise AcpError("Prompt error: {'data': 'Prompt already in progress'}")
            yield  # make it an async generator  # noqa: E501

        mock_client.stream = _raise_busy
        mock_client.stream_command = _raise_busy
        mock_client.shutdown = AsyncMock()

        await _run_chat(state, slot, "test message")

        # Session must be reset (kill the stuck kiro-cli process)
        state.sessions.reset.assert_awaited_once()
        # The finally block drains the re-queued message into a new task
        assert slot.task is not None
        # No ❌ error shown to the user for the busy case
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert not any("already in progress" in m.get("content", "") for m in error_msgs)

    @pytest.mark.asyncio
    async def test_process_exited_resets_session_and_requeues(self, tmp_path: Path) -> None:
        """When ACP subprocess dies (SIGTERM/SIGKILL), _run_chat must reset
        the session and re-queue the message so autonudges land on a fresh
        provider instead of a bare ❌ error card with no work done."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("dead-slot")
        slot.append("user", "hello", "msg msg-u")

        mock_client = state.sessions.get_or_create.return_value[0]

        async def _raise_dead(msg):
            raise AcpError("ACP process exited (code=-15)")
            yield  # make it an async generator  # noqa: E501

        mock_client.stream = _raise_dead
        mock_client.stream_command = _raise_dead
        mock_client.shutdown = AsyncMock()

        await _run_chat(state, slot, "test message")

        state.sessions.reset.assert_awaited_once()
        assert slot.task is not None
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert not any("process exited" in m.get("content", "") for m in error_msgs)


class TestAcpProcessDiedRecoveryRequeue:
    """TestAcpProcessDiedRecovery: the requeue, the retry budget, stop suppression and the counters."""

    _make_state_and_slot = _AcpDiedSuite._make_state_and_slot
    _make_stream_raise = _AcpDiedSuite._make_stream_raise

    @pytest.mark.asyncio
    async def test_retry_at_depth_0_requeues_message(self, tmp_path: Path) -> None:
        """First pipe death at depth 0 → message re-queued, retrying shown."""
        from kiro_crew.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message")

        state.sessions.reset.assert_awaited_once()
        assert slot._acp_pipe_death_retries == 1
        # Retry shows a single PERSISTED error card (reliably visible at
        # turn-teardown, unlike an ephemeral chat_status which the frontend drops
        # once the streaming turn ends). slot.append both persists the card and
        # emits a single chat_message via _on_message.
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("retrying" in m.get("content", "") for m in error_msgs)
        # Regression guard for the duplicate-card bug: the retry card must NOT be
        # ALSO explicitly broadcast over chat_message (slot.append already emits it
        # once via _on_message). A redundant broadcast_ws renders a second card.
        dup_broadcasts = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args
            and c.args[0] == "chat_message"
            and len(c.args) > 1
            and "retrying" in c.args[1].get("content", "")
        ]
        assert dup_broadcasts == [], "retry card must not be double-emitted via broadcast_ws"

    @pytest.mark.parametrize("ambiguous", [False, True])
    @pytest.mark.asyncio
    async def test_an_ambiguous_death_requeues_written_steers_as_possibly_delivered(
        self, tmp_path: Path, ambiguous: bool
    ) -> None:
        """A steer written into a turn sits in the same pipe as its prompt, so
        when the turn's death is ambiguous the teardown requeues it with the
        possibly-delivered mark; a plain death requeues it plainly."""
        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.dashboard.chat_utils import STEER_POSSIBLY_DELIVERED_META

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _steered_then_died(msg):
            slot._pending_steers.append("also tag it")  # written mid-turn
            raise AcpProcessDied("stdin stalled", ambiguous_delivery=ambiguous)
            yield  # noqa: E501

        client.stream = _steered_then_died
        client.stream_command = _steered_then_died
        from kiro_crew.dashboard import chat_runner

        requeued: list[dict] = []
        _real = chat_runner._requeue_unconsumed_steers

        def _spy(state_, slot_):
            _real(state_, slot_)
            requeued.extend(e for e in slot_._queue if e.get("content") == "also tag it")

        with patch.object(chat_runner, "_requeue_unconsumed_steers", _spy):
            await _run_chat(state, slot, "test message")

        assert len(requeued) == 1
        assert bool((requeued[0].get("meta") or {}).get(STEER_POSSIBLY_DELIVERED_META)) is ambiguous

    @pytest.mark.asyncio
    async def test_a_possibly_delivered_steer_note_leads_only_the_model_input(
        self, tmp_path: Path
    ) -> None:
        """The note reaches the model; slash-command dispatch and the mirror still
        read the user's own text."""
        from kiro_crew.dashboard.chat_utils import STEER_POSSIBLY_DELIVERED_NOTE

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        sent: list[str] = []

        async def _capture(msg):
            sent.append(msg)
            return
            yield  # noqa: E501

        client.stream = _capture
        await _run_chat(state, slot, "also tag it", _steer_possibly_delivered=True)

        assert sent, "the turn must reach the model"
        assert STEER_POSSIBLY_DELIVERED_NOTE.strip() in sent[0]
        assert sent[0].rstrip().endswith("also tag it")

    @pytest.mark.asyncio
    async def test_budget_exhaustion_shows_stuck(self, tmp_path: Path) -> None:
        """4th pipe death → 'Session stuck' shown, no re-queue."""
        from kiro_crew.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._acp_pipe_death_retries = 3  # already exhausted
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message")

        assert slot._acp_pipe_death_retries == 4
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("stuck" in m.get("content", "").lower() for m in error_msgs)

    @pytest.mark.asyncio
    async def test_nested_depth_shows_please_retry(self, tmp_path: Path) -> None:
        """Pipe death at depth > 0 → 'please retry' shown, no re-queue."""
        from kiro_crew.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message", _prompt_depth=1)

        assert slot._acp_pipe_death_retries == 1
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("please retry" in m.get("content", "").lower() for m in error_msgs)

    @pytest.mark.asyncio
    async def test_prompt_busy_requeue_does_not_claim_a_lost_connection(
        self, tmp_path: Path
    ) -> None:
        """A busy-session reset must requeue the busy continuation, not the connection one.

        Both causes reset the session and requeue a continuation, and the queued
        marker is what the transcript renders -- so borrowing the connection
        marker here reports a dropped connection to a user whose status card
        says the session was busy.
        """
        from kiro_crew.dashboard.chat_runner import PromptBusyExhaustedError
        from kiro_crew.dashboard.chat_utils import (
            _BUSY_RECOVER_MSG,
            SYNTHETIC_RECOVERY_KIND,
            RecoveryPayload,
        )
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._titled = True

        async def _stream_then_busy(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial output")
            raise PromptBusyExhaustedError("busy")

        client.stream = _stream_then_busy
        client.stream_command = _stream_then_busy

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(state, slot, "test message")

        assert slot._queue == [
            {
                "id": slot._queue[0]["id"],
                "content": _BUSY_RECOVER_MSG,
                "kind": SYNTHETIC_RECOVERY_KIND,
                # A continuation, not the user's request: the turn had emitted, so
                # this text is the runner's and must not mirror as user speech.
                "payload": RecoveryPayload.CONTINUATION,
                # Admission stamp: recovery requeues record the containment
                # that held at requeue so the drain can re-validate the retry.
                "meta": slot._queue[0]["meta"],
            }
        ]
        # The status card and the queued marker describe the same event to two
        # audiences; they disagreed until the continuation became cause-aware.
        errors = [m.get("content", "") for m in slot.messages if m.get("role") == "error"]
        assert any("Session busy" in text for text in errors)
        assert not any("Connection lost" in text for text in errors)

    @pytest.mark.asyncio
    async def test_a_second_failure_before_output_keeps_the_text_machine_authored(
        self, tmp_path: Path
    ) -> None:
        """A recovery turn that dies again before emitting must stay machine-authored.

        The requeue replays ``message`` unchanged when nothing was emitted -- but on a
        second consecutive failure that message is the runner's own continuation from
        the previous recovery, not the user's request. Tagging it ORIGINAL makes the
        next dequeue mirror internal orchestration to a linked thread as user speech.
        """
        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.dashboard.chat_utils import (
            _CONN_RECOVER_MSG,
            SYNTHETIC_RECOVERY_KIND,
            RecoveryPayload,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._titled = True
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(state, slot, _CONN_RECOVER_MSG, _synthetic_payload=True)

        assert slot._queue == [
            {
                "id": slot._queue[0]["id"],
                "content": _CONN_RECOVER_MSG,
                "kind": SYNTHETIC_RECOVERY_KIND,
                "payload": RecoveryPayload.CONTINUATION,
                # Admission stamp: recovery requeues record the containment
                # that held at requeue so the drain can re-validate the retry.
                "meta": slot._queue[0]["meta"],
            }
        ]

    @pytest.mark.asyncio
    async def test_a_recovery_turns_reply_still_reaches_the_linked_thread(
        self, tmp_path: Path
    ) -> None:
        """Withholding the user echo must not also withhold the assistant reply.

        A Slack-linked turn that emits output and then loses its connection recovers as
        a synthetic continuation. Skipping the whole mirror SETUP for that turn leaves
        no thread to reply into, so the continuation's answer is never delivered and the
        question asked on Slack stays unanswered.
        """
        from kiro_crew.dashboard.chat_utils import _CONN_RECOVER_MSG
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._titled = True
        state.sessions.get_slack_link = MagicMock(return_value=("ts-1", "C123"))
        state.slack_client = AsyncMock()
        state.slack_client.start_stream = AsyncMock(return_value="")

        async def _stream_text(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the finished answer")

        client.stream = _stream_text
        client.stream_command = _stream_text

        await _run_chat(state, slot, _CONN_RECOVER_MSG, _synthetic_payload=True)

        posted = [str(c.args) for c in state.slack_client.post_message.await_args_list]
        assert any(
            "the finished answer" in a for a in posted
        ), f"recovery reply never delivered to the linked thread; posted={posted}"
        # The user echo stays withheld: that text is the runner's, not the user's.
        assert not any("\U0001f4ac" in a for a in posted), f"echoed runner text: {posted}"

    @pytest.mark.asyncio
    async def test_session_reset_propagated(self, tmp_path: Path) -> None:
        """Verify the finally block resets the session after AcpProcessDied."""
        from kiro_crew.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message")

        state.sessions.reset.assert_awaited_once_with(f"dashboard:{slot.key}")

    @pytest.mark.asyncio
    async def test_stop_state_suppresses_requeue(self, tmp_path: Path) -> None:
        """AcpProcessDied during active stop → no re-queue."""
        from kiro_crew.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._stop_state = "killing"
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message")

        assert not slot._queue, "message should not be re-queued during active stop"

    @pytest.mark.asyncio
    async def test_stop_state_suppresses_requeue_prompt_busy(self, tmp_path: Path) -> None:
        """PromptBusyExhaustedError during active stop → no re-queue."""
        from kiro_crew.dashboard.chat_runner import PromptBusyExhaustedError

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._stop_state = "soft_pending"
        self._make_stream_raise(client, PromptBusyExhaustedError("busy"))

        await _run_chat(state, slot, "test message")

        assert not slot._queue, "message should not be re-queued during active stop"

    @pytest.mark.asyncio
    async def test_stop_state_suppresses_requeue_acp_error(self, tmp_path: Path) -> None:
        """AcpError retry-eligible during active stop → no re-queue."""
        from kiro_crew.acp.client import AcpError

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._stop_state = "killing"
        self._make_stream_raise(client, AcpError("process exited"))

        await _run_chat(state, slot, "test message")

        assert not slot._queue, "message should not be re-queued during active stop"

    @pytest.mark.asyncio
    async def test_should_suppress_requeue_helper(self, tmp_path: Path) -> None:
        """_should_suppress_requeue returns True for non-idle states."""
        from kiro_crew.dashboard.chat_runner import _should_suppress_requeue

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("helper-test")

        slot._stop_state = "idle"
        assert _should_suppress_requeue(slot) is False

        slot._stop_state = "soft_pending"
        assert _should_suppress_requeue(slot) is True

        slot._stop_state = "killing"
        assert _should_suppress_requeue(slot) is True

    @pytest.mark.asyncio
    async def test_retry_requeues_via_queue_insert(self, tmp_path: Path) -> None:
        """First pipe death at depth 0 → queue_insert is called."""
        from unittest.mock import patch as _patch

        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.dashboard.state import _ChatSlot

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        calls = []
        orig = _ChatSlot.queue_insert

        def spy(self_slot, *a, **kw):
            calls.append(a)
            return orig(self_slot, *a, **kw)

        with _patch.object(_ChatSlot, "queue_insert", spy):
            await _run_chat(state, slot, "test message")

        assert (0, "test message") in calls

    @pytest.mark.asyncio
    async def test_retry_requeues_with_consumption_callback(self, tmp_path: Path) -> None:
        """A pre-consumption pipe-death retry keeps its producer callback."""
        from unittest.mock import Mock
        from unittest.mock import patch as _patch

        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.dashboard.state import _ChatSlot

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))
        on_consumed = Mock()
        on_irreversibly_consumed = Mock()
        callbacks = []
        orig = _ChatSlot.queue_insert

        def spy(self_slot, *args, **kwargs):
            callbacks.append(
                (
                    kwargs.get("on_consumed"),
                    kwargs.get("on_irreversibly_consumed"),
                )
            )
            return orig(self_slot, *args, **kwargs)

        with (
            _patch.object(_ChatSlot, "queue_insert", spy),
            _patch(
                "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
                new=AsyncMock(return_value=False),
            ),
            _patch("kiro_crew.dashboard.chat_runner._finish_queue_cycle"),
        ):
            await _run_chat(
                state,
                slot,
                "test message",
                _on_consumed=on_consumed,
                _on_irreversibly_consumed=on_irreversibly_consumed,
            )

        assert callbacks[0] == (on_consumed, on_irreversibly_consumed)

    @pytest.mark.parametrize("event_type", ["text", "tool"])
    @pytest.mark.asyncio
    async def test_output_reports_irreversible_consumption_before_turn_end(
        self, tmp_path: Path, event_type: str
    ) -> None:
        """First token or tool closes durable replay windows immediately."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        irreversible_reported = asyncio.Event()
        allow_turn_finish = asyncio.Event()

        async def _stream(_message):
            if event_type == "text":
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="started")
            else:
                yield LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read")
            await allow_turn_finish.wait()
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream
        turn = asyncio.create_task(
            _run_chat(
                state,
                slot,
                "test message",
                _on_irreversibly_consumed=irreversible_reported.set,
            )
        )

        report = asyncio.create_task(irreversible_reported.wait())
        # flake-ok: pre-existing rendezvous, the turn holds at allow_turn_finish until the consumption report lands; moved verbatim, tracked for a de-flake pass
        done, _pending = await asyncio.wait(
            (turn, report),
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert report in done, turn.exception()
        assert not turn.done()

        allow_turn_finish.set()
        await turn

    @pytest.mark.asyncio
    async def test_acperror_process_exited_uses_pipe_death_counter(self, tmp_path: Path) -> None:
        """Option Y: AcpError 'process exited' increments the pipe-death counter, not busy."""
        from kiro_crew.acp.client import AcpError

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpError("ACP process exited (code=1)"))

        await _run_chat(state, slot, "test message")

        assert slot._acp_pipe_death_retries == 1
        assert slot._prompt_busy_retries == 0
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("Connection lost" in m.get("content", "") for m in error_msgs)
        # A recovery IS queued here, so the notice must be tagged: an untagged row is
        # indistinguishable from a terminal failure and re-offers an executing choice.
        from kiro_crew.dashboard.chat_utils import TRANSIENT_RETRY_KIND

        _retrying = [m for m in error_msgs if "retrying" in m.get("content", "").lower()]
        assert _retrying, "no retrying notice to inspect"
        assert all((m.get("meta") or {}).get("kind") == TRANSIENT_RETRY_KIND for m in _retrying)
        # LIVE path: slot.append already emits one tagged chat_message, so an explicit
        # frame here would be an untagged duplicate that re-arms the pills mid-backoff.
        _dupes = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args
            and c.args[0] == "chat_message"
            and isinstance(c.args[1], dict)
            and "retrying" in str(c.args[1].get("content", "")).lower()
        ]
        assert not _dupes, f"untagged explicit chat_message frame(s): {_dupes}"

    @pytest.mark.asyncio
    async def test_acperror_already_in_progress_uses_busy_counter(self, tmp_path: Path) -> None:
        """Option Y: AcpError 'already in progress' increments the busy counter, not pipe-death."""
        from kiro_crew.acp.client import AcpError

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpError("Prompt already in progress"))

        await _run_chat(state, slot, "test message")

        assert slot._prompt_busy_retries == 1
        assert slot._acp_pipe_death_retries == 0
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("Session busy" in m.get("content", "") for m in error_msgs)

    @pytest.mark.asyncio
    async def test_prompt_busy_exhausted_depth_gt0_shows_please_retry(self, tmp_path: Path) -> None:
        """PromptBusyExhaustedError at _prompt_depth>0 with budget remaining hits the
        else branch: surfaces a 'Session busy — please retry' card (not a silent
        failure) and does NOT re-queue (mirrors the AcpProcessDied depth>0 handling)."""
        from kiro_crew.llm_helpers import PromptBusyExhaustedError

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, PromptBusyExhaustedError("busy"))

        await _run_chat(state, slot, "test message", _prompt_depth=1)

        # depth>0 + counter (1) <= 3: neither the retry-and-requeue `if` nor the
        # `elif > 3` fires, so the new `else` surfaces feedback instead of failing
        # silently. No re-queue at depth>0.
        assert slot._prompt_busy_retries == 1
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("please retry" in m.get("content", "").lower() for m in error_msgs)
        assert any("Session busy" in m.get("content", "") for m in error_msgs)

    @pytest.mark.asyncio
    async def test_acperror_process_exited_depth_gt0_resets_no_requeue(
        self, tmp_path: Path
    ) -> None:
        """A pipe-death AcpError ('process exited') at _prompt_depth>0 must STILL reset
        the dead session and increment the pipe-death counter (mirrors AcpProcessDied /
        PromptBusyExhaustedError), and surface a 'Connection lost — please retry' card —
        but NOT re-queue (re-queue is depth-0 only). Gating the whole reset/counter
        block on `_prompt_depth == 0` makes a depth>0 pipe-death fall through to
        the generic else: no session reset (the next turn hit the dead process) and the
        failure never counted toward the exhaustion threshold."""
        from kiro_crew.acp.client import AcpError

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpError("ACP process exited (code=1)"))

        await _run_chat(state, slot, "test message", _prompt_depth=1)

        # The dead process IS reset and the failure IS counted, regardless of depth.
        state.sessions.reset.assert_awaited_once()
        assert slot._acp_pipe_death_retries == 1
        # A friendly feedback card (not a bare ❌ raw-error card), and NO re-queue.
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("Connection lost" in m.get("content", "") for m in error_msgs)
        assert any("please retry" in m.get("content", "").lower() for m in error_msgs)
        assert not slot._queue, "depth>0 must not re-queue"
        # Discriminating control: nothing is queued, so this notice is genuinely terminal
        # and must NOT be tagged -- tagging it would hide pills a user still needs.
        from kiro_crew.dashboard.chat_utils import TRANSIENT_RETRY_KIND

        assert all((m.get("meta") or {}).get("kind") != TRANSIENT_RETRY_KIND for m in error_msgs)
