"""Delivering a subagent completion to its parent.

Covers the injection into a Slack session and its timeout retry,
``_inject_with_retry``'s error handling, persisting a completion injected into a
Slack session, and replies to parents born on another channel through that
channel's transport (governance, redaction, chunking and the degraded paths).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _make_orchestrator,
    _mock_context_builder,
    _mock_dashboard_state,
    _mock_sessions,
)

# ═══════════════════════════════════════════════════════════════════════════
# Tests: Subagent Slack injection timeout
# ═══════════════════════════════════════════════════════════════════════════


class TestSubagentSlackInjection:
    """Subagent injection into Slack sessions."""

    def _setup(self):
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()
        orch.slack.post_blocks = AsyncMock(return_value="ts")
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst.running = []
                mock_sm_inst.queued_count_for = MagicMock(return_value=0)
                mock_sm_inst.queued_count_for_async = AsyncMock(return_value=0)
                mock_sm_inst.has_pending_work_for = MagicMock(return_value=False)
                mock_sm_inst.has_pending_work_for_async = AsyncMock(return_value=False)
                mock_sm_inst.running_agents_for = MagicMock(return_value=[])
                mock_sm_inst.get = MagicMock(return_value=None)
                mock_sm_inst.notify_injection_failed = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        return orch, mock_sm

    @pytest.mark.asyncio
    async def test_slack_injection_timeout_retries(self):
        """Slack injection timeout → retries then fails."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]

        info = MagicMock()
        info.id = "agent-timeout"
        info.parent_session_key = "C123:ts.123"
        info.error = None
        info.result = "result"
        info.result_path = ""
        info.task = "task"
        info.agent = ""
        info.silent = False
        info.elapsed = 1.0
        info.started = 0.0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError,
        ) as mock_stream:
            await on_done(info)

        assert mock_stream.await_count == 2
        assert orch.sessions.reset.await_args_list == [
            ((info.parent_session_key,),),
            ((info.parent_session_key,),),
        ]
        orch.subagent_mgr.notify_injection_failed.assert_called_once()
        failed_info = orch.subagent_mgr.notify_injection_failed.call_args.args[0]
        reason = orch.subagent_mgr.notify_injection_failed.call_args.kwargs["reason"]
        assert failed_info is info
        assert "attempt 1 timed out" in reason
        assert "attempt 2 timed out" in reason
        orch.slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cron_injection_timeout(self):
        """Cron injection timeout → notifies failure."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        orch.subagent_mgr.running = []

        info = MagicMock()
        info.id = "agent-cron-timeout"
        info.parent_session_key = "cron:job1"
        info.error = None
        info.result = "result"
        info.result_path = ""
        info.task = "cron task"
        info.agent = ""
        info.silent = False
        info.elapsed = 1.0
        info.started = 0.0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError,
        ):
            await on_done(info)

        from kiro_crew.subagent_parts.run_limits import INJECTION_TIMEOUT

        orch.subagent_mgr.notify_injection_failed.assert_called_once_with(
            info, reason=f"injection timed out after {int(INJECTION_TIMEOUT)}s"
        )
        orch.sessions.reset.assert_any_await("cron:job1")


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Subagent _inject_with_retry paths
# ═══════════════════════════════════════════════════════════════════════════


class TestInjectWithRetry:
    """_inject_with_retry error handling."""

    def _setup(self):
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()
        orch.slack.post_blocks = AsyncMock(return_value="ts")
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst.running = []
                mock_sm_inst.queued_count_for = MagicMock(return_value=0)
                mock_sm_inst.queued_count_for_async = AsyncMock(return_value=0)
                mock_sm_inst.has_pending_work_for = MagicMock(return_value=False)
                mock_sm_inst.has_pending_work_for_async = AsyncMock(return_value=False)
                mock_sm_inst.running_agents_for = MagicMock(return_value=[])
                mock_sm_inst.get = MagicMock(return_value=None)
                mock_sm_inst.notify_injection_failed = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        return orch, mock_sm

    @pytest.mark.asyncio
    async def test_acp_process_died_during_injection(self):
        """AcpProcessDied during injection → resets session."""
        from kiro_crew.acp.client import AcpProcessDied

        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]

        info = MagicMock()
        info.id = "agent-died"
        info.parent_session_key = "C123:ts.1"
        info.error = None
        info.result = "result"
        info.result_path = ""
        info.task = "task"
        info.agent = ""
        info.silent = False
        info.elapsed = 1.0
        info.started = 0.0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=AcpProcessDied("dead"),
        ) as mock_stream:
            await on_done(info)

        mock_stream.assert_awaited_once()
        orch.sessions.reset.assert_awaited_once_with(info.parent_session_key)
        orch.subagent_mgr.notify_injection_failed.assert_called_once_with(
            info, reason="ACP process died"
        )
        orch.slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prompt_busy_exhausted(self):
        """PromptBusyExhaustedError → resets session."""
        from kiro_crew.llm_helpers import PromptBusyExhaustedError

        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]

        info = MagicMock()
        info.id = "agent-busy"
        info.parent_session_key = "C123:ts.2"
        info.error = None
        info.result = "result"
        info.result_path = ""
        info.task = "task"
        info.agent = ""
        info.silent = False
        info.elapsed = 1.0
        info.started = 0.0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=PromptBusyExhaustedError("exhausted"),
        ) as mock_stream:
            await on_done(info)

        mock_stream.assert_awaited_once()
        orch.sessions.reset.assert_awaited_once_with(info.parent_session_key)
        orch.subagent_mgr.notify_injection_failed.assert_called_once_with(
            info, reason="provider dead after prompt-busy retries"
        )
        orch.slack.post_message.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Slack subagent completion persistence
# ═══════════════════════════════════════════════════════════════════════════


class TestSlackSubagentCompletionPersistence:
    """Verify subagent completions injected into Slack sessions are persisted."""

    def _setup(self):
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()
        orch.slack.post_blocks = AsyncMock(return_value="ts")
        orch.conv_log = MagicMock()
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst.running = []
                mock_sm_inst.queued_count_for = MagicMock(return_value=0)
                mock_sm_inst.queued_count_for_async = AsyncMock(return_value=0)
                mock_sm_inst.has_pending_work_for = MagicMock(return_value=False)
                mock_sm_inst.has_pending_work_for_async = AsyncMock(return_value=False)
                mock_sm_inst.running_agents_for = MagicMock(return_value=[])
                mock_sm_inst.get = MagicMock(return_value=None)
                mock_sm_inst.notify_injection_failed = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        return orch, mock_sm

    def _make_info(self, parent_key="1234567890.123456"):
        info = MagicMock()
        info.id = "agent-persist"
        info.parent_session_key = parent_key
        info.error = None
        info.result = "synthesis result"
        info.result_path = ""
        info.task = "analyze code"
        info.agent = "kirocrew"
        info.silent = False
        info.elapsed = 5.0
        # flake-ok: pre-existing real-clock start stamp, moved verbatim; tracked for a de-flake pass
        info.started = time.time() - 5.0
        return info

    @pytest.mark.asyncio
    async def test_slack_subagent_persists_to_conversation_log(self):
        """Successful Slack injection persists both announce and response."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="synthesized response",
            ),
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=False),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=False),
        ):
            await on_done(info)

        assert orch.conv_log.append.call_count == 2
        user_call = orch.conv_log.append.call_args_list[0]
        assistant_call = orch.conv_log.append.call_args_list[1]
        assert user_call[0][0] == info.parent_session_key
        assert user_call[0][1] == "user"
        assert "[Subagent completion event]" in user_call[0][2]
        assert assistant_call[0][0] == info.parent_session_key
        assert assistant_call[0][1] == "assistant"
        assert assistant_call[0][2] == "synthesized response"

    @pytest.mark.asyncio
    async def test_slack_subagent_redacts_response_before_persist(self):
        """LLM response is redacted (credentials/exfil URLs) before persisting,
        since the dashboard replay is an external surface."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()
        # Response carries a credential-shaped token that must not reach disk raw.
        leaked = "result aws_secret_" "access_key=AKIAIOSFODNN7EXAMPLE done"

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value=leaked,
            ),
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=False),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=False),
        ):
            await on_done(info)

        assert orch.conv_log.append.call_count == 2
        persisted_response = orch.conv_log.append.call_args_list[1][0][2]
        # The raw secret value must not be persisted verbatim.
        assert "AKIAIOSFODNN7EXAMPLE" not in persisted_response

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "temporary, incognito",
        [
            # Temporary (restricted) threads.
            pytest.param(
                True, False, id="test_slack_subagent_skips_persistence_for_temporary_thread"
            ),
            pytest.param(
                False, True, id="test_slack_subagent_skips_persistence_for_incognito_thread"
            ),
        ],
    )
    async def test_slack_subagent_skips_persistence_for_restricted_thread(
        self, temporary, incognito
    ):
        """Temporary and incognito threads are delivered but never persisted."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="response",
            ) as mock_stream,
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=temporary),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=incognito),
        ):
            await on_done(info)

        mock_stream.assert_awaited_once()
        assert orch.slack.post_message.await_args.args[:2] == ("D_U1", "response")
        orch.conv_log.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_slack_subagent_persistence_failure_does_not_break_flow(self):
        """Persistence failure should not prevent Slack posting or break the flow."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()
        orch.conv_log.append = MagicMock(side_effect=OSError("disk full"))

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="response",
            ),
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=False),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=False),
        ):
            await on_done(info)

        orch.conv_log.append.assert_called_once()
        assert orch.slack.post_message.await_args.args[:2] == ("D_U1", "response")

    @pytest.mark.asyncio
    async def test_slack_subagent_no_persistence_without_conv_log(self):
        """When conv_log is None, persistence is skipped gracefully."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()
        orch.conv_log = None

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="response",
        ):
            await on_done(info)

        assert orch.slack.post_message.await_args.args[:2] == ("D_U1", "response")

    @pytest.mark.asyncio
    async def test_slack_subagent_persists_even_when_slack_post_fails(self):
        """Persistence is gated on ACP injection, not Slack delivery: a failed
        Slack post must NOT prevent the completion turn from being persisted."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()
        # Slack delivery fails (best-effort), but injection already succeeded.
        orch.slack.post_message = AsyncMock(side_effect=RuntimeError("slack down"))

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="synthesized response",
            ),
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=False),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=False),
        ):
            await on_done(info)

        orch.slack.post_message.assert_awaited()
        assert orch.conv_log.append.call_count == 2
        assert orch.conv_log.append.call_args_list[0][0][1] == "user"
        assert orch.conv_log.append.call_args_list[1][0][1] == "assistant"

    @pytest.mark.asyncio
    async def test_slack_subagent_persists_exactly_once_after_timeout_retry(self):
        """Unit guard: persistence fires once across a timeout-retry cycle."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                side_effect=[asyncio.TimeoutError(), "response text"],
            ),
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=False),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=False),
            # flake-ok: pre-existing worker-wide sleep patch inside the test, moved verbatim; tracked for a de-flake pass
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await on_done(info)

        # Exactly ONE completion persisted (2 appends: user + assistant), not 4.
        assert orch.conv_log.append.call_count == 2

    @pytest.mark.asyncio
    async def test_taskrunner_parent_injects_without_slack_post(self):
        """A machine parent keeps the synthesized turn but is not a Slack thread."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("taskrunner:TASK_example:review")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="synthesized response",
            ),
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=False),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=False),
        ):
            await on_done(info)

        assert orch.conv_log.append.call_count == 2
        orch.slack.open_dm.assert_not_awaited()
        orch.slack.post_message.assert_not_awaited()
        orch.slack.post_blocks.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: subagent completion delivery to non-Slack channel parents
# ═══════════════════════════════════════════════════════════════════════════


class TestSubagentChannelTransportDelivery:
    """Completion replies for channel-born parents reach the channel transport.

    A parent session started on Telegram/Discord has no dashboard tab and no
    Slack conversation, so its synthesized reply must go through the governed
    cross-surface transport ladder; a missing transport degrades to the
    dashboard notification without raising.
    """

    def _setup(self, *, parent_channel=None, transport=None):
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        # MagicMock attribute lookups return truthy mocks; the link ladder
        # treats those as real links, so pin the optional sources to None.
        orch.sessions.get_origin_link = MagicMock(return_value=None)
        orch.sessions.get_mirror_link = MagicMock(return_value=None)
        orch.sessions.get_channel = MagicMock(return_value=parent_channel)
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.dashboard_state = _mock_dashboard_state()
        orch.dashboard_state.get_channel_transport = MagicMock(return_value=transport)
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()
        orch.slack.post_blocks = AsyncMock(return_value="ts")
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst.running = []
                mock_sm_inst.queued_count_for = MagicMock(return_value=0)
                mock_sm_inst.queued_count_for_async = AsyncMock(return_value=0)
                mock_sm_inst.has_pending_work_for = MagicMock(return_value=False)
                mock_sm_inst.has_pending_work_for_async = AsyncMock(return_value=False)
                mock_sm_inst.running_agents_for = MagicMock(return_value=[])
                mock_sm_inst.get = MagicMock(return_value=None)
                mock_sm_inst.notify_injection_failed = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        return orch, mock_sm

    @staticmethod
    def _fake_transport(channel_type="telegram", proactive=True, max_chars=4096):
        async def _identity_target(target_id):
            # Mirror the Telegram transport: "user:<id>" -> (<id>, None).
            kind, _, value = target_id.partition(":")
            return (value, None) if kind == "user" and value else None

        return SimpleNamespace(
            channel_type=channel_type,
            capabilities=SimpleNamespace(
                supports_proactive_send=proactive,
                max_message_chars=max_chars,
                # 0 = not byte-capped; the byte path is Webex's and is covered
                # by test_messaging_split.py.
                max_message_bytes=0,
            ),
            send_message=AsyncMock(return_value="mid-1"),
            resolve_configured_target=AsyncMock(side_effect=_identity_target),
            # Part of the MessagingTransport contract the send ladder consults: a
            # proactive send re-checks that the link's recipient is still on the
            # roster. Permissive here so these tests keep exercising delivery;
            # test_channel_transport_outbound_authz owns the refusal path.
            may_send_to=lambda conversation_id, thread_id=None, principal="": True,
        )

    def _make_info(self, parent_key):
        info = MagicMock()
        info.id = "agent-channel"
        info.parent_session_key = parent_key
        info.error = None
        info.result = "channel result"
        info.result_path = ""
        info.task = "analyze code"
        info.agent = "kirocrew"
        info.silent = False
        info.elapsed = 5.0
        # flake-ok: pre-existing real-clock start stamp, moved verbatim; tracked for a de-flake pass
        info.started = time.time() - 5.0
        return info

    @staticmethod
    def _permit_governance():
        return patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=SimpleNamespace(permitted=True),
        )

    @pytest.mark.asyncio
    async def test_telegram_parent_reply_reaches_registered_transport(self):
        """A telegram:-born parent's synthesized reply is sent via the transport,
        addressed to the session's own conversation id, never through Slack."""
        transport = self._fake_transport("telegram")
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        # set_channel() persists an empty thread sentinel for non-Slack sessions;
        # it must not turn this Telegram route into an implicit Slack mirror.
        orch.sessions.get_thread = MagicMock(return_value="")
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:direct:12345")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="synthesized reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_awaited_once_with(
            "12345", "synthesized reply", thread_id=None
        )
        orch.slack.post_message.assert_not_awaited()
        orch.slack.open_dm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_origin_link_wins_over_stored_channel(self):
        """A recorded origin link (the conversation's real send target) takes
        precedence over the stored channel value."""
        transport = self._fake_transport("discord")
        orch, mock_sm = self._setup(parent_channel="discord:U999", transport=transport)
        from kiro_crew.messaging.link import ChannelLink

        orch.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="C777")
        )
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("discord:kirocrew:direct:U999")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_awaited_once_with("C777", "reply", thread_id=None)

    @pytest.mark.asyncio
    async def test_slack_parent_still_posts_through_slack_client(self):
        """Regression: a Slack-born parent keeps the dedicated Slack posting."""
        orch, mock_sm = self._setup(parent_channel="C123")
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("slack:1234567890.123456")

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="slack reply",
        ):
            await on_done(info)

        orch.slack.post_message.assert_awaited()
        posted_channel = orch.slack.post_message.await_args_list[0][0][0]
        assert posted_channel == "C123"
        orch.dashboard_state.get_channel_transport.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_transport_degrades_to_notification_only(self):
        """No registered transport: no crash, no Slack misdelivery, and the
        dashboard notification still fires."""
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=None)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:direct:12345")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="synthesized reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        orch.dashboard_state.get_channel_transport.assert_called()
        orch.slack.post_message.assert_not_awaited()
        orch.slack.open_dm.assert_not_awaited()
        orch.dashboard_state.notify.assert_called_once()
        assert orch.dashboard_state.notify.call_args.args[0] == "subagent"

    @pytest.mark.asyncio
    async def test_transport_send_failure_never_raises(self):
        """A transport that fails to send must not break completion handling."""
        transport = self._fake_transport("telegram")
        transport.send_message = AsyncMock(side_effect=RuntimeError("network down"))
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:direct:12345")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="synthesized reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_awaited_once()
        orch.dashboard_state.notify.assert_called_once()
        assert orch.dashboard_state.notify.call_args.args[0] == "subagent"

    @pytest.mark.asyncio
    async def test_target_snapshotted_before_injection_survives_session_reset(self):
        """A timeout-path sessions.reset() evicts the in-memory origin link;
        the delivery target is snapshotted before injection, so a retry that
        then succeeds still delivers to the original conversation."""
        transport = self._fake_transport("discord")
        orch, mock_sm = self._setup(parent_channel=None, transport=transport)
        from kiro_crew.messaging.link import ChannelLink

        # Origin link present at entry, gone after the first (timed-out)
        # injection attempt — exactly what reset() does to a live session.
        orch.sessions.get_origin_link = MagicMock(
            side_effect=[ChannelLink("discord", channel_id="C777", thread_id="T1")] + [None] * 8
        )
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("discord:kirocrew:direct:U999")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                side_effect=[asyncio.TimeoutError, "reply after retry"],
            ),
            # flake-ok: pre-existing worker-wide sleep patch inside the test, moved verbatim; tracked for a de-flake pass
            patch("asyncio.sleep", new_callable=AsyncMock),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_awaited_once_with("C777", "reply after retry", thread_id="T1")

    @pytest.mark.asyncio
    async def test_peer_resolution_outcome_is_sel_audited(self):
        """The configured-target allow-list decision lands in the SEL trail
        (allowed and denied alike), matching the chat_mirror precedent."""
        for resolved_target, expected in ((("12345", None), "allowed"), (None, "denied")):
            transport = self._fake_transport("telegram")
            transport.resolve_configured_target = AsyncMock(return_value=resolved_target)
            orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
            on_done = mock_sm.call_args[1]["on_done"]
            info = self._make_info("telegram:kirocrew:direct:12345")

            with (
                patch(
                    "kiro_crew.slack.gateway.stream_and_collect",
                    new_callable=AsyncMock,
                    return_value="reply",
                ),
                self._permit_governance(),
                patch("kiro_crew.slack.gateway.sel") as mock_sel,
            ):
                mock_sel.return_value.log_api_access = MagicMock()
                await on_done(info)

            audit_calls = [
                c
                for c in mock_sel.return_value.log_api_access.call_args_list
                if c.kwargs.get("operation") == "subagent.reply_target_resolve"
            ]
            assert len(audit_calls) == 1
            assert audit_calls[0].kwargs["outcome"] == expected

    @pytest.mark.asyncio
    async def test_reply_is_redacted_before_send(self):
        """Fresh LLM output is redacted at the channel egress."""
        transport = self._fake_transport("telegram")
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:direct:12345")
        leaked = "result aws_secret_" "access_key=AKIAIOSFODNN7EXAMPLE done"

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value=leaked,
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_awaited_once()
        sent_text = transport.send_message.await_args[0][1]
        assert "AKIAIOSFODNN7EXAMPLE" not in sent_text

    @pytest.mark.asyncio
    async def test_forum_parent_without_links_degrades_to_notification(self):
        """A forum-born parent's stored channel value carries the SENDER's user
        id; without an origin/mirror link the reply must NOT be sent (a send
        would leak group conversation content into a private DM)."""
        transport = self._fake_transport("telegram")
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:forum:987:5")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="synthesized reply",
            ) as mock_stream,
            self._permit_governance(),
        ):
            await on_done(info)

        mock_stream.assert_awaited_once()
        transport.send_message.assert_not_awaited()
        orch.slack.post_message.assert_not_awaited()
        orch.dashboard_state.notify.assert_called_once()
        assert orch.dashboard_state.notify.call_args.args[0] == "subagent"

    @pytest.mark.asyncio
    async def test_mirror_link_delivers_into_forum_topic(self):
        """A Telegram /link mirror binding wins over the stored value and
        carries the forum Topic thread id."""
        transport = self._fake_transport("telegram")
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        import dataclasses

        from kiro_crew.messaging.link import ChannelLink
        from kiro_crew.mirror_admission import sign_mirror_admission

        # Signed as the in-channel /link stores it: a key-named row must be.
        topic = ChannelLink("telegram", channel_id="987", thread_id="5")
        topic = dataclasses.replace(
            topic, admission=sign_mirror_admission("telegram:kirocrew:forum:987:5", topic)
        )
        orch.sessions.get_mirror_link = MagicMock(return_value=topic)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:forum:987:5")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_awaited_once_with("987", "reply", thread_id="5")
        # A link recorded by the transport is already a postable conversation.
        transport.resolve_configured_target.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unified_parent_uses_stored_channel_value(self):
        """A unified: DM bucket (direct-only by construction) resolves the
        stored channel value even though its namespace differs from the key's."""
        transport = self._fake_transport("telegram")
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("unified:kirocrew")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_awaited_once_with("12345", "reply", thread_id=None)

    @pytest.mark.asyncio
    async def test_stored_peer_id_is_resolved_to_a_postable_conversation(self):
        """The stored value is the peer's USER id; the transport resolves the
        postable conversation (e.g. Discord DM-channel creation, Teams'
        learned conversation) via resolve_configured_target."""
        transport = self._fake_transport("discord")
        transport.resolve_configured_target = AsyncMock(return_value=("DM123", None))
        orch, mock_sm = self._setup(parent_channel="discord:U999", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("discord:kirocrew:direct:U999")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.resolve_configured_target.assert_awaited_once_with("user:U999")
        transport.send_message.assert_awaited_once_with("DM123", "reply", thread_id=None)

    @pytest.mark.asyncio
    async def test_unreachable_peer_fails_closed(self):
        """A peer the transport cannot reach (e.g. Teams with no learned
        conversation/serviceUrl) degrades to notification-only, no send."""
        transport = self._fake_transport("teams")
        transport.resolve_configured_target = AsyncMock(return_value=None)
        orch, mock_sm = self._setup(parent_channel="teams:user@example.com", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("teams:kirocrew:direct:user@example.com")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.resolve_configured_target.assert_awaited_once_with("user:user@example.com")
        transport.send_message.assert_not_awaited()
        orch.dashboard_state.notify.assert_called_once()
        assert orch.dashboard_state.notify.call_args.args[0] == "subagent"

    @pytest.mark.asyncio
    async def test_governance_denial_blocks_the_send(self):
        """A non-permitting governance decision must block the egress."""
        transport = self._fake_transport("telegram")
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:direct:12345")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="reply",
            ),
            patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit",
                return_value=SimpleNamespace(permitted=False),
            ) as mock_vet,
        ):
            await on_done(info)

        mock_vet.assert_called()
        transport.send_message.assert_not_awaited()
        orch.slack.post_message.assert_not_awaited()
        orch.dashboard_state.notify.assert_called_once()
        assert orch.dashboard_state.notify.call_args.args[0] == "subagent"

    @pytest.mark.asyncio
    async def test_long_reply_is_chunked_to_the_transport_limit(self):
        """A reply longer than max_message_chars arrives as multiple sends."""
        transport = self._fake_transport("telegram", max_chars=20)
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:direct:12345")
        long_reply = "\n".join(f"line {i} of the reply" for i in range(6))

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value=long_reply,
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        assert transport.send_message.await_count > 1
        reassembled = "".join(c.args[1] for c in transport.send_message.await_args_list)
        assert "line 5 of the reply" in reassembled
