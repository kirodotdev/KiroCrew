"""GatewayOrchestrator cron wiring.

Covers ``_init_cron`` and the cron callback (single agents, persistent sessions,
turn identity, agent sequences, redaction, dedup and silent jobs), the volatile
patterns the dedup hash strips, the failure paths, the success reminder, the ACP
retry, acked items, a failed Slack delivery, and ``_deliver_cron_response``.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _make_orchestrator,
    _mock_context_builder,
    _mock_dashboard_state,
    _mock_sessions,
)

from kiro_crew.slack.gateway import (
    _CRON_MSG_LIMIT,
    _EPOCH_RE,
    _EPOCH_WINDOW_SECS,
    _FAILURE_REMINDER_SECS,
    _MAX_INJECT_ATTEMPTS,
    _SUCCESS_REMINDER_SECS,
    _VOLATILE_RE,
    _result_hash,
)

# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_cron
# ═══════════════════════════════════════════════════════════════════════════


class TestInitCron:
    """Cron service initialization and callback."""

    @pytest.mark.asyncio
    async def test_init_cron_no_crons_flag(self):
        orch = _make_orchestrator(no_crons=True)
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()
        assert orch.cron_svc is not None
        mock_cs_inst.start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_init_cron_starts_when_enabled(self):
        orch = _make_orchestrator(no_crons=False)
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()
        assert orch.cron_svc is mock_cs_inst
        mock_cs_inst.start.assert_awaited_once()
        mock_cs_inst.start_reaper.assert_called_once()

    @pytest.mark.asyncio
    async def test_cron_callback_single_agent(self, tmp_path, monkeypatch):
        """Cron callback admits policy before the first model allocation."""
        from kiro_crew.subagent_persistence import read_session_memory_mode

        monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path / "subagents")
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        acquire = orch.sessions.get_or_create

        async def checked_acquire(key, **kwargs):
            assert read_session_memory_mode(key) == "persistent"
            assert orch.ctx_builder._session_memory_modes[key] == "persistent"
            return await acquire(key, **kwargs)

        orch.sessions.get_or_create = AsyncMock(side_effect=checked_acquire)
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder._session_memory_modes = {}
        orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        # Extract the callback
        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "j1"
        job.name = "test-job"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="cron result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:j1", "run task"),
            ):
                result = await callback(job)

        assert result == "cron result"
        job.set_run_result.assert_called_once_with("cron result")

    @pytest.mark.asyncio
    async def test_cron_callback_reuses_persistent_session_context(self):
        """A persistent cron job on an existing session must not re-inject
        session-start context.

        ``build_message`` receives the ``is_new``/``resumed`` freshness that
        acquisition returns, so a reused persistent session keeps its
        context instead of re-injecting the full session-start block.
        """
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        # The persistent session already exists: reused, not new.
        orch.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, True))
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.script = ""
        job.command = ""
        job.id = "j1"
        job.name = "test-job"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0
        job.member_id = ""
        job.memory_store = ""

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="cron result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:j1", "run task"),
            ):
                await callback(job)

        positional, keywords = orch.ctx_builder.build_message.call_args
        assert positional[1] is False
        assert keywords.get("resumed") is True

    @pytest.mark.asyncio
    async def test_cron_callback_publishes_turn_identity(self):
        """Regression: the cron turn must publish session_pid_<pid>.txt.

        The cron path was the one turn-running surface that never called
        publish_turn_identity. Under session sharing the runtime env carries
        no KIROCREW_SESSION_KEY and macOS sets no KIROCREW_HOST_PID, so the
        ancestor PID-walk over session_pid files is the ONLY parent-identity
        source for spawn_run — without the publish, sub-agents spawned from a
        cron turn resolved an empty parent ('notification only (parent=)')
        unless an unrelated surface happened to be mid-turn."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "j1"
        job.name = "test-job"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        publish_events: list[str] = []

        async def _publish(sessions, key):
            publish_events.append(f"publish:{key}")

        async def _stream(*a, **k):
            # Identity must already be published when the model turn starts —
            # spawn_run can fire at any point inside it.
            publish_events.append("stream")
            return "cron result"

        with patch("kiro_crew.slack.gateway.publish_turn_identity", side_effect=_publish):
            with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=_stream):
                with patch(
                    "kiro_crew.slack.gateway.build_cron_session_context",
                    return_value=("cron:j1", "run task"),
                ):
                    await callback(job)

        # Ordering is the contract: the publish must precede the model turn.
        assert publish_events == ["publish:cron:j1", "stream"]

    @pytest.mark.asyncio
    async def test_cron_callback_publishes_identity_per_sequence_agent(self):
        """Each agent in an agent_sequence runs on its own per-agent session
        key (cron:<job>:<agent>) — identity must be re-published for each."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "j1"
        job.name = "test-job"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = ["planner", "worker"]
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        publish = AsyncMock()
        with patch("kiro_crew.slack.gateway.publish_turn_identity", publish):
            with patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="cron result",
            ):
                with patch(
                    "kiro_crew.slack.gateway.build_cron_session_context",
                    return_value=("cron:j1", "run task"),
                ):
                    await callback(job)

        assert publish.await_count == 2
        published_keys = [c.args[1] for c in publish.await_args_list]
        assert published_keys == ["cron:j1:planner", "cron:j1:worker"]

    @pytest.mark.asyncio
    async def test_sequence_agent_reset_deferred_while_subagents_pending(self):
        """The sequential finally must mirror the single-agent deferral.

        Now that the sequence path publishes turn identity, a non-final
        agent's spawn_run resolves a REAL parent key. An unconditional reset
        at the end of that agent's turn would tear down the session a pending
        sub-agent completion is about to inject into (cold-starting a
        context-free replacement) and strip the reaper registration for the
        NEXT agent's still-in-flight turn."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.sessions.reset = AsyncMock()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        # A sub-agent of the FIRST sequence agent is still running when its
        # turn ends; the second agent has no pending sub-agents.
        pending = MagicMock()
        pending.parent_session_key = "cron:j1:planner"
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = [pending]
        # Real predicate semantics: pending work == a running agent with this
        # parent (no queued spawns in this scenario).
        orch.subagent_mgr.queued_count_for = MagicMock(return_value=0)
        orch.subagent_mgr.queued_count_for_async = AsyncMock(return_value=0)
        orch.subagent_mgr.has_pending_work_for_async = AsyncMock(
            side_effect=lambda key: any(
                a.parent_session_key == key for a in orch.subagent_mgr.running
            )
        )

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "j1"
        job.name = "test-job"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = ["planner", "worker"]
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch("kiro_crew.slack.gateway.publish_turn_identity", new_callable=AsyncMock):
            with patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="cron result",
            ):
                with patch(
                    "kiro_crew.slack.gateway.build_cron_session_context",
                    return_value=("cron:j1", "run task"),
                ):
                    await callback(job)

        # planner's reset deferred (sub-agent pending); worker's reset ran.
        reset_keys = [c.args[0] for c in orch.sessions.reset.await_args_list]
        assert "cron:j1:planner" not in reset_keys
        assert "cron:j1:worker" in reset_keys
        # The reaper registration is cleared only by the agent that actually
        # reset — one clear, not two.
        assert mock_cs_inst.clear_active_session_key.call_count == 1

    @pytest.mark.asyncio
    async def test_sequence_agent_reset_deferred_while_subagents_queued(self):
        """A spawn accepted behind the concurrency/stagger gate is in the
        manager's QUEUE, not `running` — the deferral must see it anyway.
        A `running`-only guard reads "no pending work" during exactly the
        window a wave is ramping, and the reset strands the queued agent's
        completion on a cold-started, context-free replacement session."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.sessions.reset = AsyncMock()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        # Nothing RUNNING for planner — but one spawn is QUEUED for it.
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.subagent_mgr.queued_count_for_async = AsyncMock(
            side_effect=lambda key: 1 if key == "cron:j1:planner" else 0
        )
        orch.subagent_mgr.has_pending_work_for_async = AsyncMock(
            side_effect=lambda key: key == "cron:j1:planner"
        )

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "j1"
        job.name = "test-job"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = ["planner", "worker"]
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch("kiro_crew.slack.gateway.publish_turn_identity", new_callable=AsyncMock):
            with patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="cron result",
            ):
                with patch(
                    "kiro_crew.slack.gateway.build_cron_session_context",
                    return_value=("cron:j1", "run task"),
                ):
                    await callback(job)

        reset_keys = [c.args[0] for c in orch.sessions.reset.await_args_list]
        assert "cron:j1:planner" not in reset_keys
        assert "cron:j1:worker" in reset_keys

    @pytest.mark.asyncio
    async def test_cron_name_is_redacted_before_delivery(self):
        """A cron NAME is LLM-authored text on its way to Slack.

        The name is interpolated into the ``⏰ *Cron: …*`` header, which stays
        outside the render pipeline on purpose (it is already Slack mrkdwn, so
        converting it would re-interpret its ``*bold*``). Skipping conversion also
        means skipping the pipeline's redaction, so the name needs its own pass --
        an agent can create a cron via the ``cron_add`` tool, so a credential can
        land in that field and ride into the channel header.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "j1"
        job.name = f"nightly {secret} sweep"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="cron result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:j1", "run task"),
            ):
                await callback(job)

        orch.slack.post_blocks.assert_awaited()
        delivered = json.dumps(orch.slack.post_blocks.call_args.args)
        assert secret not in delivered, "a credential in the cron name reached Slack"
        assert secret[:8] not in delivered, "a credential fragment reached Slack"

    @pytest.mark.asyncio
    async def test_cron_callback_dedup_suppresses(self):
        """Duplicate result suppresses Slack delivery."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "j2"
        job.name = "dedup-job"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = _result_hash("stable output")
        job.consecutive_dupes = 1
        # flake-ok: pre-existing real-clock stamp inside the dedup window, moved verbatim; tracked for a de-flake pass
        job.last_posted_at = time.time()
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="stable output",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:j2", "run"),
            ):
                with patch("kiro_crew.sel.sel") as mock_sel:
                    mock_sel.return_value.log_tool_invocation = MagicMock()
                    result = await callback(job)

        assert result == "stable output"
        assert job.consecutive_dupes == 2
        # Slack post_blocks should NOT have been called (suppressed)
        orch.slack.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cron_callback_silent_suppresses(self):
        """Silent job suppresses delivery."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = None
        orch.slack = None

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "j3"
        job.name = "silent-job"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = ""
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = True
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="silent result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:j3", "run"),
            ):
                with patch("kiro_crew.sel.sel") as mock_sel:
                    mock_sel.return_value.log_tool_invocation = MagicMock()
                    result = await callback(job)

        assert result == "silent result"


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Volatile regex patterns
# ═══════════════════════════════════════════════════════════════════════════


