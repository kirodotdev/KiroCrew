"""The FRESH first-turn history debt: what arms it, what keeps it armed, what settles it.

Each test drives ``_run_chat`` end to end on the state ``_l1_state`` builds, with a
``_DurableFirstTurnDebt`` standing in for the session manager's ``first_turn_history_owed``
debt; where a later prompt matters, ``_context_is_new_per_prompt`` shows whether each prompt
was built context-new. A pre-output transient 5xx re-queues a turn that rebuilds the
history. A landed first turn, a post-token recovery (a follow-up takeover too), a
promise-only turn, a completed compaction and output followed by a non-transient error
settle the debt. Output followed by a cancel or a bare cancellation, a slash first turn and
an empty response keep it armed, and a natively resumed session never arms it. The handle
classification, the runner's source-level branch pins (the debt's own among them), the
backoff re-reads and the terminal-arm pins live in ``test_recovery_l1_chat_runner.py``; the
slot, state and error builders both files share live in
``recovery_l1_chat_runner_helpers.py``; the flush of the on-disk unlanded marker that
arming the debt can write is pinned in ``test_first_turn_debt_flush.py``.
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


class _DurableFirstTurnDebt:
    """A stand-in for the SessionManager's FRESH first-turn history debt.

    ``_l1_state`` hands the runner a ``MagicMock`` for ``sessions``, whose
    ``first_turn_history_owed_pending(...) is True`` check is always falsy — so
    the durable flag never reads back there and these tests must track it for
    real. One live key is enough: the three methods carry the same read-without-
    clearing / arm / settle contract the real ``_Session`` field does.
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

    The ``is_new`` observation is spent at claim time, so a pre-output backend
    5xx — which the transient path re-queues onto the SAME live session — would
    otherwise land the re-queued turn with ``is_new=False`` and no history (a
    bare replay of the user's message, with no SESSION RESUMED marker, which
    sends the agent searching workspace-wide and running another tab's work).
    The durable ``first_turn_history_owed`` debt, armed from the FRESH
    observation and settled once a non-slash context-bearing turn DELIVERS its
    history — it assembled the replay (``_first_turn_history_assembled``) AND the
    provider accepted the prompt (``_turn_emitted``) — carries it across the
    failure. A Stop, cancel or 5xx-after-output exit where the turn assembled and
    the provider already emitted must NOT leave the debt armed, or the next turn
    re-sends history the provider already holds. A slash command (``/help``) emits
    native output without assembling context, so it must NOT settle the debt.
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
                    # flake-ok: pre-existing await of the turn task _run_chat started on a scripted client, moved verbatim; tracked for a de-flake pass
                    await slot.task

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
        """A FRESH session whose first message is a slash command (``/help``)
        streams native output through ``stream_command`` — which flips
        ``_turn_emitted`` — but bypasses ContextBuilder, so it assembles no
        history and must NOT settle the debt. The next ordinary prompt is the
        context-bearing turn that pays it; settling on ``_turn_emitted`` alone
        would clear a debt the slash turn never paid and the next prompt would
        lose history.
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
                    # flake-ok: pre-existing await of the turn task _run_chat started on a scripted client, moved verbatim; tracked for a de-flake pass
                    await slot.task

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
                    # flake-ok: pre-existing await of the turn task _run_chat started on a scripted client, moved verbatim; tracked for a de-flake pass
                    await slot.task

        assert debt.owed is False, (
            "a follow-up that suppresses post-token recovery still runs on the "
            "same live session that holds the history, so the debt must settle "
            "before the recover/suppress branch"
        )

    @pytest.mark.asyncio
    async def test_a_promise_only_first_turn_settles_the_debt(self, tmp_path):
        """A FRESH first turn recovered as PROMISE-ONLY (the model said it would
        act, then didn't) ends on a normal ``end_turn`` the provider KEPT, but
        kiro-cli deliberately does NOT record it as a landed success — so
        ``_turn_landed`` stays False. The provider still holds the conversation,
        so re-sending the full fresh history on the next turn duplicates it. The
        debt MUST settle on the TERMINAL, not on ``_turn_landed``.

        The promise-only arm is forced via ``should_recover_promise_only`` so the
        turn deterministically takes the un-landed promise path (``_turn_landed``
        stays False) within a single observed turn — making this a genuine
        discriminator: re-introducing the ``_turn_landed`` conjunct leaves the
        debt armed and reds this test.
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
                # flake-ok: pre-existing await of the turn task _run_chat started on a scripted client, moved verbatim; tracked for a de-flake pass
                await slot.task

        assert debt.owed is False, (
            "a promise-only first turn ended on a normal end_turn the provider "
            "kept, so the assembled first-turn history IS delivered and the debt "
            "must settle on the terminal — even though _turn_landed stays False"
        )

    @pytest.mark.asyncio
    async def test_a_completed_compaction_first_turn_settles_the_debt(self, tmp_path):
        """A FRESH first turn whose window overflows mid-turn and compacts to
        COMPLETION keeps the summarized session; the queued continuation runs on
        it. ``_recovering_compaction`` is itself a guard on the landed-success
        block so ``_turn_landed`` never fires — the compaction OR-clause in the
        settle predicate is what retires the debt, so the continuation does not
        re-prepend the full pre-compaction replay into the just-summarized
        session.
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
                    # flake-ok: pre-existing await of the turn task _run_chat started on a scripted client, moved verbatim; tracked for a de-flake pass
                    await slot.task

        assert debt.owed is False, (
            "a completed mid-turn compaction keeps the summarized session the "
            "continuation runs on, so the first-turn history debt must settle "
            "via the compaction clause"
        )

    @pytest.mark.asyncio
    async def test_output_then_a_non_transient_error_settles_the_debt(self, tmp_path):
        """B1 (buluoray / iamwhatever): a FRESH first turn that STREAMS output and
        THEN hits a NON-transient ``AcpError`` takes the generic terminal ``else``
        (no ``end_turn`` terminal, no re-queue, no session reset). The live
        session already holds the assembled prompt + history (the same premise the
        post-token transient arm settles on), so the debt MUST settle — otherwise
        the next ordinary turn, claiming the SAME live session, is still
        context-new and re-prepends the whole conversation into a session that
        already holds it.

        Discriminator: with the old ``end_turn``-only predicate the debt stays
        armed (no terminal event on the error path) and the second turn reads
        ``is_new=True`` -> ``[True, True]``; the B1 ``_turn_emitted`` disjunct
        settles it -> ``[True, False]``.
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
                # flake-ok: pre-existing await of the turn task _run_chat started on a scripted client, moved verbatim; tracked for a de-flake pass
                await slot.task
            assert debt.owed is False, (
                "output streamed before the non-transient error, so the live "
                "session already holds the history — the debt must settle, not "
                "stay armed to re-prepend it next turn (buluoray B1)"
            )
            # A genuine ordinary follow-up on the SAME live session.
            await _run_chat(state, slot, "and now continue")
            if slot.task:
                # flake-ok: pre-existing await of the turn task _run_chat started on a scripted client, moved verbatim; tracked for a de-flake pass
                await slot.task

        assert debt.owed is False, "the warm follow-up must not re-arm the debt"
        assert _context_is_new_per_prompt(state.context_builder) == [True, False], (
            "the FRESH turn emitted output and the session kept it; the next turn "
            "must NOT be context-new, or it re-sends history the provider already "
            "holds — the duplication B1 names"
        )

    @pytest.mark.asyncio
    async def test_output_then_a_bare_cancel_keeps_the_debt_armed(self, tmp_path):
        """GPT 6.1 F1: a FRESH first turn that STREAMS output and then is cancelled
        by a BARE ``CancelledError`` — a user Stop press, or the wall-clock turn
        ceiling firing during token persistence — never reaches the
        ``EVENT_COMPLETE`` latch, so ``_stop_reason`` would stay ``""``. kiro-cli
        DISCARDS the cancelled turn (the one carrying the assembled replay), so the
        history is not durably kept and the debt MUST stay armed.

        Discriminator: without the fix the finally sees ``_turn_emitted`` True and
        ``_stop_reason == ""`` (which ``!= STOP_REASON_CANCELLED``), so the B1
        disjunct wrongly SETTLES the debt and the next warm turn goes out without
        the history -> ``[True, False]``. Marking the cancel on ``_stop_reason`` in
        the bare-``CancelledError`` arm keeps it armed -> ``[True, True]``.
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
                # flake-ok: pre-existing await of the turn task _run_chat started on a scripted client, moved verbatim; tracked for a de-flake pass
                await slot.task
            assert debt.owed is True, (
                "a bare CancelledError after output is a discarded turn (kiro-cli "
                "drops the cancelled prompt carrying the history); the debt must "
                "stay armed even though _stop_reason was never set via "
                "EVENT_COMPLETE (GPT 6.1 F1)"
            )
            await _run_chat(state, slot, "carry on")
            if slot.task:
                # flake-ok: pre-existing await of the turn task _run_chat started on a scripted client, moved verbatim; tracked for a de-flake pass
                await slot.task

        assert _context_is_new_per_prompt(state.context_builder) == [True, True], (
            "the cancelled FRESH turn did not durably deliver history; the next "
            "turn must be context-new so it rebuilds the history dropped with the "
            "discarded cancelled prompt (GPT 6.1 F1 — crash-data-loss)"
        )
