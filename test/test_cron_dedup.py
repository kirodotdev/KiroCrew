"""Tests for cron result deduplication.

Verifies that repeated identical cron results suppress Slack delivery
while still logging to the dashboard, and that different results reset
the dedup state.
"""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock, patch

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.slack.gateway import _result_hash


def _make_gateway():
    """Build a minimal GatewayOrchestrator with mocked dependencies.

    NOTE: Must mirror all instance attributes from GatewayOrchestrator.__init__.
    If tests fail with AttributeError, add the missing attr here.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.ctx_builder = MagicMock()
    gw.slack = MagicMock()
    gw.conv_log = None
    gw.dashboard_state = MagicMock()
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._no_crons = False
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.set_thread = AsyncMock()
    gw.sessions.set_channel = AsyncMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    return gw


def _make_job(**overrides):
    defaults = dict(
        id="j1",
        name="test-job",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=300),
        approval_mode="auto",
        channel="C123",
    )
    defaults.update(overrides)
    return CronJob(**defaults)


def _run_callback(gw, job, stream_result="done"):
    """Init cron on the gateway, capture the callback, and invoke it."""
    captured_cb = None

    async def fake_stream(client, msg, **kwargs):
        return stream_result

    with patch("kiro_crew.slack.gateway.stream_and_collect", fake_stream), patch(
        "kiro_crew.slack.gateway.CronService"
    ) as mock_cron_cls:

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            return await captured_cb(job)

        return asyncio.run(_init_and_run())


class TestResultHash:
    """_result_hash strips volatile data before hashing."""

    def test_strips_iso_timestamps(self) -> None:
        a = _result_hash("Error at 2026-04-06T15:30:00 in module X")
        b = _result_hash("Error at 2026-04-07T09:00:00 in module X")
        assert a == b

    def test_strips_uuids(self) -> None:
        a = _result_hash("Session a1b2c3d4-e5f6-7890-abcd-ef1234567890 failed")
        b = _result_hash("Session ffffffff-ffff-ffff-ffff-ffffffffffff failed")
        assert a == b

    def test_strips_fractional_seconds_and_tz(self) -> None:
        a = _result_hash("Error at 2026-04-06T15:30:00.123456Z in module X")
        b = _result_hash("Error at 2026-04-07T09:00:00.999999+05:30 in module X")
        assert a == b

    def test_strips_epoch_seconds_near_now(self) -> None:
        """Epoch seconds within ±5min of now are stripped."""
        import time

        now = int(time.time())
        a = _result_hash(f"Timestamp {now} error")
        b = _result_hash(f"Timestamp {now + 60} error")
        assert a == b

    def test_strips_epoch_millis_near_now(self) -> None:
        """Epoch millis within ±5min of now are stripped."""
        import time

        now_ms = int(time.time() * 1000)
        a = _result_hash(f"Created at {now_ms} ok")
        b = _result_hash(f"Created at {now_ms + 60000} ok")
        assert a == b

    def test_preserves_old_epoch_values(self) -> None:
        """Epoch values far from now are NOT stripped (could be IDs)."""
        a = _result_hash("Build 1000000001 failed")
        b = _result_hash("Build 1000000002 failed")
        assert a != b

    def test_preserves_aws_account_ids(self) -> None:
        a = _result_hash("Failed for account 700638339968")
        b = _result_hash("Failed for account 309322535530")
        assert a != b

    def test_different_messages_differ(self) -> None:
        a = _result_hash("Error: connection refused")
        b = _result_hash("Error: timeout exceeded")
        assert a != b


class TestCronDedup:
    """Duplicate cron results suppress Slack delivery."""

    def test_first_run_posts_to_slack(self) -> None:
        gw = _make_gateway()
        gw.slack.post_blocks = AsyncMock(return_value="ts1")
        job = _make_job()
        import time

        before = time.time()
        _run_callback(gw, job, stream_result="pipeline ok")
        gw.slack.post_blocks.assert_called_once()
        assert job.last_posted_hash != ""
        assert job.consecutive_dupes == 0
        assert job.last_posted_at >= before

    def test_duplicate_suppresses_slack(self) -> None:
        gw = _make_gateway()
        gw.slack.post_blocks = AsyncMock(return_value="ts1")
        job = _make_job()

        _run_callback(gw, job, stream_result="error: token expired")
        assert gw.slack.post_blocks.call_count == 1

        gw.slack.post_blocks.reset_mock()
        _run_callback(gw, job, stream_result="error: token expired")
        gw.slack.post_blocks.assert_not_called()
        assert job.consecutive_dupes == 1

    def test_duplicate_logs_to_dashboard(self) -> None:
        gw = _make_gateway()
        gw.slack.post_blocks = AsyncMock(return_value="ts1")
        job = _make_job()

        _run_callback(gw, job, stream_result="error: token expired")
        gw.dashboard_state.notify.reset_mock()

        _run_callback(gw, job, stream_result="error: token expired")
        assert gw.dashboard_state.notify.called
        title = gw.dashboard_state.notify.call_args.args[1]
        assert "🔇" in title
        assert "dup #1" in title

    def test_different_result_resets_dedup(self) -> None:
        gw = _make_gateway()
        gw.slack.post_blocks = AsyncMock(return_value="ts1")
        job = _make_job()

        _run_callback(gw, job, stream_result="error: token expired")
        _run_callback(gw, job, stream_result="error: token expired")
        assert job.consecutive_dupes == 1

        gw.slack.post_blocks.reset_mock()
        _run_callback(gw, job, stream_result="pipeline healthy")
        gw.slack.post_blocks.assert_called_once()
        assert job.consecutive_dupes == 0

    def test_dedup_emits_sel_audit(self) -> None:
        gw = _make_gateway()
        gw.slack.post_blocks = AsyncMock(return_value="ts1")
        job = _make_job()

        _run_callback(gw, job, stream_result="same error")
        with patch("kiro_crew.slack.gateway.sel") as mock_sel:
            _run_callback(gw, job, stream_result="same error")
            mock_sel.return_value.log_tool_invocation.assert_called_once()
            call_kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
            assert call_kwargs["tool_name"] == "cron_dedup_suppress"
            assert call_kwargs["outcome"] == "suppressed"

    def test_slack_failure_does_not_poison_dedup_state(self) -> None:
        gw = _make_gateway()
        gw.slack.post_blocks = AsyncMock(side_effect=Exception("network"))
        job = _make_job()

        _run_callback(gw, job, stream_result="error: token expired")
        # Slack failed — hash should NOT be set
        assert job.last_posted_hash == ""

        gw.slack.post_blocks = AsyncMock(return_value="ts1")
        _run_callback(gw, job, stream_result="error: token expired")
        # Should post since previous delivery failed
        gw.slack.post_blocks.assert_called_once()

    def test_reminder_after_24h(self) -> None:
        gw = _make_gateway()
        gw.slack.post_blocks = AsyncMock(return_value="ts1")
        job = _make_job()

        # First run — posts normally
        _run_callback(gw, job, stream_result="error: token expired")
        assert gw.slack.post_blocks.call_count == 1

        # Second run — suppressed (within 24h)
        gw.slack.post_blocks.reset_mock()
        _run_callback(gw, job, stream_result="error: token expired")
        gw.slack.post_blocks.assert_not_called()

        # Simulate 24h passing
        job.last_posted_at -= 86401
        gw.slack.post_blocks.reset_mock()
        import time

        before = time.time()
        _run_callback(gw, job, stream_result="error: token expired")
        gw.slack.post_blocks.assert_called_once()
        # Verify reminder prefix in the posted text
        posted_text = gw.slack.post_blocks.call_args
        assert "⚠️" in str(posted_text)
        # Dedup state should reset after successful reminder delivery
        assert job.consecutive_dupes == 0
        assert job.last_posted_at >= before


# ─────────────────────────────────────────────────────────────────────────────
# Failure dedup
# ─────────────────────────────────────────────────────────────────────────────


def _run_callback_raising(gw, job, exc):
    """Invoke the cron callback with a failing stream_and_collect.

    Asserts the production code re-raises the original exception — callers
    like CronService._execute rely on this contract to set last_status='error'.
    """
    captured_cb = None

    async def fake_stream(client, msg, **kwargs):
        raise exc

    with patch("kiro_crew.slack.gateway.stream_and_collect", fake_stream), patch(
        "kiro_crew.slack.gateway.CronService"
    ) as mock_cron_cls, patch("kiro_crew.sel.sel"):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            try:
                await captured_cb(job)
            except Exception as e:
                return e
            return None

        result = asyncio.run(_init_and_run())
        assert result is exc, f"Expected original {exc!r} to be re-raised, got {result!r}"
        return result


class TestCronFailureDedup:
    """Cron failure notifications deduplicate identical repeated crashes."""

    def test_first_failure_alerts_slack(self) -> None:
        gw = _make_gateway()
        gw.slack.post_message = AsyncMock()
        job = _make_job()
        _run_callback_raising(gw, job, RuntimeError("boom"))
        # First failure always posts to Slack
        assert gw.slack.post_message.await_count == 1
        # The framework-level failure DM names the machine hostname so
        # multi-gateway setups can tell which machine's session failed.
        host = socket.gethostname().split(".")[0]
        assert f"on {host}" in gw.slack.post_message.await_args.args[1]
        # State advanced only after successful delivery
        assert job.last_failure_hash != ""
        assert job.consecutive_failures == 1

    def test_duplicate_failure_suppressed(self) -> None:
        gw = _make_gateway()
        gw.slack.post_message = AsyncMock()
        job = _make_job()
        exc = RuntimeError("boom")
        _run_callback_raising(gw, job, exc)
        assert gw.slack.post_message.await_count == 1
        # Second identical failure within 1h → suppressed
        _run_callback_raising(gw, job, exc)
        assert gw.slack.post_message.await_count == 1  # no new Slack post
        assert job.consecutive_failures == 2
        # Dashboard still gets notified (with dup marker)
        dup_calls = [
            c for c in gw.dashboard_state.notify.call_args_list if "dup failure" in str(c)
        ]
        assert dup_calls, "Expected a dup failure dashboard notification"

    def test_different_failure_re_alerts(self) -> None:
        gw = _make_gateway()
        gw.slack.post_message = AsyncMock()
        job = _make_job()
        _run_callback_raising(gw, job, RuntimeError("first"))
        _run_callback_raising(gw, job, ValueError("different"))
        # Different exception → not a dup → fresh alert
        assert gw.slack.post_message.await_count == 2
        # Counter reset on new alert
        assert job.consecutive_failures == 1

    def test_reminder_window_reexpired_realerts(self) -> None:
        gw = _make_gateway()
        gw.slack.post_message = AsyncMock()
        job = _make_job()
        exc = RuntimeError("same")
        _run_callback_raising(gw, job, exc)
        assert job.consecutive_failures == 1
        # Simulate a suppression during the window (bumps counter to 2) then 1h+ passes
        _run_callback_raising(gw, job, exc)  # suppressed, counter = 2
        assert gw.slack.post_message.await_count == 1
        assert job.consecutive_failures == 2
        job.last_failure_at -= 3601
        _run_callback_raising(gw, job, exc)
        # Re-alerted after window expired
        assert gw.slack.post_message.await_count == 2
        # Counter continues from the suppressed count (3) and the re-alert message
        # should reflect the persistent-failure variant.
        assert job.consecutive_failures == 3
        realert_call = gw.slack.post_message.await_args_list[-1]
        assert "still failing" in realert_call.args[1]
        assert "3 consecutive" in realert_call.args[1]
        assert f"on {socket.gethostname().split('.')[0]}" in realert_call.args[1]

    def test_recovery_clears_failure_state(self) -> None:
        gw = _make_gateway()
        gw.slack.post_message = AsyncMock()
        gw.slack.post_blocks = AsyncMock(return_value="ts1")
        job = _make_job()
        _run_callback_raising(gw, job, RuntimeError("boom"))
        assert job.last_failure_hash != ""
        # Successful run clears failure dedup state
        _run_callback(gw, job, stream_result="recovered")
        assert job.last_failure_hash == ""
        assert job.consecutive_failures == 0
        # Subsequent identical failure alerts fresh
        _run_callback_raising(gw, job, RuntimeError("boom"))
        assert gw.slack.post_message.await_count == 2

    def test_slack_failure_does_not_advance_dedup_state(self) -> None:
        gw = _make_gateway()
        gw.slack.post_message = AsyncMock(side_effect=Exception("slack down"))
        job = _make_job()
        _run_callback_raising(gw, job, RuntimeError("boom"))
        # Slack delivery failed → dedup state NOT advanced → next run re-alerts
        assert job.last_failure_hash == ""
        assert job.consecutive_failures == 0

    def test_no_channel_still_advances_dedup_state(self) -> None:
        """When Slack is configured but no channel can be resolved, dedup must
        still advance. Otherwise every identical failure would re-notify the
        dashboard — the exact scenario dedup is meant to prevent."""
        gw = _make_gateway()
        gw.slack.post_message = AsyncMock()
        job = _make_job()
        job.channel = ""
        job.created_by = ""
        gw._owner_id = ""  # no way to resolve a channel
        _run_callback_raising(gw, job, RuntimeError("boom"))
        # Slack didn't post (no channel) but dedup advanced anyway
        assert gw.slack.post_message.await_count == 0
        assert job.last_failure_hash != ""
        assert job.consecutive_failures == 1
        # Second identical failure → suppressed (dedup works)
        _run_callback_raising(gw, job, RuntimeError("boom"))
        assert job.consecutive_failures == 2


class TestCronFailurePersistence:
    """last_failure_* fields round-trip through _save/_load."""

    def test_timeout_clears_failure_dedup_state(self, tmp_path) -> None:
        """Timeout handler must clear failure dedup state so a subsequent real
        error isn't silently suppressed as a dup of the pre-timeout failure."""
        import asyncio

        from kiro_crew.cron import CronService

        async def _hang(*args, **kwargs):
            await asyncio.sleep(9999)  # simulate hang; timeout will cancel

        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="test",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=60),
            last_failure_hash="stale-hash-from-prior-failure",
            consecutive_failures=3,
            timeout_secs=0,
        )
        # Pretend _execute hangs so _execute_with_timeout triggers the timeout.
        with patch.object(svc, "_execute", side_effect=_hang), patch(
            "kiro_crew.cron._JOB_TIMEOUT_SECS", 0.05
        ):
            asyncio.run(svc._execute_with_timeout(job))
        assert job.last_status == "error"
        assert "Timed out" in job.last_error
        # Failure dedup state cleared — next real error will trigger fresh alert
        assert job.last_failure_hash == ""
        assert job.last_failure_at == 0.0
        # ...but the timeout still counts toward the auto-pause threshold (#424):
        # started at 3, one timeout -> 4.
        assert job.consecutive_failures == 4

    def test_timeout_persists_cleared_state(self, tmp_path) -> None:
        """Verify _run_job_isolated persists the cleared failure state to disk."""
        import asyncio

        from kiro_crew.cron import CronService

        async def _hang(*args, **kwargs):
            await asyncio.sleep(9999)

        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="test",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=60),
            last_failure_hash="stale",
            last_failure_at=1776400000.0,
            consecutive_failures=3,
            timeout_secs=0,
        )
        svc._jobs = [job]
        svc._save()
        with patch.object(svc, "_execute", side_effect=_hang), patch(
            "kiro_crew.cron._JOB_TIMEOUT_SECS", 0.05
        ):
            asyncio.run(svc._run_job_isolated(job))
        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        assert svc2._jobs[0].last_failure_hash == ""
        assert svc2._jobs[0].last_failure_at == 0.0
        # Timeout counted toward auto-pause and was persisted (#424): 3 -> 4.
        assert svc2._jobs[0].consecutive_failures == 4

    def test_save_load_round_trip(self, tmp_path) -> None:
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="test",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=60),
            last_failure_hash="abc123",
            last_failure_at=1776400000.0,
            consecutive_failures=5,
        )
        svc._jobs = [job]
        svc._save()

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        loaded = svc2._jobs[0]
        assert loaded.last_failure_hash == "abc123"
        assert loaded.last_failure_at == 1776400000.0
        assert loaded.consecutive_failures == 5

    def test_load_missing_fields_defaults(self, tmp_path) -> None:
        """Old crons.json without new fields loads with safe defaults."""
        import json

        from kiro_crew.cron import CronService

        path = tmp_path / "crons.json"
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "jobs": [
                        {
                            "id": "j1",
                            "name": "old",
                            "message": "go",
                            "schedule": {"kind": "every", "every_secs": 60},
                        }
                    ],
                }
            )
        )
        svc = CronService(base_dir=tmp_path)
        svc._load()
        j = svc._jobs[0]
        assert j.last_failure_hash == ""
        assert j.last_failure_at == 0.0
        assert j.consecutive_failures == 0


