"""The heartbeat, task-runner and AutoNudge services the gateway wires.

Covers their initialization, the heartbeat and AutoNudge fire callbacks, the
notification metadata builder, the notice a monitoring loop sends when it stops
at its cycle cap, and the truncation of a large result before it is prompted.
"""

from __future__ import annotations

import os
import subprocess
import sys
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

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.slack.gateway import GatewayOrchestrator

# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_heartbeat
# ═══════════════════════════════════════════════════════════════════════════


class TestInitHeartbeat:
    """Heartbeat service initialization."""

    @pytest.mark.asyncio
    async def test_init_heartbeat_creates_service(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.memory = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.gateway.HeartbeatService") as mock_hs:
            mock_hs_inst = MagicMock()
            mock_hs_inst.start = AsyncMock()
            mock_hs.return_value = mock_hs_inst
            await orch._init_heartbeat()
        assert orch.heartbeat_svc is not None
        mock_hs_inst.start.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_task_runner
# ═══════════════════════════════════════════════════════════════════════════


class TestInitTaskRunner:
    """Task runner initialization."""

    def test_init_task_runner_creates_runner(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.gateway.TaskRunner") as mock_tr:
            mock_tr_inst = MagicMock()
            mock_tr.return_value = mock_tr_inst
            orch._init_task_runner()
        assert orch.task_runner is not None

    def test_init_task_runner_passes_the_configured_test_command(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        orch._cfg.taskrunner.test_command = ["python", "-m", "pytest", "-q"]
        orch._cfg.taskrunner.require_verification = True
        with patch("kiro_crew.slack.gateway.TaskRunner") as mock_tr:
            orch._init_task_runner()
        kw = mock_tr.call_args.kwargs
        assert kw["test_cmd"] == ["python", "-m", "pytest", "-q"]
        assert kw["require_verification"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _notif_meta
# ═══════════════════════════════════════════════════════════════════════════


class TestNotifMeta:
    """Notification metadata builder."""

    def test_none_for_empty_key(self):
        assert GatewayOrchestrator._notif_meta("") is None
        assert GatewayOrchestrator._notif_meta(None) is None

    def test_dashboard_slot(self):
        result = GatewayOrchestrator._notif_meta("dashboard:my-slot")
        assert result == {"slot": "my-slot"}

    def test_slack_link(self):
        result = GatewayOrchestrator._notif_meta("C123:1234.567890")
        assert result is not None
        assert "slack_link" in result
        assert "C123" in result["slack_link"]

    def test_cron_key_returns_none(self):
        assert GatewayOrchestrator._notif_meta("cron:j1") is None

    def test_subagent_key_returns_none(self):
        assert GatewayOrchestrator._notif_meta("subagent:a1") is None

    def test_hook_key_returns_none(self):
        assert GatewayOrchestrator._notif_meta("hook:h1") is None


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _notify_nudge_expired
# ═══════════════════════════════════════════════════════════════════════════


class TestNotifyNudgeExpired:
    """A monitoring loop that stops at its cycle cap must tell the user.

    Reaching ``max_cycles`` is a runaway backstop, not a finish line — the loop
    stopped with its goal possibly unmet. Without this the only trace is a log
    line plus an ``active=False`` state change indistinguishable from a manual
    Stop, so a capped-out loop looks the same as the agent stopping itself.
    """

    @staticmethod
    def _orch(state):
        """A minimal stand-in — the method only needs dashboard_state."""
        orch = SimpleNamespace(
            dashboard_state=state,
            _notif_meta=GatewayOrchestrator._notif_meta,
        )
        return orch

    def _loop(self, slot_key="chat-7-1700000000"):
        return NudgeLoop(
            id="loop-x",
            slot_key=slot_key,
            message="babysit the PR",
            idle_secs=300,
            max_cycles=24,
            cycle_count=24,
        )

    def test_a_merged_subject_says_no_action_needed_and_a_closed_one_does_not(self):
        """Both are terminal; only one is good news.

        A pull request closed WITHOUT merging stopped on a question the operator has
        to answer -- reopen, or abandon -- so telling them "no action needed" would
        be false about the one outcome that needs them. The wording follows the
        monitor's recorded outcome rather than lumping both under a finish.
        """
        import kiro_crew.autonudge as _an
        from kiro_crew.monitoring.models import MonitorOutcome, MonitorState

        for outcome, expect_no_action in (
            (MonitorOutcome.SUCCESS, True),
            (MonitorOutcome.BLOCKED, False),
        ):
            loop = self._loop()
            # NOT capped: the cap branch outranks the terminal one, so a 24-of-24
            # loop would exercise the wrong case entirely.
            loop.cycle_count = 3
            loop.stopped_reason = _an.MONITOR_TERMINAL_REASON
            loop.monitor = MonitorState(
                kind="github_pull_request",
                target="https://github.com/acme/widgets/pull/7",
                objective="review_ready",
                created_ts=1.0,
                outcome=outcome,
                stopped_at=2.0,
                stopped_reason="pull_request_merged" if expect_no_action else "pull_request_closed",
            )
            state = MagicMock()
            GatewayOrchestrator._notify_nudge_expired(self._orch(state), loop)
            _args, _kwargs = state.notify.call_args
            body = _args[2]
            assert ("No action needed" in body) is expect_no_action, (outcome, body)
            if not expect_no_action:
                assert "reopen" in body.lower(), body

    def test_notifies_with_cycle_counts_and_slot_link(self):
        state = MagicMock()
        GatewayOrchestrator._notify_nudge_expired(self._orch(state), self._loop())
        state.notify.assert_called_once()
        args, kwargs = state.notify.call_args
        # Body names the real numbers so the user can judge whether to resume.
        assert "24 of 24" in args[2]
        # Dashboard loops bind on the BARE slot key; the notification must
        # still deep-link, which requires re-qualifying it for _notif_meta.
        assert kwargs["meta"] == {"slot": "chat-7-1700000000"}

    def test_channel_loop_gets_no_synthesized_meta(self):
        """A channel key must not be fed to _notif_meta at all.

        Its generic ``chan:ts`` split would read the NAMESPACE as the channel
        id — ``slack:1700000000.123456`` became a link to an "archives/slack"
        channel, and a Discord loop got a Slack URL. Asserting only "not a
        slot" passed on exactly that bogus link, so assert the value exactly.
        """
        for key in ("slack:1700000000.123456", "discord:kirocrew:direct:42"):
            state = MagicMock()
            loop = self._loop(slot_key=key)
            GatewayOrchestrator._notify_nudge_expired(self._orch(state), loop)
            state.notify.assert_called_once()
            assert state.notify.call_args.kwargs["meta"] is None, key

    def test_no_dashboard_state_is_a_noop(self):
        # Must not raise when the dashboard isn't wired up (Slack-only host).
        GatewayOrchestrator._notify_nudge_expired(self._orch(None), self._loop())

    def test_notify_failure_never_propagates(self):
        """Runs inside the observer loop — an exception here would also skip
        the WS broadcast that follows it, so it must be swallowed."""
        state = MagicMock()
        state.notify.side_effect = RuntimeError("bus down")
        GatewayOrchestrator._notify_nudge_expired(self._orch(state), self._loop())


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_autonudge
# ═══════════════════════════════════════════════════════════════════════════


class TestInitAutonudge:
    """AutoNudge service initialization."""

    @pytest.mark.asyncio
    async def test_disabled_when_feature_flag_off(self):
        orch = _make_orchestrator()
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=False):
            await orch._init_autonudge()
        assert not hasattr(orch, "autonudge_svc") or orch.autonudge_svc is None  # noqa: E501

    def test_disabled_gateway_import_does_not_load_monitor_runtime(self, tmp_path):
        env = os.environ.copy()
        env["KIROCREW_AUTONUDGE"] = "0"
        env["KIROCREW_HOME"] = str(tmp_path / "crew")
        env["KIRO_HOME"] = str(tmp_path / "kiro")
        # flake-ok: pre-existing literal bound on the cold-import child, moved verbatim; tracked for a de-flake pass
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import kiro_crew.slack.gateway; "
                    "print(sorted(name for name in sys.modules if name in {"
                    "'kiro_crew.monitoring.controller',"
                    "'kiro_crew.monitoring.github_pull_request',"
                    "'kiro_crew.monitoring.gitlab_merge_request',"
                    "'kiro_crew.monitoring.azure_devops_pull_request',"
                    "'kiro_crew.monitoring.bitbucket_pull_request'}))"
                ),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "[]"

    @pytest.mark.asyncio
    async def test_enabled_creates_service(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_ans:
                mock_inst = MagicMock()
                mock_inst.start = AsyncMock()
                mock_inst.subscribe = MagicMock()
                mock_ans.return_value = mock_inst
                await orch._init_autonudge()
        assert orch.autonudge_svc is not None
        mock_inst.start.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Heartbeat callback
# ═══════════════════════════════════════════════════════════════════════════


class TestHeartbeatCallback:
    """Heartbeat task execution callback."""

    @pytest.mark.asyncio
    async def test_heartbeat_task_success(self):
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.memory = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()
        orch._deliver_result = AsyncMock()

        with patch("kiro_crew.slack.gateway.HeartbeatService") as mock_hs:
            mock_hs_inst = MagicMock()
            mock_hs_inst.start = AsyncMock()
            mock_hs.return_value = mock_hs_inst
            await orch._init_heartbeat()

        # Get the on_task callback
        callback = mock_hs.call_args[1]["on_task"]

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="heartbeat done",
        ):
            result = await callback("check status", "")

        assert result == "heartbeat done"
        orch._deliver_result.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_heartbeat_task_keep_response(self):
        """HEARTBEAT_KEEP response suppresses delivery."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.memory = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = None
        orch._deliver_result = AsyncMock()

        with patch("kiro_crew.slack.gateway.HeartbeatService") as mock_hs:
            mock_hs_inst = MagicMock()
            mock_hs_inst.start = AsyncMock()
            mock_hs.return_value = mock_hs_inst
            await orch._init_heartbeat()

        callback = mock_hs.call_args[1]["on_task"]

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="still checking HEARTBEAT_KEEP",
        ):
            result = await callback("poll endpoint", "dashboard:s1")

        assert "HEARTBEAT_KEEP" in result
        orch._deliver_result.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heartbeat_task_failure(self):
        """Heartbeat task exception propagates."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.memory = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = None

        with patch("kiro_crew.slack.gateway.HeartbeatService") as mock_hs:
            mock_hs_inst = MagicMock()
            mock_hs_inst.start = AsyncMock()
            mock_hs.return_value = mock_hs_inst
            await orch._init_heartbeat()

        callback = mock_hs.call_args[1]["on_task"]

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=RuntimeError("llm error"),
        ):
            with pytest.raises(RuntimeError, match="llm error"):
                await callback("broken task", "")


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _deliver_result truncation
# ═══════════════════════════════════════════════════════════════════════════


class TestDeliverResultTruncation:
    """Prompt truncation for large results."""

    @pytest.mark.asyncio
    async def test_prompt_truncates_large_result(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.key = "s1"
        slot.enqueue_or_run_prompt = MagicMock(return_value=True)
        slot.queue_depth = 0
        ds.resolve_slot = MagicMock(return_value=slot)
        orch.dashboard_state = ds
        # Create a result larger than MAX_PROMPT_BYTES
        large_result = "x" * 200000
        with patch("kiro_crew.slack.gateway.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await orch._deliver_result("T", "s", large_result, "prompt:dashboard:s1")
        slot.enqueue_or_run_prompt.assert_called_once()
        # Verify the prompt was truncated
        call_args = slot.enqueue_or_run_prompt.call_args[0]
        assert len(call_args[0].encode("utf-8")) <= 131072 + 100  # MAX_PROMPT_BYTES + overhead


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_autonudge _fire callback
# ═══════════════════════════════════════════════════════════════════════════


class TestAutonudgeFire:
    """AutoNudge fire callback."""

    @pytest.mark.asyncio
    async def test_fire_no_dashboard(self):
        """Fire with no dashboard → returns False."""
        orch = _make_orchestrator()
        orch.dashboard_state = None
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_ans:
                mock_inst = MagicMock()
                mock_inst.start = AsyncMock()
                mock_inst.subscribe = MagicMock()
                mock_ans.return_value = mock_inst
                await orch._init_autonudge()

        # Get the on_fire callback
        on_fire = mock_ans.call_args[1]["on_fire"]
        loop = MagicMock()
        loop.id = "loop1"
        loop.slot_key = "s1"
        loop.message = "nudge"
        loop.stop_sentinel_path = None
        loop.cycle_count = 0
        result = await on_fire(loop)
        assert result is False

    @pytest.mark.asyncio
    async def test_fire_slot_missing(self):
        """Fire with missing slot → removes loop.

        The rehydrate fallback is stubbed to a miss because that is what "slot
        missing" means here. It is stubbed explicitly rather than relying on the
        mock dashboard state's MagicMock metadata reading as ``closed`` and the
        rehydrate helper's closed-guard bailing: the fire path passes
        ``adopt_closed=True`` (idle archival must not destroy a loop), so that
        accident does not stop the walk.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds._slots = {}
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_ans:
                mock_inst = MagicMock()
                mock_inst.start = AsyncMock()
                mock_inst.subscribe = MagicMock()
                mock_inst.remove = AsyncMock()
                mock_ans.return_value = mock_inst
                await orch._init_autonudge()

        on_fire = mock_ans.call_args[1]["on_fire"]
        loop = MagicMock()
        loop.id = "loop2"
        loop.slot_key = "gone"
        loop.message = "nudge"
        loop.stop_sentinel_path = None
        loop.cycle_count = 0
        with patch(
            "kiro_crew.slack.gateway.rehydrate_slot_from_history_async",
            new=AsyncMock(return_value=None),
        ):
            result = await on_fire(loop)
        assert result is False

    @pytest.mark.asyncio
    async def test_fire_slot_running_skips(self):
        """Fire with running slot → returns False (skip)."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.running = True
        slot.key = "busy"
        ds._slots = {"busy": slot}
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_ans:
                mock_inst = MagicMock()
                mock_inst.start = AsyncMock()
                mock_inst.subscribe = MagicMock()
                mock_ans.return_value = mock_inst
                await orch._init_autonudge()

        on_fire = mock_ans.call_args[1]["on_fire"]
        loop = MagicMock()
        loop.id = "loop3"
        loop.slot_key = "busy"
        loop.message = "nudge"
        loop.stop_sentinel_path = None
        loop.cycle_count = 0
        result = await on_fire(loop)
        assert result is False
