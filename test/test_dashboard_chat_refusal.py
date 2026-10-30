"""Dashboard chat: a model refusal, the refusal fallback retry, and the turn's single tail."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, await_successor, chat_done_frames, run_as_slot_task
from dashboard_chat_helpers import _provider_mock

from kiro_crew.dashboard.recovery_replays import ReplayFamily
from kiro_crew.testing.wait import await_until

# ── Model refusal (kiro-cli `refusal` stop reason) ──


class TestRunChatModelRefusal:
    """A turn that ends on stop_reason 'refusal' with no text is a DETERMINISTIC
    model-side content refusal — it must surface a distinct, non-retried card
    instead of falling into the blind empty-response retry loop (which just
    re-hits the same refusal and burns credits)."""

    @staticmethod
    def _make_mock_client(events, prompts: list[str] | None = None):
        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)

        async def _stream(msg):
            if prompts is not None:
                prompts.append(str(msg))
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_stale_recover_notice_is_tagged_and_emits_one_frame(self, tmp_path, monkeypatch):
        """The LIVE frame must carry the retry tag, and there must be exactly one."""
        from kiro_crew.acp.types import STOP_REASON_STALE_RECOVER
        from kiro_crew.dashboard.chat_utils import TRANSIENT_RETRY_KIND
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_STALE_RECOVER)]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        _notices = [
            m
            for m in slot.messages
            if m.get("role") == "error" and "Recovering a stalled turn" in m.get("content", "")
        ]
        assert _notices, "stale-recovery notice not emitted"
        assert all((m.get("meta") or {}).get("kind") == TRANSIENT_RETRY_KIND for m in _notices)
        # slot.append already emits ONE tagged chat_message; an explicit frame here is an
        # untagged duplicate, and a client rendering it re-offers the executing choice.
        _dupes = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args
            and c.args[0] == "chat_message"
            and isinstance(c.args[1], dict)
            and "Recovering a stalled turn" in str(c.args[1].get("content", ""))
        ]
        assert not _dupes, f"untagged explicit chat_message duplicate(s): {_dupes}"

    @pytest.mark.asyncio
    async def test_refusal_shows_declined_card_and_does_not_retry(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_REFUSAL
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_REFUSAL)]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # A single declined card is surfaced, NOT the empty-response card.
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("declined" in m.get("content", "").lower() for m in error_msgs)
        assert not any("empty response" in m.get("content", "").lower() for m in error_msgs)
        # No blind re-queue: the message is not re-enqueued and the retry budget
        # is untouched.
        assert not slot._queue
        assert slot._empty_response_retries == 0


class TestRunChatRefusalFallback:
    """agent.refusal_fallback_model: one single-message retry on a DIFFERENT
    model after a content-filter refusal, then restore the primary.

    A refusal is deterministic for the model that issued it, but not across
    model families — the whole point of the retry. The feature ships OFF
    (empty default); every test here pins the seam explicitly."""

    _make_state_for_run_chat = staticmethod(TestRunChatModelRefusal._make_state_for_run_chat)

    @staticmethod
    def _make_refusing_client(events, prompts: list[str] | None = None):
        client = TestRunChatModelRefusal._make_mock_client(events, prompts)
        # provider_active_model / provider_raw_model read `served_model` first;
        # the witness needs set_model to observably move it.
        client.served_model = "fable-5"
        # Real-int epoch: a bare MagicMock auto-attribute here would compare
        # unequal to the slot's integer snapshot and trip the alias-pick guard.
        client._explicit_pick_epoch = 0

        def _apply(mid: str) -> None:
            client.served_model = mid

        client.set_model = AsyncMock(side_effect=_apply)
        return client

    @staticmethod
    def _refusal_events(**refusal_kwargs):
        from kiro_crew.acp.types import STOP_REASON_REFUSAL, RefusalInfo
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        refusal = RefusalInfo(**refusal_kwargs) if refusal_kwargs else None
        return [LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_REFUSAL, refusal=refusal)]

    @pytest.mark.asyncio
    async def test_refusal_retries_once_on_configured_model_then_terminal(
        self, tmp_path, monkeypatch
    ):
        """First refusal swaps + replays the SAME message; a second refusal
        (from the fallback) is terminal and says the fallback also declined."""
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

        await _run_chat(state, slot, "hello")

        # Turn 1: the swap landed, the retry notice (not the terminal card)
        # rendered, and the one-per-user-message allowance is spent.
        client.set_model.assert_awaited_once_with("opus-test")
        notices = [
            m
            for m in slot.messages
            if m.get("role") == "error" and "retrying once on 'opus-test'" in m.get("content", "")
        ]
        assert len(notices) == 1, f"expected exactly one retry notice, got {slot.messages}"
        # The notice stays plain-language: the raw classifier category
        # ("cyber") is jargon and never reaches the user.
        assert "(cyber)" not in notices[0]["content"]
        assert slot._refusal_fallback_attempted is True
        assert slot._refusal_fallback_primary == "fable-5"
        assert not any(
            "Response declined by the model." in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "error"
        ), "terminal card must not render on the retried turn"

        # The replay was dispatched as a background task; run it. The mock
        # stream refuses again — the fallback also declined.
        assert slot.task is not None, "queued replay was not dispatched"
        await slot.task

        cards = [
            m
            for m in slot.messages
            if m.get("role") == "error"
            and "Response declined by the model." in m.get("content", "")
        ]
        assert len(cards) == 1
        assert (
            "The configured fallback model ('opus-test') also declined this request."
            in cards[0]["content"]
        )
        # Still exactly one swap (no retry loop), replay record consumed.
        client.set_model.assert_awaited_once_with("opus-test")
        assert not slot.replays.armed(ReplayFamily.CONTENT_FILTER)
        assert not slot._queue

    @pytest.mark.asyncio
    async def test_primary_restored_on_next_genuine_turn(self, tmp_path, monkeypatch):
        """The swap is single-message: the first turn that is not the replay
        moves the session back to the primary before dispatching."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")
        assert slot.task is not None
        await slot.task  # the retry turn (fallback also refuses; terminal)
        assert slot._refusal_fallback_primary == "fable-5"

        # Next GENUINE message completes normally; restore precedes dispatch.
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        async def _ok_stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _ok_stream
        client.stream_command = _ok_stream
        await _run_chat(state, slot, "a fresh question")

        assert [c.args[0] for c in client.set_model.await_args_list] == ["opus-test", "fable-5"]
        assert slot._refusal_fallback_primary == ""
        assert slot._refusal_fallback_candidate == ""
        # The fresh message also re-armed the one-retry allowance.
        assert slot._refusal_fallback_attempted is False

    @pytest.mark.asyncio
    async def test_disabled_seam_keeps_terminal_card_and_never_swaps(self, tmp_path, monkeypatch):
        """Empty config (the default) is byte-for-byte the pre-feature path."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        client.set_model.assert_not_awaited()
        assert not slot._queue
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("Response declined by the model." in m.get("content", "") for m in error_msgs)
        # No fallback ran, so the card must NOT claim one declined.
        assert not any("fallback model also declined" in m.get("content", "") for m in error_msgs)

    @pytest.mark.asyncio
    async def test_auto_uses_provider_recommended_model(self, tmp_path, monkeypatch):
        """'auto' defers to the refusal envelope's recommended_model."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "auto",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(
            self._refusal_events(category="CYBER", recommended_model="rec-model-1")
        )
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        client.set_model.assert_awaited_once_with("rec-model-1")
        assert slot._refusal_fallback_candidate == "rec-model-1"

    @pytest.mark.asyncio
    async def test_auto_without_recommendation_is_terminal(self, tmp_path, monkeypatch):
        """'auto' with no recommended_model has nothing to retry on."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "auto",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        client.set_model.assert_not_awaited()
        assert not slot._queue
        assert any(
            "Response declined by the model." in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "error"
        )

    @pytest.mark.asyncio
    async def test_failed_swap_falls_back_to_terminal_card(self, tmp_path, monkeypatch):
        """A set_model failure surfaces the ordinary card — never a dead retry."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        client.set_model = AsyncMock(side_effect=RuntimeError("model unavailable"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert not slot._queue
        assert slot._refusal_fallback_attempted is False
        assert any(
            "Response declined by the model." in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "error"
        )

    @pytest.mark.asyncio
    async def test_retry_after_tool_dispatch_continues_instead_of_replaying(
        self, tmp_path, monkeypatch
    ):
        """A refusal terminal arriving AFTER the turn dispatched a tool still
        moves the session to the fallback model, but the replay is the
        Continue-style continuation, never the user's message: replaying the
        message would run the dispatched tool a second time."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        from kiro_crew.dashboard.chat_utils import (
            _REFUSAL_FALLBACK_RESUME_MSG,
            RecoveryPayload,
        )
        from kiro_crew.providers.base import EVENT_TOOL_CALL, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TOOL_CALL, title="write_file", tool_kind="write"),
            *self._refusal_events(category="CYBER"),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        # Capture what the swap queues: the entry is consumed by the drain as
        # soon as the turn ends, so it cannot be inspected on ``slot._queue``.
        # ``_ChatSlot`` declares ``__slots__``, so the spy goes on the class.
        queued: list[tuple[tuple, dict]] = []
        _orig_queue_insert = type(slot).queue_insert

        def _spy(self, *args, **kwargs):
            if self is slot:
                queued.append((args, kwargs))
            return _orig_queue_insert(self, *args, **kwargs)

        monkeypatch.setattr(type(slot), "queue_insert", _spy)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello", _attachments=[str(tmp_path / "a.txt")])

        client.set_model.assert_awaited_once_with("opus-test")
        assert slot._refusal_fallback_attempted is True
        assert slot._refusal_fallback_primary == "fable-5"
        assert len(queued) == 1, f"expected exactly one queued continuation, got {queued}"
        (_index, body), kwargs = queued[0]
        assert _index == 0
        assert body == _REFUSAL_FALLBACK_RESUME_MSG
        assert "hello" not in body
        # Runner text, so a linked thread must not mirror it as user speech...
        assert kwargs["payload"] == RecoveryPayload.CONTINUATION
        # ...and the original's attachments do not ride along — the refused
        # turn already delivered them into the session.
        assert not any(k in kwargs.get("meta", {}) for k in ("files", "dirs"))
        notices = [
            m
            for m in slot.messages
            if m.get("role") == "error" and "continuing on 'opus-test'" in m.get("content", "")
        ]
        assert len(notices) == 1, f"expected exactly one continue notice, got {slot.messages}"
        assert "after 1 tool call —" in notices[0]["content"]
        assert not any(
            "Response declined by the model." in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "error"
        ), "terminal card must not render on the continued turn"

        # The continuation runs on the fallback; the mock stream refuses again
        # (after a tool call again), and the allowance is spent — terminal.
        assert slot.task is not None, "queued continuation was not dispatched"
        await slot.task
        cards = [
            m
            for m in slot.messages
            if m.get("role") == "error"
            and "Response declined by the model." in m.get("content", "")
        ]
        assert len(cards) == 1
        assert "The configured fallback model ('opus-test') also declined" in cards[0]["content"]
        assert client.set_model.await_count == 1, "the continuation must not swap again"

    def test_resume_msg_fails_safe_when_completed_work_is_not_in_view(self):
        """The continuation is sent ONLY after the turn dispatched a tool, so
        "nothing was done yet" is never true of it: a clause letting the
        fallback model start the request over could only ever fire on a
        session that did not keep the partial turn — re-running the writes.
        The nudge must tell such a model to stop instead."""
        from kiro_crew.dashboard.chat_utils import _REFUSAL_FALLBACK_RESUME_MSG

        assert "start the request now" not in _REFUSAL_FALLBACK_RESUME_MSG
        assert "do NOT start the request over" in _REFUSAL_FALLBACK_RESUME_MSG
        assert "say so and stop" in _REFUSAL_FALLBACK_RESUME_MSG

    @pytest.mark.asyncio
    async def test_clean_refusal_still_replays_the_message_verbatim(self, tmp_path, monkeypatch):
        """With no tool dispatched, the retry stays the verbatim replay of the
        user's own words, carrying the original attachments."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        from kiro_crew.dashboard.chat_utils import RecoveryPayload

        events = self._refusal_events(category="CYBER")
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        queued: list[tuple[tuple, dict]] = []
        _orig_queue_insert = type(slot).queue_insert

        def _spy(self, *args, **kwargs):
            if self is slot:
                queued.append((args, kwargs))
            return _orig_queue_insert(self, *args, **kwargs)

        monkeypatch.setattr(type(slot), "queue_insert", _spy)

        from kiro_crew.dashboard.chat import _run_chat

        _file = str(tmp_path / "a.txt")
        await _run_chat(state, slot, "hello", _attachments=[_file])

        assert len(queued) == 1
        (_index, body), kwargs = queued[0]
        assert _index == 0
        assert body == "hello"
        assert kwargs["payload"] == RecoveryPayload.ORIGINAL
        assert kwargs["meta"].get("files") == [_file]
        assert any(
            "retrying once on 'opus-test'" in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "error"
        )
        if slot.task is not None:
            await slot.task

    @staticmethod
    def _answering_events():
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        return [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="follow-up answer"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]


