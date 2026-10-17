"""A failed script/command cron must tell the user WHY, not only the log.

Regression cover for kirodotdev/KiroCrew#4157: the script and command cron
branches signal failure by mutating the job and returning normally, so they
never reached the message path's failure alert. The reason lived in the gateway
log and in a dashboard field nobody reads while waiting for a notification that
never comes -- a job dying on the same environmental ``RuntimeError`` every fire
looked idle rather than broken.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.slack.gateway import _FAILURE_REMINDER_SECS

# The reason from the issue: a startup guard raising before the script body runs.
CONFLICT = (
    "data-home conflict: completion marker present at /home/u/.kiro/crew but a "
    "non-empty legacy home /home/u/.kirocrew also exists"
)


def _make_gw():
    """A GatewayOrchestrator with only the attributes the cron callback touches."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.ctx_builder = MagicMock()
    gw.slack = MagicMock()
    gw.slack.post_message = AsyncMock()
    gw.conv_log = None
    gw.dashboard_state = MagicMock()
    gw.dashboard_state.get_slot = MagicMock(return_value=None)
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._running_script_ids = set()
    gw._no_crons = False
    gw.cron_svc = MagicMock()
    gw.cron_svc.remove_job_async = AsyncMock(return_value=True)
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.set_thread = AsyncMock()
    gw.sessions.set_channel = AsyncMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    return gw


def _script_job(**overrides):
    defaults = dict(
        id="sj1",
        name="monitor-keep-75",
        message="",
        schedule=CronSchedule(kind="every", every_secs=60),
        script="~/.kiro/crew/crons/monitor.py:run",
        channel="C123",
    )
    defaults.update(overrides)
    return CronJob(**defaults)


def _command_job(**overrides):
    defaults = dict(
        id="cj1",
        name="cmd-job",
        message="",
        schedule=CronSchedule(kind="every", every_secs=60),
        command="false",
        channel="C123",
    )
    defaults.update(overrides)
    return CronJob(**defaults)


def _message_job(**overrides):
    defaults = dict(
        id="mj1",
        name="nightly-report",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=300),
        approval_mode="auto",
        channel="C123",
    )
    defaults.update(overrides)
    return CronJob(**defaults)


async def _run_script(gw, job, script_result=None, *, vet_reason=None, side_effect=None):
    captured = None
    mock_kw = (
        {"side_effect": side_effect} if side_effect is not None else {"return_value": script_result}
    )
    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch("kiro_crew.slack.gateway.run_script_sandboxed", **mock_kw),
        patch("kiro_crew.slack.gateway.vet_job_at_fire_time", return_value=vet_reason),
        patch("kiro_crew.slack.gateway.sel"),
    ):

        def capture_cron(on_job=None, **kw):
            nonlocal captured
            captured = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            svc.remove_job_async = AsyncMock(return_value=True)
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
        await gw._init_cron()
        assert captured is not None
        return await captured(job)


async def _run_command(gw, job, cmd_result=None, *, vet_reason=None, side_effect=None):
    captured = None
    mock_kw = (
        {"side_effect": side_effect} if side_effect is not None else {"return_value": cmd_result}
    )
    gate = (
        patch("kiro_crew.slack.gateway.vet_job_at_fire_time", return_value=vet_reason)
        if vet_reason is not None
        else nullcontext()
    )
    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch("kiro_crew.slack.gateway.run_command_sandboxed", **mock_kw),
        gate,
        patch("kiro_crew.slack.gateway.sel"),
    ):

        def capture_cron(on_job=None, **kw):
            nonlocal captured
            captured = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            svc.remove_job_async = AsyncMock(return_value=True)
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
        await gw._init_cron()
        assert captured is not None
        return await captured(job)


async def _run_message_callback_raising(gw, job, exc):
    """Drive the message (LLM) cron path to its failure alert.

    That path signals failure by RAISING, which is exactly why it had an alert
    while the script/command paths did not.
    """
    captured = None

    async def fake_stream(client, msg, **kwargs):
        raise exc

    with (
        patch("kiro_crew.slack.gateway.stream_and_collect", fake_stream),
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch("kiro_crew.slack.gateway.sel"),
        patch("kiro_crew.slack.gateway.vet_job_at_fire_time", return_value=None),
    ):

        def capture_cron(on_job=None, **kw):
            nonlocal captured
            captured = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
        await gw._init_cron()
        assert captured is not None
        with pytest.raises(type(exc)):
            await captured(job)


def _bodies(gw) -> list[str]:
    """Every notification body pushed during the run."""
    return [str(c) for c in gw.dashboard_state.notify.call_args_list]


