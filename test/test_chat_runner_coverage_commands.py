"""Coverage tests for ``kiro_crew.dashboard.chat_runner``: local commands and recovery.

Covers the model backfill and pinned-model guards, the slash commands a ``_run_chat``
turn answers locally, and the retry ladders a terminal ``stop_reason`` walks.
``test_chat_runner_coverage.py`` describes the two harnesses the family uses; the
shared builders are in ``chat_runner_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_runner_coverage_helpers import (
    _async_iter,
    _complete,
    _drive,
    _errors,
    _quiet_sel,
    _runner_state,
    _set_stream,
    _settle,
    _slot,
)
from chat_test_helpers import pin_harness_choice

from kiro_crew.acp.types import (
    EVENT_TEXT_CHUNK,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.base import LLMEvent
from kiro_crew.testing.wait import await_until

# ── model backfill / pin guards ───────────────────────────────────────────


class TestModelBackfill:
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("global.anthropic.claude-opus-4-8[1m]", True),
            ("us.anthropic.claude-sonnet-4-6", True),
            ("claude-opus-4.7", False),
            ("deepseek-3.2", False),
        ],
    )
    def test_bedrock_profile_detection(self, model, expected):
        assert chat_runner._is_bedrock_profile_id(model) is expected

    @pytest.mark.parametrize(
        "model",
        [
            pytest.param("", id="test_missing_provider_model_backfills_nothing"),
            pytest.param("auto", id="test_auto_sentinel_is_skipped"),
            # Caching a resolved profile id would pin the slot to one region.
            pytest.param(
                "global.anthropic.claude-opus-4-8[1m]",
                id="test_profile_id_is_dropped_off_the_claude_code_path",
            ),
        ],
    )
    def test_unusable_model_backfills_nothing(self, model):
        client = MagicMock()
        client.client._model = model

        assert chat_runner._backfill_canonical_model(client, "kiro") == ""

    def test_portable_alias_is_kept(self):
        client = MagicMock()
        client.client._model = "deepseek-3.2"

        with patch.object(
            chat_runner.model_registry, "canonicalize_for_provider", return_value="deepseek-3.2"
        ):
            assert chat_runner._backfill_canonical_model(client, "kiro") == "deepseek-3.2"


class TestPinnedModelWithheld:
    @pytest.mark.parametrize(
        "model,provider", [("", "kiro"), ("auto", "kiro"), ("x", "claude_code")]
    )
    def test_unpinnable_combinations_are_never_withheld(self, model, provider):
        assert chat_runner._pinned_model_verdict(MagicMock(), model, provider) is None

    def test_claude_backend_is_exempt(self):
        client = MagicMock()
        client.is_claude_backend = True

        assert chat_runner._pinned_model_verdict(client, "claude-opus-5", "kiro") is None

    def test_provider_without_an_advertiser_leaves_the_pin_alone(self):
        client = MagicMock()
        client.is_claude_backend = False
        client.available_models = "not-callable"

        assert chat_runner._pinned_model_verdict(client, "claude-opus-5", "kiro") is None

    def test_advertiser_failure_fails_open(self):
        client = MagicMock()
        client.is_claude_backend = False
        client.available_models = MagicMock(side_effect=RuntimeError("no session"))

        assert chat_runner._pinned_model_verdict(client, "claude-opus-5", "kiro") is None

    def test_unadvertised_pin_is_reported_as_withheld(self):
        client = MagicMock()
        client.is_claude_backend = False
        client.available_models = MagicMock(return_value=[{"modelId": "claude-sonnet-4.6"}])

        with patch.object(chat_runner, "advertised_model_ids", return_value={"claude-sonnet-4.6"}):
            assert chat_runner._pinned_model_verdict(client, "claude-opus-5", "kiro") is True


# ── _run_chat: local commands ─────────────────────────────────────────────


class TestRunChatLocalCommands:
    @pytest.mark.asyncio
    async def test_blocked_slash_command_never_acquires_a_session(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        blocked = sorted(chat_runner._BLOCKED_SLASH_COMMANDS)[0]

        await _drive(state, slot, blocked)

        state.sessions.get_or_create.assert_not_awaited()
        assert any("not available in the dashboard" in m.get("content", "") for m in slot.messages)
        assert slot.messages[-1]["role"] == "done"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/issue", "/issue details from the user"])
    async def test_issue_is_refused_before_session_acquisition(self, tmp_path, command):
        state, client = _runner_state(tmp_path)
        slot = _slot("issue-command")

        await _drive(state, slot, command)

        state.sessions.get_or_create.assert_not_awaited()
        client.stream.assert_not_called()
        client.stream_command.assert_not_called()
        assert any("not available in the dashboard" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "provider,acp_backend,refused",
        [
            ("acp", "", True),
            ("acp", "kas", True),
            pytest.param("claude_code", "", True, id="legacy-label-does-not-select-claude"),
            pytest.param("claude_code", "claude", False, id="legacy-label-still-uses-backend"),
            ("acp", "claude", False),
        ],
    )
    async def test_todos_is_refused_only_where_the_harness_lacks_it(
        self, tmp_path, provider, acp_backend, refused
    ):
        """OSS selects the harness through acp_backend, even with a legacy label.

        H2 admits only provider=acp. The injected claude_code rows prove an
        unsupported label cannot override the public factory's backend selector.
        """
        state, client = _runner_state(tmp_path)
        _set_stream(client, [_complete()])
        slot = _slot()
        cfg = await asyncio.to_thread(chat_runner.KiroCrewConfig.load)
        cfg.agent.provider = provider
        cfg.agent.acp_backend = acp_backend
        client.capabilities = capabilities_for(acp_backend)
        # A KAS capability set arms the claimed-session hook re-projection check;
        # a mock's auto-generated batch attribute would read as stale. Record none.
        client.kas_auto_approved_capabilities = frozenset()

        with (
            patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg),
            patch.object(
                chat_runner, "select_provider_backend", wraps=chat_runner.select_provider_backend
            ) as select_backend,
        ):
            await _drive(state, slot, "/todos")

        select_backend.assert_called_once_with(
            chat_runner.effective_session_key(slot),
            cfg.agent.member_acp_backend,
            acp_backend,
            crew_backend=None,
        )

        notices = [
            m for m in slot.messages if "not available in the dashboard" in m.get("content", "")
        ]
        assert bool(notices) is refused
        if refused:
            state.sessions.get_or_create.assert_not_awaited()
        else:
            state.sessions.get_or_create.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "config_backend,stale_backend,actual_backend,refused",
        [
            # The stale row says Claude, but get_or_create evicts it and builds
            # the configured Kiro harness: the acquired provider must refuse.
            ("", "claude", "", True),
            # The inverse transition must forward to the acquired Claude
            # harness even though the dead row would have refused it.
            ("claude", "", "claude", False),
            # A supported successor hint still needs acquired-provider confirmation.
            ("claude", "claude", "", True),
        ],
    )
    async def test_todos_gate_uses_the_provider_acquired_after_stale_eviction(
        self, tmp_path, config_backend, stale_backend, actual_backend, refused
    ):
        """A dead registry row cannot answer for its get_or_create successor.

        Session allocation probes liveness and can replace the row with a
        different harness from current config. The command gate must use the
        provider whose lease it actually acquired, not either pre-turn hint.
        """
        from kiro_crew.providers.acp import AcpProvider

        state, client = _runner_state(tmp_path)
        _set_stream(client, [_complete()])
        slot = _slot()
        stale = AcpProvider(acp_backend=stale_backend)
        state.sessions._sessions = {
            chat_runner.effective_session_key(slot): SimpleNamespace(provider=stale)
        }
        state.sessions.get_provider = MagicMock(return_value=stale)
        state.sessions.is_provider_alive = AsyncMock(return_value=False)
        client.capabilities = capabilities_for(actual_backend)

        async def acquire_successor(*_args, **_kwargs):
            assert stale.is_process_alive() is False
            return client, True, False

        state.sessions.get_or_create = AsyncMock(side_effect=acquire_successor)
        cfg = await asyncio.to_thread(chat_runner.KiroCrewConfig.load)
        cfg.agent.provider = "acp"
        cfg.agent.acp_backend = config_backend

        with patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg):
            await _drive(state, slot, "/todos")

        notices = [
            m for m in slot.messages if "not available in the dashboard" in m.get("content", "")
        ]
        assert bool(notices) is refused
        if config_backend == "":
            state.sessions.get_or_create.assert_not_awaited()
        else:
            state.sessions.get_or_create.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("projected_backend,refused", [("", True), ("claude", False)])
    async def test_todos_confirms_the_reprojected_claim(self, tmp_path, projected_backend, refused):
        """The final claim owns dispatch even when hook re-projection replaced it."""
        state, client = _runner_state(tmp_path)
        _set_stream(client, [_complete()])
        client.capabilities = capabilities_for(projected_backend)
        initial = SimpleNamespace(capabilities=capabilities_for("claude"))
        state.sessions.get_or_create = AsyncMock(return_value=(initial, True, False))
        slot = _slot("todos-reprojected")
        cfg = await asyncio.to_thread(chat_runner.KiroCrewConfig.load)
        cfg.agent.acp_backend = "claude"

        with (
            patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg),
            patch.object(
                chat_runner,
                "reproject_claimed_session",
                new=AsyncMock(return_value=(client, True, False)),
            ) as reproject,
        ):
            await _drive(state, slot, "/todos")

        assert reproject.await_args.args[3] == (initial, True, False)
        notices = [
            m for m in slot.messages if "not available in the dashboard" in m.get("content", "")
        ]
        assert bool(notices) is refused
        if refused:
            client.stream_command.assert_not_called()
        else:
            client.stream_command.assert_called_once_with("/todos")
        state.sessions.release.assert_called_once_with(chat_runner.effective_session_key(slot))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefetched_resume", [False, True])
    async def test_fresh_todos_refusal_replays_session_start_once_for_member(
        self, tmp_path, prefetched_resume
    ):
        """A fresh or resume-prefetched first command owes member start context."""
        from kiro_crew import member_memory_auth
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.memory_stores import provision_member_memory
        from kiro_crew.session import FirstTurnState, SessionManager, _Session
        from kiro_crew.skills import SkillsLoader

        builds: list[tuple[bool, str]] = []

        def build_message(text, is_new, session_key=None, **kwargs):
            member = kwargs.get("member", "")
            builds.append((is_new, member))
            assert kwargs["resumed"] is prefetched_resume
            if prefetched_resume:
                assert kwargs["compressed_history"] == ""
            # A fresh claim replays as session start (is_new); a resumed claim
            # keeps its native transcript and re-owes only the member section
            # through needs_reinjection. Either way the start block is injected
            # on the first ordinary turn and not the second.
            owes_start = (is_new or kwargs.get("needs_reinjection")) and member
            start = "[PERMANENT RULES]\nmember start\n" if owes_start else ""
            return start + text, MagicMock(action=None, text="")

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        builder.build_message = build_message
        state, client = _runner_state(tmp_path, context_builder=builder)
        slot = _slot("member-oncall")
        state._slots[slot.key] = slot
        slot.mode = "member"
        slot.agent = "oncall"
        cfg = KiroCrewConfig.load()
        # The pre-claim authority permits acquisition; the acquired Kiro refuses.
        cfg.agent.member_acp_backend = "claude"
        cfg.agents["oncall"] = KiroCrewAgentConfig()
        store = provision_member_memory(cfg, "oncall")
        cfg.save()
        # save() keeps the harness fields as stored, so the choice goes on disk.
        pin_harness_choice(member_acp_backend="claude")
        member_memory_auth.bind_private_session_store(
            chat_runner.effective_session_key(slot), store
        )
        client.capabilities = capabilities_for("")
        client.memory_mode = "persistent"
        client.is_process_alive = MagicMock(return_value=True)
        client.client.resumed = prefetched_resume
        client.client.pop_pending_oauth_requests = MagicMock(return_value=[])
        _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), _complete()])
        session_key = chat_runner.effective_session_key(slot)
        manager = SessionManager(cfg)
        # This is the record left by an eager spawn / slot-focused resume.
        # Drive the real claim so the first /todos consumes the armed observation.
        session = _Session(
            provider=client,
            first_turn=FirstTurnState.RESUMED if prefetched_resume else FirstTurnState.FRESH,
        )
        manager._sessions[session_key] = session
        state.sessions.get_or_create = AsyncMock(wraps=manager.get_or_create)
        state.sessions.release = MagicMock(wraps=manager.release)
        state.sessions.provider_switch_replay_pending = manager.provider_switch_replay_pending
        state.sessions.mark_provider_switch_replay = MagicMock(
            wraps=manager.mark_provider_switch_replay
        )
        state.sessions.commit_provider_switch_replay_sid = manager.commit_provider_switch_replay_sid
        state.sessions.mark_needs_reinjection = manager.mark_needs_reinjection
        state.sessions.consume_needs_reinjection = manager.consume_needs_reinjection

        with (
            patch.object(
                chat_runner, "record_activity", wraps=chat_runner.record_activity
            ) as activity,
            patch.object(chat_runner, "_surface_agent_welcome", new_callable=AsyncMock) as welcome,
        ):
            await _drive(state, slot, "/todos")
            assert client.stream.call_count == 0
            assert session.first_turn is FirstTurnState.NOTHING_ARMED
            # A fresh refusal re-arms the replay lease (native session holds no
            # history); a resumed refusal keeps its transcript and re-arms only
            # the member re-injection. A replay lease forces a resumed=False
            # next turn, so the resumed case must NOT take it.
            assert session.provider_switch_replay is (not prefetched_resume)
            assert session.needs_context_reinjection is True
            assert not session.semaphore.locked()
            activity.assert_called_once()
            assert activity.call_args.args[:2] == ("oncall", session_key)
            welcome.assert_awaited_once()

            await _drive(state, slot, "ordinary first prompt")
            await _drive(state, slot, "ordinary second prompt")
            activity.assert_called_once()
            welcome.assert_awaited_once()

        # The fresh ordinary turn rebuilds as session start (is_new); the resumed
        # ordinary turn keeps its transcript (is_new False) and re-injects the
        # member section through needs_reinjection instead.
        assert builds == [(not prefetched_resume, "oncall"), (False, "oncall")]
        prompts = [call.args[0] for call in client.stream.call_args_list]
        assert prompts[0].count("[PERMANENT RULES]") == 1
        assert "[PERMANENT RULES]" not in prompts[1]
        assert session.provider_switch_replay is False
        assert session.needs_context_reinjection is False
        if prefetched_resume:
            state.sessions.mark_provider_switch_replay.assert_not_called()
        else:
            state.sessions.mark_provider_switch_replay.assert_called_once_with(session_key)
        assert not session.semaphore.locked()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "backend,acquired_backend,refused",
        [("", "", True), ("claude", "claude", False), ("claude", "", True)],
    )
    async def test_todos_expires_slack_options_only_after_capability_acceptance(
        self, tmp_path, backend, acquired_backend, refused
    ):
        from kiro_crew.dashboard.chat_utils import options_records, remember_slack_options
        from kiro_crew.slack.outbound import PostedOptions

        state, client = _runner_state(tmp_path)
        slot = _slot("todos-options")
        state._slots[slot.key] = slot
        state.slack_client = MagicMock(update_message=AsyncMock(return_value=True))
        session_key = chat_runner.effective_session_key(slot)
        posted = PostedOptions(channel="C1", ts="options-ts", choices=("A", "B"), blocks=())
        remember_slack_options(state, session_key, posted)
        client.capabilities = capabilities_for(acquired_backend)
        client.client.pop_pending_oauth_requests = MagicMock(return_value=[])
        _set_stream(client, [_complete()])
        client.stream_command = client.stream
        cfg = await asyncio.to_thread(chat_runner.KiroCrewConfig.load)
        cfg.agent.acp_backend = backend
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg),
            patch.object(
                chat_runner, "expire_slack_options", wraps=chat_runner.expire_slack_options
            ) as expire,
        ):
            await _drive(state, slot, "/todos")
        if refused:
            if backend:
                state.sessions.get_or_create.assert_awaited_once()
            else:
                state.sessions.get_or_create.assert_not_awaited()
            expire.assert_not_awaited()
            client.stream.assert_not_called()
            assert options_records(state, session_key) == (posted,)
            state.slack_client.update_message.assert_not_awaited()
        else:
            state.sessions.get_or_create.assert_awaited_once()
            expire.assert_awaited_once_with(state, session_key)
            client.stream.assert_called_once()
            assert options_records(state, session_key) == ()
            state.slack_client.update_message.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend,refused", [("", True), ("claude", False)])
    @pytest.mark.parametrize("failure_site", ["memory", "claim", "spec-hooks"])
    async def test_todos_deferred_options_cleanup_survives_early_failure(
        self, tmp_path, backend, refused, failure_site
    ):
        from kiro_crew.dashboard.chat_utils import options_records, remember_slack_options
        from kiro_crew.slack.outbound import PostedOptions

        state, client = _runner_state(tmp_path)
        slot = _slot("todos-options-failure")
        state._slots[slot.key] = slot
        state.slack_client = MagicMock(update_message=AsyncMock(return_value=True))
        session_key = chat_runner.effective_session_key(slot)
        posted = PostedOptions(channel="C1", ts="failure-options-ts", choices=("A", "B"), blocks=())
        remember_slack_options(state, session_key, posted)
        client.capabilities = capabilities_for(backend)
        cfg = await asyncio.to_thread(chat_runner.KiroCrewConfig.load)
        cfg.agent.acp_backend = backend
        error = RuntimeError("early todos failure")
        if failure_site == "memory":
            failure_target = "kiro_crew.memory_startup.wait_for_memory_preparation"
        elif failure_site == "claim":
            failure_target = state.sessions.get_or_create
            failure_target.side_effect = chat_runner.SessionBusyError("early todos failure")
        else:
            failure_target = "kiro_crew.dashboard.chat_runner._prepare_spec_hooks"

        with (
            patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg),
            patch.object(
                chat_runner, "expire_slack_options", wraps=chat_runner.expire_slack_options
            ) as expire,
        ):
            if failure_site == "claim":
                await _drive(state, slot, "/todos")
            else:
                with patch(failure_target, new=AsyncMock(side_effect=error)) as fail:
                    await _drive(state, slot, "/todos")
                    if refused:
                        fail.assert_not_awaited()
                    else:
                        fail.assert_awaited_once()
        client.stream.assert_not_called()
        if refused:
            state.sessions.get_or_create.assert_not_awaited()
            expire.assert_not_awaited()
            assert options_records(state, session_key) == (posted,)
            state.slack_client.update_message.assert_not_awaited()
        else:
            expire.assert_awaited_once_with(state, session_key)
            assert options_records(state, session_key) == ()
            state.slack_client.update_message.assert_awaited_once()
            assert any("early todos failure" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_todos_deferred_expiry_cancellation_releases_session(self, tmp_path):
        from kiro_crew.dashboard.chat_utils import remember_slack_options
        from kiro_crew.slack.outbound import PostedOptions

        state, client = _runner_state(tmp_path)
        slot = _slot("todos-options-cancel")
        state._slots[slot.key] = slot
        session_key = chat_runner.effective_session_key(slot)
        remember_slack_options(
            state,
            session_key,
            PostedOptions(channel="C1", ts="cancel-options-ts", choices=("A", "B"), blocks=()),
        )
        client.capabilities = capabilities_for("claude")
        cfg = await asyncio.to_thread(chat_runner.KiroCrewConfig.load)
        cfg.agent.acp_backend = "claude"
        preparing = asyncio.Event()
        expiring = asyncio.Event()
        unblock = asyncio.Event()

        async def wait_preparation(*args, **kwargs):
            preparing.set()
            # flake-ok: pre-existing unbounded wait in the blocking stub; the test cancels the turn, then sets it, moved verbatim; tracked for a de-flake pass
            await unblock.wait()

        async def wait_expiry(*args, **kwargs):
            expiring.set()
            # flake-ok: pre-existing unbounded wait in the blocking stub; the test cancels the turn, then sets it, moved verbatim; tracked for a de-flake pass
            await unblock.wait()

        state.slack_client = MagicMock(update_message=AsyncMock(side_effect=wait_expiry))
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg),
            patch.object(
                chat_runner, "_prepare_spec_hooks", new=AsyncMock(side_effect=wait_preparation)
            ),
            patch.object(
                chat_runner, "expire_slack_options", wraps=chat_runner.expire_slack_options
            ) as expire,
        ):
            task = asyncio.create_task(_drive(state, slot, "/todos"))
            try:
                await await_until(preparing)
                task.cancel()
                await await_until(expiring)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await await_until(task)
            finally:
                unblock.set()
                if not task.done():
                    task.cancel()
                await await_until(asyncio.gather(task, return_exceptions=True))

        expire.assert_awaited_once_with(state, session_key)
        state.slack_client.update_message.assert_awaited_once()
        state.sessions.release.assert_called_once_with(session_key)
        assert slot._active_turn_session_key == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("alive", [True, False, None, "absent"])
    async def test_todos_member_override_selects_claude_over_default_kiro(self, tmp_path, alive):
        """Live capability or member-aware successor wins over the global default."""
        from kiro_crew.config.loader import KiroCrewAgentConfig
        from kiro_crew.memory_stores import provision_member_memory

        state, client = _runner_state(tmp_path)
        slot = _slot("member-oncall")
        state._slots[slot.key] = slot
        slot.mode = "member"
        slot.agent = "oncall"
        client.capabilities = capabilities_for("claude")
        client.client.pop_pending_oauth_requests = MagicMock(return_value=[])
        _set_stream(client, [_complete()])
        client.stream_command = client.stream
        # A dead Kiro must not override the member's Claude successor either.
        hint = SimpleNamespace(capabilities=capabilities_for("claude" if alive is True else ""))
        state.sessions.get_provider = MagicMock(return_value=None if alive == "absent" else hint)
        state.sessions.is_provider_alive = AsyncMock(return_value=alive)
        cfg = await asyncio.to_thread(chat_runner.KiroCrewConfig.load)
        cfg.agent.acp_backend = ""
        cfg.agent.member_acp_backend = "claude"
        cfg.agents["oncall"] = KiroCrewAgentConfig()
        provision_member_memory(cfg, "oncall")
        cfg.save()
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg),
            patch.object(
                chat_runner, "select_provider_backend", wraps=chat_runner.select_provider_backend
            ) as select_backend,
        ):
            await _drive(state, slot, "/todos")
        # Behavior first: the global-default-only gate refused here, so these are
        # the assertions that fail against it, not the selector spy below.
        assert not any(
            "not available in the dashboard" in m.get("content", "") for m in slot.messages
        )
        state.sessions.get_or_create.assert_awaited_once()
        client.stream.assert_called_once()
        if alive is True:
            select_backend.assert_not_called()
        else:
            select_backend.assert_called_once_with(
                chat_runner.effective_session_key(slot), "claude", "", crew_backend=None
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("alive", [False, None, "absent"])
    async def test_todos_member_kiro_refuses_without_acquisition_over_default_claude(
        self, tmp_path, alive
    ):
        """The mirror case: a Claude global default cannot admit a Kiro member.

        An empty ``member_acp_backend`` routes the member DM to kiro, which has no
        native /todos. Read from the global default alone, the gate admitted the
        command and cold-started that kiro session only to refuse after the claim.
        A dead Claude row is no better a witness than the global default.
        """
        from kiro_crew.config.loader import KiroCrewAgentConfig
        from kiro_crew.memory_stores import provision_member_memory

        state, client = _runner_state(tmp_path)
        slot = _slot("member-oncall")
        state._slots[slot.key] = slot
        slot.mode = "member"
        slot.agent = "oncall"
        client.capabilities = capabilities_for("")
        _set_stream(client, [_complete()])
        hint = SimpleNamespace(capabilities=capabilities_for("claude"))
        state.sessions.get_provider = MagicMock(return_value=None if alive == "absent" else hint)
        state.sessions.is_provider_alive = AsyncMock(return_value=alive)
        cfg = await asyncio.to_thread(chat_runner.KiroCrewConfig.load)
        cfg.agent.acp_backend = "claude"
        cfg.agent.member_acp_backend = ""
        cfg.agents["oncall"] = KiroCrewAgentConfig()
        provision_member_memory(cfg, "oncall")
        cfg.save()
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg),
            patch.object(
                chat_runner, "expire_slack_options", wraps=chat_runner.expire_slack_options
            ) as expire_options,
        ):
            await _drive(state, slot, "/todos")
        assert any("not available in the dashboard" in m.get("content", "") for m in slot.messages)
        state.sessions.get_or_create.assert_not_awaited()
        client.stream.assert_not_called()
        client.stream_command.assert_not_called()
        expire_options.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_busy_kiro_todos_refuses_before_session_claim(self, tmp_path):
        """A held turn semaphore cannot delay an unsupported local command."""
        from kiro_crew.session import SessionManager, _Session

        state, client = _runner_state(tmp_path)
        slot = _slot("busy-todos")
        client.capabilities = capabilities_for("")
        client.is_process_alive = MagicMock(return_value=True)
        cfg = await asyncio.to_thread(chat_runner.KiroCrewConfig.load)
        cfg.agent.acp_backend = "claude"
        manager = SessionManager(cfg)
        session = _Session(provider=client)
        await session.semaphore.acquire()
        session_key = chat_runner.effective_session_key(slot)
        manager._sessions[session_key] = session
        state.sessions.get_provider = manager.get_provider
        state.sessions.is_provider_alive = manager.is_provider_alive
        state.sessions.get_or_create = AsyncMock(wraps=manager.get_or_create)
        try:
            with patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg):
                await await_until(asyncio.ensure_future(_drive(state, slot, "/todos")))
            state.sessions.get_or_create.assert_not_awaited()
            assert session.semaphore.locked()
            client.stream.assert_not_called()
            assert any(
                "not available in the dashboard" in m.get("content", "") for m in slot.messages
            )
        finally:
            session.semaphore.release()

    @pytest.mark.asyncio
    async def test_warm_todos_refusal_does_not_create_replay(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot("warm-todos")
        client.capabilities = capabilities_for("")
        state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
        state.sessions.mark_provider_switch_replay = MagicMock()

        await _drive(state, slot, "/todos")

        state.sessions.mark_provider_switch_replay.assert_not_called()
        client.stream.assert_not_called()
        assert any("not available in the dashboard" in m.get("content", "") for m in slot.messages)

    def test_kiro_only_members_are_still_forwarded_once_unblocked(self):
        """The gate drops these for the claude harness; forwarding must then happen.

        ``is_harness_slash_command`` reads the provider axis alone, so it only
        agrees with the widened gate while every kiro-only member is in
        ``_SLASH_COMMANDS`` and forwards on any provider.
        """
        from kiro_crew.dashboard.chat_utils import (
            _KIRO_ONLY_BLOCKED_SLASH_COMMANDS,
            _SLASH_COMMANDS,
            is_harness_slash_command,
        )

        assert _KIRO_ONLY_BLOCKED_SLASH_COMMANDS
        assert _KIRO_ONLY_BLOCKED_SLASH_COMMANDS <= _SLASH_COMMANDS
        for cmd in _KIRO_ONLY_BLOCKED_SLASH_COMMANDS:
            assert is_harness_slash_command(cmd, cc_provider=False) is True, cmd

    @pytest.mark.asyncio
    async def test_terminal_only_commands_stay_blocked_on_the_claude_harness(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        cfg = await asyncio.to_thread(chat_runner.KiroCrewConfig.load)
        cfg.agent.acp_backend = "claude"

        with patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg):
            await _drive(state, slot, "/quit")

        state.sessions.get_or_create.assert_not_awaited()
        assert any("not available in the dashboard" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_goal_command_is_handled_locally(self, tmp_path, monkeypatch):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        # No AutoNudge service: the local handler answers with its own notice.
        monkeypatch.setattr(chat_runner, "get_instance", lambda: None)

        await _drive(state, slot, "/goal ship the thing")

        state.sessions.get_or_create.assert_not_awaited()
        assert any("Goal loops are unavailable" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name,status,reported",
        [
            pytest.param(
                "secret",
                "blocked",
                "sensitive path",
                id="test_prompts_get_blocked_path_reports_a_sensitive_path",
            ),
            pytest.param(
                "big",
                "too_large",
                "exceeds size limit",
                id="test_prompts_get_too_large_path_reports_the_limit",
            ),
            pytest.param(
                "nope",
                "not_found",
                "not found",
                id="test_prompts_get_missing_prompt_reports_not_found",
            ),
        ],
    )
    async def test_prompts_get_refusal_is_reported_locally(self, tmp_path, name, status, reported):
        state, client = _runner_state(tmp_path)
        slot = _slot()

        with patch.object(
            chat_runner, "_resolve_prompt_mention", return_value=(f"@{name}", status, "")
        ) as resolve:
            await _drive(state, slot, f"/prompts get {name}")

        assert resolve.call_args.args[0] == f"@{name}"
        assert any(reported in m.get("content", "") for m in slot.messages)
        state.sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prompts_listing_with_none_available(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()

        with patch.object(chat_runner, "_list_aim_prompts", return_value=[]):
            await _drive(state, slot, "/prompts")

        assert any("No prompts found" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_prompts_listing_groups_by_source(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        prompts = [
            {"fullName": "a", "description": "first", "source": "aim"},
            {"fullName": "b", "description": "", "source": "team"},
        ]

        with patch.object(chat_runner, "_list_aim_prompts", return_value=prompts):
            await _drive(state, slot, "/prompts list")

        body = "\n".join(m.get("content", "") for m in slot.messages)
        assert "User Prompts (team)" in body
        assert "`@a` — first" in body

    @pytest.mark.asyncio
    async def test_prompts_listing_survives_a_walk_failure(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()

        with patch.object(chat_runner, "_list_aim_prompts", side_effect=OSError("bad root")):
            await _drive(state, slot, "/prompts")

        assert any("No prompts found" in m.get("content", "") for m in slot.messages)


# ── _run_chat: recovery ladders ───────────────────────────────────────────


class TestRunChatRecoveryLadders:
    @pytest.mark.asyncio
    async def test_stale_recover_requeues_a_continuation(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_complete(STOP_REASON_STALE_RECOVER)])

        with patch.object(chat_runner, "_start_next_queued_turn", new=AsyncMock(return_value=True)):
            await _drive(state, slot)

        assert slot._stale_recovery_retries == 1
        assert any("Recovering a stalled turn" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_stale_recover_budget_exhaustion_asks_for_a_new_chat(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._stale_recovery_retries = 3
        _set_stream(client, [_complete(STOP_REASON_STALE_RECOVER)])

        await _drive(state, slot)

        assert any("start a new chat" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_nested_stale_recover_surfaces_a_retry_notice(self, tmp_path):
        """depth>0 resets the session but must not re-queue — it still reports."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_complete(STOP_REASON_STALE_RECOVER)])

        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "hello", _prompt_depth=1)
        await _settle(slot)

        assert any("please retry" in err for err in _errors(slot))
        assert slot._queue == []

    @pytest.mark.asyncio
    async def test_compaction_failure_neither_retries_nor_claims_a_lost_link(self, tmp_path):
        """A PERMANENT compaction failure is terminal: the reason is in the
        "error:" family, so without its own branch it lands in pipe-death
        recovery — a re-queue plus a "Connection lost" card, both false. An
        overflowing conversation fails again identically, so it earns no retry."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(
            client,
            [
                LLMEvent(
                    kind="compaction_status",
                    text="failed",
                    title="context window exceeded",
                ),
                _complete(STOP_REASON_COMPACTION_FAILED),
            ],
        )

        await _drive(state, slot)

        errors = _errors(slot)
        assert not any("Connection lost" in err for err in errors), errors
        assert not any("Session stuck" in err for err in errors), errors
        assert slot._acp_pipe_death_retries == 0
        assert slot._queue == []
        # The failure is still explained — by the compaction notice the status
        # path appended, which is the only message this turn needs.
        assert any(
            "Compaction failed" in m.get("content", "")
            and "context window exceeded" in m.get("content", "")
            for m in slot.messages
        ), slot.messages

    @pytest.mark.asyncio
    async def test_compaction_failure_resets_the_abandoned_backend_session(self, tmp_path):
        """The completion is synthetic — the client stopped reading; the
        backend never sent end_turn — so without a reset the runtime still
        counts the turn as in progress and the NEXT prompt collides with
        "prompt already in progress". The finally must reset (tear down +
        session/load resume) WITHOUT re-queuing anything."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        state.sessions.reset = AsyncMock()
        _set_stream(client, [_complete(STOP_REASON_COMPACTION_FAILED)])

        await _drive(state, slot)

        state.sessions.reset.assert_awaited_once()
        assert slot._queue == []
        assert slot._acp_pipe_death_retries == 0

    @pytest.mark.asyncio
    async def test_a_throttled_compaction_requeues_the_abandoned_message(self, tmp_path):
        """A summarization call that was throttled has nothing wrong with it, so
        the turn it abandoned is worth running again. Dropping the message here
        ended the turn on a backend hiccup the next attempt would clear."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        client.last_compaction_transient = True
        _set_stream(client, [_complete(STOP_REASON_COMPACTION_FAILED)])

        slot._empty_response_retries = 2
        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "do the thing")
        # The requeue is proven by the follow-up turn the finally DISPATCHED
        # from it. Asserting on slot._queue cannot see it: that dispatch is the
        # drain, so the entry is already gone by the time the turn returns. Read
        # before settling: a settled follow-up's own tail releases slot.task.
        assert slot.task is not None
        await _settle(slot)

        assert slot._compaction_failed_retries == 1
        assert any("Compaction failed — retrying" in err for err in _errors(slot)), _errors(slot)
        # Still not pipe-death: that budget and its card stay untouched.
        assert slot._acp_pipe_death_retries == 0
        assert not any("Connection lost" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_a_stopped_turn_is_not_revived_by_the_throttle_requeue(self, tmp_path):
        """A requeue lands at queue index 0, so a message the user STOPPED during
        the turn would come back and run first. The stop completed, so _stopping
        and _stop_state have both snapped back to idle — only the monotonic
        generation counter still records that it happened."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        client.last_compaction_transient = True
        # The stop has to land DURING the turn: the generation is snapshotted at
        # turn start, and the branch reads it before the finally block runs.
        slot._stop_generation = 7

        def _stream(*_a, **_kw):
            slot._stop_generation = 8
            return _async_iter([_complete(STOP_REASON_COMPACTION_FAILED)])

        client.stream = MagicMock(side_effect=_stream)

        slot._empty_response_retries = 2
        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "do the thing")
        await _settle(slot)

        assert slot._compaction_failed_retries == 0
        assert not any("retrying" in err for err in _errors(slot)), _errors(slot)

    @pytest.mark.asyncio
    async def test_a_user_followup_outranks_the_throttle_requeue(self, tmp_path):
        """The user typed a correction while the turn was failing. Re-queuing the
        superseded message at index 0 would run it BEFORE their correction, so the
        turn stays abandoned and their message is what runs."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        client.last_compaction_transient = True
        _set_stream(client, [_complete(STOP_REASON_COMPACTION_FAILED)])
        slot.queue_insert(0, "actually, do this instead", kind="user", payload="")

        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "do the thing")
        await _settle(slot)

        assert slot._compaction_failed_retries == 0
        assert not any("retrying" in err for err in _errors(slot)), _errors(slot)

    @pytest.mark.asyncio
    async def test_the_throttle_requeue_is_bounded(self, tmp_path):
        """A throttle still firing after the budget is not clearing inside this
        turn, and every attempt pays for the summarization call again."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        client.last_compaction_transient = True
        slot._compaction_failed_retries = chat_runner._COMPACTION_FAILED_RETRIES
        _set_stream(client, [_complete(STOP_REASON_COMPACTION_FAILED)])

        await _drive(state, slot)

        # Unchanged: the budget was already spent, so this failure bought no
        # further attempt and added no retry notice.
        assert slot._compaction_failed_retries == chat_runner._COMPACTION_FAILED_RETRIES
        assert slot._queue == []
        assert not any("retrying" in err for err in _errors(slot)), _errors(slot)

    @pytest.mark.asyncio
    async def test_a_transient_failure_after_output_is_not_replayed(self, tmp_path):
        """Verbatim replay is only safe before anything landed. Once a tool call
        has been delivered, re-sending the message could repeat its side
        effect — so an emitted turn keeps the give-up behaviour even for a
        reason that would otherwise be retried."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        client.last_compaction_transient = True
        _set_stream(
            client,
            [
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer"),
                _complete(STOP_REASON_COMPACTION_FAILED),
            ],
        )

        await _drive(state, slot)

        assert slot._compaction_failed_retries == 0
        assert slot._queue == []
        assert not any("retrying" in err for err in _errors(slot)), _errors(slot)

    @pytest.mark.asyncio
    async def test_tool_stall_recovery_names_the_stalled_tool(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(
            client,
            [
                _complete(
                    STOP_REASON_TOOL_STALL,
                    title="Running: tail -f app.log",
                    tool_input="tail -f app.log",
                    text="idle_secs=900 stuck_input",
                )
            ],
        )

        with patch.object(chat_runner, "_start_next_queued_turn", new=AsyncMock(return_value=True)):
            await _drive(state, slot)

        assert slot._tool_stall_retries == 1
        assert any("Tool appeared stalled" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_tool_stall_budget_exhaustion_asks_for_a_new_chat(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._tool_stall_retries = 3
        _set_stream(client, [_complete(STOP_REASON_TOOL_STALL, text="idle_secs=60")])

        await _drive(state, slot)

        assert any("start a new chat" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_nested_tool_stall_surfaces_a_retry_notice(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_complete(STOP_REASON_TOOL_STALL, text="idle_secs=60")])

        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "hello", _prompt_depth=1)
        await _settle(slot)

        assert any("please retry" in err for err in _errors(slot))
        assert slot._queue == []

    @pytest.mark.asyncio
    async def test_pipe_death_requeues_and_reports_the_exit_code(self, tmp_path):
        state, client = _runner_state(tmp_path)
        client.exit_code = 137
        slot = _slot()
        _set_stream(client, [_complete("error: pipe closed")])

        with patch.object(chat_runner, "_start_next_queued_turn", new=AsyncMock(return_value=True)):
            await _drive(state, slot)

        assert slot._acp_pipe_death_retries == 1
        assert any("exit 137" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_pipe_death_budget_exhaustion_asks_for_a_new_chat(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._acp_pipe_death_retries = 3
        _set_stream(client, [_complete("error: pipe closed")])

        await _drive(state, slot)

        assert any("start a new chat" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_nested_pipe_death_surfaces_a_retry_notice(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_complete("error: pipe closed")])

        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "hello", _prompt_depth=1)
        await _settle(slot)

        assert any("please retry" in err for err in _errors(slot))
        assert slot._queue == []