class TestVolatilePatterns:
    """Regex constants used in dedup."""

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("2026-05-14T10:30:00Z", id="test_volatile_re_matches_iso_timestamp"),
            pytest.param(
                "550e8400-e29b-41d4-a716-446655440000", id="test_volatile_re_matches_uuid"
            ),
        ],
    )
    def test_volatile_re_matches(self, text: str):
        assert _VOLATILE_RE.search(text)

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("1715700000", id="test_epoch_re_matches_10_digit"),
            pytest.param("1715700000000", id="test_epoch_re_matches_13_digit"),
        ],
    )
    def test_epoch_re_matches(self, text: str):
        assert _EPOCH_RE.search(text)

    def test_constants_values(self):
        assert _MAX_INJECT_ATTEMPTS == 2
        assert _CRON_MSG_LIMIT == 3000
        assert _SUCCESS_REMINDER_SECS == 86400
        assert _FAILURE_REMINDER_SECS == 3600
        assert _EPOCH_WINDOW_SECS == 300


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Cron failure paths
# ═══════════════════════════════════════════════════════════════════════════


class TestCronFailurePaths:
    """Cron callback error handling."""

    @pytest.mark.asyncio
    async def test_cron_callback_failure_alerts(self):
        """First failure sends alert."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "jfail"
        job.name = "fail-job"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0
        job.auto_paused = False
        job._acp_retried = False

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jfail", "run"),
            ):
                with patch("kiro_crew.sel.sel") as mock_sel:
                    mock_sel.return_value.log_tool_invocation = MagicMock()
                    with pytest.raises(RuntimeError, match="boom"):
                        await callback(job)

        orch.slack.post_message.assert_awaited()
        # Failure accounting is single-owned by CronJob.record_failure() so the
        # auto-pause threshold stays reachable from the delivery path.
        job.record_failure.assert_called_once()

    @pytest.mark.asyncio
    async def test_cron_callback_failure_dedup_suppresses(self):
        """Duplicate failure within window suppresses Slack."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "jfail2"
        job.name = "fail-dedup"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        # Pre-set failure hash to match what will be generated
        job.last_failure_hash = _result_hash("RuntimeError: boom")
        # flake-ok: pre-existing real-clock stamp inside the dedup window, moved verbatim; tracked for a de-flake pass
        job.last_failure_at = time.time()
        job.consecutive_failures = 1
        job.auto_paused = False
        job._acp_retried = False

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jfail2", "run"),
            ):
                with patch("kiro_crew.sel.sel") as mock_sel:
                    mock_sel.return_value.log_tool_invocation = MagicMock()
                    with pytest.raises(RuntimeError, match="boom"):
                        await callback(job)

        # Slack should NOT be called (suppressed)
        orch.slack.post_message.assert_not_awaited()
        # A suppressed duplicate still counts toward auto-pause via the
        # counter's single owner.
        job.record_failure.assert_called_once()

    @pytest.mark.asyncio
    async def test_cron_multi_agent_sequence(self):
        """Multi-agent sequence runs agents sequentially."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = None
        orch.slack = None

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "jmulti"
        job.name = "multi-agent"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = ["agent-a", "agent-b"]
        job.agent_id = None
        job.channel = ""
        job.created_by = ""
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="agent result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jmulti", "run"),
            ):
                result = await callback(job)

        assert result == "agent result"
        job.set_run_result.assert_called_once_with("agent result")
        # get_or_create called twice (once per agent)
        assert orch.sessions.get_or_create.await_count == 2

    @pytest.mark.asyncio
    async def test_a_rescoped_sequence_ends_each_keys_conversation_once(self, tmp_path):
        """A repeated alias claims the same key twice; the rescope ends that
        key's old conversation on the FIRST claim only, so the second does not
        cancel the work the first step left running in the new project."""
        repo = tmp_path / "repo"
        repo.mkdir()
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.sessions.discard_conversation = AsyncMock(return_value=True)
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = None
        orch.slack = None

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs_inst.update_job_async = AsyncMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "jrepeat"
        job.name = "repeat-agent"
        job.persistent_session = True
        job.project_dir = str(repo)
        job.project_dir_was = str(tmp_path / "old")
        job.agent_sequence = ["worker", "worker"]
        job.agent_id = None
        job.channel = ""
        job.created_by = ""
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="agent result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jrepeat", "run"),
            ):
                result = await callback(job)

        assert result == "agent result"
        assert orch.sessions.get_or_create.await_count == 2
        ended = [c.args[0] for c in orch.sessions.discard_conversation.await_args_list]
        assert ended.count("cron:jrepeat:worker") == 1, ended

    @staticmethod
    def _rescoped_sequence_job(tmp_path, aliases):
        repo = tmp_path / "repo"
        repo.mkdir(exist_ok=True)
        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "jseq"
        job.name = "seq-rescoped"
        job.persistent_session = True
        job.project_dir = str(repo)
        job.project_dir_was = str(tmp_path / "old")
        job.agent_sequence = list(aliases)
        job.agent_id = None
        job.channel = ""
        job.created_by = ""
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0
        return job

    async def _run_rescoped_sequence(self, job, *, settle, order):
        """Drive a rescoped sequence through the real callback; ``order``
        receives the trace of ends, the marker clear and streamed steps."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()

        async def _discard(key, **_kw):
            order.append(f"end:{key}")
            return True

        orch.sessions.discard_conversation = _discard
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = None
        orch.slack = None

        async def _update(job_id, **kwargs):
            order.append("clear-marker")
            return settle()

        async def _stream(*_a, **_kw):
            order.append("stream")
            return "agent result"

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs_inst.update_job_async = _update
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()
        callback = mock_cs.create.call_args[1]["on_job"]

        with patch("kiro_crew.slack.gateway.stream_and_collect", _stream):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jseq", "run"),
            ):
                result = await callback(job)
        return result, orch

    @pytest.mark.asyncio
    async def test_a_rescoped_sequence_ends_every_key_and_clears_the_marker_before_its_first_turn(
        self, tmp_path
    ):
        # A marker still set once the steps have started would make the next
        # wake end these keys again -- and the children the steps left
        # running in the new project. So every step key's old conversation is
        # ended and the clear committed before any step streams.
        job = self._rescoped_sequence_job(tmp_path, ["agent-a", "agent-b"])
        order: list[str] = []
        result, orch = await self._run_rescoped_sequence(job, settle=lambda: job, order=order)
        assert result == "agent result"
        assert order.index("clear-marker") < order.index("stream")
        ends = [o for o in order if o.startswith("end:")]
        assert sorted(ends) == ["end:cron:jseq:agent-a", "end:cron:jseq:agent-b"]
        assert all(order.index(e) < order.index("clear-marker") for e in ends)
        assert order.count("stream") == 2
        assert orch.sessions.get_or_create.await_count == 2

    @pytest.mark.asyncio
    async def test_a_rescoped_sequence_runs_no_step_when_the_store_cannot_clear_the_marker(
        self, tmp_path
    ):
        from kiro_crew.cron import CronStoreBusy

        def _busy():
            raise CronStoreBusy("cron store lock held")

        job = self._rescoped_sequence_job(tmp_path, ["agent-a", "agent-b"])
        order: list[str] = []
        with pytest.raises(RuntimeError, match="rescope marker"):
            await self._run_rescoped_sequence(job, settle=_busy, order=order)
        assert "stream" not in order, order
        assert order[-1] == "clear-marker"
        job.set_run_result.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stateless_rescoped_sequence_runs_no_step_when_the_stable_key_is_busy(
        self, tmp_path
    ):
        # persistent_session off: the steps run on their own keys, and the
        # STABLE key's old conversation is ended too before the clear. When
        # that end is declined (a dashboard turn holds the key) the marker
        # must stay set, so no step may start -- a step's work would be
        # cancelled by the next wake's end of these same step keys.
        job = self._rescoped_sequence_job(tmp_path, ["agent-a", "agent-b"])
        job.persistent_session = False
        order: list[str] = []
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()

        async def _discard(key, **_kw):
            order.append(f"end:{key}")
            return key != "cron:jseq"  # the stable key is busy elsewhere

        orch.sessions.discard_conversation = _discard
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = None
        orch.slack = None

        async def _stream(*_a, **_kw):
            order.append("stream")
            return "agent result"

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs_inst.update_job_async = AsyncMock(
                side_effect=lambda *a, **k: order.append("clear-marker")
            )
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()
        callback = mock_cs.create.call_args[1]["on_job"]

        with patch("kiro_crew.slack.gateway.stream_and_collect", _stream):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jseq:run-1", "run"),
            ):
                with pytest.raises(RuntimeError, match="busy"):
                    await callback(job)
        assert "end:cron:jseq" in order
        assert "stream" not in order, order
        assert "clear-marker" not in order, order
        job.set_run_result.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Cron success reminder after 24h
