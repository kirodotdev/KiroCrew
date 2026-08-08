"""Tests for cron → Slack thread routing.

Verifies that _cron_callback stores thread_ts after posting, and that
_subagent_done routes cron-spawned subagent results via session injection.
"""

from __future__ import annotations

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.subagent import SubagentInfo

# ── Helpers (same pattern as test_cron_slack_delivery.py) ──


def _make_gateway():
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.sessions = MagicMock()
    gateway.sessions.get_pid = MagicMock(return_value=None)
    gateway.ctx_builder = MagicMock()
    gateway.slack = MagicMock()
    gateway.conv_log = None
    gateway.dashboard_state = MagicMock()
    gateway._owner_id = "U000"
    gateway._cron_injecting = {}
    gateway._no_crons = False
    gateway.subagent_mgr = MagicMock()
    gateway.subagent_mgr.running = []
    gateway.subagent_mgr.queued_count_for = MagicMock(return_value=0)
    gateway.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gateway.sessions.release = MagicMock()
    gateway.sessions.reset = AsyncMock()
    gateway.sessions.cancel_current = AsyncMock()
    gateway.sessions.set_thread = AsyncMock()
    gateway.sessions.set_channel = AsyncMock()
    gateway.sessions.get_thread = MagicMock(return_value=None)
    gateway.sessions.get_channel = MagicMock(return_value=None)
    gateway.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gateway.ctx_builder.hooks = MagicMock()
    gateway._interactive_approval = MagicMock(return_value="cb")
    gateway._cfg = MagicMock()
    gateway._cfg.agent.max_subagents = 4
    return gateway


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


def _run_callback(gateway, job, stream_result="done"):
    captured_callback = None

    async def fake_stream(client, msg, **kwargs):
        return stream_result

    with patch("kiro_crew.slack.gateway.stream_and_collect", fake_stream), patch(
        "kiro_crew.slack.gateway.CronService"
    ) as mock_cron_cls:

        def capture_cron(on_job=None, **kw):
            nonlocal captured_callback
            captured_callback = on_job
            service = MagicMock()
            service.start = AsyncMock()
            return service

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        async def _init_and_run():
            await gateway._init_cron()
            assert captured_callback is not None
            return await captured_callback(job)

        return asyncio.run(_init_and_run())


def _capture_subagent_done(gateway):
    """Init subagents on the gateway and return the captured _subagent_done."""
    captured_done = None

    with patch("kiro_crew.slack.gateway.SubagentManager") as mock_mgr_cls:

        def capture_manager(**kw):
            nonlocal captured_done
            captured_done = kw["on_done"]
            mgr = MagicMock()
            mgr.running = []
            mgr.queued_count_for = MagicMock(return_value=0)
            gateway.subagent_mgr = mgr
            return mgr

        mock_mgr_cls.side_effect = capture_manager
        gateway._init_subagents()

    assert captured_done is not None
    return captured_done


# ── Tests: _cron_callback stores thread_ts ──


class TestCronCallbackStoresThread:
    """_cron_callback must store thread_ts and channel after posting to Slack."""

    def test_stores_thread_ts_after_post(self) -> None:
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        _run_callback(gateway, _make_job())
        gateway.sessions.set_thread.assert_awaited_once_with("cron:j1", "1711957800.001234")

    def test_stores_channel_after_post(self) -> None:
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        _run_callback(gateway, _make_job(channel="C999"))
        gateway.sessions.set_channel.assert_awaited_once_with("cron:j1", "C999")

    def test_skips_storage_when_post_returns_none(self) -> None:
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value=None)
        _run_callback(gateway, _make_job())
        gateway.sessions.set_thread.assert_not_awaited()


# ── Tests: _subagent_done injects cron results via session ──