class TestScriptFailureReachesTheUser:
    @pytest.mark.asyncio
    async def test_script_error_rings_the_bell_with_the_reason(self) -> None:
        gw = _make_gw()
        job = _script_job()
        await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        assert job.last_status == "error"
        bodies = _bodies(gw)
        assert bodies, "a failed script cron must ring the dashboard bell"
        assert any("data-home conflict" in b for b in bodies), (
            "the alert must carry the REASON -- a bare 'failed' is what the log "
            f"already said: {bodies}"
        )

    @pytest.mark.asyncio
    async def test_script_error_dms_the_reason(self) -> None:
        gw = _make_gw()
        await _run_script(gw, _script_job(), {"status": "error", "error": CONFLICT})
        gw.slack.post_message.assert_awaited_once()
        channel, text = gw.slack.post_message.await_args.args
        assert channel == "C123"
        assert "data-home conflict" in text
        assert "monitor-keep-75" in text

    @pytest.mark.asyncio
    async def test_script_timeout_alerts(self) -> None:
        gw = _make_gw()
        job = _script_job()
        await _run_script(gw, job, side_effect=asyncio.TimeoutError())
        assert any("timeout" in b for b in _bodies(gw))

    @pytest.mark.asyncio
    async def test_raising_script_alerts_with_the_exception_type(self) -> None:
        gw = _make_gw()
        await _run_script(gw, _script_job(), side_effect=RuntimeError(CONFLICT))
        bodies = _bodies(gw)
        assert any("RuntimeError" in b and "data-home conflict" in b for b in bodies)