class TestRunChatEarlyExitHandOff:
    """Every ``_run_chat`` exit runs the turn's tail exactly once.

    A local slash command answers without a provider and returns before the
    turn's ``finally``. The exit guard runs the tail for it, so a message queued
    behind the command runs next, the cycle sends exactly one terminal
    chat_done, and the slot's task is released -- and the tail takes its policy
    from what the turn and the slot recorded, never from where the turn
    stopped."""

    _make_state_for_run_chat = staticmethod(TestRunChatModelRefusal._make_state_for_run_chat)

    def _setup(self, tmp_path, monkeypatch, events=None):
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        prompts: list[str] = []
        client = TestRunChatRefusalFallback._make_refusing_client(
            TestRunChatRefusalFallback._answering_events() if events is None else events, prompts
        )
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.get_instance", lambda: None)
        state.push_refresh = MagicMock()
        state.refresh_slot_source_status = MagicMock()
        return state, slot, prompts

    @staticmethod
    def _run(state, slot, message, **kwargs):
        from kiro_crew.dashboard.chat import _run_chat

        return run_as_slot_task(slot, _run_chat(state, slot, message, **kwargs))

    @staticmethod
    def _count_nudge_notifies(monkeypatch) -> list[dict]:
        notifies: list[dict] = []
        nudge = MagicMock()
        nudge.notify_turn_complete = MagicMock(side_effect=lambda _key, **kw: notifies.append(kw))
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: nudge)
        return notifies

    @pytest.mark.parametrize(
        "command", ["/quit", "/goal status", "/workflow", "/prompts", "/prompts get no-such-prompt"]
    )
    @pytest.mark.asyncio
    async def test_a_local_command_runs_the_message_queued_behind_it(
        self, tmp_path, monkeypatch, command
    ):
        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        slot.queue_insert(0, "do X please", kind="")

        first = await self._run(state, slot, command)
        await await_successor(slot, first)

        assert sum("do X please" in p for p in prompts) == 1, prompts
        assert not any(command in p for p in prompts), "a local command reached the provider"
        assert not slot._queue
        assert slot.task is None
        assert len(chat_done_frames(state)) == 1

    @pytest.mark.asyncio
    async def test_a_lone_local_command_ends_the_cycle_without_titling(self, tmp_path, monkeypatch):
        """No provider saw the cycle, so there is no reply to title or summarize
        and no remote work to re-read, and the session list is left alone."""
        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        notifies = self._count_nudge_notifies(monkeypatch)
        title = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.title_then_refresh", title)

        await self._run(state, slot, "/prompts")

        assert prompts == []
        assert slot.task is None
        assert [m.get("role") for m in slot.messages][-2:] == ["assistant", "done"]
        frames = chat_done_frames(state)
        assert len(frames) == 1 and isinstance(frames[0], dict), frames
        state.push_refresh.assert_not_called()
        state.refresh_slot_source_status.assert_not_called()
        title.assert_not_called()
        assert len(notifies) == 1, "the AutoNudge timer re-arms once per exit"

    @pytest.mark.asyncio
    async def test_a_provider_turn_handing_off_to_a_local_command_still_titles(
        self, tmp_path, monkeypatch
    ):
        """Titling belongs to the cycle: a provider reply followed by a queued
        local command is still titled when the command ends the cycle."""
        state, slot, _prompts = self._setup(tmp_path, monkeypatch)
        title = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.title_then_refresh", title)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.generate_session_summary", AsyncMock())
        slot.queue_insert(0, "/prompts", kind="")

        first = await self._run(state, slot, "answer me")
        await await_successor(slot, first)
        for task in list(state._background_tasks):
            await await_until(task, what="a background task finishing")

        title.assert_awaited_once()
        state.refresh_slot_source_status.assert_called_once_with(slot.key)
        assert slot._cycle_reached_provider is False

    @pytest.mark.parametrize("command", ["/quit", "/goal status", "/prompts"])
    @pytest.mark.asyncio
    async def test_a_local_command_keeps_a_queue_held_after_a_sign_in_failure(
        self, tmp_path, monkeypatch, command
    ):
        """The command learned nothing about sign-in, so the slot keeps the
        outcome the last turn recorded and the held queue stays held: draining
        it would walk each prompt into the same signed-out CLI."""
        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        slot._last_turn_auth_required = True
        slot.queue_insert(0, "held until login", kind="")

        await self._run(state, slot, command)

        assert prompts == []
        assert [q.get("content") for q in slot._queue] == ["held until login"]
        assert slot._last_turn_auth_required is True
        assert slot.task is None
        assert len(chat_done_frames(state)) == 1

    @pytest.mark.asyncio
    async def test_a_local_command_keeps_the_queue_while_memory_preparation_runs(
        self, tmp_path, monkeypatch
    ):
        """The command never reached the barrier, so the shared preparation task
        decides: while it still runs, a drained prompt would only wait at the
        same barrier, so the queue is held."""
        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        preparation = asyncio.get_running_loop().create_future()
        state.memory_startup_task = preparation
        slot.queue_insert(0, "after memory is ready", kind="")
        try:
            await self._run(state, slot, "/prompts")
        finally:
            preparation.cancel()

        assert prompts == []
        assert [q.get("content") for q in slot._queue] == ["after memory is ready"]
        assert slot.task is None
        assert len(chat_done_frames(state)) == 1

    @pytest.mark.asyncio
    async def test_a_refused_command_leaves_a_deferred_reset_armed(self, tmp_path, monkeypatch):
        """A turn that acquired no session consumes no deferred reset: a refused
        ``/compact`` or ``/quit`` behaves as if the turn never started."""
        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        consume = AsyncMock(return_value=False)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._consume_pending_reset", consume)

        await self._run(state, slot, "/quit")

        consume.assert_not_awaited()
        assert prompts == []
        assert slot.task is None

    @pytest.mark.asyncio
    async def test_a_setup_error_holds_the_queue(self, tmp_path, monkeypatch):
        """An exception above the main try comes from setup the next queued
        entry would run through too, so the cycle ends with the queue intact
        rather than failing every entry in turn, and the error propagates."""
        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._discard_stale_decision",
            MagicMock(side_effect=RuntimeError("setup broke")),
        )
        slot.queue_insert(0, "second", kind="")
        slot.queue_insert(1, "third", kind="")

        with pytest.raises(RuntimeError, match="setup broke"):
            await self._run(state, slot, "first")

        assert prompts == []
        assert [q.get("content") for q in slot._queue] == ["second", "third"]
        assert slot.task is None
        assert len(chat_done_frames(state)) == 1

    @pytest.mark.asyncio
    async def test_a_cancel_cutting_the_finally_short_keeps_the_turns_own_policy(
        self, tmp_path, monkeypatch
    ):
        """A second cancel landing on an await in the finally skips the rest of
        it. The outcome was recorded first, so the guard runs the tail with this
        turn's policy: a sign-in failure still holds the queue and is still
        recorded, and the AutoNudge timer re-arms exactly once, with its facts."""
        from kiro_crew.acp.transport_errors import AcpAuthRequired

        async def _signed_out(_msg):
            raise AcpAuthRequired("Please sign in")
            yield  # pragma: no cover - makes this an async generator

        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        client = TestRunChatRefusalFallback._make_refusing_client([])
        client.stream = _signed_out
        client.stream_command = _signed_out
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        notifies = self._count_nudge_notifies(monkeypatch)

        async def _cancelled_reset(_key, **_kw):
            raise asyncio.CancelledError

        state.sessions.reset = AsyncMock(side_effect=_cancelled_reset)
        slot.queue_insert(0, "held until login", kind="")

        with pytest.raises(asyncio.CancelledError):
            await self._run(state, slot, "first")

        assert [q.get("content") for q in slot._queue] == ["held until login"]
        assert slot._last_turn_auth_required is True
        assert len(notifies) == 1 and "tool_calls" in notifies[0], notifies
        assert len(chat_done_frames(state)) == 1

    @pytest.mark.asyncio
    async def test_a_cancel_inside_the_tail_still_hands_off_the_queue(self, tmp_path, monkeypatch):
        """A cancel landing while the tail awaits its deferred-reset consume (a
        turn deadline) is deferred until the hand-off ran: the queued message
        still runs, and the turn then ends cancelled."""
        from kiro_crew.dashboard import chat_runner

        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        entered = asyncio.Event()
        release = asyncio.Event()
        parked: list[bool] = []

        async def _consume(_state, _slot, *, allow_discard=False, **_kw):
            if allow_discard and not parked:
                parked.append(True)
                entered.set()
                await release.wait()
            return False

        monkeypatch.setattr(chat_runner, "_consume_pending_reset", _consume)
        slot.queue_insert(0, "do X please", kind="")

        runner = asyncio.ensure_future(self._run(state, slot, "answer me"))
        await await_until(entered, what="the turn reaching its gate")
        first = slot.task
        first.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await runner

        await await_successor(slot, first)
        assert sum("do X please" in p for p in prompts) == 1, prompts
        assert not slot._queue
        assert slot.task is None

    @pytest.mark.asyncio
    async def test_a_closed_coroutine_runs_no_tail(self, tmp_path, monkeypatch):
        """A coroutine closed while suspended above its main try (a pending task
        destroyed at loop or harness teardown) cannot await, so the guard skips
        the tail instead of raising out of the close."""
        from kiro_crew.dashboard import chat_runner

        state, slot, _prompts = self._setup(tmp_path, monkeypatch)
        tails = AsyncMock()
        monkeypatch.setattr(chat_runner, "_end_turn_tail", tails)
        release = asyncio.Event()

        async def _parked(*_args, **_kwargs):
            await release.wait()
            return True

        monkeypatch.setattr(chat_runner, "_expand_prompt_mention_off_loop", _parked)
        coro = chat_runner._run_chat(state, slot, "/prompts get review")
        assert coro.send(None) is not None  # parked inside the turn
        coro.close()
        tails.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_nested_prompts_get_turn_hands_off_exactly_once(self, tmp_path, monkeypatch):
        """``/prompts get`` runs the expanded prompt as a nested turn, which runs
        the tail itself; the outer call must not run it a second time."""
        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._expand_prompt_mention_off_loop",
            AsyncMock(return_value=("Execute the following instructions: review it", "ok")),
        )
        slot.queue_insert(0, "then this", kind="")

        first = await self._run(state, slot, "/prompts get review")
        await await_successor(slot, first)

        assert sum("review it" in p for p in prompts) == 1, prompts
        assert sum("then this" in p for p in prompts) == 1, prompts
        assert not slot._queue and slot.task is None
        assert len(chat_done_frames(state)) == 1

    @pytest.mark.asyncio
    async def test_the_remote_slot_refusal_does_not_drain_locally(self, tmp_path, monkeypatch):
        """A remote-bound slot's queue drains on its peer; the local refusal sends
        its chat_done and leaves the queue alone."""
        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        slot.executor = "remote"
        slot.queue_insert(0, "for the peer", kind="")

        first = await self._run(state, slot, "hello")

        assert prompts == []
        assert [q.get("content") for q in slot._queue] == ["for the peer"]
        assert slot.task is first
        assert len(chat_done_frames(state)) == 1

    @pytest.mark.asyncio
    async def test_a_send_during_the_payload_read_queues_and_runs(self, tmp_path, monkeypatch):
        """The cycle's chat_done payload is read while the finishing turn still
        holds the slot, so a send arriving in that read sees a busy slot and
        queues -- no reservation can take the floor either -- and the cycle
        hands it the floor instead of ending: one terminal chat_done, after the
        message it ran."""
        from kiro_crew.apps.builtins.spec_builder.backend.runtime import _reserve_slot_turn
        from kiro_crew.dashboard import chat_runner

        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        real_payload = chat_runner.chat_done_payload
        reservations: list = []

        async def _payload(the_state, the_slot, **kw):
            if not reservations:
                assert the_slot.turn_running, "the floor was released before the read"
                reservations.append(_reserve_slot_turn(the_state, the_slot))
                the_slot.queue_insert(0, "sent during the read", kind="")
            return await real_payload(the_state, the_slot, **kw)

        monkeypatch.setattr(chat_runner, "chat_done_payload", _payload)

        first = await self._run(state, slot, "/prompts")
        await await_successor(slot, first)

        assert reservations == [None], "a reservation took the floor mid-read"
        assert sum("sent during the read" in p for p in prompts) == 1, prompts
        assert not slot._queue and slot.task is None
        assert len(chat_done_frames(state)) == 1

    @staticmethod
    def _fail_first_setup(monkeypatch) -> None:
        """Make the first ``_run_chat`` raise above its main try, and no later one."""
        calls: list[int] = []

        def _discard(_slot):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("setup broke")

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._discard_stale_decision", _discard)

    @pytest.mark.parametrize("hold", ["sign-in", "setup"])
    @pytest.mark.asyncio
    async def test_a_send_during_the_read_of_a_held_queue_runs(self, tmp_path, monkeypatch, hold):
        """A held queue keeps what it held, but a send arriving during the cycle's
        payload read is the user's next send, the one a hold resumes on: it runs
        instead of waiting on an idle slot, and the held entries drain after it."""
        from kiro_crew.dashboard import chat_runner

        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        if hold == "sign-in":
            slot._last_turn_auth_required = True
            message = "/prompts"
        else:
            self._fail_first_setup(monkeypatch)
            message = "first"
        slot.queue_insert(0, "held entry", kind="")
        real_payload = chat_runner.chat_done_payload
        sent: list[str] = []

        async def _payload(the_state, the_slot, **kw):
            if not sent:
                sent.append(the_slot.queue_insert(1, "sent during the read", kind=""))
            return await real_payload(the_state, the_slot, **kw)

        monkeypatch.setattr(chat_runner, "chat_done_payload", _payload)

        try:
            await self._run(state, slot, message)
        except RuntimeError:
            assert hold == "setup"
        assert slot.task is not None, "the send was stranded on an idle slot"
        await await_until(slot.task, what="the slot's turn finishing")
        while slot.task is not None and not slot.task.done():
            await await_until(slot.task, what="the slot's turn finishing")

        ran = [p for p in prompts if "sent during the read" in p or "held entry" in p]
        assert len(ran) == 2 and "sent during the read" in ran[0], prompts
        assert not slot._queue and slot.task is None

    @pytest.mark.asyncio
    async def test_a_send_during_the_read_stays_queued_while_sub_agents_run(
        self, tmp_path, monkeypatch
    ):
        """With sub-agents attached a user send waits, as it would on an idle
        slot. The hand-off for a send that arrived during the read of a held
        queue must not start a held sub-agent completion in its place."""
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_utils import SUBAGENT_COMPLETION_KIND

        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=["child-2"])
        slot._last_turn_auth_required = True
        slot.queue_insert(0, "child-1 done", kind=SUBAGENT_COMPLETION_KIND)
        real_payload = chat_runner.chat_done_payload
        sent: list[str] = []

        async def _payload(the_state, the_slot, **kw):
            if not sent:
                sent.append(the_slot.queue_insert(1, "sent during the read", kind=""))
            return await real_payload(the_state, the_slot, **kw)

        monkeypatch.setattr(chat_runner, "chat_done_payload", _payload)

        await self._run(state, slot, "/prompts")

        assert prompts == []
        assert [q.get("content") for q in slot._queue] == ["child-1 done", "sent during the read"]
        assert slot.task is None
        assert len(chat_done_frames(state)) == 1

    @pytest.mark.parametrize("command", ["/prompts", "/goal status"])
    @pytest.mark.asyncio
    async def test_a_local_command_leaves_a_sub_agents_wait_state_alone(
        self, tmp_path, monkeypatch, command
    ):
        """A sub-agent's ``wait`` keeps its countdown on the parent slot. An exit
        that never reached the main try ran no wait of its own, so it leaves the
        countdown, an uncollected End-wait request and the contested latch to
        the sleep they belong to."""
        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        wait = {"wait_id": "w1", "seconds": 60, "deadline_ts": 1e12}
        slot._wait_state = dict(wait)
        slot._end_wait_request = "w1"
        slot._wait_contested = True

        await self._run(state, slot, command)

        assert prompts == []
        assert slot._wait_state == wait
        assert slot._end_wait_request == "w1"
        assert slot._wait_contested is True

    @pytest.mark.parametrize("stream", [False, True])
    @pytest.mark.asyncio
    async def test_a_setup_error_is_a_server_error_on_the_openai_endpoint(
        self, tmp_path, monkeypatch, stream
    ):
        """The cycle still ends with a ``done`` row, which the OpenAI-compatible
        readers wait for, so the failed turn's error row is what tells them to
        answer ``server_error`` rather than an empty successful completion."""
        from kiro_crew.dashboard import openai_compat
        from kiro_crew.dashboard.chat import _run_chat

        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        self._fail_first_setup(monkeypatch)
        with slot.pending_consumer():
            task = asyncio.ensure_future(_run_chat(state, slot, "first"))
            slot.task = task
            if stream:
                written: list[bytes] = []
                resp = MagicMock()
                resp.headers = {}
                resp.prepare = AsyncMock()
                resp.write = AsyncMock(side_effect=written.append)
                monkeypatch.setattr(openai_compat.web, "StreamResponse", lambda: resp)
                await await_until(
                    asyncio.ensure_future(
                        openai_compat._stream_response(
                            MagicMock(), state, slot, "id1", "m", 0, False
                        )
                    ),
                    what="the error stream response finishing",
                )
                body = b"".join(written).decode()
                assert "server_error" in body and '"finish_reason": "stop"' not in body, body
            else:
                response = await await_until(
                    asyncio.ensure_future(
                        openai_compat._blocking_response(state, slot, "id1", "m", 0, False)
                    ),
                    what="the error blocking response finishing",
                )
                assert response.status == 502
                error = json.loads(response.text)["error"]
                assert error["type"] == "server_error"
                assert error["code"] == "turn_failed"
                assert "failed before it started" in error["message"]
        with pytest.raises(RuntimeError, match="setup broke"):
            await task
        assert prompts == []

    @pytest.mark.asyncio
    async def test_a_setup_error_shows_an_error_and_a_finished_frame(self, tmp_path, monkeypatch):
        """The dispatcher only logs the exception, so the turn says it failed; and
        the queue it held runs on the user's next send, not by itself, so the
        terminal frame does not report it as work that continues."""
        state, slot, _prompts = self._setup(tmp_path, monkeypatch)
        self._fail_first_setup(monkeypatch)
        slot.queue_insert(0, "second", kind="")

        with pytest.raises(RuntimeError, match="setup broke"):
            await self._run(state, slot, "first")

        errors = [m.get("content", "") for m in slot.messages if m.get("role") == "error"]
        assert any("failed before it started" in e for e in errors), errors
        [frame] = chat_done_frames(state)
        assert frame["continuing"] is False, frame

    @pytest.mark.asyncio
    async def test_a_failing_local_command_does_not_hold_the_queue(self, tmp_path, monkeypatch):
        """A local command's own handler raising is that entry's failure, not
        setup every queued entry shares: the user sees it failed and the message
        queued behind it runs."""
        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._handle_workflow_command",
            AsyncMock(side_effect=OSError("store unavailable")),
        )
        slot.queue_insert(0, "summarize the PR", kind="")

        with pytest.raises(OSError, match="store unavailable"):
            await self._run(state, slot, "/workflow nightly")
        assert slot.task is not None, "the queued message was held"
        await await_until(slot.task, what="the slot's turn finishing")

        assert sum("summarize the PR" in p for p in prompts) == 1, prompts
        errors = [m.get("content", "") for m in slot.messages if m.get("role") == "error"]
        assert any("`/workflow` failed" in e for e in errors), errors
        assert not slot._queue and slot.task is None

    @pytest.mark.asyncio
    async def test_a_local_command_keeps_the_queue_after_memory_preparation_failed(
        self, tmp_path, monkeypatch
    ):
        """A preparation task can finish without being cancelled and still have
        recorded a failure the turn's barrier refuses. The tail asks the barrier's
        own rule, so the queue is held rather than walked into that refusal."""
        from kiro_crew import memory_startup

        state, slot, prompts = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(memory_startup, "_active", None)
        startup = memory_startup.MemoryStartup.begin()
        startup.fail(RuntimeError("structural"))
        finished = asyncio.get_running_loop().create_future()
        finished.set_result(None)
        state.memory_startup_task = finished
        slot.queue_insert(0, "after memory is ready", kind="")

        await self._run(state, slot, "/prompts")

        assert prompts == []
        assert [q.get("content") for q in slot._queue] == ["after memory is ready"]
        assert slot.task is None