# ═══════════════════════════════════════════════════════════════════════════


class TestCronSuccessReminder:
    """Cron dedup reminder after 24h."""

    @pytest.mark.asyncio
    async def test_success_reminder_after_24h(self):
        """After 24h of same result, re-posts with warning."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "j_remind"
        job.name = "reminder-job"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = _result_hash("same output")
        job.consecutive_dupes = 5
        # Posted more than 24h ago
        # flake-ok: pre-existing real-clock stamp past the reminder window, moved verbatim; tracked for a de-flake pass
        job.last_posted_at = time.time() - _SUCCESS_REMINDER_SECS - 100
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="same output",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:j_remind", "run"),
            ):
                result = await callback(job)

        # Should have posted (reminder path)
        orch.slack.post_blocks.assert_awaited()
        assert "same result" in result


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Cron ACP retry path
# ═══════════════════════════════════════════════════════════════════════════


class TestCronAcpRetry:
    """Cron ACP process death retry."""

    @pytest.mark.asyncio
    async def test_acp_retry_on_process_death(self):
        """ACP error with 'not running' triggers retry."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = None

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "jacp"
        job.name = "acp-retry"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = ""
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0
        job._acp_retried = False

        from kiro_crew.acp.client import AcpError

        call_count = [0]

        async def _fake_stream(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise AcpError("process not running")
            return "retry success"

        with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=_fake_stream):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jacp", "run"),
            ):
                result = await callback(job)

        assert result == "retry success"


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Cron with acked_items
# ═══════════════════════════════════════════════════════════════════════════


