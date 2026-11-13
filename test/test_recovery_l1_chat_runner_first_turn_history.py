"""The FRESH first-turn history debt: what arms it, what keeps it armed, what settles it.

Each test drives ``_run_chat`` end to end, with ``_DurableFirstTurnDebt`` standing in for
``first_turn_history_owed``. Shared builders are in ``recovery_l1_chat_runner_helpers.py``;
the other runner pins are in ``test_recovery_l1_chat_runner.py`` and the marker flush in
``test_first_turn_debt_flush.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from recovery_l1_chat_runner_helpers import (
    _l1_state,
    _RecordingSlot,
    _transient_error,
)

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.providers.base import LLMEvent
from kiro_crew.testing.wait import await_until


class _DurableFirstTurnDebt:
    """A stand-in for the SessionManager's FRESH first-turn history debt.

    On the ``MagicMock`` sessions ``first_turn_history_owed_pending(...) is True`` is
    always falsy, so the flag is tracked here with the real read / arm / settle contract.
    """

    def __init__(self) -> None:
        self.owed = False
        self.replay_pending = False

    def first_turn_history_owed_pending(self, _key: str) -> bool:
        return self.owed

    def mark_first_turn_history_owed(self, _key: str) -> bool:
        self.owed = True
        return True

    def consume_first_turn_history_owed(self, _key: str) -> bool:
        if not self.owed:
            return False
        self.owed = False
        return True

    def provider_switch_replay_pending(self, _key: str) -> bool:
        return self.replay_pending

    def consume_replay_suppression(self, _key: str) -> bool:
        # No reset armed in these tests: the suppression arm must not fire, so
        # the build arm's assembled-and-accepted gate is what settles the debt.
        return False


def _context_is_new_per_prompt(cb) -> list[bool]:
    """``_context_is_new`` as it reached ``build_message`` on each prompt.

    The runner passes it as the second positional argument, so one entry lands
    per turn that assembled a prompt.
    """
    return [call.args[1] for call in cb.build_message.call_args_list]


class TestAFreshFirstTurnThatFailsPreOutputStillRebuildsHistory:
    """A context-bearing FRESH first turn that fails before the provider
    accepts it must replay WITH history, not bare.

    ``is_new`` is spent at claim time, so a re-queued turn would replay bare and the
    agent would hunt workspace-wide. The debt settles only when the turn assembled
    history (``_first_turn_history_assembled``) AND the provider emitted
    (``_turn_emitted``); a slash command emits without assembling, so it does not.
    """

    @pytest.mark.asyncio
    async def test_the_requeued_turn_rebuilds_history(self, tmp_path):
        state, client = _l1_state(tmp_path)
        # FRESH claim; not an L1 turn (the infra verdict must not claim it).
        client.last_infra_error = None
        debt = _DurableFirstTurnDebt()
        for name in (
            "first_turn_history_owed_pending",
            "mark_first_turn_history_owed",
            "consume_first_turn_history_owed",
            "provider_switch_replay_pending",
            "consume_replay_suppression",
        ):
            setattr(state.sessions, name, getattr(debt, name))

        # The real bug: the FRESH ``is_new`` observation is a one-shot consumed
        # at claim time, so the FIRST claim reports ``is_new=True`` and the
        # re-queued turn's claim reports ``is_new=False`` on the SAME live
        # session. Only the durable debt can carry history across that.
        claims = {"n": 0}

        async def _claim(_key, **_kw):
            claims["n"] += 1
            return (client, claims["n"] == 1, False)

        state.sessions.get_or_create = _claim

        prompts: list[str] = []

        def _stream(message, *_a, **_kw):
            prompts.append(message)
            first = len(prompts) == 1

            async def _gen():
                if first:
                    # Pre-output 5xx: the FRESH observation is already spent.
                    raise _transient_error()
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the answer")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream = MagicMock(side_effect=_stream)

        async def _no_wait(_secs):
            return None

        slot = _RecordingSlot("chat-1-freshretry")
        with patch.object(chat_runner, "_recovery_delay", _no_wait):
            with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
                mock_sel.return_value = MagicMock()
                await _run_chat(state, slot, "Resume now please")
                if slot.task:
                    await await_until(slot.task, what="the turn task finishing")

        assert len(prompts) == 2, "the pre-output failure was not re-queued"
        assert claims["n"] == 2 and prompts, "the re-queue must claim the session again"
        built = _context_is_new_per_prompt(state.context_builder)
        assert built == [True, True], (
            "the FRESH first turn built history; the re-queued turn (which now "
            "claims is_new=False) after a pre-output 5xx must rebuild it from the "
            "durable debt, not replay the message bare"
        )
        # The retry emitted a token, so the debt is settled — nothing
        # re-injects forever.
        assert debt.owed is False

    @pytest.mark.asyncio
    async def test_a_landed_first_turn_settles_then_a_warm_turn_is_not_new(self, tmp_path):
        """No failure: the FRESH turn emits and lands, so the debt is armed then
        cleared, and a SECOND warm turn on the same live session is NOT treated
        as context-new (the debt does not keep re-injecting history forever)."""
        state, client = _l1_state(tmp_path)
        client.last_infra_error = None
        debt = _DurableFirstTurnDebt()
        for name in (
            "first_turn_history_owed_pending",
            "mark_first_turn_history_owed",
            "consume_first_turn_history_owed",
            "provider_switch_replay_pending",
            "consume_replay_suppression",
        ):
            setattr(state.sessions, name, getattr(debt, name))

        # FRESH only on the first claim; the warm second turn claims is_new=False.
        claims = {"n": 0}

        async def _claim(_key, **_kw):
            claims["n"] += 1
            return (client, claims["n"] == 1, False)

        state.sessions.get_or_create = _claim

        def _clean_stream(_message, *_a, **_kw):
            async def _gen():
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream = MagicMock(side_effect=_clean_stream)
        slot = _RecordingSlot("chat-1-freshland")
        with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            await _run_chat(state, slot, "hello")
            assert debt.owed is False, "a first turn that emitted must settle the debt"
            # A genuine warm follow-up on the SAME session.
            await _run_chat(state, slot, "and again")

        assert debt.owed is False, "the warm turn must not re-arm the debt"
        assert _context_is_new_per_prompt(state.context_builder) == [True, False], (
            "the FRESH turn builds history; the warm follow-up must NOT be "
            "context-new once the debt is settled"
        )

    @pytest.mark.asyncio
    async def test_output_then_cancel_keeps_the_debt_armed(self, tmp_path):
        """A FRESH first turn that EMITS a token then is cancelled mid-stream (a
        user Stop is common) does NOT land: kiro-cli discards the cancelled turn —
        the one carrying the assembled replay — so the provider does not durably
        keep the history. The debt must stay armed and the next prompt must
        rebuild it. Settling on mere visible output would silently drop history.
        """
        state, client = _l1_state(tmp_path)
        client.last_infra_error = None
        debt = _DurableFirstTurnDebt()
        for name in (
            "first_turn_history_owed_pending",
            "mark_first_turn_history_owed",
            "consume_first_turn_history_owed",
            "provider_switch_replay_pending",
            "consume_replay_suppression",
        ):
            setattr(state.sessions, name, getattr(debt, name))

        claims = {"n": 0}

        async def _claim(_key, **_kw):
            claims["n"] += 1
            return (client, claims["n"] == 1, False)

        state.sessions.get_or_create = _claim

        prompts: list[str] = []

        def _stream(message, *_a, **_kw):
            prompts.append(message)
            first = len(prompts) == 1

            async def _gen():
                # Visible output, then a soft Stop: the provider yields a
                # cancelled terminal (sets _saw_terminal_event) but kiro-cli
                # discards the turn, so the history is NOT durably kept.
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the first half ")
                if first:
                    yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="cancelled")
                    return
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the rest")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream = MagicMock(side_effect=_stream)
        slot = _RecordingSlot("chat-1-outputcancel")
        with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            await _run_chat(state, slot, "hello")
            assert debt.owed is True, (
                "a cancelled first turn is discarded by kiro-cli, so the "
                "assembled history is not durably kept and the debt must stay armed"
            )
            await _run_chat(state, slot, "carry on")

        assert _context_is_new_per_prompt(state.context_builder) == [True, True], (
            "the cancelled FRESH turn did not durably deliver history; the next "
            "turn must be context-new so it rebuilds the history that was dropped"
        )

    @pytest.mark.asyncio
    async def test_a_resumed_session_never_arms_the_debt(self, tmp_path):
        """A natively-resumed session (``resumed=True``) replayed its transcript
        itself, so it must NEVER arm the first-turn history debt — re-injecting
        would duplicate the context the native resume already delivered."""
        state, client = _l1_state(tmp_path)
        client.last_infra_error = None
        debt = _DurableFirstTurnDebt()
        for name in (
            "first_turn_history_owed_pending",
            "mark_first_turn_history_owed",
            "consume_first_turn_history_owed",
            "provider_switch_replay_pending",
            "consume_replay_suppression",
        ):
            setattr(state.sessions, name, getattr(debt, name))

        # is_new=True AND resumed=True: a FRESH-looking claim that was actually
        # satisfied by a native transcript replay. The arm guard is
        # ``is_new and not resumed``, so this must not arm.
        async def _claim(_key, **_kw):
            return (client, True, True)

        state.sessions.get_or_create = _claim

        def _clean_stream(_message, *_a, **_kw):
            async def _gen():
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream = MagicMock(side_effect=_clean_stream)
        with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            await _run_chat(state, _RecordingSlot("chat-1-resumed"), "hello")

        assert debt.owed is False, "a resumed session must never arm the debt"

    @pytest.mark.asyncio
    async def test_a_slash_first_turn_does_not_settle_the_debt(self, tmp_path):
        """A slash first turn (``/help``) does not settle the debt.

        ``stream_command`` flips ``_turn_emitted`` but bypasses ContextBuilder, so
        no history was sent; the next ordinary prompt pays the debt.
        """
        from chat_test_helpers import pin_kiro_cli_harness

        pin_kiro_cli_harness()
        state, client = _l1_state(tmp_path)
        client.last_infra_error = None
        debt = _DurableFirstTurnDebt()
        for name in (
            "first_turn_history_owed_pending",
            "mark_first_turn_history_owed",
            "consume_first_turn_history_owed",
            "provider_switch_replay_pending",
            "consume_replay_suppression",
        ):
            setattr(state.sessions, name, getattr(debt, name))

        async def _claim(_key, **_kw):
            return (client, True, False)

        state.sessions.get_or_create = _claim

        def _stream_command(_message, *_a, **_kw):
            async def _gen():
                # Native slash output: /help lists commands. This flips
                # _turn_emitted, but no context was assembled.
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Available commands: ...")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream_command = MagicMock(side_effect=_stream_command)
        # stream must not be used on a slash turn; make it fail loudly if it is.
        client.stream = MagicMock(side_effect=AssertionError("slash turn used stream()"))
        with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            await _run_chat(state, _RecordingSlot("chat-1-slashfirst"), "/help")

        assert debt.owed is True, (
            "a slash first turn assembles no history, so it must NOT settle the "
            "debt even though its native output flips _turn_emitted"
        )

    @pytest.mark.asyncio
    async def test_an_empty_response_first_turn_keeps_the_debt_armed(self, tmp_path):
        """A FRESH first turn that reaches ``EVENT_COMPLETE`` with NO text chunk
        and no tool call produces an empty response: the empty-response rung
        re-queues the message, so the provider does not durably keep that turn's
        assembled history. Like the sibling ``provider_switch_replay`` lease
        (which excludes the empty-response verdict), the debt must stay ARMED so
        the re-queue rebuilds history rather than replaying it bare."""
        state, client = _l1_state(tmp_path)
        client.last_infra_error = None
        debt = _DurableFirstTurnDebt()
        for name in (
            "first_turn_history_owed_pending",
            "mark_first_turn_history_owed",
            "consume_first_turn_history_owed",
            "provider_switch_replay_pending",
            "consume_replay_suppression",
        ):
            setattr(state.sessions, name, getattr(debt, name))

        claims = {"n": 0}

        async def _claim(_key, **_kw):
            claims["n"] += 1
            return (client, claims["n"] == 1, False)

        state.sessions.get_or_create = _claim

        def _empty_then_clean(_message, *_a, **_kw):
            async def _gen():
                # First turn: a terminal completion with NO text and NO tool call
                # (empty response). The empty-response rung re-queues it, so the
                # turn is not durably kept.
                if claims["n"] == 1:
                    yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")
                else:
                    yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
                    yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream = MagicMock(side_effect=_empty_then_clean)
        slot = _RecordingSlot("chat-1-emptyfirst")
        with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            await _run_chat(state, slot, "hello")
            assert debt.owed is True, (
                "an empty-response FRESH turn is re-queued, not durably kept, so "
                "the debt must stay armed (mirrors the sibling replay lease)"
            )

    @pytest.mark.asyncio
    async def test_posttoken_recovery_settles_the_debt(self, tmp_path):
        """A FRESH first turn that STREAMS a token (``_turn_emitted``) then hits a
        transient 5xx recovers via the post-token one-shot: a CONTINUE is re-queued
        onto the SAME live session, which already holds the assembled prompt and
        history. The debt must be settled when that recovery is selected, so the
        continuation does NOT rebuild and re-prepend the full replay the provider
        already retained. (Distinct from the pre-output 5xx, where nothing reached
        the provider and the debt stays armed.)"""
        state, client = _l1_state(tmp_path)
        client.last_infra_error = None
        debt = _DurableFirstTurnDebt()
        for name in (
            "first_turn_history_owed_pending",
            "mark_first_turn_history_owed",
            "consume_first_turn_history_owed",
            "provider_switch_replay_pending",
            "consume_replay_suppression",
        ):
            setattr(state.sessions, name, getattr(debt, name))

        claims = {"n": 0}

        async def _claim(_key, **_kw):
            claims["n"] += 1
            return (client, claims["n"] == 1, False)

        state.sessions.get_or_create = _claim

        prompts: list[str] = []

        def _stream(message, *_a, **_kw):
            prompts.append(message)
            first = len(prompts) == 1

            async def _gen():
                # Tokens stream (provider accepts + retains the prompt), then a
                # transient 5xx on the first turn only -> post-token recovery.
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the first half ")
                if first:
                    raise _transient_error()
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the rest")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream = MagicMock(side_effect=_stream)
        slot = _RecordingSlot("chat-1-posttoken")

        async def _no_wait(_secs):
            return None

        with patch.object(chat_runner, "_recovery_delay", _no_wait):
            with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
                mock_sel.return_value = MagicMock()
                await _run_chat(state, slot, "hello")
                if slot.task:
                    await await_until(slot.task, what="the turn task finishing")

        assert debt.owed is False, (
            "post-token recovery re-queues a CONTINUE onto the live session that "
            "already holds the history, so the debt must be settled when that "
            "recovery is selected"
        )
        assert _context_is_new_per_prompt(state.context_builder) == [True, False], (
            "the FRESH turn built history; the post-token CONTINUE must NOT be "
            "context-new, or it re-prepends the replay the provider already has"
        )

    @pytest.mark.asyncio
    async def test_posttoken_followup_takeover_still_settles_the_debt(self, tmp_path):
        """A FRESH first turn STREAMS a token then hits a transient 5xx, but a
        queued follow-up (``_should_suppress_requeue``) takes over, so the CONTINUE
        is NOT re-queued. The live session still holds the streamed prompt and
        history, so the debt must settle BEFORE the recover/suppress branch — the
        follow-up turn runs on that same session and must not re-prepend. Settling
        only inside the recover arm would miss this takeover path."""
        state, client = _l1_state(tmp_path)
        client.last_infra_error = None
        debt = _DurableFirstTurnDebt()
        for name in (
            "first_turn_history_owed_pending",
            "mark_first_turn_history_owed",
            "consume_first_turn_history_owed",
            "provider_switch_replay_pending",
            "consume_replay_suppression",
        ):
            setattr(state.sessions, name, getattr(debt, name))

        claims = {"n": 0}

        async def _claim(_key, **_kw):
            claims["n"] += 1
            return (client, claims["n"] == 1, False)

        state.sessions.get_or_create = _claim

        prompts: list[str] = []

        def _stream(message, *_a, **_kw):
            prompts.append(message)
            first = len(prompts) == 1

            async def _gen():
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the first half ")
                if first:
                    raise _transient_error()
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the rest")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream = MagicMock(side_effect=_stream)
        slot = _RecordingSlot("chat-1-posttoken-takeover")

        async def _seam_queue_followup(_secs):
            # A follow-up arrives during the backoff: it suppresses the CONTINUE
            # re-queue, so the post-token recovery is dropped (takeover).
            slot.queue_insert(0, "never mind — summarise what you have")

        with patch.object(chat_runner, "_recovery_delay", _seam_queue_followup):
            with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
                mock_sel.return_value = MagicMock()
                await _run_chat(state, slot, "hello")
                if slot.task:
                    await await_until(slot.task, what="the turn task finishing")

        assert debt.owed is False, (
            "a follow-up that suppresses post-token recovery still runs on the "
            "same live session that holds the history, so the debt must settle "
            "before the recover/suppress branch"
        )

    @pytest.mark.asyncio
    async def test_a_promise_only_first_turn_settles_the_debt(self, tmp_path):
        """A PROMISE-ONLY first turn settles the debt on the terminal.

        The provider kept the ``end_turn`` but ``_turn_landed`` stays False, so the
        debt must not wait for it. ``should_recover_promise_only`` forces that path.
        """
        state, client = _l1_state(tmp_path)
        client.last_infra_error = None
        debt = _DurableFirstTurnDebt()
        for name in (
            "first_turn_history_owed_pending",
            "mark_first_turn_history_owed",
            "consume_first_turn_history_owed",
            "provider_switch_replay_pending",
            "consume_replay_suppression",
        ):
            setattr(state.sessions, name, getattr(debt, name))

        async def _claim(_key, **_kw):
            return (client, True, False)

        state.sessions.get_or_create = _claim

        def _stream(_message, *_a, **_kw):
            async def _gen():
                # Visible output ending in a bare promise-to-act, then a normal
                # end_turn the provider keeps. The forced promise-only gate below
                # routes it through the un-landed recovery arm.
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="I'll do that now.")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream = MagicMock(side_effect=_stream)
        slot = _RecordingSlot("chat-1-promiseonly")
        with (
            patch("kiro_crew.dashboard.chat.sel") as mock_sel,
            patch.object(chat_runner, "should_recover_promise_only", return_value=True),
        ):
            mock_sel.return_value = MagicMock()
            await _run_chat(state, slot, "please do the thing")
            if slot.task:
                await await_until(slot.task, what="the turn task finishing")

        assert debt.owed is False, (
            "a promise-only first turn ended on a normal end_turn the provider "
            "kept, so the assembled first-turn history IS delivered and the debt "
            "must settle on the terminal — even though _turn_landed stays False"
        )

    @pytest.mark.asyncio
    async def test_a_completed_compaction_first_turn_settles_the_debt(self, tmp_path):
        """A first turn that compacts to COMPLETION settles the debt.

        ``_recovering_compaction`` blocks ``_turn_landed``, so the compaction clause
        must settle it, or the continuation re-prepends the pre-compaction replay.
        """
        from kiro_crew.acp.types import EVENT_COMPACTION_STATUS

        state, client = _l1_state(tmp_path)
        client.last_infra_error = None
        debt = _DurableFirstTurnDebt()
        for name in (
            "first_turn_history_owed_pending",
            "mark_first_turn_history_owed",
            "consume_first_turn_history_owed",
            "provider_switch_replay_pending",
            "consume_replay_suppression",
        ):
            setattr(state.sessions, name, getattr(debt, name))

        claims = {"n": 0}

        async def _claim(_key, **_kw):
            claims["n"] += 1
            return (client, claims["n"] == 1, False)

        state.sessions.get_or_create = _claim

        def _stream(_message, *_a, **_kw):
            async def _gen():
                # Visible output, then a mid-turn window overflow that compacts
                # to COMPLETION: sets _compaction_completed and routes the turn
                # through the compaction-recovery arm (_recovering_compaction).
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="working on it ")
                yield LLMEvent(kind=EVENT_COMPACTION_STATUS, text="started")
                yield LLMEvent(kind=EVENT_COMPACTION_STATUS, text="completed")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        async def _no_wait(*_a, **_kw):
            return None

        client.stream = MagicMock(side_effect=_stream)
        slot = _RecordingSlot("chat-1-compaction")
        with patch.object(chat_runner, "_recovery_delay", _no_wait):
            with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
                mock_sel.return_value = MagicMock()
                await _run_chat(state, slot, "a long first turn")
                if slot.task:
                    await await_until(slot.task, what="the turn task finishing")

        assert debt.owed is False, (
            "a completed mid-turn compaction keeps the summarized session the "
            "continuation runs on, so the first-turn history debt must settle "
            "via the compaction clause"
        )

    @pytest.mark.asyncio
    async def test_output_then_a_non_transient_error_settles_the_debt(self, tmp_path):
        """Output then a NON-transient ``AcpError`` settles the debt.

        The error path has no ``end_turn``, but the live session already holds the
        history, so ``_turn_emitted`` settles it: ``[True, False]``.
        """
        state, client = _l1_state(tmp_path)
        client.last_infra_error = None
        debt = _DurableFirstTurnDebt()
        for name in (
            "first_turn_history_owed_pending",
            "mark_first_turn_history_owed",
            "consume_first_turn_history_owed",
            "provider_switch_replay_pending",
            "consume_replay_suppression",
        ):
            setattr(state.sessions, name, getattr(debt, name))

        claims = {"n": 0}

        async def _claim(_key, **_kw):
            claims["n"] += 1
            return (client, claims["n"] == 1, False)

        state.sessions.get_or_create = _claim

        prompts: list[str] = []

        def _stream(message, *_a, **_kw):
            prompts.append(message)
            first = len(prompts) == 1

            async def _gen():
                # Output streams (flips _turn_emitted), THEN — on the FRESH turn
                # only — a non-transient AcpError. No rejected_model, so it is not
                # the model-access-fallback arm; it falls to the generic terminal
                # else: that persists the partial and ends the turn without an
                # end_turn terminal and without resetting the session.
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the answer begins ")
                if first:
                    err = AcpError("Prompt error: ValidationException: input is not valid")
                    err.transient = False
                    raise err
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the rest")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream = MagicMock(side_effect=_stream)
        slot = _RecordingSlot("chat-1-outputerror")
        with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            await _run_chat(state, slot, "hello")
            if slot.task:
                await await_until(slot.task, what="the turn task finishing")
            assert debt.owed is False, (
                "output streamed before the non-transient error, so the live "
                "session already holds the history — the debt must settle, not "
                "stay armed to re-prepend it next turn (buluoray B1)"
            )
            # A genuine ordinary follow-up on the SAME live session.
            await _run_chat(state, slot, "and now continue")
            if slot.task:
                await await_until(slot.task, what="the turn task finishing")

        assert debt.owed is False, "the warm follow-up must not re-arm the debt"
        assert _context_is_new_per_prompt(state.context_builder) == [True, False], (
            "the FRESH turn emitted output and the session kept it; the next turn "
            "must NOT be context-new, or it re-sends history the provider already "
            "holds — the duplication B1 names"
        )

    @pytest.mark.asyncio
    async def test_output_then_a_bare_cancel_keeps_the_debt_armed(self, tmp_path):
        """Output then a BARE ``CancelledError`` keeps the debt armed.

        kiro-cli discards the cancelled turn, and ``EVENT_COMPLETE`` never sets
        ``_stop_reason``, so the bare-cancel arm must mark it: ``[True, True]``.
        """
        state, client = _l1_state(tmp_path)
        client.last_infra_error = None
        debt = _DurableFirstTurnDebt()
        for name in (
            "first_turn_history_owed_pending",
            "mark_first_turn_history_owed",
            "consume_first_turn_history_owed",
            "provider_switch_replay_pending",
            "consume_replay_suppression",
        ):
            setattr(state.sessions, name, getattr(debt, name))

        claims = {"n": 0}

        async def _claim(_key, **_kw):
            claims["n"] += 1
            return (client, claims["n"] == 1, False)

        state.sessions.get_or_create = _claim

        prompts: list[str] = []

        def _stream(message, *_a, **_kw):
            prompts.append(message)
            first = len(prompts) == 1

            async def _gen():
                # Output streams (flips _turn_emitted), THEN — on the FRESH turn
                # only — a BARE CancelledError raises with NO EVENT_COMPLETE. This
                # is the Stop-press / turn-ceiling shape: nothing ever assigns
                # _stop_reason, so it stays "" unless the except arm marks it.
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the answer begins ")
                if first:
                    raise asyncio.CancelledError()
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the rest")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream = MagicMock(side_effect=_stream)
        slot = _RecordingSlot("chat-1-barecancel")
        with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            await _run_chat(state, slot, "hello")
            if slot.task:
                await await_until(slot.task, what="the turn task finishing")
            assert debt.owed is True, (
                "a bare CancelledError after output is a discarded turn (kiro-cli "
                "drops the cancelled prompt carrying the history); the debt must "
                "stay armed even though _stop_reason was never set via "
                "EVENT_COMPLETE (GPT 6.1 F1)"
            )
            await _run_chat(state, slot, "carry on")
            if slot.task:
                await await_until(slot.task, what="the turn task finishing")

        assert _context_is_new_per_prompt(state.context_builder) == [True, True], (
            "the cancelled FRESH turn did not durably deliver history; the next "
            "turn must be context-new so it rebuilds the history dropped with the "
            "discarded cancelled prompt (GPT 6.1 F1 — crash-data-loss)"
        )