class TestSubagentDoneCronRouting:
    """_subagent_done must inject cron subagent results into the cron session."""

    @pytest.mark.asyncio
    async def test_injects_into_cron_session(self) -> None:
        gateway = _make_gateway()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(
            id="sub1",
            task="analyse data",
            parent_session_key="cron:j1",
        )
        info.result = "analysis complete"
        info.done = True
        with (
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="injected",
            ),
        ):
            await subagent_done(info)

        gateway.sessions.get_or_create.assert_awaited()
        gateway.sessions.release.assert_called_once_with("cron:j1")

    @pytest.mark.asyncio
    async def test_resets_session_when_last_subagent(self) -> None:
        gateway = _make_gateway()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(
            id="sub2",
            task="check status",
            parent_session_key="cron:j1",
        )
        info.result = "all good"
        info.done = True
        with (
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="done",
            ),
        ):
            await subagent_done(info)

        gateway.sessions.reset.assert_awaited_once_with("cron:j1")

    @pytest.mark.asyncio
    async def test_injection_failure_does_not_propagate(self) -> None:
        gateway = _make_gateway()
        gateway.sessions.get_or_create = AsyncMock(side_effect=Exception("session_down"))
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(
            id="sub3",
            task="risky task",
            parent_session_key="cron:j1",
        )
        info.result = "done"
        info.done = True
        with (
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
        ):
            # Should not raise
            await subagent_done(info)
        gateway.dashboard_state.notify.assert_called()

    @pytest.mark.asyncio
    async def test_retries_on_acp_error(self) -> None:
        """stream_and_collect retries on AcpError then succeeds."""
        from kiro_crew.acp.client import AcpError

        gateway = _make_gateway()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(id="sub4", task="retry task", parent_session_key="cron:j1")
        info.result = "ok"
        info.done = True

        call_count = 0

        async def flaky_stream(client, msg, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpError("transient 500")
            return "recovered"

        with (
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=flaky_stream),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await subagent_done(info)

        assert call_count == 2
        mock_sleep.assert_awaited_once_with(1)  # 2 ** 0
        gateway.sessions.cancel_current.assert_any_await("cron:j1")


# ── Tests: cancel_current called before release in _subagent_done ──


class TestSubagentDoneCancelsBeforeRelease:
    """_subagent_done must call cancel_current before release to prevent
    'Prompt already in progress' when wait_for cancels mid-stream."""

    def _patches(self):
        return (
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
        )

    @pytest.mark.asyncio
    async def test_cron_injection_cancels_before_release(self) -> None:
        gateway = _make_gateway()
        call_order: list[str] = []
        gateway.sessions.cancel_current = AsyncMock(side_effect=lambda k: call_order.append("cancel"))
        gateway.sessions.release = MagicMock(side_effect=lambda k: call_order.append("release"))
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(id="s1", task="work", parent_session_key="cron:j1")
        info.result = "done"
        info.done = True
        p1, p2 = self._patches()
        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", new_callable=AsyncMock, return_value="ok"),
            p1, p2,
        ):
            await subagent_done(info)
        assert call_order == ["cancel", "release"]

    @pytest.mark.asyncio
    async def test_slack_injection_cancels_before_release(self) -> None:
        gateway = _make_gateway()
        gateway.slack.open_dm = AsyncMock(return_value="D123")
        gateway.slack.post_message = AsyncMock()
        call_order: list[str] = []
        gateway.sessions.cancel_current = AsyncMock(side_effect=lambda k: call_order.append("cancel"))
        gateway.sessions.release = MagicMock(side_effect=lambda k: call_order.append("release"))
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(id="s3", task="work", parent_session_key="slack:U000")
        info.result = "done"
        info.done = True
        p1, p2 = self._patches()
        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", new_callable=AsyncMock, return_value="ok"),
            # gateway renders through the shared Slack pipeline now, so this is
            # the one seam to stub -- patching to_slack_mrkdwn/split_message
            # individually no longer intercepts anything.
            patch("kiro_crew.slack.gateway.render_for_slack", return_value=["ok"]),
            p1, p2,
        ):
            await subagent_done(info)
        assert call_order == ["cancel", "release"]

    @pytest.mark.asyncio
    async def test_cancel_failure_does_not_prevent_release(self) -> None:
        """If cancel_current raises, release must still be called."""
        gateway = _make_gateway()
        gateway.sessions.cancel_current = AsyncMock(side_effect=Exception("cancel failed"))
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(id="s4", task="work", parent_session_key="cron:j1")
        info.result = "done"
        info.done = True
        p1, p2 = self._patches()
        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", new_callable=AsyncMock, return_value="ok"),
            p1, p2,
        ):
            await subagent_done(info)
        gateway.sessions.release.assert_called_once_with("cron:j1")


# ── Tests: dashboard/Slack injection silently loses results on AcpError ──


