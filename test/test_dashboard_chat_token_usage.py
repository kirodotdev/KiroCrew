"""Dashboard chat: per-turn token and cost persistence, the slot.model backfill, and a withheld pin."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from dashboard_chat_helpers import _provider_mock

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO, TurnUsage
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.state import _ChatSlot


class TestTokenPersistenceBackfill:
    """Regression tests for the late-backfill of slot.model before
    persist_token_record is called from _on_complete (chat_turn/turn_stats.py).

    Background: Claude Code reports its model only after the prompt is
    dispatched (via the `init` system event). The original eager backfill
    at the start of _run_chat reads client._model too early for CC, so
    slot.model stays empty and tokens.jsonl records get model="". The
    fix re-reads client._model right before persisting the token record.
    """

    @staticmethod
    def _make_mock_client(events, prov_model=""):
        """Mock provider that exposes a nested client._model attribute,
        mirroring AcpClient/CcClient layout (provider.client._model).
        """
        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        # Expose `client.client._model` like the real provider wrappers
        inner = MagicMock()
        inner._model = prov_model
        client.client = inner

        async def _stream(msg):
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
    async def test_safe_complete_reports_structured_monitor_usage(self, tmp_path, monkeypatch):
        """Normal runner return is not the accounting boundary; safe evidence is."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.monitoring.completion import MonitorCompletionHook
        from kiro_crew.monitoring.models import (
            MonitorActionCompletion,
            MonitorActionDisposition,
        )
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason="end_turn",
                usage=TurnUsage(input_tokens=12, output_tokens=4),
            ),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_window_tokens = MagicMock(return_value=0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.generate_session_summary",
            AsyncMock(return_value=None),
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        await _run_chat(
            state,
            slot,
            "hello",
            monitor_completion=MonitorCompletionHook("monitor1", "failure-a", _capture),
        )

        assert len(completions) == 1
        assert completions[0].disposition is MonitorActionDisposition.SUCCESS
        assert completions[0].input_tokens == 12
        assert completions[0].output_tokens == 4

    @pytest.mark.asyncio
    async def test_synthetic_timeout_complete_does_not_report_monitor_usage(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.monitoring.completion import MonitorCompletionHook
        from kiro_crew.monitoring.models import MonitorActionCompletion
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        event = LLMEvent(
            kind=EVENT_COMPLETE,
            stop_reason="timeout",
            usage=TurnUsage(input_tokens=12, output_tokens=4),
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client([LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"), event])
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_window_tokens = MagicMock(return_value=0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.generate_session_summary",
            AsyncMock(return_value=None),
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        await _run_chat(
            state,
            slot,
            "hello",
            monitor_completion=MonitorCompletionHook("monitor1", "failure-a", _capture),
        )

        assert completions == []

    @pytest.mark.asyncio
    async def test_synthesized_end_turn_does_not_report_monitor_usage(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.monitoring.completion import MonitorCompletionHook
        from kiro_crew.monitoring.models import MonitorActionCompletion
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        event = LLMEvent(
            kind=EVENT_COMPLETE,
            stop_reason="end_turn",
            synthetic_completion=True,
            usage=TurnUsage(input_tokens=12, output_tokens=4),
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client([LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"), event])
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_window_tokens = MagicMock(return_value=0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.generate_session_summary",
            AsyncMock(return_value=None),
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        await _run_chat(
            state,
            slot,
            "hello",
            monitor_completion=MonitorCompletionHook("monitor1", "failure-a", _capture),
        )

        assert completions == []

    @pytest.mark.asyncio
    async def test_monitor_reauthorizes_at_provider_entry(self, tmp_path, monkeypatch):
        """A stopped claim cannot cross the runner's final provider boundary."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.monitoring.completion import MonitorCompletionHook

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client([])
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_window_tokens = MagicMock(return_value=0)
        stream = MagicMock(side_effect=client.stream)
        client.stream = stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        authorize = AsyncMock(return_value=False)
        completion = MonitorCompletionHook(
            "monitor1",
            "failure-a",
            AsyncMock(),
            authorization_callback=authorize,
        )

        await _run_chat(state, slot, "hello", monitor_completion=completion)

        authorize.assert_awaited_once_with("monitor1", "failure-a")
        assert completion.accepted is False
        stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_shutdown_gate_precedes_acceptance(self, tmp_path, monkeypatch):
        """A closing session cannot acknowledge a wake that never entered the provider."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.monitoring.completion import MonitorCompletionHook
        from kiro_crew.session import SessionClosingError

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client([])
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_window_tokens = MagicMock(return_value=0)
        stream = MagicMock(side_effect=client.stream)
        client.stream = stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.begin_turn = MagicMock(side_effect=SessionClosingError("closing"))
        accepted = MagicMock()
        completion = MonitorCompletionHook(
            "monitor1",
            "failure-a",
            AsyncMock(),
            authorization_callback=AsyncMock(return_value=True),
            acceptance_callback=accepted,
        )

        await _run_chat(state, slot, "hello", monitor_completion=completion)

        assert completion.accepted is False
        accepted.assert_not_called()
        stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_raw_complete_survives_cancellation_during_token_persistence(
        self, tmp_path, monkeypatch
    ):
        """Provider completion evidence is durable before cancellable analytics I/O."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.monitoring.completion import MonitorCompletionHook
        from kiro_crew.monitoring.models import MonitorActionCompletion
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        event = LLMEvent(
            kind=EVENT_COMPLETE,
            stop_reason="cancelled",
            usage=TurnUsage(input_tokens=12, output_tokens=4),
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client([event])
        client.context_used_tokens = MagicMock(return_value=0)
        client.context_window_tokens = MagicMock(return_value=0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.generate_session_summary",
            AsyncMock(return_value=None),
        )

        async def _cancel_persistence(*_args, **_kwargs) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async",
            _cancel_persistence,
        )
        completions: list[MonitorActionCompletion] = []

        async def _capture(completion: MonitorActionCompletion) -> None:
            completions.append(completion)

        await _run_chat(
            state,
            slot,
            "hello",
            monitor_completion=MonitorCompletionHook("monitor1", "failure-a", _capture),
        )

        assert len(completions) == 1
        assert completions[0].input_tokens == 12
        assert completions[0].output_tokens == 4

    @pytest.mark.asyncio
    async def test_late_backfill_populates_model_for_cc_session(self, tmp_path, monkeypatch):
        """When slot.model is empty at EVENT_COMPLETE but the provider has
        learned its model (CC init event), persist_token_record receives the
        provider model. slot.model stays empty: the slot pins nothing and its
        crew pins nothing, so it inherits and the served model is not a pin.

        The mock starts with an empty ``inner._model`` so the *early* backfill
        at the top of _run_chat finds nothing, then mutates ``inner._model``
        just before yielding EVENT_COMPLETE — mirroring CC reporting its
        model only after the prompt is dispatched. This way only the *late*
        backfill branch can populate the record's model, so removing the
        late-backfill code would cause this test to fail.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [
            LLMEvent(
                kind=EVENT_COMPLETE,
                usage=TurnUsage(input_tokens=12, output_tokens=34),
            ),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = ""  # CC has not yet emitted init when _run_chat begins

        # This simulates a claude_code session, so the backfill must run under
        # provider=claude_code for canonicalize_for_provider to map 'opus' ->
        # 'opus-4.8-1m'. Under the acp label the backfill (correctly) leaves a
        # kiro/acp model unchanged.
        #
        # The provider label is resolved from the LIVE CLIENT's
        # ``SessionCapabilities.provider_seam``, not from cfg.agent.provider: that
        # field is declared enum=["acp"] and validate_config_data deletes an
        # out-of-enum value, so no real config can ever say "claude_code" and this
        # branch was unreachable in production while the test mocked it green. The
        # client here is a bare AsyncMock, so it is given the claude backend's
        # record below -- a real CC session's client resolves the same one.
        _cc_cfg = KiroCrewConfig()
        _cc_cfg.dashboard.merge_queued_messages = False
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.KiroCrewConfig.load", lambda: _cc_cfg)

        # Build a mock whose inner._model starts EMPTY so the early backfill
        # branch (chat_runner.py:471-476) finds nothing and leaves slot.model
        # blank. Then mutate inner._model mid-stream — just before yielding
        # EVENT_COMPLETE — so only the late backfill branch can populate it.
        client = _provider_mock()
        client.capabilities = capabilities_for(ACP_BACKEND_CLAUDE)
        client.context_usage_pct = MagicMock(return_value=10.0)
        inner = MagicMock()
        inner._model = ""  # empty at session-create time
        client.client = inner

        async def _stream(msg):
            # Simulate CC's `init` system event arriving mid-turn, after the
            # prompt has been dispatched but before EVENT_COMPLETE.
            inner._model = "opus"
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        captured: list[tuple] = []

        async def _fake_persist(slot_key, model, event, provider="", **kwargs):
            captured.append((slot_key, model, provider))

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert len(captured) == 1
        slot_key, model, provider = captured[0]
        assert slot_key == "s1"
        # The backfill canonicalizes the provider's model via from_provider_id so
        # it matches the canonical-keyed dropdown: 'opus' alias -> 'opus-4.8-1m'.
        assert model == "opus-4.8-1m", "late backfill should populate canonical model"
        # An inheriting slot is recorded but never pinned, so it keeps following
        # the default.
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_auto_sentinel_reaches_persistence_fallback(self, tmp_path, monkeypatch):
        """An unresolved Auto request delegates to the recorder's fallback.

        The slot remains blank because it has no concrete model id to persist,
        while the client lets the usage recorder distinguish Auto from an
        unavailable model source.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [
            LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=5, output_tokens=7)),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = ""

        client = self._make_mock_client(events, prov_model="auto")
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        captured: list[tuple] = []

        async def _fake_persist(k, m, e, provider="", **kwargs):
            captured.append((k, m, provider, kwargs["model_source"]))

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert len(captured) == 1
        assert captured[0][1] == ""
        assert captured[0][3] is client
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_existing_slot_model_is_not_overwritten(self, tmp_path, monkeypatch):
        """OpenCode resolves model synchronously; slot.model is already set
        when EVENT_COMPLETE arrives. Backfill must not clobber it.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [
            LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=1, output_tokens=2)),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-4.6"

        # Even if the inner client somehow reports a different value,
        # slot.model wins because it was already set explicitly.
        client = self._make_mock_client(events, prov_model="should-not-be-used")
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        captured: list[tuple] = []

        async def _fake_persist(k, m, e, provider="", **kwargs):
            captured.append((k, m, provider))

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert len(captured) == 1
        assert captured[0][1] == "claude-opus-4.6"
        assert slot.model == "claude-opus-4.6"


class TestTokenUsageSurface:
    """Usage rows record the slot's effective session source."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("slot_key", "linked_session_key", "expected"),
        [
            ("named-dashboard-tab", "", "dashboard"),
            ("slack-tab", "slack:1234567890.123456", "slack"),
            ("telegram-tab", "telegram:123:456", "telegram"),
        ],
    )
    async def test_completion_uses_effective_session_identity(
        self,
        tmp_path,
        monkeypatch,
        slot_key,
        linked_session_key,
        expected,
    ):
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        complete = LLMEvent(
            kind=EVENT_COMPLETE,
            usage=TurnUsage(input_tokens=1, output_tokens=1, duration_ms=25),
        )
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot(slot_key)
        slot._titled = True
        slot.linked_session_key = linked_session_key
        client = TestTokenPersistenceBackfill._make_mock_client(
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), complete]
        )
        client.context_used_tokens = MagicMock(return_value=10)
        client.context_window_tokens = MagicMock(return_value=100)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        persist = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.persist_token_record_async", persist)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert persist.await_args.args[0] == slot_key
        assert persist.await_args.kwargs["surface"] == expected

    @pytest.mark.asyncio
    async def test_completion_keeps_source_of_session_that_ran(self, tmp_path, monkeypatch):
        """A mid-turn rebind must not move the completed turn's attribution."""
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        complete = LLMEvent(
            kind=EVENT_COMPLETE,
            usage=TurnUsage(input_tokens=1, output_tokens=1, duration_ms=25),
        )
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("channel-tab")
        slot._titled = True
        slot.linked_session_key = "slack:1234567890.123456"
        client = TestTokenPersistenceBackfill._make_mock_client([])
        client.context_used_tokens = MagicMock(return_value=10)
        client.context_window_tokens = MagicMock(return_value=100)

        async def _stream(_message):
            # _run_chat has already selected the Slack session for this turn.
            slot.linked_session_key = "telegram:123:456"
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield complete

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        persist = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.persist_token_record_async", persist)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert persist.await_args.args[0] == slot.key
        assert persist.await_args.kwargs["surface"] == "slack"


class TestCostOnlyTurnPersistGate:
    """The chat_runner turn-end persist gate must fire on ``cost_usd`` alone.

    A claude-seam turn ending via a synthetic EVENT_COMPLETE (timeout,
    tool-stall, cancel-unacked) can carry cost with zero tokens and zero
    credits; the footer already reads ``_u.cost_usd`` off the same event, so
    only the gate stood between the cost and the usage store.
    """

    @pytest.mark.asyncio
    async def test_a_cost_only_turn_persists_its_row(self, tmp_path, monkeypatch):
        """Mutation guard: dropping the gate's ``cost_usd`` conjunct makes the
        persist call disappear and this fail."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(cost_usd=0.42))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = TestTokenPersistenceBackfill._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        persist = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.persist_token_record_async", persist)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert persist.await_count == 1
        assert persist.await_args.args[2].usage.cost_usd == pytest.approx(0.42)

    @pytest.mark.asyncio
    async def test_an_all_zero_turn_still_writes_no_row(self, tmp_path, monkeypatch):
        """The widened gate must not have become unconditional: a turn whose
        usage carries no billing dimension at all stays out of the store."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage())]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = TestTokenPersistenceBackfill._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        persist = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.persist_token_record_async", persist)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        persist.assert_not_awaited()


class TestKiroBackfillProfileGuard:
    """Regression tests for the kiro/acp backfill must NOT store a
    resolved Bedrock inference-profile id into slot.model.

    kiro-cli reports the RESOLVED profile id (e.g.
    ``global.anthropic.claude-opus-4-8[1m]``) on ``client._model`` — not the
    alias the user picked. Because ``canonicalize_for_provider`` is a no-op for
    kiro, an un-guarded backfill stores that profile id verbatim and re-sends it
    as a ``set_model`` override on every resume, pinning the slot to one
    profile+region. When that profile is capacity-throttled the session is stuck
    and the picker can't dislodge it. The guard drops a profile-form id for
    non-claude_code providers so the slot re-resolves; portable aliases are kept.
    """

    @staticmethod
    def _client_with_model(prov_model):
        client = MagicMock()
        inner = MagicMock()
        inner._model = prov_model
        client.client = inner
        return client

    def test_predicate_flags_bedrock_profile_ids(self):
        from kiro_crew.dashboard.chat_runner import _is_bedrock_profile_id

        assert _is_bedrock_profile_id("global.anthropic.claude-opus-4-8[1m]")
        assert _is_bedrock_profile_id("us.anthropic.claude-opus-4-7")
        assert _is_bedrock_profile_id("global.anthropic.claude-sonnet-4-6[1m]")
        # Portable aliases the picker actually sets — NOT profile ids.
        assert not _is_bedrock_profile_id("claude-opus-4.7")
        assert not _is_bedrock_profile_id("claude-opus-4.8")
        assert not _is_bedrock_profile_id("sonnet")
        assert not _is_bedrock_profile_id("deepseek-3.2")

    @pytest.mark.parametrize(
        "model, providers",
        [
            # acp/kiro provider: the throttled profile id must NOT be backfilled.
            pytest.param(
                "global.anthropic.claude-opus-4-8[1m]",
                ("acp", "kiro"),
                id="kiro_profile_id_is_dropped",
            ),
            pytest.param("auto", ("acp", "claude_code"), id="auto_sentinel_still_skipped"),
        ],
    )
    def test_not_backfilled(self, model, providers):
        from kiro_crew.dashboard.chat_runner import _backfill_canonical_model

        client = self._client_with_model(model)
        for provider in providers:
            assert _backfill_canonical_model(client, provider) == ""

    def test_kiro_portable_alias_is_kept(self):
        from kiro_crew.dashboard.chat_runner import _backfill_canonical_model

        # A dotted alias is the picker's value and routes with capacity
        # awareness — keep it so the header/dropdown still reflect the model.
        client = self._client_with_model("claude-opus-4.7")
        assert _backfill_canonical_model(client, "acp") == "claude-opus-4.7"

    def test_claude_code_profile_id_still_canonicalizes(self):
        from kiro_crew.dashboard.chat_runner import _backfill_canonical_model

        # claude_code is unaffected: its profile id maps to the dropdown key the
        # user explicitly chose, so the guard must not strip it.
        client = self._client_with_model("global.anthropic.claude-opus-4-8[1m]")
        out = _backfill_canonical_model(client, "claude_code")
        assert out == "opus-4.8-1m"

    @pytest.mark.asyncio
    async def test_run_chat_kiro_resolved_profile_not_pinned(self, tmp_path, monkeypatch):
        """End-to-end: a kiro session whose backend resolves to the 1M Opus
        profile mid-turn must leave slot.model EMPTY (not pinned to the profile
        id), so the next resume re-resolves rather than re-sending the throttled
        profile as a set_model override.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]

        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = ""  # user picked nothing explicit on this turn

        # Default test config provider is acp/kiro — exercise that path.
        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        inner = MagicMock()
        inner._model = ""  # empty at create; kiro learns the profile mid-turn
        client.client = inner

        async def _stream(msg):
            # kiro resolves the picked alias to a concrete Bedrock profile id.
            inner._model = "global.anthropic.claude-opus-4-8[1m]"
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        captured: list[tuple] = []

        async def _fake_persist(k, m, e, provider="", **kwargs):
            captured.append((k, m, provider))

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # slot.model must NOT be pinned to the throttled profile id.
        assert slot.model == "", "kiro profile id must not poison slot.model"
        assert len(captured) == 1
        assert captured[0][1] == "", "token record must not carry the profile id"


class TestPinnedModelWithheld:
    """The slot's pin must not outlive the account's access to that model.

    ``providers.acp`` withholds a configured model the live session did not
    advertise and leaves the session on the backend default, so the turn
    succeeds. Nothing told the slot, so the composer chip and the picker went on
    naming a model no turn would use — the reported symptom after a plan
    downgrade (chip read ``claude-opus-5``; every turn ran on auto). The runner
    reports the dead pin using the same predicate the withhold uses, and carries
    the verdict in the slots payload so the frontend reads it instead of
    inferring it from picker-list membership.
    """

    @staticmethod
    def _client(advertised, *, claude_backend=False):
        client = MagicMock()
        client.available_models = MagicMock(return_value=[{"modelId": m} for m in advertised])
        # The verdict is skipped for a backend that resolves its wire id from its
        # OWN advertised list, so the double carries that backend's record rather
        # than an identity flag.
        client.capabilities = capabilities_for(
            ACP_BACKEND_CLAUDE if claude_backend else ACP_BACKEND_KIRO
        )
        return client

    @pytest.mark.parametrize(
        "advertised, pin, provider, verdict",
        [
            pytest.param(
                ["auto", "claude-sonnet-5"],
                "claude-opus-5",
                "acp",
                True,
                id="pin_absent_from_advertised_is_withheld",
            ),
            pytest.param(
                ["auto", "claude-opus-5"],
                "claude-opus-5",
                "acp",
                False,
                id="advertised_pin_is_kept",
            ),
            # A persisted pin can carry a `<namespace>::<bare-id>` qualifier from the
            # catalog that advertised it when it was stored, while the session being
            # judged advertises the BARE id. The literal comparison missed for every
            # such pin and the banner claimed a fully supported model "isn't offered
            # right now". The verdict must peel the qualifier and resolve to runnable
            # -- for ANY namespace vocabulary, not just `agent.provider` (a fixed enum
            # that no catalog qualifies ids with, so keying on it would leave the fold
            # unreachable).
            pytest.param(
                ["auto", "z-ai/glm-5.3-flash"],
                "openrouter::z-ai/glm-5.3-flash",
                "acp",
                False,
                id="namespaced_pin_matches_bare_advertised",
            ),
            # _run_chat passes provider_name="" when the config could not be read.
            # The fold keys on the pin's own qualifier, not on the provider string,
            # so an unreadable config must not degrade the verdict back to the
            # false withhold (nor is it needed for the peel to fire).
            pytest.param(
                ["auto", "z-ai/glm-5.3-flash"],
                "openrouter::z-ai/glm-5.3-flash",
                "",
                False,
                id="namespaced_pin_judged_even_when_provider_unreadable",
            ),
            # The peel must not turn the verdict into a rubber stamp: a pin the
            # backend serves under NEITHER spelling still answers withheld.
            pytest.param(
                ["auto", "z-ai/glm-5.3-flash"],
                "openrouter::no-such-model",
                "acp",
                True,
                id="pin_absent_after_peel_is_still_withheld",
            ),
            # The fold peels exactly ONE leading qualifier. A doubly-qualified pin
            # whose innermost tail happens to be advertised is not a spelling of
            # that model -- unbounded stripping would rubber-stamp arbitrary junk
            # around any advertised id.
            pytest.param(
                ["auto", "glm-5.3-flash"],
                "a::b::glm-5.3-flash",
                "acp",
                True,
                id="only_one_qualifier_level_is_peeled",
            ),
            # slot.model is a canonical key there while the backend advertises bare
            # ids -- comparing the two namespaces would call every model unusable.
            pytest.param(
                ["claude-opus-4-8[1m]"],
                "opus-4.8-1m",
                "claude_code",
                None,
                id="claude_code_provider_is_exempt",
            ),
            # A backend that advertises the qualified spelling itself matches on
            # the first (full) comparison -- the peel is a miss-only retry, so it
            # can only clear a false withhold, never create one.
            pytest.param(
                ["openrouter::z-ai/glm-5.3-flash"],
                "openrouter::z-ai/glm-5.3-flash",
                "acp",
                False,
                id="pin_advertised_verbatim_needs_no_peel",
            ),
        ],
    )
    def test_verdict(self, advertised, pin, provider, verdict):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        client = self._client(advertised)
        assert _pinned_model_verdict(client, pin, provider) is verdict

    def test_unknown_entitlement_keeps_the_pin(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        # Advertised nothing (no session yet / backend omits the list) must read
        # as "unknown", never as "nothing is allowed" — and, since the verdict is
        # displayed, never as "entitled" either: None is its own answer.
        assert _pinned_model_verdict(self._client([]), "claude-opus-5", "acp") is None

    def test_auto_and_empty_are_never_withheld(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        client = self._client(["claude-sonnet-5"])
        assert _pinned_model_verdict(client, "auto", "acp") is None
        assert _pinned_model_verdict(client, "", "acp") is None

    def test_claude_backend_provider_is_exempt(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        client = self._client(["claude-opus-4-8[1m]"], claude_backend=True)
        assert _pinned_model_verdict(client, "claude-opus-4.8", "acp") is None

    def test_provider_without_getter_keeps_the_pin(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        client = MagicMock()
        del client.available_models
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        assert _pinned_model_verdict(client, "claude-opus-5", "acp") is None

    def test_getter_raising_keeps_the_pin(self):
        from kiro_crew.dashboard.chat_runner import _pinned_model_verdict

        client = MagicMock()
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        client.available_models = MagicMock(side_effect=RuntimeError("boom"))
        assert _pinned_model_verdict(client, "claude-opus-5", "acp") is None

    def test_slot_reports_no_verdict_until_one_is_recorded(self):
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        # A slot that has never spawned knows nothing about entitlement, and the
        # frontend fails open on that — so the default must be unknown, not False.
        assert slot.model_withheld is None
        assert slot.to_dict()["model_withheld"] is None

    def test_slots_payload_carries_both_answers(self):
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)
        assert slot.to_dict()["model_withheld"] is True
        slot.record_model_withheld(False)
        assert slot.to_dict()["model_withheld"] is False

    def test_served_model_is_unknown_until_a_session_reports_one(self):
        slot = _ChatSlot("s1")
        # Nothing has run, so nothing can name the model a turn would use. The
        # frontend keeps showing `auto` on this.
        assert slot.to_dict()["served_model"] == ""

    def test_served_model_is_carried_and_forgotten_with_the_session(self):
        """It describes the SESSION, so a teardown drops it.

        Unlike the withhold verdict it is not pinned to `slot.model`: an
        inheriting slot has no pin to pin it to.
        """
        slot = _ChatSlot("s1")
        slot.record_served_model("gpt-5.6-sol")
        assert slot.to_dict()["served_model"] == "gpt-5.6-sol"
        slot.record_served_model(None)
        assert slot.to_dict()["served_model"] == ""

    def test_verdict_is_reported_only_for_the_model_it_was_computed_for(self):
        """Re-pinning must invalidate the verdict without anyone remembering to.

        `slot.model` has many writers (the picker, the bulk pick, the pick
        rollback, two restore paths, the canonical backfill). A verdict that
        outlived a re-pin would label the NEW model with the OLD model's
        entitlement, so the verdict is stored against the id it was computed for
        instead of relying on each of those writers to clear it.
        """
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)
        assert slot.model_withheld is True

        slot.model = "claude-sonnet-5"  # the user picks a model they can run
        assert slot.model_withheld is None, "a verdict must not survive a re-pin"

        slot.model = "claude-opus-5"  # ...and back again
        assert slot.model_withheld is True, "the verdict still describes this pin"

        slot.model = ""  # pin cleared entirely
        assert slot.model_withheld is None

    def test_teardown_forgets_the_verdict(self):
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)
        # The verdict describes the session that advertised the list, so a
        # session teardown drops it rather than leaving the next session labelled
        # by the previous one's entitlement.
        slot.record_model_withheld(None)
        assert slot.model_withheld is None

    def test_every_session_teardown_drops_the_verdict(self):
        """A teardown site added later must not silently keep a dead verdict.

        The drop cannot live in one chokepoint: `_reset_slot_session` is the
        funnel for the switch handlers, but the runner tears a session down
        directly at a turn boundary too (the deferred project-change reset, the
        deferred conversation discard, the post-turn agent-switch reset) and
        routing those through the funnel would also cancel pending question cards.
        So the invariant is pinned here instead of trusting each author to
        remember it.
        """
        import importlib
        from pathlib import Path

        from kiro_crew.dashboard import chat_handlers, chat_runner

        # The handler bodies are split between `chat_handlers` and its
        # `chat_api` owners, so every owner is scanned beside the two facades.
        owner_paths = sorted((Path(chat_handlers.__file__).parent / "chat_api").glob("[!_]*.py"))
        owners = [
            importlib.import_module(f"kiro_crew.dashboard.chat_api.{path.stem}")
            for path in owner_paths
        ]
        assert owners, "no chat_api owner module found beside chat_handlers"
        # The turn's code is the runner plus the `chat_turn` owners its compose
        # runs, so a runner teardown site may sit in either and both are scanned.
        turn_owners = [
            importlib.import_module(f"kiro_crew.dashboard.chat_turn.{path.stem}")
            for path in sorted((Path(chat_runner.__file__).parent / "chat_turn").glob("[!_]*.py"))
        ]
        assert turn_owners, "no chat_turn owner module found beside chat_runner"
        teardowns = ("state.sessions.reset(", "state.sessions.discard_conversation(")
        runner_sites = 0
        handler_sites = 0
        for module in (chat_runner, *turn_owners, chat_handlers, *owners):
            lines = Path(module.__file__).read_text(encoding="utf-8").splitlines()
            sites = [i for i, line in enumerate(lines) if any(t in line for t in teardowns)]
            if module is chat_handlers:
                assert sites, f"no teardown call site found in {module.__name__}"
            if module is chat_runner or module in turn_owners:
                runner_sites += len(sites)
            else:
                handler_sites += len(sites)
            for i in sites:
                # Asymmetric window: a drop placed AFTER the call has to clear the
                # multi-line call plus its refusal check (`discard_conversation`
                # returns False when a turn is in flight), so the trailing half is
                # the wider one.
                window = "\n".join(lines[max(0, i - 12) : i + 23])
                assert "forget_session_model_state()" in window, (
                    f"{module.__name__}:{i + 1} replaces the session the withhold "
                    f"verdict and served model describe without dropping them -> "
                    f"{lines[i].strip()}"
                )
        assert runner_sites, "no teardown call site found in chat_runner or its chat_turn owners"
        # The reset funnel and the conversation discard: a scan that finds fewer
        # has lost sight of a handler teardown site rather than proven it safe.
        assert handler_sites >= 2, (
            f"only {handler_sites} handler teardown call site(s) found across "
            f"chat_handlers and its chat_api owners"
        )

    @pytest.mark.asyncio
    async def test_reset_chokepoint_forgets_the_verdict(self):
        """The one reset funnel must drop it, so all its call sites do."""
        from kiro_crew.dashboard.chat_handlers import _reset_slot_session

        state = MagicMock()

        async def _reset(_key, *, skip_if_busy=False):
            return True

        state.sessions.reset = _reset
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)

        with patch("kiro_crew.dashboard.chat_handlers._unblock_pending_waits"):
            await _reset_slot_session(state, slot, "dashboard:s1")

        assert slot.model_withheld is None

    @pytest.mark.asyncio
    async def test_a_declined_reset_keeps_the_live_session_s_verdict(self):
        """`skip_if_busy` declining leaves the session -- and its verdict -- alive.

        The reload route resets with ``skip_if_busy=True``, which returns False
        when a turn is in flight and touches nothing. Dropping the verdict there
        would republish a runnable pin as `auto` -- the defect the verdict exists
        to remove -- for a session that never changed.
        """
        from kiro_crew.dashboard.chat_handlers import _reset_slot_session

        state = MagicMock()

        async def _declined(_key, *, skip_if_busy=False):
            return False

        state.sessions.reset = _declined
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)

        with patch("kiro_crew.dashboard.chat_handlers._unblock_pending_waits"):
            reloaded = await _reset_slot_session(state, slot, "dashboard:s1", skip_if_busy=True)

        assert reloaded is False
        assert slot.model_withheld is True, "a declined reset must not erase the verdict"

    @pytest.mark.asyncio
    async def test_a_reset_that_raises_drops_the_verdict(self):
        """A teardown that raised leaves a session the slot cannot vouch for."""
        from kiro_crew.dashboard.chat_handlers import _reset_slot_session

        state = MagicMock()

        async def _boom(_key, *, skip_if_busy=False):
            raise RuntimeError("teardown exploded")

        state.sessions.reset = _boom
        slot = _ChatSlot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)

        with patch("kiro_crew.dashboard.chat_handlers._unblock_pending_waits"):
            with pytest.raises(RuntimeError):
                await _reset_slot_session(state, slot, "dashboard:s1")

        assert slot.model_withheld is None

    @pytest.mark.asyncio
    async def test_run_chat_reports_but_keeps_a_withheld_pin(self, tmp_path, monkeypatch):
        """End-to-end: a slot pinned to an unentitled model gets an in-chat
        notice, and the pin SURVIVES so a plan re-upgrade self-heals.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-5"  # pinned before the plan downgrade

        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        # The live session advertises the free tier only.
        client.available_models = MagicMock(
            return_value=[{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}]
        )
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # Keeping the pin is the point: withhold keeps it off the wire and
        # displayModel keeps it off the chip, so deleting an explicit user
        # setting buys nothing and costs the self-heal on re-upgrade.
        assert slot.model == "claude-opus-5", "an inert pin must not be deleted"
        # The activity line must not name the withheld model while the notice card
        # beside it says that model is not what is running.
        session_frames = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "activity_event" and c.args[1].get("kind") == "session"
        ]
        assert any(
            f.get("spawned") is True for f in session_frames
        ), f"a new session must report spawned=True, got {session_frames}"
        assert all(
            "claude-opus-5" not in f.get("text", "") for f in session_frames
        ), f"the session line must report the effective model, got {session_frames}"
        assert any(
            "auto" in f.get("text", "") for f in session_frames
        ), f"expected the effective model on the session line, got {session_frames}"
        # But the user must be able to learn why their model is not being used, and
        # that explanation has to survive a reload the same way the pin does — so
        # it is a persisted transcript row, not a transient activity line.
        notices = [m.get("content", "") for m in slot.messages if m.get("role") == "notice"]
        assert any(
            "claude-opus-5" in t and "isn't offered right now" in t for t in notices
        ), f"expected a persisted notice naming the withheld model, got {notices}"
        # And the verdict is CARRIED, not left for the frontend to re-derive: the
        # slots payload states it, so the chip does not have to read "absent from
        # GET /api/models" as "not entitled".
        assert slot.model_withheld is True
        assert slot.to_dict()["model_withheld"] is True

    @pytest.mark.asyncio
    async def test_run_chat_records_the_served_model_of_an_unpinned_slot(
        self, tmp_path, monkeypatch
    ):
        """The inheriting slot is the one whose chip has nothing else to name.

        `slot.model == ""` takes the backfill branch (which keeps the slot
        unpinned on purpose), so the served model must be recorded OUTSIDE the
        pinned-verdict branch. And it must be read through the provider's public
        `served_model` accessor: `client` is the AcpProvider wrapper, which has
        no `_resolved_model_id` — a private-field read would store `""` forever
        and the chip would keep saying `auto`.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = ""  # inheriting: no pin

        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.is_claude_backend = False
        client.available_models = MagicMock(
            return_value=[{"modelId": "auto"}, {"modelId": "gpt-5.6-sol"}]
        )
        # The wrapper's public accessor is the ONLY thing that names the served
        # model; the ACP-private field is not on this shape.
        client.served_model = "gpt-5.6-sol"
        del client._resolved_model_id
        inner = MagicMock()
        inner._model = ""  # the session still inherits; only the wire was fixed
        client.client = inner

        async def _stream(msg):
            del msg
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert slot.model == "", "an inheriting slot must stay unpinned"
        assert slot.to_dict()["served_model"] == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_run_chat_records_an_entitled_pin_as_not_withheld(self, tmp_path, monkeypatch):
        """The false answer must be carried too, not just the withhold.

        It is the half that stops an unrelated `/api/models` filter from acting as
        an entitlement signal. The verdict is computed against the wire id the
        session was actually given, so a pin the picker's list omits is still
        reported runnable.

        Asserted on a DEPRECATED pin's replacement on purpose: `api_models` drops
        deprecated ids before the entitlement narrowing, and `_normalize_model`
        rewrites such a pin to its replacement at turn start — so the id the
        session (and this verdict) sees is the replacement, never the deprecated
        spelling the slot was carrying.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-4.6-1m"  # deprecated spelling: absent from GET /api/models

        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        # The session serves the replacement the pin normalizes to.
        client.available_models = MagicMock(
            return_value=[{"modelId": "auto"}, {"modelId": "claude-opus-4.6"}]
        )
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            del msg
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert slot.model == "claude-opus-4.6", "the deprecated pin is rewritten at turn start"
        assert slot.to_dict()["model_withheld"] is False
        notices = [m.get("content", "") for m in slot.messages if m.get("role") == "notice"]
        assert not any(
            "isn't offered right now" in t for t in notices
        ), f"an entitled pin must not be reported as withheld, got {notices}"

    @pytest.mark.asyncio
    async def test_a_replacement_session_that_advertises_nothing_publishes_unknown(
        self, tmp_path, monkeypatch
    ):
        """A new session must never inherit the previous session's verdict.

        A replacement can arrive without a teardown this slot saw (a reaped
        session, a provider that re-spawned) and can advertise nothing at all --
        a dead provider, or a backend that omits `models`. That is "not known",
        and publishing the previous session's answer for it would label a live
        session from an entitlement snapshot it never took.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-5"
        slot.record_model_withheld(True)  # the PREVIOUS session withheld this pin

        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        client.available_models = MagicMock(return_value=[])  # advertises nothing
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            del msg
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert slot.to_dict()["model_withheld"] is None, "a stale verdict must not be republished"
        assert slot.model == "claude-opus-5", "the pin itself is untouched"
        notices = [m.get("content", "") for m in slot.messages if m.get("role") == "notice"]
        assert not any(
            "isn't offered right now" in t for t in notices
        ), f"unknown entitlement must not be reported as a withhold, got {notices}"

    @pytest.mark.asyncio
    async def test_run_chat_survives_an_unreadable_config_on_a_pinned_slot(
        self, tmp_path, monkeypatch
    ):
        """Config going unreadable mid-turn must not abort the turn.

        `cfg` is bound inside a try whose except only logs, so a later
        `cfg.agent.provider` read would raise UnboundLocalError. A PINNED slot is
        what exercises it: the withhold check runs only when slot.model is set, so
        an unpinned slot never reaches the second read.

        The failing load has to be the SECOND one. An earlier unguarded
        `KiroCrewConfig.load()` (slash-command detection) would abort the turn
        first if config were broken from the start, so the reachable shape is
        config that loads once and then goes bad — config is read live through a
        fingerprint cache, so an edit mid-turn does exactly that.
        """
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-5"

        real_cfg = KiroCrewConfig.load()
        seen = {"n": 0}

        def load_then_break():
            # Deliberately NOT a finite side_effect list: an extra call must not
            # raise StopIteration and turn a guard test into a mock artifact.
            seen["n"] += 1
            if seen["n"] == 2:
                raise ValueError("config became unreadable mid-turn")
            return real_cfg

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.KiroCrewConfig.load", load_then_break)

        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        client.available_models = MagicMock(return_value=[{"modelId": "claude-sonnet-5"}])
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        # Completes. Reading cfg.agent.provider here raises UnboundLocalError.
        await _run_chat(state, slot, "hello")

        assert seen["n"] >= 2, "the guarded load must actually have been reached"
        assert slot.model == "claude-opus-5", "the pin survives regardless"

    @pytest.mark.asyncio
    async def test_run_chat_keeps_the_real_model_on_the_label_when_entitled(
        self, tmp_path, monkeypatch
    ):
        """The withheld case reports `auto` on the activity line; the healthy case
        must still report the model the session actually runs on."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-5"

        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        # This account CAN run the pin, so nothing is withheld.
        client.available_models = MagicMock(
            return_value=[{"modelId": "auto"}, {"modelId": "claude-opus-5"}]
        )
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        session_frames = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "activity_event" and c.args[1].get("kind") == "session"
        ]
        assert any(
            "claude-opus-5" in f.get("text", "") for f in session_frames
        ), f"an entitled pin must still be named on the session line, got {session_frames}"
        notices = [m.get("content", "") for m in slot.messages if m.get("role") == "notice"]
        assert not any(
            "isn't offered right now" in t for t in notices
        ), f"nothing was withheld, so there must be no notice, got {notices}"

    @pytest.mark.asyncio
    async def test_run_chat_stays_quiet_on_a_warm_session(self, tmp_path, monkeypatch):
        """The notice reports the SPAWN-time withhold, so a warm session (neither
        new nor resumed) must not repeat it on every turn.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-5"

        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        client.available_models = MagicMock(return_value=[{"modelId": "claude-sonnet-5"}])
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        # Warm session: not new, not resumed.
        state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        notices = [m.get("content", "") for m in slot.messages if m.get("role") == "notice"]
        assert not any(
            "isn't offered right now" in t for t in notices
        ), f"the withhold notice must not repeat every turn, got {notices}"
        assert slot.model == "claude-opus-5"
        # A warm turn respawns nothing, so the session frame must say so: the
        # frontend refetches the entitlement-narrowed model list on `spawned`,
        # and /api/models spawns a subprocess, so a truthy flag here would run
        # one per prompt.
        session_frames = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "activity_event" and c.args[1].get("kind") == "session"
        ]
        assert session_frames, "the session frame is emitted on warm turns too"
        assert all(
            f.get("spawned") is False for f in session_frames
        ), f"warm turn must not claim a spawn, got {session_frames}"

    @pytest.mark.asyncio
    async def test_run_chat_keeps_an_entitled_pin(self, tmp_path, monkeypatch):
        """Counterpart: an advertised pin is left exactly as the user set it."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(input_tokens=3, output_tokens=4))]
        state = TestTokenPersistenceBackfill._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-sonnet-5"

        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        client.available_models = MagicMock(
            return_value=[{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}]
        )
        inner = MagicMock()
        inner._model = ""
        client.client = inner

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        async def _fake_persist(k, m, e, provider="", **kwargs):
            del k, m, e, provider, kwargs

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.persist_token_record_async", _fake_persist
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert slot.model == "claude-sonnet-5"