class TestCronAckedItems:
    """Cron callback with acked_items."""

    @pytest.mark.asyncio
    async def test_acked_items_appended_to_message(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = None
        orch.slack = None

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "jack"
        job.name = "acked-job"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = ""
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = ["item1", "item2"]
        job.silent = True
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="acked result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jack", "run"),
            ):
                with patch("kiro_crew.sel.sel") as mock_sel:
                    mock_sel.return_value.log_tool_invocation = MagicMock()
                    result = await callback(job)

        assert result == "acked result"
        call_args = orch.ctx_builder.build_message.call_args[0][0]
        assert "item1" in call_args
        assert "item2" in call_args


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Cron with Slack delivery failure
# ═══════════════════════════════════════════════════════════════════════════


class TestCronSlackDeliveryFailure:
    """Cron Slack delivery exception handling."""

    @pytest.mark.asyncio
    async def test_slack_delivery_exception_notifies_dashboard(self):
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(side_effect=RuntimeError("slack error"))

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.execution_context = None
        job.member_id = ""
        job.memory_store = ""
        job.script = ""
        job.command = ""
        job.id = "jslack"
        job.name = "slack-fail"
        job.persistent_session = True
        job.project_dir = ""
        job.project_dir_was = ""
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jslack", "run"),
            ):
                result = await callback(job)

        assert result == "result"
        # Dashboard should have been notified about the Slack failure
        assert ds.notify.call_count >= 2  # once for result, once for slack failure


