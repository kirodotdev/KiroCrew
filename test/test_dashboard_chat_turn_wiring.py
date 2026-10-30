"""Dashboard chat: runtime wiring into ``_run_chat`` — memory store, resume, replay, reinjection."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import AsyncIterator, _make_state

from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog


class TestRuntimeWiringTurn:
    """TestRuntimeWiring: what the slot's runtime wiring hands each ``_run_chat`` turn."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("binding_change", ["none", "project", "replacement", "unavailable"])
    async def test_run_chat_passes_memory_store_to_build_message(
        self, tmp_path, monkeypatch, binding_change
    ):
        """_run_chat resolves agent bindings and passes memory_store to build_message.

        Requirements: 3.1
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        # Track calls to build_message
        build_message_calls: list[dict] = []

        def mock_build_message(self_ctx, text, is_new, session_key=None, **kwargs):
            build_message_calls.append({"text": text, "session_key": session_key, "kwargs": kwargs})
            return text, MagicMock(action=None, text="")

        from kiro_crew.config.loader import (
            KiroCrewAgentConfig,
            KiroCrewConfig,
            WorkspaceConfig,
            resolve_agent_bindings,
        )
        from kiro_crew.memory_stores import provision_member_memory

        mock_cfg = KiroCrewConfig.load()
        mock_cfg.agents["oncall"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", workspace="oncall-ws"
        )
        mock_cfg.workspaces["oncall-ws"] = WorkspaceConfig(dir=str(tmp_path / "oncall-ws"))
        private_store = provision_member_memory(mock_cfg, "oncall")
        mock_cfg.save()
        mock_bindings = resolve_agent_bindings(mock_cfg, "oncall")
        # The provider and context are doubles; model a supported runtime while
        # keeping member ownership and persisted conversation metadata real.

        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )

        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        ctx_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        monkeypatch.setattr(
            ctx_builder, "build_message", lambda *a, **kw: mock_build_message(ctx_builder, *a, **kw)
        )
        monkeypatch.setattr(ctx_builder, "ensure_store", AsyncMock(return_value=object()))
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.title_then_refresh", AsyncMock())
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.generate_session_summary", AsyncMock())

        state = _make_state(tmp_path, context_builder=ctx_builder)

        # Create a slot with retained history, then verify a cold start replays it.
        slot = state.get_or_create_slot("mem-test", agent="oncall")
        if binding_change == "none":
            from kiro_crew.execution_context import bind_session_execution, resolve_member_execution

            captured_execution = await asyncio.to_thread(
                resolve_member_execution, mock_cfg, "oncall"
            )
            await asyncio.to_thread(
                bind_session_execution, "dashboard:mem-test", captured_execution
            )
        conversation_log = ConversationLog(base_dir=tmp_path / "sessions")
        conversation_log.init()
        await asyncio.to_thread(
            conversation_log.append, "dashboard:mem-test", "user", "frozen retained question"
        )
        await asyncio.to_thread(
            conversation_log.append,
            "dashboard:mem-test",
            "assistant",
            "frozen retained answer",
        )
        await asyncio.to_thread(
            conversation_log.append, "dashboard:mem-test", "user", "test message"
        )
        ctx_builder.conversation_log = conversation_log
        state.conversation_log = conversation_log
        slot.append("user", "retained question", "msg msg-u")
        slot.append("assistant", "retained answer", "msg msg-a")
        slot.append("user", "test message", "msg msg-u")

        # Mock session manager to return a mock client
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        mock_client = MagicMock()
        mock_client.stream = MagicMock(
            return_value=AsyncIterator(
                [LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"), LLMEvent(kind=EVENT_COMPLETE)]
            )
        )
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.consume_replay_suppression = MagicMock(return_value=False)
        state.sessions.record_failure = AsyncMock()

        loop = asyncio.get_running_loop()
        binding_reads = []

        def checked_bindings(cfg, name, project_dir=None, **kwargs):
            from kiro_crew.memory_stores import UnknownMemoryStore

            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            assert kwargs["validate_memory_files"] is False
            binding_reads.append((name, project_dir))
            if binding_change == "unavailable":
                raise UnknownMemoryStore("member identity offline")
            resolved = resolve_agent_bindings(cfg, name, project_dir, **kwargs)
            if binding_change == "project":
                loop.call_soon_threadsafe(setattr, slot, "project", str(tmp_path / "new-project"))
            elif binding_change == "replacement":
                loop.call_soon_threadsafe(state._slots.__setitem__, slot.key, _ChatSlot(slot.key))
            return resolved

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.resolve_agent_bindings", checked_bindings
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "test message")
        await asyncio.gather(*state._background_tasks)

        assert len(binding_reads) == 1
        assert binding_reads[0][0] == "oncall"
        if binding_change != "none":
            from kiro_crew.member_memory_auth import read_private_session_store

            state.sessions.get_or_create.assert_not_awaited()
            state.sessions.record_failure.assert_awaited_once_with("dashboard:mem-test")
            assert build_message_calls == []
            assert read_private_session_store("dashboard:mem-test") is None
            assert any(
                message.get("role") == "error"
                and message.get("meta", {}).get("code") == "memory_unavailable"
                for message in slot.messages
            )
            return

        state.sessions.record_failure.assert_not_awaited()
        assert len(build_message_calls) == 1
        assert build_message_calls[0]["kwargs"].get("memory_store") == private_store
        assert build_message_calls[0]["session_key"] == "dashboard:mem-test"
        metadata = conversation_log.get_metadata("dashboard:mem-test")
        assert metadata["memory_store"] == private_store
        assert metadata["agent"] == "oncall"
        from kiro_crew.execution_context import read_session_execution

        retained_execution = read_session_execution("dashboard:mem-test")
        assert retained_execution.member_id == captured_execution.member_id
        assert retained_execution.store == captured_execution.store
        assert retained_execution.template_id == captured_execution.template_id
        replay = build_message_calls[0]["kwargs"].get("compressed_history")
        assert "frozen retained question" in replay
        assert "frozen retained answer" in replay

    @pytest.mark.parametrize(
        ("message_kind", "resumed", "expect_restore"),
        [
            ("manual_resume", True, True),
            ("conn_recover", True, True),
            ("busy_recover", True, True),
            # A cold start that did NOT natively resume carries the Kiro Crew
            # replay, which already holds the interrupted turn.
            ("manual_resume", False, False),
            # Continue on a turn that ended normally: kiro-cli logged it.
            ("manual_continue", True, False),
            # A cron delivery opened the interrupted turn as an ``inject``.
            ("cron_opener", True, True),
        ],
    )
    @pytest.mark.asyncio
    async def test_resume_on_a_natively_loaded_session_restores_the_interrupted_turn(
        self, tmp_path, monkeypatch, message_kind, resumed, expect_restore
    ):
        """kiro-cli logs a prompt only once it is answered, so a turn cut off by
        the process dying is absent from what ``session/load`` restores. A
        resumed session gets no replay, so the Resume press on that turn must
        carry the turn itself, or the model is told to finish a request it cannot
        see and answers the one before it (or reports nothing to continue)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        from kiro_crew.context import ContextBuilder
        from kiro_crew.dashboard.chat_utils import (
            _BUSY_RECOVER_MSG,
            _CONN_RECOVER_MSG,
            _MANUAL_CONTINUE_MSG,
            _MANUAL_RESUME_MSG,
        )
        from kiro_crew.memory import MemoryStore
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
        from kiro_crew.skills import SkillsLoader

        resume_text = {
            "manual_resume": _MANUAL_RESUME_MSG,
            "conn_recover": _CONN_RECOVER_MSG,
            "busy_recover": _BUSY_RECOVER_MSG,
            "manual_continue": _MANUAL_CONTINUE_MSG,
            "cron_opener": _MANUAL_RESUME_MSG,
        }[message_kind]
        built: list[str] = []

        def mock_build_message(text, context_is_new, session_key=None, **kwargs):
            built.append(text)
            return text, MagicMock(action=None, text="")

        ctx_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        ctx_builder.conversation_log = MagicMock()
        monkeypatch.setattr(ctx_builder, "build_message", mock_build_message)
        monkeypatch.setattr("kiro_crew.context.build_session_replay", lambda *a, **k: "")

        state = _make_state(tmp_path, context_builder=ctx_builder)
        hook_store = MagicMock()
        hook_store.fire = AsyncMock(return_value=[])
        state._hook_store = hook_store
        slot = state.get_or_create_slot("resume-native")
        slot.append("user", "Earlier request, answered long ago")
        slot.append("assistant", "Earlier answer")
        if message_kind == "cron_opener":
            slot.append(
                "inject",
                "Summarise the incident timeline for TICKET-42",
                meta={"injectKind": "cron"},
            )
        else:
            slot.append("user", "Summarise the incident timeline for TICKET-42")
        slot.append("assistant", "Pulling the timeline now; first event at 09:14")
        resume_row = slot.append("inject", resume_text, meta={"injectKind": "recovery"})

        streamed: list[str] = []

        async def stream(stream_message):
            streamed.append(stream_message)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        mock_client = MagicMock()
        mock_client.stream = stream
        mock_client.stream_command = stream
        mock_client.context_usage_pct = MagicMock(return_value=10.0)
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, True, resumed))
        state.sessions.get_pid = MagicMock(return_value=None)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(
            state,
            slot,
            resume_text,
            _current_message=resume_row,
            _synthetic_recovery_turn=True,
        )

        assert len(built) == 1 and len(streamed) == 1
        sent = streamed[0]
        # Minted after the egress scrub, as a prefix of the final prompt: the
        # builder's own input stays the bare recovery text.
        assert built[0] == resume_text
        assert sent.startswith("[INTERRUPTED TURN") is expect_restore
        assert ("Summarise the incident timeline for TICKET-42" in sent) is expect_restore
        assert ("first event at 09:14" in sent) is expect_restore
        # Only the interrupted turn is restored, never the one before it.
        assert "Earlier request, answered long ago" not in sent

    @pytest.mark.parametrize(
        ("accepts_inbound", "expected_channel"),
        [(True, "chat-42"), (False, None)],
    )
    @pytest.mark.asyncio
    async def test_run_chat_passes_only_resumable_mirror_channel_to_provider(
        self, tmp_path, monkeypatch, accepts_inbound, expected_channel
    ):
        """Only an inbound-capable persisted mirror identifies a dispatcher.

        Telegram/Discord mirrors can reuse a ``dashboard:*`` key while their
        identity exists only in ``SessionMap.mirror``. Two-way links must retain
        native resume; outbound-only mirrors still run as direct dashboard turns
        and need the Tool Search resume workaround.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        from kiro_crew.messaging.link import ChannelLink
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state = _make_state(tmp_path)
        state.context_builder = None
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("linked-provider")
        slot._slack_channel = ""
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="chat-42", thread_id="thread-7")
        )
        state.sessions.mirror_accepts_inbound = MagicMock(return_value=accepts_inbound)

        async def stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        mock_client = MagicMock()
        mock_client.stream = stream
        mock_client.stream_command = stream
        mock_client.context_usage_pct = MagicMock(return_value=10.0)
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "linked dashboard turn")

        assert state.sessions.get_or_create.await_args.kwargs["channel_id"] == expected_channel

    @pytest.mark.asyncio
    async def test_first_slash_preserves_pending_replay_for_next_prompt(
        self, tmp_path, monkeypatch
    ):
        """A fresh session's slash command bypasses ContextBuilder entirely.

        The command may consume SessionManager's one-shot ``is_new`` observation,
        but it must not consume the conversation replay debt. Pre-dispatch Stop
        leaves it armed; an accepted turn consumes it on the first provider event;
        and a cancelled terminal re-arms it because the provider discards that
        turn. The next accepted ordinary prompt finally clears the lease.
        """
        from chat_test_helpers import pin_kiro_cli_harness

        pin_kiro_cli_harness()
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        build_message_calls: list[dict] = []
        build_behavior = {"stop_during_build": False}

        def mock_build_message(text, context_is_new, session_key=None, **kwargs):
            build_message_calls.append(
                {
                    "text": text,
                    "context_is_new": context_is_new,
                    "session_key": session_key,
                    "kwargs": kwargs,
                }
            )
            if build_behavior["stop_during_build"]:
                slot._stop_generation += 1
                build_behavior["stop_during_build"] = False
            return text, MagicMock(action=None, text="")

        from kiro_crew.acp.types import STOP_REASON_CANCELLED
        from kiro_crew.context import ContextBuilder
        from kiro_crew.hooks import HOOK_EVENT_AGENT_SPAWN
        from kiro_crew.memory import MemoryStore
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
        from kiro_crew.skills import SkillsLoader

        ctx_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        ctx_builder.conversation_log = MagicMock()
        monkeypatch.setattr(ctx_builder, "build_message", mock_build_message)
        monkeypatch.setattr(
            "kiro_crew.context.build_session_replay",
            lambda *args, **kwargs: "retained conversation replay",
        )

        state = _make_state(tmp_path, context_builder=ctx_builder)
        hook_store = MagicMock()
        hook_store.fire = AsyncMock(return_value=[])
        state._hook_store = hook_store
        slot = state.get_or_create_slot("slash-replay")
        slot.folder_id = "folder-1"
        state.folder_breadcrumb = MagicMock(return_value="Workspace / Demo")
        persona_first_turn: list[bool] = []

        def inject_persona(message, _theme, context_is_new, **_kwargs):
            persona_first_turn.append(context_is_new)
            return message

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._maybe_inject_persona",
            inject_persona,
        )

        dispatched: list[str] = []
        stream_behavior = {
            "cancel_after_accept": False,
            "raise_after_accept": False,
            "synthetic_end_turn": False,
            "empty_end_turn": False,
        }

        async def stream(stream_message):
            dispatched.append(stream_message)
            if stream_message != "/tools":
                assert replay["pending"] is True
            if stream_behavior["cancel_after_accept"]:
                stream_behavior["cancel_after_accept"] = False
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED)
                return
            if stream_behavior["synthetic_end_turn"]:
                stream_behavior["synthetic_end_turn"] = False
                yield LLMEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason="end_turn",
                    synthetic_completion=True,
                )
                return
            if stream_behavior["empty_end_turn"]:
                stream_behavior["empty_end_turn"] = False
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            if stream_message != "/tools":
                assert replay["pending"] is True
            if stream_behavior["raise_after_accept"]:
                stream_behavior["raise_after_accept"] = False
                raise RuntimeError("stream failed after accepting replay")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        mock_client = MagicMock()
        mock_client.stream = stream
        mock_client.stream_command = stream
        mock_client.context_usage_pct = MagicMock(return_value=10.0)
        state.sessions.get_or_create = AsyncMock(
            side_effect=[
                (mock_client, True, False),
                (mock_client, False, False),
                (mock_client, False, False),
                (mock_client, False, False),
                (mock_client, False, False),
                (mock_client, False, False),
                (mock_client, False, False),
                (mock_client, False, False),
            ]
        )
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.record_failure = AsyncMock()
        state.sessions.consume_replay_suppression = MagicMock(return_value=False)

        replay = {"pending": True}
        durable = {"sid": "old-full-history-sid"}

        def replay_pending(_key):
            return replay["pending"]

        def consume_replay(_key):
            was_pending = replay["pending"]
            replay["pending"] = False
            return was_pending

        def mark_replay(_key):
            replay["pending"] = True
            return True

        def commit_replay(_key):
            durable["sid"] = "fresh-replayed-sid"
            replay["pending"] = False
            return True

        state.sessions.provider_switch_replay_pending = MagicMock(side_effect=replay_pending)
        state.sessions.consume_provider_switch_replay = MagicMock(side_effect=consume_replay)
        state.sessions.mark_provider_switch_replay = MagicMock(side_effect=mark_replay)
        state.sessions.commit_provider_switch_replay_sid = MagicMock(side_effect=commit_replay)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/tools")

        assert build_message_calls == []
        assert replay["pending"] is True
        assert durable["sid"] == "old-full-history-sid"
        assert dispatched == ["/tools"]
        state.sessions.consume_provider_switch_replay.assert_not_called()
        state.sessions.commit_provider_switch_replay_sid.assert_not_called()

        build_behavior["stop_during_build"] = True
        await _run_chat(state, slot, "stop during replay prep")

        assert len(build_message_calls) == 1
        assert build_message_calls[0]["context_is_new"] is True
        assert build_message_calls[0]["kwargs"]["folder_path"] == "Workspace / Demo"
        assert (
            build_message_calls[0]["kwargs"]["compressed_history"] == "retained conversation replay"
        )
        assert persona_first_turn == [True]
        assert replay["pending"] is True
        assert durable["sid"] == "old-full-history-sid"
        assert dispatched == ["/tools"]
        state.sessions.consume_provider_switch_replay.assert_not_called()
        state.sessions.commit_provider_switch_replay_sid.assert_not_called()

        stream_behavior["cancel_after_accept"] = True
        await _run_chat(state, slot, "cancel accepted replay")

        assert len(build_message_calls) == 2
        cancelled_call = build_message_calls[1]
        assert cancelled_call["context_is_new"] is True
        assert cancelled_call["kwargs"]["folder_path"] == "Workspace / Demo"
        assert cancelled_call["kwargs"]["compressed_history"] == "retained conversation replay"
        assert persona_first_turn == [True, True]
        assert dispatched == ["/tools", "cancel accepted replay"]
        assert replay["pending"] is True
        assert durable["sid"] == "old-full-history-sid"
        state.sessions.consume_provider_switch_replay.assert_not_called()
        state.sessions.mark_provider_switch_replay.assert_called_once_with("dashboard:slash-replay")
        state.sessions.commit_provider_switch_replay_sid.assert_not_called()

        stream_behavior["raise_after_accept"] = True
        await _run_chat(state, slot, "raise after accepted replay")

        assert len(build_message_calls) == 3
        assert replay["pending"] is True
        assert durable["sid"] == "old-full-history-sid"
        state.sessions.consume_provider_switch_replay.assert_not_called()
        assert state.sessions.mark_provider_switch_replay.call_count == 2
        state.sessions.commit_provider_switch_replay_sid.assert_not_called()

        stream_behavior["synthetic_end_turn"] = True
        slot._empty_response_retries = 2
        await _run_chat(state, slot, "synthetic replay completion")

        assert len(build_message_calls) == 4
        assert replay["pending"] is True
        assert durable["sid"] == "old-full-history-sid"
        state.sessions.consume_provider_switch_replay.assert_not_called()
        assert state.sessions.mark_provider_switch_replay.call_count == 3
        state.sessions.commit_provider_switch_replay_sid.assert_not_called()

        stream_behavior["empty_end_turn"] = True
        slot._empty_response_retries = 2
        await _run_chat(state, slot, "real empty replay with exhausted budget")

        assert len(build_message_calls) == 5
        assert replay["pending"] is True
        assert durable["sid"] == "old-full-history-sid"
        state.sessions.consume_provider_switch_replay.assert_not_called()
        assert state.sessions.mark_provider_switch_replay.call_count == 4
        state.sessions.commit_provider_switch_replay_sid.assert_not_called()

        stream_behavior["empty_end_turn"] = True
        slot._empty_response_retries = 1
        with patch(
            "kiro_crew.dashboard.chat_runner._empty_auto_continue_enabled",
            return_value=False,
        ):
            await _run_chat(state, slot, "real empty replay with continuation off")

        assert len(build_message_calls) == 6
        assert replay["pending"] is True
        assert durable["sid"] == "old-full-history-sid"
        state.sessions.consume_provider_switch_replay.assert_not_called()
        assert state.sessions.mark_provider_switch_replay.call_count == 5
        state.sessions.commit_provider_switch_replay_sid.assert_not_called()

        await _run_chat(state, slot, "continue after uncommitted replay turns")

        assert len(build_message_calls) == 7
        call = build_message_calls[6]
        assert call["context_is_new"] is True
        assert call["kwargs"]["folder_path"] == "Workspace / Demo"
        assert call["kwargs"]["compressed_history"] == "retained conversation replay"
        assert persona_first_turn == [True, True, True, True, True, True, True]
        assert dispatched == [
            "/tools",
            "cancel accepted replay",
            "raise after accepted replay",
            "synthetic replay completion",
            "real empty replay with exhausted budget",
            "real empty replay with continuation off",
            "continue after uncommitted replay turns",
        ]
        assert replay["pending"] is False
        assert durable["sid"] == "fresh-replayed-sid"
        state.sessions.consume_provider_switch_replay.assert_not_called()
        assert state.sessions.mark_provider_switch_replay.call_count == 5
        state.sessions.commit_provider_switch_replay_sid.assert_called_once_with(
            "dashboard:slash-replay"
        )
        agent_spawn_calls = [
            call
            for call in hook_store.fire.await_args_list
            if call.args and call.args[0] == HOOK_EVENT_AGENT_SPAWN
        ]
        assert len(agent_spawn_calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("native_flag", [False, True])
    async def test_resumed_observation_does_not_drop_an_owed_replay(
        self, tmp_path, monkeypatch, native_flag
    ):
        """A replay lease outranks a ``resumed`` observation.

        Allocation arms the lease only when the native session started fresh, as
        a Tool Search resume does when it replaces ``session/load``. If the turn
        also reads ``resumed=True`` (from the allocation tuple or the native
        client flag) it must still build a full first turn with the replay, not
        the slim resume that claims the history was restored.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        build_message_calls: list[dict] = []

        def mock_build_message(text, context_is_new, session_key=None, **kwargs):
            build_message_calls.append({"context_is_new": context_is_new, "kwargs": kwargs})
            return text, MagicMock(action=None, text="")

        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
        from kiro_crew.skills import SkillsLoader

        ctx_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        ctx_builder.conversation_log = MagicMock()
        monkeypatch.setattr(ctx_builder, "build_message", mock_build_message)
        monkeypatch.setattr(
            "kiro_crew.context.build_session_replay",
            lambda *args, **kwargs: "retained conversation replay",
        )

        state = _make_state(tmp_path, context_builder=ctx_builder)
        hook_store = MagicMock()
        hook_store.fire = AsyncMock(return_value=[])
        state._hook_store = hook_store
        slot = state.get_or_create_slot("resume-replay")

        async def stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        mock_client = MagicMock()
        mock_client.stream = stream
        mock_client.stream_command = stream
        mock_client.context_usage_pct = MagicMock(return_value=10.0)
        mock_client.client.resumed = native_flag
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, True, not native_flag))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.record_failure = AsyncMock()
        state.sessions.consume_replay_suppression = MagicMock(return_value=False)
        state.sessions.provider_switch_replay_pending = MagicMock(return_value=True)
        state.sessions.consume_provider_switch_replay = MagicMock(return_value=True)
        state.sessions.mark_provider_switch_replay = MagicMock(return_value=True)
        state.sessions.commit_provider_switch_replay_sid = MagicMock(return_value=True)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "what were we doing?")

        assert len(build_message_calls) == 1
        call = build_message_calls[0]
        assert call["context_is_new"] is True
        assert call["kwargs"]["resumed"] is False
        assert call["kwargs"]["compressed_history"] == "retained conversation replay"

    @pytest.mark.asyncio
    async def test_run_chat_forwards_and_clears_the_reinjection_flag(self, tmp_path, monkeypatch):
        """A compaction flags the session; the NEXT _run_chat must forward
        needs_reinjection=True to build_message and clear the flag so the turn
        after that does not re-inject again.

        Regression guard: without this wiring the flag is set by the compact
        callback and read by nobody, so the whole feature is dead code.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        build_message_calls: list[dict] = []

        def mock_build_message(self_ctx, text, is_new, session_key=None, **kwargs):
            build_message_calls.append({"text": text, "kwargs": kwargs})
            return text, MagicMock(action=None, text="")

        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        ctx_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        monkeypatch.setattr(
            ctx_builder, "build_message", lambda *a, **kw: mock_build_message(ctx_builder, *a, **kw)
        )

        state = _make_state(tmp_path, context_builder=ctx_builder)
        slot = state.get_or_create_slot("reinject-test")

        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=AsyncIterator([]))
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)

        # Stand in for the real SessionManager flag store, including the
        # mark/consume round-trip so the empty-response re-queue path is
        # exercised the way production behaves.
        flag = {"set": True}

        def _consume(key):
            was = flag["set"]
            flag["set"] = False
            return was

        def _mark(key):
            flag["set"] = True

        state.sessions.consume_needs_reinjection = MagicMock(side_effect=_consume)
        state.sessions.mark_needs_reinjection = MagicMock(side_effect=_mark)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "first turn after compaction")

        # The stream is empty, so this turn does not land. The flag must be put
        # BACK -- asserting on the mark call directly, because asserting that some
        # build carried True is tautological (the first assertion already pins it,
        # and the re-queue is drained by an outer worker, not an inner loop).
        assert build_message_calls, "build_message must have been called"
        assert (
            build_message_calls[0]["kwargs"].get("needs_reinjection") is True
        ), "the turn right after a compaction must re-inject the skills index"
        assert state.sessions.mark_needs_reinjection.called, (
            "a turn that consumed the flag but did not land must restore it, "
            "or the skills index is lost for the rest of the session"
        )
        assert flag["set"] is True, "the flag must be back on after a non-landing turn"

        # Once a turn genuinely lands, the flag is gone and later turns are clean.
        flag["set"] = False
        build_message_calls.clear()
        await _run_chat(state, slot, "a later turn")
        assert build_message_calls, "build_message must have been called again"
        assert all(
            c["kwargs"].get("needs_reinjection") is False for c in build_message_calls
        ), "the flag must be one-shot, not sticky for every later turn"

    @pytest.mark.asyncio
    async def test_member_first_turn_that_never_lands_rearms_reinjection(
        self, tmp_path, monkeypatch
    ):
        """A member DM's FIRST turn that ends without landing — a user Stop's
        cancelled completion, an empty stream, a graceful cancel — never enters
        the except arm, so an except-arm-only re-arm leaves the warm session
        running WITHOUT [PERMANENT RULES] for the rest of its life. The re-arm
        must live in the finally, on every exit path."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        ctx_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        monkeypatch.setattr(
            ctx_builder,
            "build_message",
            lambda text, is_new, session_key=None, **kw: (text, MagicMock(action=None, text="")),
        )

        state = _make_state(tmp_path, context_builder=ctx_builder)
        cfg = KiroCrewConfig()
        cfg.agents["rearm-test"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.default_agent = "rearm-test"
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.KiroCrewConfig.load", lambda: cfg)
        slot = state.get_or_create_slot("member-rearm-test", agent="rearm-test", mode="member")

        mock_client = MagicMock()
        # Empty stream: the turn ends with no landing and NO exception — the
        # same observable shape as a Stop's cancelled completion.
        mock_client.stream = MagicMock(return_value=AsyncIterator([]))
        # is_new=True: this is the member session's FIRST turn.
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        # The compaction flag is OFF, so only the member condition can re-arm.
        state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
        state.sessions.mark_needs_reinjection = MagicMock()

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "first member turn")

        assert state.sessions.mark_needs_reinjection.called, (
            "a member first turn that never landed must re-arm reinjection on "
            "EVERY exit path (finally), or the next warm turn runs without the "
            "member section and its [PERMANENT RULES]"
        )