class TestDashboardInjectionRoutesRunChat:
    """Dashboard subagent injection routes through _run_chat for full streaming."""

    def _patches(self):
        return (
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", side_effect=lambda s: (s, False)),
            patch("kiro_crew.slack.gateway.redact_credentials", side_effect=lambda s: (s, False)),
        )

    @pytest.mark.asyncio
    async def test_routes_through_run_chat_when_idle(self) -> None:
        """Idle slot triggers _run_chat with the announce message."""
        gateway = _make_gateway()
        slot = MagicMock()
        slot.running = False
        slot.key = "chat-1-123"
        slot.task = None
        gateway.dashboard_state.get_slot = MagicMock(return_value=slot)
        gateway.dashboard_state._background_tasks = set()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(id="ar1", task="review code", parent_session_key="dashboard:chat-1-123")
        info.result = "done"
        info.done = True
        p1, p2 = self._patches()
        with (
            patch("kiro_crew.slack.gateway._run_chat", new_callable=AsyncMock),
            p1, p2,
        ):
            await subagent_done(info)

        assert slot.task is not None, "slot.task must be set for running indicator"

    @pytest.mark.asyncio
    async def test_notification_when_slot_gone(self) -> None:
        """Missing slot falls through to notification-only path."""
        gateway = _make_gateway()
        gateway.dashboard_state.get_slot = MagicMock(return_value=None)
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(id="ar3", task="review code", parent_session_key="dashboard:chat-1-789")
        info.result = "done"
        info.done = True
        p1, p2 = self._patches()
        with p1, p2:
            await subagent_done(info)

        gateway.dashboard_state.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_busy_slot_awaits_then_injects(self) -> None:
        """Busy slot is awaited, then _run_chat starts after it finishes."""
        from unittest.mock import PropertyMock

        gateway = _make_gateway()
        slot = MagicMock()
        _done_future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        _done_future.set_result(None)
        # First access: True (enters busy branch), second access: False (re-check passes)
        type(slot).running = PropertyMock(side_effect=[True, False])
        slot.task = _done_future
        slot.key = "chat-1-busy"
        gateway.dashboard_state.get_slot = MagicMock(return_value=slot)
        gateway.dashboard_state._background_tasks = set()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(id="busy1", task="work", parent_session_key="dashboard:chat-1-busy")
        info.result = "done"
        info.done = True
        p1, p2 = self._patches()
        _mock_run_chat = AsyncMock(return_value=None)
        with (
            patch("kiro_crew.slack.gateway._run_chat", _mock_run_chat),
            p1, p2,
        ):
            await subagent_done(info)

        # _run_chat should have been triggered
        assert _mock_run_chat.called, "_run_chat must be called after busy slot becomes idle"
        assert slot.task is not _done_future, "slot.task must be reassigned to the new _run_chat task"

    @pytest.mark.asyncio
    async def test_busy_slot_timeout_queues_result(self) -> None:
        """If busy slot doesn't finish within timeout, result is queued."""
        gateway = _make_gateway()
        slot = MagicMock()
        slot.running = True  # stays busy even after timeout
        # Task that never completes
        _stuck = asyncio.get_running_loop().create_future()
        slot.task = _stuck
        slot.key = "chat-1-stuck"
        gateway.dashboard_state.get_slot = MagicMock(return_value=slot)
        gateway.dashboard_state._background_tasks = set()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(id="stuck1", task="work", parent_session_key="dashboard:chat-1-stuck")
        info.result = "done"
        info.done = True
        p1, p2 = self._patches()
        with (
            patch("kiro_crew.slack.gateway.INJECTION_TIMEOUT", 0.01),
            p1, p2,
        ):
            await subagent_done(info)

        slot.queue_append.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_callback_notifies_with_redacted_reason(self) -> None:
        """_on_inject_done calls notify_injection_failed with redacted reason."""
        gateway = _make_gateway()
        slot = MagicMock()
        slot.running = False
        slot.key = "chat-1-err"
        slot.task = None
        gateway.dashboard_state.get_slot = MagicMock(return_value=slot)
        gateway.dashboard_state._background_tasks = set()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(id="err1", task="work", parent_session_key="dashboard:chat-1-err")
        info.result = "done"
        info.done = True

        # Make _run_chat raise an error
        _mock_run_chat = AsyncMock(side_effect=RuntimeError("provider crashed"))
        p1, p2 = self._patches()
        with (
            patch("kiro_crew.slack.gateway._run_chat", _mock_run_chat),
            p1, p2,
        ):
            await subagent_done(info)
            # Wait for the task's done callbacks to fire (may need multiple event-loop ticks under load)
            for _ in range(20):
                await asyncio.sleep(0.05)
                if gateway.subagent_mgr.notify_injection_failed.call_count:
                    break

        gateway.subagent_mgr.notify_injection_failed.assert_called_once()
        call_kwargs = gateway.subagent_mgr.notify_injection_failed.call_args
        assert "provider crashed" in str(call_kwargs)