class TestDeliverCronResponse:
    """_deliver_cron_response — Slack delivery of post-subagent cron output."""

    def _orch_with_slack(self):
        orch = _make_orchestrator(owner_id="U_OWNER")
        orch.sessions = _mock_sessions()
        slack = MagicMock()
        slack.post_message = AsyncMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        orch.slack = slack
        return orch, slack

    @pytest.mark.asyncio
    async def test_posts_to_stored_channel_and_thread(self):
        orch, slack = self._orch_with_slack()
        orch.sessions.get_channel = MagicMock(return_value="C123")
        orch.sessions.get_thread = MagicMock(return_value="T456")

        posted = await orch._deliver_cron_response("cron:job1", "hello world")

        assert posted is True
        slack.post_message.assert_awaited_once()
        args = slack.post_message.call_args.args
        assert args[0] == "C123"
        assert "hello world" in args[1]
        assert args[2] == "T456"

    @pytest.mark.asyncio
    async def test_falls_back_to_owner_dm(self):
        orch, slack = self._orch_with_slack()
        # No stored channel → open owner DM. A stale thread_ts from another
        # channel must be dropped (invalid in a DM).
        orch.sessions.get_thread = MagicMock(return_value="T_STALE")
        posted = await orch._deliver_cron_response("cron:job1", "hi")

        assert posted is True
        slack.open_dm.assert_awaited_once_with("U_OWNER")
        assert slack.post_message.call_args.args[0] == "D_OWNER"
        assert slack.post_message.call_args.args[2] is None

    @pytest.mark.asyncio
    async def test_noop_when_silent(self):
        orch, slack = self._orch_with_slack()
        orch.sessions.get_channel = MagicMock(return_value="C123")

        posted = await orch._deliver_cron_response("cron:job1", "hi", silent=True)

        assert posted is False
        slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_noop_when_text_blank(self):
        orch, slack = self._orch_with_slack()
        orch.sessions.get_channel = MagicMock(return_value="C123")

        posted = await orch._deliver_cron_response("cron:job1", "   ")

        assert posted is False
        slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_renders_options_action_block(self):
        # an [OPTIONS: ...] tag in cron output renders as an
        # actions block posted after the message.
        orch, slack = self._orch_with_slack()
        slack.post_blocks = AsyncMock()
        orch.sessions.get_channel = MagicMock(return_value="C123")
        orch.sessions.get_thread = MagicMock(return_value="T456")

        posted = await orch._deliver_cron_response("cron:job1", "pick one\n\n[OPTIONS: Yes | No]")

        assert posted is True
        body = slack.post_message.call_args.args[1]
        assert "OPTIONS" not in body
        slack.post_blocks.assert_awaited_once()
        assert slack.post_blocks.call_args.args[0] == "C123"

    @pytest.mark.asyncio
    async def test_no_options_no_action_block(self):
        orch, slack = self._orch_with_slack()
        slack.post_blocks = AsyncMock()
        orch.sessions.get_channel = MagicMock(return_value="C123")

        posted = await orch._deliver_cron_response("cron:job1", "plain text")

        assert posted is True
        slack.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redacts_before_posting(self):
        # Defense-in-depth: the helper must redact at the Slack boundary even
        # if the caller already redacted (security-controls).
        #
        # Asserted on the OUTCOME rather than on which redactor got called. The
        # boundary is now render_for_slack (which runs redact_via_context on both
        # sides of the mrkdwn conversion), so a mock-call assertion against
        # gateway.redact_credentials would only prove the old wiring still
        # existed -- it would pass for a path that redacted nothing and fail for
        # a correct path that redacts somewhere else. What must be true is that
        # the secret does not reach Slack.
        orch, slack = self._orch_with_slack()
        orch.sessions.get_channel = MagicMock(return_value="C123")

        secret = "AKIAIOSFODNN7EXAMPLE"
        posted = await orch._deliver_cron_response("cron:job1", f"tok {secret}")

        assert posted is True
        body = slack.post_message.call_args.args[1]
        assert secret not in body
        assert secret[:8] not in body, "a credential fragment reached Slack"

    @pytest.mark.asyncio
    async def test_redacts_a_credential_ansi_escapes_had_split(self):
        """The reassembly hazard, at this call site.

        An escape sequence dropped into the middle of a key hides it from the
        credential regex, and the ANSI strip inside to_slack_mrkdwn puts it back
        together -- so a path that redacts BEFORE normalising posts the key
        intact. This is the case the old redact-then-convert ordering here got
        wrong, and it is why the shared pipeline strips ANSI first.
        """
        orch, slack = self._orch_with_slack()
        orch.sessions.get_channel = MagicMock(return_value="C123")

        secret = "AKIAIOSFODNN7EXAMPLE"
        obfuscated = secret[:4] + "\x1b[0m" + secret[4:]
        posted = await orch._deliver_cron_response("cron:job1", f"tok {obfuscated}")

        assert posted is True
        body = slack.post_message.call_args.args[1]
        assert secret not in body, "the ANSI strip reassembled the credential"