class TestCommandFailureReachesTheUser:
    @pytest.mark.asyncio
    async def test_non_zero_exit_carries_the_output(self) -> None:
        """The output IS the reason for a shell cron -- the exit code alone is not."""
        gw = _make_gw()
        job = _command_job()
        await _run_command(
            gw, job, {"status": "error", "output": "psql: connection refused", "exit_code": 2}
        )
        assert job.last_status == "error"
        bodies = _bodies(gw)
        assert any("exit_code=2" in b for b in bodies)
        assert any("connection refused" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_non_ok_with_no_output_still_alerts(self) -> None:
        gw = _make_gw()
        await _run_command(gw, _command_job(), {"status": "error", "output": "", "exit_code": 1})
        assert any("no output" in b for b in _bodies(gw))

    @pytest.mark.asyncio
    async def test_command_timeout_alerts(self) -> None:
        gw = _make_gw()
        await _run_command(gw, _command_job(), side_effect=asyncio.TimeoutError())
        assert any("timeout" in b for b in _bodies(gw))

    @pytest.mark.asyncio
    async def test_successful_command_does_not_alert(self) -> None:
        """The alert is failure-only: a healthy job must stay quiet."""
        gw = _make_gw()
        job = _command_job()
        await _run_command(gw, job, {"status": "ok", "output": "42 widgets", "exit_code": 0})
        assert job.last_status == "ok"
        gw.slack.post_message.assert_not_awaited()
        assert not any("Run failed" in b for b in _bodies(gw))


class TestFireTimeDenialIsNotSilent:
    """A policy denial killed every fire with no user-facing signal at all.

    ``fire_time_denied`` only keeps a one-shot job alive; it is not a surface.
    """

    @pytest.mark.asyncio
    async def test_script_denial_alerts_as_a_policy_block(self) -> None:
        gw = _make_gw()
        job = _script_job()
        await _run_script(gw, job, {"status": "ok"}, vet_reason="cron capability disabled")
        bodies = _bodies(gw)
        assert any("Blocked by policy" in b for b in bodies), bodies
        assert any("cron capability disabled" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_command_denial_alerts(self) -> None:
        gw = _make_gw()
        await _run_command(
            gw,
            _command_job(),
            {"status": "ok", "output": "x", "exit_code": 0},
            vet_reason="cron capability disabled",
        )
        assert any("Blocked by policy" in b for b in _bodies(gw))

    @pytest.mark.asyncio
    async def test_denial_does_not_count_toward_auto_pause(self) -> None:
        """The alert must not smuggle in the failure count the deny path omits."""
        gw = _make_gw()
        job = _script_job()
        await _run_script(gw, job, {"status": "ok"}, vet_reason="cron capability disabled")
        assert job.consecutive_failures == 0


class TestAlertNoiseControl:
    @pytest.mark.asyncio
    async def test_identical_failure_alerts_once_per_window(self) -> None:
        gw = _make_gw()
        job = _script_job()
        await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        # The DM is the noisy surface, so it is the one withheld on a repeat.
        gw.slack.post_message.assert_awaited_once()
        # The local bell still rings, marked as a repeat, so a user watching the
        # feed can see the job is still down.
        assert any("suppressed" in b for b in _bodies(gw))
        # Suppression is time-boxed, not permanent.
        job.last_failure_at -= _FAILURE_REMINDER_SECS + 1
        await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        assert gw.slack.post_message.await_count == 2

    @pytest.mark.asyncio
    async def test_a_different_reason_is_not_suppressed(self) -> None:
        gw = _make_gw()
        job = _script_job()
        await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        await _run_script(gw, job, {"status": "error", "error": "disk full"})
        assert any("disk full" in b for b in _bodies(gw))

    @pytest.mark.asyncio
    async def test_suppressed_run_still_counts_toward_auto_pause(self) -> None:
        """Dedup silences the bell, never the auto-pause evidence."""
        gw = _make_gw()
        job = _script_job()
        await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        assert job.consecutive_failures == 2

    @pytest.mark.asyncio
    async def test_silent_job_alerts_nowhere_but_still_counts(self) -> None:
        gw = _make_gw()
        job = _script_job(silent=True)
        await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        gw.slack.post_message.assert_not_awaited()
        gw.dashboard_state.notify.assert_not_called()
        assert job.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_a_success_reopens_alerting(self) -> None:
        """record_success clears the dedup fields, so a relapse alerts fresh."""
        gw = _make_gw()
        job = _script_job()
        await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        await _run_script(gw, job, {"status": "ok"})
        assert job.last_failure_hash == ""
        await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        assert gw.slack.post_message.await_count == 2


class TestSlackEntityInjection:
    """Slack PARSES entity markup in a message's text.

    Both halves of the DM are attacker-shaped: the job name is user-authored and
    the reason carries subprocess output.
    """

    @pytest.mark.asyncio
    async def test_broadcast_mention_in_the_job_name_is_escaped(self) -> None:
        gw = _make_gw()
        job = _script_job(name="<!channel> nightly")
        await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        _, text = gw.slack.post_message.await_args.args
        assert "<!channel>" not in text, "a job name must not reach Slack as a live mention"
        assert "&lt;!channel&gt;" in text

    @pytest.mark.asyncio
    async def test_broadcast_mention_in_the_reason_is_escaped(self) -> None:
        gw = _make_gw()
        await _run_script(gw, _script_job(), {"status": "error", "error": "boom <!here> boom"})
        _, text = gw.slack.post_message.await_args.args
        assert "<!here>" not in text
        assert "&lt;!here&gt;" in text

    @pytest.mark.asyncio
    async def test_reason_cannot_escape_the_code_fence(self) -> None:
        """Escaping alone does not close this hole -- a fence needs neutralizing."""
        gw = _make_gw()
        await _run_script(gw, _script_job(), {"status": "error", "error": "a ``` *b* ``` c"})
        _, text = gw.slack.post_message.await_args.args
        # Exactly the opening and closing fence this message builds, no more.
        assert text.count("```") == 2

    @pytest.mark.asyncio
    async def test_dashboard_body_is_not_escaped(self) -> None:
        """The bell is not a mrkdwn sink; escaping there shows a literal `&lt;`."""
        gw = _make_gw()
        await _run_script(gw, _script_job(), {"status": "error", "error": "boom <!here>"})
        assert any("<!here>" in b for b in _bodies(gw))


class TestMessagePathCarriesTheReasonToo:
    """The one path that already alerted said only "Job failed" / "check logs".

    Its own suppressed-duplicate body carried the reason, so the alert a user
    actually reads was the least informative surface of the three.
    """

    @pytest.mark.asyncio
    async def test_first_alert_bell_carries_the_reason(self) -> None:
        gw = _make_gw()
        job = _message_job()
        await _run_message_callback_raising(gw, job, RuntimeError(CONFLICT))
        bodies = _bodies(gw)
        assert any("Job failed" in b for b in bodies)
        assert any("data-home conflict" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_first_alert_dm_carries_the_reason(self) -> None:
        gw = _make_gw()
        await _run_message_callback_raising(gw, _message_job(), RuntimeError(CONFLICT))
        _, text = gw.slack.post_message.await_args.args
        assert "check logs" in text, "the log is still where the full traceback lives"
        assert "data-home conflict" in text, "but the DM must not withhold the reason itself"

    @pytest.mark.asyncio
    async def test_dm_reason_is_escaped_and_fence_safe(self) -> None:
        gw = _make_gw()
        await _run_message_callback_raising(gw, _message_job(), RuntimeError("<!here> ``` x"))
        _, text = gw.slack.post_message.await_args.args
        assert "<!here>" not in text
        assert text.count("```") == 2


class TestAlertNeverBreaksTheRun:
    @pytest.mark.asyncio
    async def test_notify_raising_does_not_break_bookkeeping(self) -> None:
        gw = _make_gw()
        gw.dashboard_state.notify = MagicMock(side_effect=RuntimeError("bell broke"))
        job = _script_job()
        result = await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        assert result is None
        assert job.last_status == "error"
        assert "data-home conflict" in job.last_error

    @pytest.mark.asyncio
    async def test_slack_failure_leaves_dedup_unadvanced(self) -> None:
        """An undelivered reason must not be remembered as delivered."""
        gw = _make_gw()
        gw.slack.post_message = AsyncMock(side_effect=RuntimeError("slack down"))
        job = _script_job()
        await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        assert job.last_failure_hash == ""
        assert job.last_status == "error"

    @pytest.mark.asyncio
    async def test_no_slack_configured_still_rings_and_dedups(self) -> None:
        gw = _make_gw()
        gw.slack = None
        job = _script_job()
        await _run_script(gw, job, {"status": "error", "error": CONFLICT})
        assert any("data-home conflict" in b for b in _bodies(gw))
        assert job.last_failure_hash, (
            "with no Slack the bell is the whole delivery, so dedup must advance "
            "or every fire re-notifies"
        )
