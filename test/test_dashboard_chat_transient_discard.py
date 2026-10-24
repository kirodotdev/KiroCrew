"""Dashboard chat: conversation-discard recovery in ``_run_chat`` — a rejected image, a poisoned conversation."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_dashboard_chat import TestRunChatTransientRetry as _TransientSuite

from kiro_crew.dashboard.recovery_replays import ReplayFamily
from kiro_crew.dashboard.state import SUBAGENT_SYNTHESIS_PROMPT


class TestRunChatTransientRetryImageDiscard:
    """TestRunChatTransientRetry: a retained image the provider rejects discards the native history."""

    _make_state = staticmethod(_TransientSuite._make_state)
    _wire_sessions = staticmethod(_TransientSuite._wire_sessions)
    _client = staticmethod(_TransientSuite._client)
    _drain_bg = staticmethod(_TransientSuite._drain_bg)
    _err_texts = _TransientSuite._err_texts
    _assistant_texts = _TransientSuite._assistant_texts
    _record_queue_inserts = staticmethod(_TransientSuite._record_queue_inserts)

    @staticmethod
    def _image_error():
        from kiro_crew.acp.client import AcpError

        exc = AcpError("The model could not process an image", transient=False)
        exc.structural_terminal = True
        exc.image_format_unsupported = True
        return exc

    @pytest.mark.asyncio
    async def test_retained_tool_image_discards_native_history_and_continues(
        self, tmp_path, monkeypatch
    ):
        """A typed image rejection after a tool result cannot be repaired by
        retrying the native conversation. With no new user attachment, discard
        the resume SID once and continue from Kiro Crew's text transcript without
        replaying the original request or its completed tool side effects."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        calls: list[str] = []

        async def _stream(msg):
            calls.append(msg)
            if len(calls) == 1:
                yield LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read")
                raise self._image_error()
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="recovered without old image bytes")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "compose the avatar")
            await self._drain_bg(state)

        assert len(calls) == 2
        assert "Continue from where it stopped" in calls[1]
        state.sessions.discard_conversation.assert_awaited_once()
        state.sessions.reset.assert_not_awaited()
        assert any("without binary image history" in t for t in self._err_texts(slot))
        assert any("recovered without old image bytes" in t for t in self._assistant_texts(slot))
        assert slot._poisoned_reset_used is False

    @pytest.mark.asyncio
    async def test_an_owed_completion_rejected_for_a_retained_image_runs_before_the_user(
        self, tmp_path, monkeypatch
    ):
        """A sub-agent completion the model never consumed is a result the parent
        is still owed. Its image-history replay is queued as the completion it is,
        so a newer user message does not cancel it: the completion runs again on
        the fresh conversation, reports itself consumed, and the user's message
        runs after it."""
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        calls: list[str] = []

        async def _stream(msg):
            calls.append(msg)
            if len(calls) == 1:
                raise self._image_error()
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="answered")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        self._wire_sessions(state, self._client(_stream))
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        inserts = self._record_queue_inserts(monkeypatch, slot)
        slot.queue_insert(0, "a newer user message", kind="")
        consumed = MagicMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(
                state,
                slot,
                "[Subagent completion event] the child finished",
                _turn_actor="subagent",
                _on_consumed=consumed,
            )
            await self._drain_bg(state)

        replays = [i for i in inserts if "the child finished" in i["content"]]
        assert replays == [
            {
                "content": "[Subagent completion event] the child finished",
                "kind": chat_runner.SUBAGENT_COMPLETION_KIND,
            }
        ], inserts
        assert not slot.replays.armed(
            ReplayFamily.IMAGE_HISTORY
        ), "an owed completion has no recovery record"
        assert ["the child finished" in c for c in calls[:2]] == [True, True], calls
        assert any("a newer user message" in c for c in calls[2:]), calls
        consumed.assert_any_call(True)
        assert not any("recovery cancelled" in m.get("content", "") for m in slot.messages)

    @pytest.mark.parametrize("turn", ["continuation", "synthesis"])
    @pytest.mark.asyncio
    async def test_a_turn_that_owes_nothing_yields_to_the_users_message(
        self, tmp_path, monkeypatch, turn
    ):
        """Only a completion still owed outranks the user. A completion whose
        output already landed would replay as a continuation, and the synthesis
        turn is runner-authored: with a user message queued, neither is retried,
        and the user's message runs next."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        calls: list[str] = []

        async def _stream(msg):
            calls.append(msg)
            if len(calls) == 1:
                if turn == "continuation":
                    yield LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read")
                raise self._image_error()
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="answered")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        self._wire_sessions(state, self._client(_stream))
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot.queue_insert(0, "a newer user message", kind="")
        if turn == "continuation":
            message = "[Subagent completion event] the child finished"
            kwargs = {"_turn_actor": "subagent", "_on_consumed": MagicMock()}
        else:
            message = SUBAGENT_SYNTHESIS_PROMPT
            # Even with a settlement callback attached, synthesis owes nothing.
            kwargs = {
                "_turn_actor": "subagent",
                "_synthetic_payload": True,
                "_on_consumed": MagicMock(),
            }

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, message, **kwargs)
            await self._drain_bg(state)

        assert len(calls) == 2, calls
        assert "a newer user message" in calls[1]
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_new_image_attachment_is_not_automatically_discarded(self, tmp_path, monkeypatch):
        """A current attachment may itself be invalid. Preserve the healthy
        native conversation and let the actionable terminal error tell the user
        to remove or re-encode that image instead of discarding history."""
        from kiro_crew.dashboard.chat import _run_chat

        async def _fail(msg):
            raise self._image_error()
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        await _run_chat(state, slot, "inspect this", _attachments=["/tmp/current.png"])

        state.sessions.discard_conversation.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        assert slot._queue == []
        assert any(t.startswith("❌") for t in self._err_texts(slot))
        assert slot._poisoned_reset_used is False

    @pytest.mark.asyncio
    async def test_retained_image_discard_is_one_shot_until_a_turn_lands(
        self, tmp_path, monkeypatch
    ):
        """If the fresh conversation also rejects an image, surface the error;
        never loop through repeated discard and synthetic replay."""
        from kiro_crew.dashboard.chat import _run_chat

        async def _fail(msg):
            raise self._image_error()
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot._poisoned_reset_used = True

        await _run_chat(state, slot, "continue")

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._queue == []
        assert any(t.startswith("❌") for t in self._err_texts(slot))
        assert slot._poisoned_reset_used is True

    @pytest.mark.asyncio
    async def test_a_current_turn_image_path_in_text_is_not_retained_history(
        self, tmp_path, monkeypatch
    ):
        """Empty dashboard attachment lists do not prove the turn shipped no
        image: a channel turn (and a dashboard turn that types a path) carries it
        as a bare path in the message text, which ``build_prompt_blocks`` inlines
        as a CURRENT-turn image block. Such a rejection is of the image the user
        just sent, so the healthy conversation must survive and the verbatim
        replay must never re-inline the same bytes."""
        from kiro_crew.dashboard.chat import _run_chat

        async def _fail(msg):
            raise self._image_error()
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        # No _attachments: the image rides in the text, exactly as the Slack
        # event handler appends its attachment paths. Use the host's own path
        # grammar: ``image_refs._PATH_RE`` is platform-gated, so a POSIX path
        # is prose on Windows and would not be inlined there.
        shot = r"C:\Users\me\shot.bmp" if os.name == "nt" else "/home/me/shot.bmp"
        await _run_chat(state, slot, f"{shot} look at this")

        state.sessions.discard_conversation.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        assert slot._queue == []
        assert any(t.startswith("❌") for t in self._err_texts(slot))
        assert slot._poisoned_reset_used is False

    @pytest.mark.asyncio
    async def test_a_stop_during_the_discard_drops_the_queued_recovery(self, tmp_path, monkeypatch):
        """The discard is awaited between the recovery's enqueue and the drain's
        dispatch. A soft Stop landing in that window preserves the queue and
        leaves ``_stopping`` back at idle, so only the enqueue-time stop-generation
        snapshot can see it. The recovery must be dropped, not dispatched: its
        replay would re-run a cancelled turn's remaining destructive step."""
        from kiro_crew.dashboard.chat import _run_chat

        calls: list[str] = []

        async def _fail(msg):
            calls.append(msg)
            raise self._image_error()
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        async def _discard_then_stop(*_a, **_kw):
            # A Stop initiated while the discard is in flight. It resolves back
            # to idle, so the counter is the only surviving evidence.
            slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1

        state.sessions.discard_conversation = AsyncMock(side_effect=_discard_then_stop)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "keep going")
            await self._drain_bg(state)

        # The original prompt ran once; the recovery was purged before dispatch.
        assert len(calls) == 1
        state.sessions.discard_conversation.assert_awaited_once()
        assert slot._queue == []
        assert any("Image-history recovery cancelled" in m["content"] for m in slot.messages)
        # The aborted episode refunds the shared one-shot.
        assert slot._poisoned_reset_used is False
        assert not slot.replays.armed(ReplayFamily.IMAGE_HISTORY)

    @pytest.mark.asyncio
    async def test_stop_after_image_recovery_dispatch_aborts_before_provider(
        self, tmp_path, monkeypatch
    ):
        """A Stop after dequeue but before the guarded task consumes the replay
        must still veto the recovery. The queue entry is already consumed at
        this seam, so the runner must use the identity and snapshots passed by
        the drain."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        provider_calls = 0

        async def _stream(msg):
            nonlocal provider_calls
            provider_calls += 1
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        session_key = effective_session_key(slot)
        state.sessions.stop_generation = lambda key: 4
        slot._poisoned_reset_used = True
        slot.replays.arm(
            ReplayFamily.IMAGE_HISTORY,
            entry_id="image-recovery-qid",
            session_key=session_key,
            stop_gen=7,
            session_stop_gen=4,
        )
        # The Stop lands after the drain's validation and task spawn.
        slot._stop_generation = 8

        await _run_chat(
            state,
            slot,
            "continue the destructive step",
            _replay=frozenset({ReplayFamily.IMAGE_HISTORY}),
            _synthetic_recovery_turn=True,
        )

        assert provider_calls == 0
        assert not slot.replays.armed(ReplayFamily.IMAGE_HISTORY)
        assert slot._poisoned_reset_used is False
        assert any(
            m.get("role") == "notice" and "Image-history recovery cancelled" in m.get("content", "")
            for m in slot.messages
        )


class TestRunChatTransientRetryPoisonedDiscard:
    """TestRunChatTransientRetry: a conversation poisoned across exhausted cycles is discarded once."""

    _TRANSIENT = _TransientSuite._TRANSIENT
    _make_state = staticmethod(_TransientSuite._make_state)
    _wire_sessions = staticmethod(_TransientSuite._wire_sessions)
    _client = staticmethod(_TransientSuite._client)
    _drain_bg = staticmethod(_TransientSuite._drain_bg)
    _err_texts = _TransientSuite._err_texts
    _assistant_texts = _TransientSuite._assistant_texts

    @pytest.mark.asyncio
    async def test_first_exhausted_cycle_never_discards(self, tmp_path, monkeypatch):
        """A SINGLE cycle that exhausts the pre-stream transient ladder
        surfaces the terminal ❌ without discarding the native conversation —
        one exhaustion is still plausibly a momentary outage, and discarding
        on it would throw away a healthy conversation on every blip that
        outlasts the ladder."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert any(t.startswith("❌") for t in self._err_texts(slot))
        state.sessions.discard_conversation.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        # The exhausted cycle is counted toward the consecutive streak.
        assert slot._prestream_exhausted_cycles == 1
        assert slot._poisoned_reset_used is False

    @pytest.mark.asyncio
    async def test_poisoned_conversation_discarded_on_second_exhausted_cycle(
        self, tmp_path, monkeypatch
    ):
        """Regression (poisoned persisted conversation): when TWO consecutive
        cycles each exhaust the full pre-stream transient ladder with zero
        output, the backend is deterministically rejecting this session's
        native conversation — retrying into it can never succeed (observed
        live: a session/load'ed conversation failing pre-stream identically
        11 hours apart while a NEW session on the same gateway+model answered
        instantly). The second exhaustion must escalate: discard the
        native conversation (discard_conversation() clears the resume sid —
        reset() would session/load the poison right back — while keeping the
        session-map entry with its channel linkage) and re-queue the message
        once, so the recovery cycle cold-starts a fresh conversation and the
        slot self-heals instead of telling the user to 'retry in a moment'
        forever."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        # Fail every attempt of two full cycles (the "poisoned conversation"),
        # then succeed — the success models the fresh post-discard conversation.
        _poisoned_calls = 2 * (TRANSIENT_RETRIES + 1)
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= _poisoned_calls:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="recovered-after-discard")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        # fallback_model="" (disabled): pins the PRE-FEATURE escalation ladder
        # (the "auto" default would walk the fallback branch instead).
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch(
                "kiro_crew.dashboard.chat_runner.run_bg_oneliner",
                new_callable=AsyncMock,
                return_value="OK",
            ) as canary,
        ):
            # Cycle 1: ladder exhausts → terminal ❌ (streak = 1, no discard,
            # no canary — below the consecutive threshold).
            await _run_chat(state, slot, "first try")
            await self._drain_bg(state)
            state.sessions.discard_conversation.assert_not_awaited()
            canary.assert_not_awaited()
            # Cycle 2 (the Continue press the ❌ invites): ladder exhausts
            # again → the canary probes a FRESH conversation and succeeds →
            # conversation-specific rejection proven → escalation fires and
            # the re-queued recovery cycle lands on the fresh session.
            await _run_chat(state, slot, "continue")
            await self._drain_bg(state)
            canary.assert_awaited_once()
            # The probe is only meaningful on the SAME served model as the
            # failing session, with fallback disabled (GPT review finding:
            # a canary succeeding on a different model must never justify
            # discarding this conversation).
            assert canary.await_args.kwargs["model"] == "claude-test-model"
            assert canary.await_args.kwargs["strict_model"] is True

        # Escalation discarded the native conversation exactly once; plain
        # reset (which preserves the poisoned resume sid) was never used.
        state.sessions.discard_conversation.assert_awaited_once()
        state.sessions.reset.assert_not_awaited()
        # Cycle 1 ❌ + cycle 2 escalation notice, then the recovered answer.
        errs = self._err_texts(slot)
        assert any(t.startswith("❌") for t in errs)
        assert any("keeps rejecting" in t for t in errs)
        assert any("recovered-after-discard" in t for t in self._assistant_texts(slot))
        # Attempt accounting: two full ladders + the single recovery prompt.
        assert call_count == _poisoned_calls + 1
        # The landed recovery turn re-arms the one-shot and breaks the streak.
        assert slot._poisoned_reset_used is False
        assert slot._prestream_exhausted_cycles == 0

    @pytest.mark.asyncio
    async def test_poisoned_discard_is_one_shot_until_a_turn_lands(self, tmp_path, monkeypatch):
        """If even the fresh post-discard conversation keeps failing (genuine
        prolonged outage), no second discard fires: the one-shot is consumed by
        the first escalation and only a LANDED turn re-arms it, so a discard
        loop is impossible. Later cycles surface the terminal ❌ as before."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch(
                "kiro_crew.dashboard.chat_runner.run_bg_oneliner",
                new_callable=AsyncMock,
                return_value="OK",
            ),
        ):
            # Cycle 1: exhausts → ❌ (streak 1).
            await _run_chat(state, slot, "first try")
            await self._drain_bg(state)
            # Cycle 2: exhausts → canary succeeds → discard #1 → recovery
            # cycle ALSO exhausts → terminal ❌ (one-shot consumed, so the
            # eligibility gate rejects before the canary even runs again).
            await _run_chat(state, slot, "continue")
            await self._drain_bg(state)
            # Cycle 3: user retries once more — still failing. Streak keeps
            # counting but the one-shot is spent: ❌ again, NO second discard.
            await _run_chat(state, slot, "continue again")
            await self._drain_bg(state)

        state.sessions.discard_conversation.assert_awaited_once()
        state.sessions.reset.assert_not_awaited()
        # No turn ever landed: the one-shot stays consumed.
        assert slot._poisoned_reset_used is True
        # Terminal ❌ surfaced after the failed recovery and again on cycle 3.
        assert sum(1 for t in self._err_texts(slot) if t.startswith("❌")) >= 2

    @pytest.mark.asyncio
    async def test_cancelled_turn_does_not_rearm_poisoned_one_shot(self, tmp_path, monkeypatch):
        """Regression: the poisoned-discard one-shot re-arms only on a LANDED
        turn. But the STREAK is evidence-based: a cancelled turn that EMITTED
        output proves the backend accepts this conversation, so it breaks the
        streak (GPT review finding — without this, exhaustion → stopped-but-
        streaming turn → exhaustion would discard a healthy conversation),
        while a cancelled turn with NO output proves nothing and preserves
        it. In both cases the spent one-shot stays consumed."""
        from kiro_crew.acp.types import STOP_REASON_CANCELLED
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        # ── Cancelled turn with NO output: both guards preserved. ──
        async def _cancelled_silent(msg):
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_cancelled_silent)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        # An escalation already consumed the one-shot mid-streak.
        slot._poisoned_reset_used = True
        slot._prestream_exhausted_cycles = 3

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "user stops before any output")
            await self._drain_bg(state)

        assert slot._poisoned_reset_used is True
        assert slot._prestream_exhausted_cycles == 3

        # ── Cancelled turn WITH output: streak broken, one-shot still spent. ──
        async def _cancelled_after_output(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial before stop")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED)

        client.stream = _cancelled_after_output
        client.stream_command = _cancelled_after_output
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "recovery attempt the user stops mid-answer")
            await self._drain_bg(state)

        assert slot._prestream_exhausted_cycles == 0  # output = evidence
        assert slot._poisoned_reset_used is True  # cancel ≠ landed

        # ── A genuinely LANDED turn re-arms the one-shot too. ──
        async def _ok(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="landed")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _ok
        client.stream_command = _ok
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "works now")
            await self._drain_bg(state)

        assert slot._poisoned_reset_used is False
        assert slot._prestream_exhausted_cycles == 0

    @pytest.mark.asyncio
    async def test_canary_failure_blocks_discard_and_preserves_one_shot(
        self, tmp_path, monkeypatch
    ):
        """GPT-review fix: two exhausted ladders alone are NOT
        conversation-specific evidence — a sustained backend-wide outage or
        throttle produces the identical pattern. The canary probe (one prompt
        on a fresh background conversation) is the discriminator: when it
        ALSO fails, no discard fires, the one-shot stays unconsumed, and the
        streak stays accrued so a later user-initiated cycle re-probes once
        the outage ends."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch(
                "kiro_crew.dashboard.chat_runner.run_bg_oneliner",
                new_callable=AsyncMock,
                side_effect=AcpError(self._TRANSIENT),
            ) as canary,
        ):
            # Two consecutive exhausted cycles — the pattern that WOULD
            # discard if the canary had succeeded.
            await _run_chat(state, slot, "first try")
            await self._drain_bg(state)
            await _run_chat(state, slot, "continue")
            await self._drain_bg(state)

        canary.assert_awaited_once()  # probed at the threshold, cycle 2
        state.sessions.discard_conversation.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        # Backend-wide failure consumes NOTHING: the one-shot survives for a
        # later cycle whose canary succeeds, and the streak keeps accruing.
        assert slot._poisoned_reset_used is False
        assert slot._prestream_exhausted_cycles == 2
        assert sum(1 for t in self._err_texts(slot) if t.startswith("❌")) >= 2

    @pytest.mark.asyncio
    async def test_canary_empty_reply_is_not_positive_evidence(self, tmp_path, monkeypatch):
        """A canary that completes with EMPTY output proves nothing about the
        fresh conversation working — fail-safe to no-discard."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch(
                "kiro_crew.dashboard.chat_runner.run_bg_oneliner",
                new_callable=AsyncMock,
                return_value="  ",
            ),
        ):
            await _run_chat(state, slot, "first try")
            await self._drain_bg(state)
            await _run_chat(state, slot, "continue")
            await self._drain_bg(state)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._poisoned_reset_used is False

    @pytest.mark.asyncio
    async def test_stop_during_canary_vetoes_discard_and_requeue(self, tmp_path, monkeypatch):
        """GPT-review fix: a Stop initiated DURING the (up to 30s) canary probe
        must veto the discard and the synthetic re-queue even when the canary
        succeeds — the user just cancelled this work, and re-queueing would
        re-execute it with tool side effects. The veto keys on the monotonic
        _stop_generation, not _stop_state, because teardown can drive
        _stop_state back to "idle" before the check (a stop that fired AND
        resolved mid-probe must still veto). Nothing is consumed: the one-shot
        stays armed and the streak stays accrued."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        async def _canary_with_midflight_stop(*a, **kw):
            # User presses Stop while the probe is in flight; the stop then
            # RESOLVES (teardown drives _stop_state back to idle) before the
            # probe returns — the generation edge is the only surviving trace.
            slot._stop_state = "soft_pending"
            slot._stop_state = "idle"
            return "OK"  # the canary itself succeeds

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch(
                "kiro_crew.dashboard.chat_runner.run_bg_oneliner",
                new_callable=AsyncMock,
                side_effect=_canary_with_midflight_stop,
            ) as canary,
        ):
            await _run_chat(state, slot, "first try")
            await self._drain_bg(state)
            await _run_chat(state, slot, "continue")
            await self._drain_bg(state)

        canary.assert_awaited_once()  # probe ran at the threshold
        # ...but the mid-probe stop vetoed everything downstream: no discard,
        # no synthetic recovery turn, nothing consumed.
        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._queue == []  # no synthetic recovery turn queued
        assert slot._poisoned_reset_used is False
        assert slot._prestream_exhausted_cycles == 2

    @pytest.mark.asyncio
    async def test_unreadable_session_model_skips_canary_and_discard(self, tmp_path, monkeypatch):
        """When the session's served model cannot be read, the canary cannot
        be pinned to it, so the probe is meaningless — fail-safe: no probe,
        no discard, one-shot preserved."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        client.served_model = ""  # unreadable/unresolved
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch(
                "kiro_crew.dashboard.chat_runner.run_bg_oneliner",
                new_callable=AsyncMock,
                return_value="OK",
            ) as canary,
        ):
            await _run_chat(state, slot, "first try")
            await self._drain_bg(state)
            await _run_chat(state, slot, "continue")
            await self._drain_bg(state)

        canary.assert_not_awaited()
        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._poisoned_reset_used is False
