"""Dashboard chat: a throttled turn is re-sent with ONE copy of the prompt in history.

KAS stores the user row before it asks the model, so a turn that fails on a
throttle leaves that row behind and a plain re-send adds another. The fake
provider here models exactly that: every ``stream`` call appends the prompt to
``history``, and ``discard_unanswered_prompt`` takes the unanswered one back out.

Pinned: after N throttled attempts and a success the history holds one copy, the
landed row's ``turn_stats`` says N retries, the throttle ladder starts near one KAS
attempt (~6 s), and telemetry counts the recovery. The nearby inputs keep the plain
retry: a non-throttle 5xx (2 s ladder, no rewind), a throttle after partial output
(continuation, no rewind) and a Stop during the backoff (no re-send, no rewind).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from test_dashboard_chat import TestRunChatTransientRetry as _TransientSuite

from kiro_crew import llm_helpers

# The engine's answer to a throttled prompt, as KAS words it.
_THROTTLE_FRAME = {
    "code": -32603,
    "message": "Prompt failed",
    "data": {
        "message": "Too many requests, please wait before trying again.",
        "errorType": "ClientThrottleError",
        "retryErrorType": "THROTTLING",
    },
}
_FIVE_XX_FRAME = {"code": -32603, "message": "Internal error: API Error: Internal server error"}


def _error(frame: dict):
    from kiro_crew.acp.client import AcpError, _raise_acp_error

    try:
        _raise_acp_error(frame)
    except AcpError as exc:
        return exc
    raise AssertionError("the frame raised nothing")


class _KasHistory:
    """The model-visible user rows of one KAS session, and a provider stream over them.

    ``before_failure(call)`` runs just before an attempt fails, where a test presses
    Stop while that attempt is in flight.
    """

    def __init__(
        self,
        failures: int,
        frame: dict,
        *,
        partial_first: bool = False,
        before_failure=None,
    ) -> None:
        self.history: list[str] = []
        self.calls = 0
        self._failures = failures
        self._frame = frame
        self._partial_first = partial_first
        self._before_failure = before_failure
        self._answered = True

    async def stream(self, msg):
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        self.calls += 1
        self.history.append(msg)  # KAS stores the row before the model request
        self._answered = False
        if self.calls <= self._failures:
            if self._partial_first and self.calls == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            if self._before_failure is not None:
                self._before_failure(self.calls)
            raise _error(self._frame)
        self._answered = True
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
        yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(duration_ms=4000, credits=0.5))

    async def discard_unanswered_prompt(self) -> bool:
        if self._answered or not self.history:
            return False
        self.history.pop()
        self._answered = True
        return True

    def copies(self, text: str) -> int:
        return sum(text in row for row in self.history)


class TestThrottledTurnRetry:
    _make_state = staticmethod(_TransientSuite._make_state)
    _wire_sessions = staticmethod(_TransientSuite._wire_sessions)
    _client = staticmethod(_TransientSuite._client)
    _drain_bg = staticmethod(_TransientSuite._drain_bg)
    _err_texts = _TransientSuite._err_texts
    _assistant_texts = _TransientSuite._assistant_texts

    async def _run(
        self,
        tmp_path,
        monkeypatch,
        kas: _KasHistory,
        *,
        waits=None,
        slot_key="s1",
        moves_on_set_model=False,
        stop_on_rewind=None,
        **slot_kw,
    ):
        from kiro_crew.dashboard.chat import _run_chat

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(kas.stream)
        slot_ref: list = []

        async def _discard():
            rewound = await kas.discard_unanswered_prompt()
            # A Stop pressed while the rewind's KAS answer is awaited.
            if stop_on_rewind is not None and stop_on_rewind(slot_ref[0]):
                slot_ref[0]._stop_generation = getattr(slot_ref[0], "_stop_generation", 0) + 1
            return rewound

        client.discard_unanswered_prompt = AsyncMock(side_effect=_discard)
        if moves_on_set_model:
            # The fallback swap witnesses the move, so the served model must follow.
            client.set_model = AsyncMock(
                side_effect=lambda model: setattr(client, "served_model", model)
            )
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot(slot_key, **slot_kw)
        slot._titled = True
        slot_ref.append(slot)
        recorded_waits: list[float] = [] if waits is None else waits

        async def _wait(secs):
            recorded_waits.append(secs)

        with (
            patch("kiro_crew.dashboard.chat_runner._recovery_delay", side_effect=_wait),
            patch("kiro_crew.dashboard.chat_runner._emit_recovery_outcome") as outcomes,
        ):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)
        return state, slot, client, outcomes

    @staticmethod
    def _landed_stats(slot) -> dict:
        rows = [m for m in slot.messages if m.get("role") == "assistant"]
        assert rows, "no assistant row landed"
        return (rows[-1].get("meta") or {}).get("turn_stats") or {}

    @pytest.mark.asyncio
    async def test_two_throttles_then_success_leave_one_prompt_copy(self, tmp_path, monkeypatch):
        kas = _KasHistory(failures=2, frame=_THROTTLE_FRAME)
        waits: list[float] = []
        _state, slot, client, outcomes = await self._run(tmp_path, monkeypatch, kas, waits=waits)

        assert kas.calls == 3
        assert kas.copies("hello") == 1, kas.history
        assert client.discard_unanswered_prompt.await_count == 2
        assert self._landed_stats(slot).get("retries") == 2
        assert any("ok-result" in t for t in self._assistant_texts(slot))
        # Append-only and visible: the retry notice stays in the transcript.
        assert any("Connection unstable" in t for t in self._err_texts(slot))
        assert len(waits) == 2
        base = llm_helpers.THROTTLE_RETRY_BASE_DELAY
        assert base <= waits[0] <= 1.25 * base
        assert 2 * base <= waits[1] <= 2.5 * base
        outcomes.assert_called_once_with("throttle", "recovered", 2)
        assert slot._turn_retries == {}

    @pytest.mark.asyncio
    async def test_throttle_on_a_crewmate_dm_slot_takes_the_same_path(self, tmp_path, monkeypatch):
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.agents["perf-mate"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.KiroCrewConfig.load", lambda: cfg)
        kas = _KasHistory(failures=2, frame=_THROTTLE_FRAME)
        _state, slot, client, _outcomes = await self._run(
            tmp_path,
            monkeypatch,
            kas,
            slot_key="member-perf-mate",
            agent="perf-mate",
            mode="member",
        )
        assert slot.mode == "member"

        assert kas.copies("hello") == 1, kas.history
        assert client.discard_unanswered_prompt.await_count == 2
        assert self._landed_stats(slot).get("retries") == 2

    @pytest.mark.asyncio
    async def test_a_throttle_on_the_last_attempt_still_leaves_one_copy(
        self, tmp_path, monkeypatch
    ):
        """The whole ladder fails: the last failed copy stays, the earlier ones do not."""
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        kas = _KasHistory(failures=TRANSIENT_RETRIES + 1, frame=_THROTTLE_FRAME)
        _state, slot, _client, outcomes = await self._run(tmp_path, monkeypatch, kas)

        assert kas.calls == TRANSIENT_RETRIES + 1
        assert kas.copies("hello") == 1, kas.history
        assert any(t.startswith("❌") for t in self._err_texts(slot))
        outcomes.assert_called_once_with("throttle", "exhausted", TRANSIENT_RETRIES)

    @pytest.mark.asyncio
    async def test_a_non_throttle_5xx_keeps_the_2s_ladder_and_no_rewind(
        self, tmp_path, monkeypatch
    ):
        kas = _KasHistory(failures=2, frame=_FIVE_XX_FRAME)
        waits: list[float] = []
        _state, slot, client, outcomes = await self._run(tmp_path, monkeypatch, kas, waits=waits)

        assert kas.calls == 3
        client.discard_unanswered_prompt.assert_not_awaited()
        assert kas.copies("hello") == 3
        assert len(waits) == 2
        assert 2.0 <= waits[0] <= 2.5
        assert 4.0 <= waits[1] <= 5.0
        assert self._landed_stats(slot).get("retries") == 2
        outcomes.assert_called_once_with("transient_5xx", "recovered", 2)

    @pytest.mark.asyncio
    async def test_a_throttle_after_partial_output_keeps_the_attempt(self, tmp_path, monkeypatch):
        """Output landed, so the attempt's rows are the work so far: continue, never rewind."""
        kas = _KasHistory(failures=1, frame=_THROTTLE_FRAME, partial_first=True)
        _state, slot, client, _outcomes = await self._run(tmp_path, monkeypatch, kas)

        client.discard_unanswered_prompt.assert_not_awaited()
        assert kas.copies("hello") == 1
        assert any("partial answer" in t for t in self._assistant_texts(slot))
        assert kas.calls == 2  # the continuation, not a verbatim re-send

    @pytest.mark.asyncio
    async def test_stop_during_the_backoff_sends_nothing_and_rewinds_nothing(
        self, tmp_path, monkeypatch
    ):
        kas = _KasHistory(failures=1, frame=_THROTTLE_FRAME)
        slot_ref: list = []

        async def _stop_during_wait(_secs):
            slot = slot_ref[0]
            slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1

        from kiro_crew.dashboard.chat import _run_chat

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(kas.stream)
        client.discard_unanswered_prompt = AsyncMock(side_effect=kas.discard_unanswered_prompt)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot_ref.append(slot)
        with patch(
            "kiro_crew.dashboard.chat_runner._recovery_delay", side_effect=_stop_during_wait
        ):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert kas.calls == 1
        client.discard_unanswered_prompt.assert_not_awaited()
        assert kas.copies("hello") == 1

    @pytest.mark.asyncio
    async def test_throttle_fallback_swap_rewinds_before_its_resend(self, tmp_path, monkeypatch):
        """The model-fallback re-send replays the same prompt, so it rewinds too."""
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ("fallback-model",)
        )
        # The swap's own short wait before the fallback attempt, not the ladder's.
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.transient_retry_delay", lambda _a: 0.0)
        kas = _KasHistory(failures=TRANSIENT_RETRIES + 1, frame=_THROTTLE_FRAME)
        _state, slot, client, _outcomes = await self._run(
            tmp_path, monkeypatch, kas, moves_on_set_model=True
        )

        assert kas.calls == TRANSIENT_RETRIES + 2
        assert kas.copies("hello") == 1, kas.history
        assert client.discard_unanswered_prompt.await_count == TRANSIENT_RETRIES + 1
        assert self._landed_stats(slot).get("retries") == TRANSIENT_RETRIES + 1

    @pytest.mark.asyncio
    async def test_stop_during_the_rewind_still_drops_the_resend(self, tmp_path, monkeypatch):
        """The rewind awaits KAS, so the Stop re-read that drops the re-send follows it."""
        kas = _KasHistory(failures=1, frame=_THROTTLE_FRAME)
        _state, slot, client, _outcomes = await self._run(
            tmp_path, monkeypatch, kas, stop_on_rewind=lambda _slot: True
        )

        assert client.discard_unanswered_prompt.await_count == 1
        assert kas.calls == 1, "a Stop pressed during the rewind must not re-send the turn"
        assert slot._transient_5xx_retries == 0

    @pytest.mark.asyncio
    async def test_stop_during_the_fallback_rewind_drops_the_fallback_resend(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ("fallback-model",)
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.transient_retry_delay", lambda _a: 0.0)
        kas = _KasHistory(failures=TRANSIENT_RETRIES + 1, frame=_THROTTLE_FRAME)
        _state, slot, client, _outcomes = await self._run(
            tmp_path,
            monkeypatch,
            kas,
            moves_on_set_model=True,
            # Only the rewind the fallback arm runs, after the swap walked a candidate.
            stop_on_rewind=lambda s: bool(s._fallback_walked),
        )

        assert client.discard_unanswered_prompt.await_count == TRANSIENT_RETRIES + 1
        assert kas.calls == TRANSIENT_RETRIES + 1, "the fallback re-send must not run"
        assert slot._fallback_walked == []
        # The Stop ended the episode, so its counted re-sends go with it.
        assert slot._turn_retries == {}

    @pytest.mark.asyncio
    async def test_a_stop_during_a_resend_drops_the_episode_count(self, tmp_path, monkeypatch):
        """A Stop that lands while a re-send is in flight ends the episode before its backoff."""
        from kiro_crew.dashboard.chat import _run_chat

        slot_ref: list = []

        def _stop_during_the_resend(call: int) -> None:
            if call == 2:
                slot_ref[0]._stop_generation = getattr(slot_ref[0], "_stop_generation", 0) + 1

        kas = _KasHistory(failures=2, frame=_THROTTLE_FRAME, before_failure=_stop_during_the_resend)
        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(kas.stream)
        client.discard_unanswered_prompt = AsyncMock(side_effect=kas.discard_unanswered_prompt)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot_ref.append(slot)
        waits: list[float] = []

        async def _wait(secs):
            waits.append(secs)

        with patch("kiro_crew.dashboard.chat_runner._recovery_delay", side_effect=_wait):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # One counted re-send; the Stop during it ended the episode before a second wait.
        assert kas.calls == 2
        assert len(waits) == 1
        assert slot._turn_retries == {}

    @pytest.mark.asyncio
    async def test_a_stop_still_resolving_drops_the_episode_count(self, tmp_path, monkeypatch):
        """A Stop still resolving when the re-send fails suppresses the next one and ends the episode."""
        from kiro_crew.dashboard.chat import _run_chat

        slot_ref: list = []

        def _stop_resolving_during_the_resend(call: int) -> None:
            if call == 2:
                slot_ref[0]._stop_state = "soft_pending"

        kas = _KasHistory(
            failures=2, frame=_THROTTLE_FRAME, before_failure=_stop_resolving_during_the_resend
        )
        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(kas.stream)
        client.discard_unanswered_prompt = AsyncMock(side_effect=kas.discard_unanswered_prompt)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot_ref.append(slot)
        waits: list[float] = []

        async def _wait(secs):
            waits.append(secs)

        with patch("kiro_crew.dashboard.chat_runner._recovery_delay", side_effect=_wait):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert kas.calls == 2
        assert len(waits) == 1
        assert slot._turn_retries == {}

    @pytest.mark.asyncio
    async def test_a_stopped_episode_leaves_no_retries_for_the_continue_turn(
        self, tmp_path, monkeypatch
    ):
        """Stop ends the throttle episode, so the Continue that follows counts none of it.

        Continue queues a synthetic recovery turn, which the per-turn reset skips
        (a recovery turn adds to the running episode), so the Stop arm is the one
        place the count of the stopped episode can be dropped.
        """
        from kiro_crew.dashboard.chat import _run_chat

        kas = _KasHistory(failures=2, frame=_THROTTLE_FRAME)
        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(kas.stream)
        client.discard_unanswered_prompt = AsyncMock(side_effect=kas.discard_unanswered_prompt)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        waits: list[float] = []

        async def _stop_on_second_wait(secs):
            waits.append(secs)
            if len(waits) == 2:
                slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1

        with (
            patch(
                "kiro_crew.dashboard.chat_runner._recovery_delay",
                side_effect=_stop_on_second_wait,
            ),
            patch("kiro_crew.dashboard.chat_runner._emit_recovery_outcome") as outcomes,
        ):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)
            # One counted re-send, then the Stop dropped the second one.
            assert kas.calls == 2
            # The drained Continue entry runs as a synthetic recovery turn.
            await _run_chat(state, slot, "Continue", _synthetic_recovery_turn=True)
            await self._drain_bg(state)

        assert kas.calls == 3
        assert any("ok-result" in t for t in self._assistant_texts(slot))
        assert "retries" not in self._landed_stats(slot)
        assert not [c for c in outcomes.call_args_list if c.args[:2] == ("throttle", "recovered")]
        assert slot._turn_retries == {}