# ─────────────────────────────────────────────────────────────────────────────
# Silent flag respected on failure
# ─────────────────────────────────────────────────────────────────────────────


class TestCronFailureRespectsSilent:
    """A silent cron is pure background: its failure is recorded (history +
    circuit breaker) but never broadcast — no Slack DM and no dashboard bell."""

    def test_silent_first_failure_suppresses_slack_and_dashboard(self) -> None:
        gw = _make_gateway()
        gw.slack.post_message = AsyncMock()
        job = _make_job(silent=True)
        _run_callback_raising(gw, job, RuntimeError("boom"))
        # No Slack DM and no dashboard bell for a silent cron...
        gw.slack.post_message.assert_not_awaited()
        gw.dashboard_state.notify.assert_not_called()
        # ...but the failure is still recorded so the 5-strike auto-pause works.
        assert job.last_failure_hash != ""
        assert job.consecutive_failures == 1

    def test_silent_duplicate_failure_stays_silent(self) -> None:
        gw = _make_gateway()
        gw.slack.post_message = AsyncMock()
        job = _make_job(silent=True)
        exc = RuntimeError("boom")
        _run_callback_raising(gw, job, exc)
        _run_callback_raising(gw, job, exc)  # identical failure within the window
        # Neither the fresh-failure nor the dup-failure path notifies when silent.
        gw.slack.post_message.assert_not_awaited()
        gw.dashboard_state.notify.assert_not_called()
        # Counter still climbs toward auto-pause.
        assert job.consecutive_failures == 2

    def test_non_silent_first_failure_still_rings_dashboard(self) -> None:
        # Regression: the silent guard must not suppress the bell for normal crons.
        gw = _make_gateway()
        gw.slack.post_message = AsyncMock()
        job = _make_job(silent=False)
        _run_callback_raising(gw, job, RuntimeError("boom"))
        gw.slack.post_message.assert_awaited_once()
        alert_calls = [
            c for c in gw.dashboard_state.notify.call_args_list
            if "❌ Job failed" in str(c)
        ]
        assert alert_calls, "Non-silent cron failure must still ring the dashboard bell"

    def test_silent_failure_records_suppressed_sel_audit(self) -> None:
        # A silent cron's failure is still audited — as a suppression, not a
        # dropped audit. Locks in the "recorded in the SEL" invariant so a
        # future refactor can't move the SEL call inside a silent guard.
        gw = _make_gateway()
        gw.slack.post_message = AsyncMock()
        job = _make_job(silent=True)
        with patch("kiro_crew.slack.gateway.sel") as mock_sel:
            _run_callback_raising(gw, job, RuntimeError("boom"))
        mock_sel.return_value.log_tool_invocation.assert_called_once()
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "cron_failure_alert"
        assert kwargs["outcome"] == "suppressed"
        assert kwargs["downstream_service"] == "none"