# ── Tests: _cron_callback dashboard slot auto-inject ──


class TestCronCallbackDashboardChat:
    """_cron_callback must call inject_cron_result_to_dashboard when persistent_session=True and slot exists."""

    def test_persistent_session_with_slot_calls_inject(self) -> None:
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        gateway.dashboard_state.has_slot = MagicMock(return_value=True)
        job = _make_job(persistent_session=True)
        with patch(
            "kiro_crew.slack.gateway.inject_cron_result_to_dashboard"
        ) as mock_inject:
            _run_callback(gateway, job, stream_result="cron output")
            mock_inject.assert_called_once_with(
                gateway.dashboard_state, job, "cron output", history=ANY,
                context_reading=ANY,
            )

    def test_persistent_session_without_slot_still_injects(self) -> None:
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        gateway.dashboard_state.has_slot = MagicMock(return_value=False)
        job = _make_job(persistent_session=True)
        with patch(
            "kiro_crew.slack.gateway.inject_cron_result_to_dashboard"
        ) as mock_inject:
            _run_callback(gateway, job, stream_result="cron output")
            mock_inject.assert_called_once_with(
                gateway.dashboard_state, job, "cron output", history=ANY,
                context_reading=ANY,
            )

    def test_non_persistent_session_does_not_inject(self) -> None:
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        gateway.dashboard_state.has_slot = MagicMock(return_value=True)
        job = _make_job(persistent_session=False)
        with patch(
            "kiro_crew.slack.gateway.inject_cron_result_to_dashboard"
        ) as mock_inject:
            _run_callback(gateway, job, stream_result="cron output")
            mock_inject.assert_not_called()

    def test_inject_fires_on_dedup_suppression(self) -> None:
        """Even when Slack dedup suppresses delivery, dashboard slot still gets updated."""
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        gateway.dashboard_state.has_slot = MagicMock(return_value=True)
        job = _make_job(persistent_session=True)
        # Simulate dedup: set last_posted_hash to match result
        job.last_posted_hash = ""  # first run posts normally
        with patch(
            "kiro_crew.slack.gateway.inject_cron_result_to_dashboard"
        ) as mock_inject:
            # First run — normal path
            _run_callback(gateway, job, stream_result="same result")
            first_call_count = mock_inject.call_count
            # Set up dedup state for second run
            from kiro_crew.slack.gateway import _result_hash

            job.last_posted_hash = _result_hash("same result")
            job.last_posted_at = 9999999999.0  # far future so reminder doesn't trigger
            # Second run — dedup suppresses Slack but inject should still fire
            _run_callback(gateway, job, stream_result="same result")
            assert mock_inject.call_count == first_call_count + 1

    def test_silent_cron_injects_to_existing_slot(self) -> None:
        """Silent cron with existing slot still injects result to dashboard."""
        gateway = _make_gateway()
        gateway.dashboard_state.has_slot = MagicMock(return_value=True)
        job = _make_job(persistent_session=True, silent=True)
        with patch(
            "kiro_crew.slack.gateway.inject_cron_result_to_dashboard"
        ) as mock_inject:
            _run_callback(gateway, job, stream_result="silent output")
            mock_inject.assert_called_once_with(
                gateway.dashboard_state, job, "silent output", context_reading=ANY
            )

    def test_silent_cron_no_slot_does_not_inject(self) -> None:
        """Silent cron without existing slot does not inject."""
        gateway = _make_gateway()
        gateway.dashboard_state.has_slot = MagicMock(return_value=False)
        job = _make_job(persistent_session=True, silent=True)
        with patch(
            "kiro_crew.slack.gateway.inject_cron_result_to_dashboard"
        ) as mock_inject:
            _run_callback(gateway, job, stream_result="silent output")
            mock_inject.assert_not_called()

    # ── hide_in_chat gate (the feature's core behavioral change) ──

    def test_hide_in_chat_suppresses_inject_on_normal_path(self) -> None:
        """hide_in_chat=True must NOT create/inject a slot on the normal delivery
        path, even for a persistent_session job (the slot-creator site)."""
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        gateway.dashboard_state.has_slot = MagicMock(return_value=False)
        job = _make_job(persistent_session=True, hide_in_chat=True)
        with patch(
            "kiro_crew.slack.gateway.inject_cron_result_to_dashboard"
        ) as mock_inject:
            _run_callback(gateway, job, stream_result="hidden output")
            mock_inject.assert_not_called()

    def test_hide_in_chat_false_still_injects(self) -> None:
        """Inverse: hide_in_chat=False (default) still injects on the normal path —
        locks both directions of the gate."""
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        gateway.dashboard_state.has_slot = MagicMock(return_value=False)
        job = _make_job(persistent_session=True, hide_in_chat=False)
        with patch(
            "kiro_crew.slack.gateway.inject_cron_result_to_dashboard"
        ) as mock_inject:
            _run_callback(gateway, job, stream_result="shown output")
            mock_inject.assert_called_once_with(
                gateway.dashboard_state, job, "shown output", history=ANY,
                context_reading=ANY,
            )

    def test_hide_in_chat_suppresses_inject_even_with_existing_slot(self) -> None:
        """hide_in_chat=True must suppress the re-inject paths too: even if a stale
        slot already exists (has_slot True), no injection fires."""
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        gateway.dashboard_state.has_slot = MagicMock(return_value=True)
        job = _make_job(persistent_session=True, hide_in_chat=True)
        with patch(
            "kiro_crew.slack.gateway.inject_cron_result_to_dashboard"
        ) as mock_inject:
            _run_callback(gateway, job, stream_result="hidden output")
            mock_inject.assert_not_called()

    def test_hide_in_chat_silent_cron_does_not_inject(self) -> None:
        """A silent + hidden cron with an existing slot must not re-inject
        (the silent path is also gated on not hide_in_chat)."""
        gateway = _make_gateway()
        gateway.dashboard_state.has_slot = MagicMock(return_value=True)
        job = _make_job(persistent_session=True, silent=True, hide_in_chat=True)
        with patch(
            "kiro_crew.slack.gateway.inject_cron_result_to_dashboard"
        ) as mock_inject:
            _run_callback(gateway, job, stream_result="silent hidden output")
            mock_inject.assert_not_called()

    @staticmethod
    def _result_notify_meta(notify_mock):
        """Return the meta dict of the cron-RESULT notify call (the one carrying
        notify_meta with job_id but no failure_hash). The callback may emit other
        notify calls (e.g. dedup ⚡ first-run), so select rather than assume one."""
        for call in notify_mock.call_args_list:
            meta = call.kwargs.get("meta", {}) or {}
            if meta.get("job_id") == "j1" and "failure_hash" not in meta:
                return meta
        raise AssertionError(
            f"no cron-result notify found; calls={notify_mock.call_args_list}"
        )

    def test_hide_in_chat_notify_meta_omits_slot_even_with_existing_slot(self) -> None:
        """notify_meta['slot'] is gated on not hide_in_chat: a hidden cron that
        still owns a stale cron-{id} slot must NOT emit meta.slot, so the
        notification CTA degrades to the no-slot 'View last result'."""
        gateway = _make_gateway()
        gateway.dashboard_state.has_slot = MagicMock(return_value=True)
        job = _make_job(persistent_session=True, hide_in_chat=True)
        with patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard"):
            _run_callback(gateway, job, stream_result="hidden output")
        meta = self._result_notify_meta(gateway.dashboard_state.notify)
        assert "slot" not in meta

    def test_shown_cron_notify_meta_includes_slot(self) -> None:
        """Inverse: a shown (hide_in_chat=False) persistent cron with a slot DOES
        emit meta.slot, so the CTA is 'Continue session'."""
        gateway = _make_gateway()
        gateway.dashboard_state.has_slot = MagicMock(return_value=True)
        job = _make_job(persistent_session=True, hide_in_chat=False)
        with patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard"):
            _run_callback(gateway, job, stream_result="shown output")
        meta = self._result_notify_meta(gateway.dashboard_state.notify)
        assert meta.get("slot") == "cron-j1"
