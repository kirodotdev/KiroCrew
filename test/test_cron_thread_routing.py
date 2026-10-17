"""Tests for cron → Slack thread routing.

Verifies that _cron_callback stores thread_ts after posting, and that
_subagent_done routes cron-spawned subagent results via session injection.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.subagent import SubagentInfo

# ── Helpers (same pattern as test_cron_slack_delivery.py) ──


def _make_gateway():
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.sessions = MagicMock()
    gateway.sessions.get_pid = MagicMock(return_value=None)
    gateway.ctx_builder = MagicMock()
    gateway.ctx_builder.conversation_log.get_metadata_status.return_value = ({}, True)
    gateway.slack = MagicMock()
    gateway.conv_log = None
    gateway.dashboard_state = MagicMock()
    gateway._owner_id = "U000"
    gateway._cron_injecting = {}
    gateway._no_crons = False
    gateway.subagent_mgr = MagicMock()
    gateway.subagent_mgr.running = []
    gateway.subagent_mgr.queued_count_for = MagicMock(return_value=0)
    gateway.subagent_mgr.queued_count_for_async = AsyncMock(return_value=0)
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
            mgr.queued_count_for_async = AsyncMock(return_value=0)
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

    # ── Regression: a cron must NOT claim a thread it did not create ──
    #
    # When a run delivers into an INHERITED or explicit ``job.thread_ts`` -- a
    # human's own Slack thread, keyed ``slack:<ts>`` -- claiming it for
    # ``cron:<id>`` via ``set_thread`` evicts the human's owner from the
    # thread->session index permanently (``cron:`` is not self-derived, so
    # ``set_slack_link`` sweeps rivals), and the human's next message in their
    # own thread then runs in the cron session. The cron may claim the thread
    # ONLY when it created it itself (no ``job.thread_ts``, so ``thread_root`` is
    # this run's own newly-posted ``parent_ts``).

    def test_does_not_claim_inherited_thread(self) -> None:
        # job.thread_ts is set (inherited from the caller or explicit): the cron
        # posts INTO the human's thread but must NOT claim it. The anchor it
        # writes is empty (clearing any stale ts), never the human's thread_ts.
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        _run_callback(gateway, _make_job(thread_ts="1699999999.000111"))
        for call in gateway.sessions.set_thread.await_args_list:
            assert (
                call.args[1] == ""
            ), "cron claimed/kept a non-empty thread anchor for an inherited thread"

    def test_clears_stale_anchor_for_inherited_thread(self) -> None:
        # Scenario: cron:<id> already holds a stale thread anchor from a
        # top-level run, and the job now carries an explicit thread_ts. The
        # callback writes an empty anchor so the readers resolve the job's own
        # thread_ts via _cron_outbound_thread, never the stale ts.
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        _run_callback(gateway, _make_job(thread_ts="1699999999.000111"))
        gateway.sessions.set_thread.assert_awaited_once_with("cron:j1", "")

    def test_still_binds_channel_for_inherited_thread(self) -> None:
        # The channel bind is harmless (set_channel writes an empty ts for an
        # unclaimed cron, which evicts no one) and still happens, so subagent
        # replies can resolve the cron's channel.
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        _run_callback(gateway, _make_job(channel="C999", thread_ts="1699999999.000111"))
        gateway.sessions.set_channel.assert_awaited_once_with("cron:j1", "C999")

    def test_overflow_still_threads_under_inherited_thread(self) -> None:
        # Not claiming the thread must not stop overflow parts from threading:
        # they still post under the inherited thread_ts.
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        gateway.slack.post_message = AsyncMock()
        long = "x" * 60000  # forces render_for_slack to split into overflow parts
        _run_callback(gateway, _make_job(thread_ts="1699999999.000111"), stream_result=long)
        assert gateway.slack.post_message.await_count >= 1, "expected overflow parts"
        for call in gateway.slack.post_message.await_args_list:
            assert call.args[2] == "1699999999.000111", "overflow part did not thread"

    def test_claims_self_created_thread(self) -> None:
        # Inverse / unchanged behavior: with NO job.thread_ts the cron created
        # the thread itself (parent_ts), so it legitimately claims it.
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        _run_callback(gateway, _make_job(thread_ts=None))
        gateway.sessions.set_thread.assert_awaited_once_with("cron:j1", "1711957800.001234")


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
        gateway.sessions.cancel_current.assert_any_await("cron:j1", owner_task=ANY)


# ── Tests: a threaded cron's follow-ups must thread, not post top-level ──


class TestCronResponseThreadsUnderInheritedThread:
    """A threaded cron does not CLAIM its inherited thread, so cron:<id> has no
    recorded outbound anchor in the session map. _deliver_cron_response must
    still thread subagent-completion follow-ups under the job's own thread_ts
    rather than posting them at the channel top level (buluoray's blocking
    finding at 5e3aaff6: the regression the thread-takeover guard introduced).
    """

    def _gateway_with_job(self, *, thread_ts):
        gateway = _make_gateway()
        # The channel bind is kept for an inherited-thread cron; the thread
        # anchor is NOT (claiming it would evict the human owner). Reproduce
        # exactly that session-map state.
        gateway.sessions.get_channel = MagicMock(return_value="C123")
        gateway.sessions.get_thread = MagicMock(return_value=None)
        # The job still carries the authoritative thread_ts.
        job = _make_job(channel="C123", thread_ts=thread_ts)
        gateway.cron_svc = MagicMock()
        gateway.cron_svc.get_job = MagicMock(return_value=job)
        # Channel leg does not deliver, so the Slack leg runs.
        gateway._deliver_cron_to_channel = AsyncMock(return_value=False)
        gateway.slack.post_message = AsyncMock()
        gateway.slack.post_blocks = AsyncMock(return_value="ts.block")
        gateway._remember_options = MagicMock()
        return gateway

    @pytest.mark.asyncio
    async def test_threads_subagent_response_under_job_thread_ts(self) -> None:
        gateway = self._gateway_with_job(thread_ts="1699999999.000111")
        await gateway._deliver_cron_response("cron:j1", "subagent finished")
        gateway.slack.post_message.assert_awaited()
        for call in gateway.slack.post_message.await_args_list:
            assert call.args[2] == "1699999999.000111", (
                "cron follow-up posted at channel top level instead of the "
                "job's inherited thread"
            )

    @pytest.mark.asyncio
    async def test_no_fallback_when_job_has_no_thread(self) -> None:
        # A cron that posted its own thread (no job.thread_ts) legitimately has
        # no inherited anchor to fall back to; it must not invent one.
        gateway = self._gateway_with_job(thread_ts=None)
        await gateway._deliver_cron_response("cron:j1", "subagent finished")
        gateway.slack.post_message.assert_awaited()
        for call in gateway.slack.post_message.await_args_list:
            assert call.args[2] is None, "fabricated a thread_ts for a self-posted cron"


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
        gateway.sessions.cancel_current = AsyncMock(
            side_effect=lambda k, **_kw: call_order.append("cancel")
        )
        gateway.sessions.release = MagicMock(side_effect=lambda k: call_order.append("release"))
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(id="s1", task="work", parent_session_key="cron:j1")
        info.result = "done"
        info.done = True
        p1, p2 = self._patches()
        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            p1,
            p2,
        ):
            await subagent_done(info)
        assert call_order == ["cancel", "release"]

    @pytest.mark.asyncio
    async def test_slack_injection_cancels_before_release(self) -> None:
        gateway = _make_gateway()
        gateway.slack.open_dm = AsyncMock(return_value="D123")
        gateway.slack.post_message = AsyncMock()
        call_order: list[str] = []
        gateway.sessions.cancel_current = AsyncMock(
            side_effect=lambda k, **_kw: call_order.append("cancel")
        )
        gateway.sessions.release = MagicMock(side_effect=lambda k: call_order.append("release"))
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(id="s3", task="work", parent_session_key="slack:U000")
        info.result = "done"
        info.done = True
        p1, p2 = self._patches()
        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            # gateway renders through the shared Slack pipeline now, so this is
            # the one seam to stub -- patching to_slack_mrkdwn/split_message
            # individually does not intercept anything.
            patch("kiro_crew.slack.gateway.render_for_slack", return_value=["ok"]),
            p1,
            p2,
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
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            p1,
            p2,
        ):
            await subagent_done(info)
        gateway.sessions.release.assert_called_once_with("cron:j1")


# ── Tests: dashboard/Slack injection silently loses results on AcpError ──


class TestDashboardInjectionRoutesRunChat:
    """Dashboard subagent injection routes through _run_chat for full streaming."""

    def _patches(self):
        return (
            patch(
                "kiro_crew.slack.gateway.redact_exfiltration_urls", side_effect=lambda s: (s, False)
            ),
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
            p1,
            p2,
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
            p1,
            p2,
        ):
            await subagent_done(info)

        # _run_chat should have been triggered
        assert _mock_run_chat.called, "_run_chat must be called after busy slot becomes idle"
        assert (
            slot.task is not _done_future
        ), "slot.task must be reassigned to the new _run_chat task"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("busy_after_wait", (False, True), ids=("inline", "busy-recheck"))
    async def test_inline_collection_releases_manager_ownership_once(
        self, busy_after_wait: bool
    ) -> None:
        """Inline collection after a wait balances the sole ownership count."""
        from unittest.mock import PropertyMock

        gateway = _make_gateway()
        slot = MagicMock()
        slot.key = "chat-1-inline"
        slot.task = asyncio.get_running_loop().create_future()
        slot.task.set_result(None)
        slot._subagents_inline_collected = set()
        slot._pending_synthesis = True
        checks = 0

        def _running() -> bool:
            nonlocal checks
            checks += 1
            if checks == 2:
                slot._subagents_inline_collected.add("inline1")
            return True if checks == 1 else busy_after_wait

        type(slot).running = PropertyMock(side_effect=_running)
        gateway.dashboard_state.get_slot = MagicMock(return_value=slot)
        gateway.dashboard_state._background_tasks = set()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(
            id="inline1",
            task="work",
            parent_session_key="dashboard:chat-1-inline",
        )
        info.result = "done"
        info.done = True
        p1, p2 = self._patches()

        with p1, p2:
            await subagent_done(info)

        gateway.subagent_mgr.retain_completion_delivery.assert_called_once_with(
            info.parent_session_key, info.id
        )
        gateway.subagent_mgr.release_completion_delivery.assert_called_once_with(
            info.parent_session_key, info.id
        )
        assert info._report_delivered is True
        assert info.id not in slot._subagents_inline_collected
        assert slot._pending_synthesis is False

    @pytest.mark.asyncio
    async def test_busy_slot_removed_settles_retained_completion(self) -> None:
        """A close during the busy wait discards durably instead of stranding ownership."""
        gateway = _make_gateway()
        slot = _ChatSlot("chat-1-removed")
        busy_release = asyncio.Event()
        slot.task = asyncio.create_task(busy_release.wait())
        gateway.dashboard_state.get_slot = MagicMock(return_value=slot)
        gateway.dashboard_state._background_tasks = set()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(
            id="removed1",
            task="work",
            parent_session_key="dashboard:chat-1-removed",
        )
        info.result = "done"
        info.done = True
        settled: list[list[str]] = []

        async def _settle(deliveries) -> None:
            settled.append([delivery.agent_id for delivery in deliveries])

        gateway.subagent_mgr.settle_queued_delivery = _settle
        run_chat = AsyncMock()
        p1, p2 = self._patches()
        with (
            patch("kiro_crew.slack.gateway._run_chat", run_chat),
            patch(
                "kiro_crew.slack.gateway.read_tombstone",
                return_value={"cause": "delivered"},
            ),
            p1,
            p2,
        ):
            delivery = asyncio.create_task(subagent_done(info))
            for _ in range(20):
                await asyncio.sleep(0)
                if gateway.subagent_mgr.retain_completion_delivery.called:
                    break
            gateway.subagent_mgr.retain_completion_delivery.assert_called_once_with(
                info.parent_session_key,
                info.id,
            )

            gateway.dashboard_state.get_slot.return_value = None
            busy_release.set()
            await delivery
            background = tuple(gateway.dashboard_state._background_tasks)
            if background:
                await asyncio.gather(*background)

        run_chat.assert_not_awaited()
        assert settled == [[info.id]]
        assert slot._subagent_delivery_pending == {}
        gateway.subagent_mgr.release_completion_delivery.assert_called_once_with(
            info.parent_session_key,
            info.id,
        )

    @pytest.mark.asyncio
    async def test_cancel_during_busy_wait_releases_transferred_ownership(self) -> None:
        """Cancellation before queue/runner handoff leaves no completion fence."""
        gateway = _make_gateway()
        slot = _ChatSlot("chat-1-cancelled-wait")
        busy_release = asyncio.Event()
        slot.task = asyncio.create_task(busy_release.wait())
        gateway.dashboard_state.get_slot = MagicMock(return_value=slot)
        gateway.dashboard_state._background_tasks = set()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(
            id="cancelled-wait",
            task="work",
            parent_session_key="dashboard:chat-1-cancelled-wait",
        )
        info.result = "done"
        info.done = True
        run_chat = AsyncMock()
        p1, p2 = self._patches()
        with patch("kiro_crew.slack.gateway._run_chat", run_chat), p1, p2:
            delivery = asyncio.create_task(subagent_done(info))
            for _ in range(20):
                await asyncio.sleep(0)
                if gateway.subagent_mgr.retain_completion_delivery.called:
                    break
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
            busy_release.set()
            await slot.task

        gateway.subagent_mgr.release_completion_delivery.assert_called_once_with(
            info.parent_session_key,
            info.id,
        )
        run_chat.assert_not_awaited()

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
            p1,
            p2,
        ):
            await subagent_done(info)

        slot.queue_insert.assert_called_once()
        queued = slot.queue_insert.call_args
        assert queued.args[0] == 0
        assert callable(queued.kwargs["on_consumed"])
        assert callable(queued.kwargs["on_discarded"])

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
            p1,
            p2,
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

    @pytest.mark.asyncio
    async def test_lifecycle_recovery_with_owned_row_suppresses_duplicate_failure_fallback(
        self,
    ) -> None:
        """A timeout suppresses fallback only after a callback-owned row exists."""
        from kiro_crew.dashboard.chat_utils import LIFECYCLE_RECOVERY_KIND
        from kiro_crew.dashboard.state import _ChatSlot

        gateway = _make_gateway()
        slot = _ChatSlot("chat-1-recovery")
        gateway.dashboard_state.get_slot = MagicMock(return_value=slot)
        gateway.dashboard_state._background_tasks = set()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(
            id="recovery1", task="work", parent_session_key="dashboard:chat-1-recovery"
        )
        info.result = "done"
        info.done = True

        async def _retained_then_timeout(*_args, **kwargs) -> None:
            slot.queue_insert(
                0,
                "recovery",
                kind=LIFECYCLE_RECOVERY_KIND,
                on_discarded=kwargs["_on_discarded"],
            )
            kwargs["_on_lifecycle_recovery_retained"]()
            raise TimeoutError("turn deadline")

        p1, p2 = self._patches()
        with (
            patch("kiro_crew.slack.gateway._run_chat", new=_retained_then_timeout),
            patch("kiro_crew.slack.gateway._arm_queued_delivery_settlement"),
            p1,
            p2,
        ):
            await subagent_done(info)
            tasks = tuple(gateway.dashboard_state._background_tasks)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        assert slot._queue[0]["kind"] == LIFECYCLE_RECOVERY_KIND
        gateway.subagent_mgr.notify_injection_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_lifecycle_signal_without_owned_row_uses_failure_fallback(self) -> None:
        """A callback signal alone cannot suppress the sole recoverable owner."""
        gateway = _make_gateway()
        slot = MagicMock()
        slot.running = False
        slot.key = "chat-1-no-row"
        slot.task = None
        slot._queue = []
        gateway.dashboard_state.get_slot = MagicMock(return_value=slot)
        gateway.dashboard_state._background_tasks = set()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(id="no-row", task="work", parent_session_key="dashboard:chat-1-no-row")
        info.result = "done"
        info.done = True

        async def _signal_then_timeout(*_args, **kwargs) -> None:
            kwargs["_on_lifecycle_recovery_retained"]()
            raise TimeoutError("queue insertion failed")

        p1, p2 = self._patches()
        with (
            patch("kiro_crew.slack.gateway._run_chat", new=_signal_then_timeout),
            patch("kiro_crew.slack.gateway._arm_queued_delivery_settlement"),
            p1,
            p2,
        ):
            await subagent_done(info)
            tasks = tuple(gateway.dashboard_state._background_tasks)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        gateway.subagent_mgr.notify_injection_failed.assert_called_once()

    @pytest.mark.parametrize(
        ("settlement", "durable", "owner_released", "retained_reason"),
        (
            ("success", True, True, None),
            ("success", False, False, "tombstone_not_durable"),
            ("error", False, False, "settlement_failed"),
            ("cancel", False, False, "settlement_cancelled"),
        ),
        ids=("durable", "missing-tombstone", "error", "cancel"),
    )
    @pytest.mark.asyncio
    async def test_cancel_before_runner_entry_releases_only_after_durable_tombstone(
        self, settlement, durable, owner_released, retained_reason
    ) -> None:
        """Pre-entry discard keeps ownership through settlement failure/cancel.

        Every fail-closed branch also leaves one structured SEL row naming the
        parent session, the child, and the reason: the retained fence has no
        in-process retry, so a log line alone would leave the next stop
        refusal unexplained until a restart reconciles it.
        """
        from kiro_crew.dashboard.state import _ChatSlot

        gateway = _make_gateway()
        slot = _ChatSlot(f"chat-1-pre-entry-{settlement}-{durable}")
        gateway.dashboard_state.get_slot = MagicMock(return_value=slot)
        gateway.dashboard_state._background_tasks = set()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(
            id=f"pre-entry-{settlement}-{durable}",
            task="work",
            parent_session_key=f"dashboard:{slot.key}",
        )
        info.result = "done"
        info.done = True
        bounded_started = asyncio.Event()
        settle_started = asyncio.Event()
        settle_release = asyncio.Event()

        async def _hold_before_runner(coro, **_bounded_kwargs: Any) -> None:
            bounded_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                coro.close()

        async def _settle_queued_delivery(_owed) -> None:
            settle_started.set()
            if settlement == "error":
                raise RuntimeError("tombstone writer failed")
            await settle_release.wait()

        gateway.subagent_mgr.settle_queued_delivery = _settle_queued_delivery
        run_chat = AsyncMock()
        mock_sel = MagicMock()
        p1, p2 = self._patches()
        with (
            patch("kiro_crew.slack.gateway.bounded_chat_turn", new=_hold_before_runner),
            patch("kiro_crew.slack.gateway._run_chat", run_chat),
            patch("kiro_crew.slack.gateway._arm_queued_delivery_settlement"),
            patch(
                "kiro_crew.slack.gateway.read_tombstone",
                return_value={"cause": "delivered"} if durable else None,
            ),
            patch("kiro_crew.slack.gateway.sel", return_value=mock_sel),
            p1,
            p2,
        ):
            await subagent_done(info)
            gateway.subagent_mgr.retain_completion_delivery.assert_called_once_with(
                info.parent_session_key, info.id
            )
            await asyncio.wait_for(bounded_started.wait(), timeout=30)
            assert slot.task is not None
            slot.task.cancel()
            await asyncio.gather(slot.task, return_exceptions=True)
            await asyncio.wait_for(settle_started.wait(), timeout=30)

            # The discarded row is already gone, but the ownership fence stays
            # until the writer has produced durable evidence.
            writer = next(iter(gateway.dashboard_state._background_tasks), None)
            if settlement == "cancel":
                assert writer is not None
                writer.cancel()
            elif settlement == "success":
                settle_release.set()
            if writer is not None:
                await asyncio.gather(writer, return_exceptions=True)

        run_chat.assert_not_awaited()
        assert (gateway.subagent_mgr.release_completion_delivery.call_count == 1) is owner_released
        if owner_released:
            gateway.subagent_mgr.release_completion_delivery.assert_called_once_with(
                info.parent_session_key, info.id
            )
        else:
            gateway.subagent_mgr.release_completion_delivery.assert_not_called()
        gateway.subagent_mgr.notify_injection_failed.assert_not_called()

        if retained_reason is None:
            mock_sel.log_tool_invocation.assert_not_called()
        else:
            mock_sel.log_tool_invocation.assert_called_once()
            audit = mock_sel.log_tool_invocation.call_args.kwargs
            assert audit["session_key"] == info.parent_session_key
            assert audit["source"] == "subagent"
            assert audit["tool_name"] == "subagent_completion_discard"
            assert audit["outcome"] == "retained"
            assert audit["metadata"] == {"subagent_id": info.id, "reason": retained_reason}

    @pytest.mark.asyncio
    async def test_report_owed_discard_settles_store_without_a_tombstone(self) -> None:
        """A memory-wait report has store debt, not a result-folder tombstone."""
        from kiro_crew.dashboard.state import _ChatSlot

        gateway = _make_gateway()
        slot = _ChatSlot("chat-1-report-owed-discard")
        gateway.dashboard_state.get_slot = MagicMock(return_value=slot)
        gateway.dashboard_state._background_tasks = set()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(
            id="report-owed-discard",
            task="work",
            parent_session_key=f"dashboard:{slot.key}",
        )
        info.error = "memory wait expired"
        info.done = True
        info._report_owed = True
        bounded_started = asyncio.Event()

        async def _hold_before_runner(coro, **_bounded_kwargs: Any) -> None:
            bounded_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                coro.close()

        settle = AsyncMock()
        gateway.subagent_mgr.settle_queued_delivery = settle
        run_chat = AsyncMock()
        p1, p2 = self._patches()
        with (
            patch("kiro_crew.slack.gateway.bounded_chat_turn", new=_hold_before_runner),
            patch("kiro_crew.slack.gateway._run_chat", run_chat),
            patch("kiro_crew.slack.gateway._arm_queued_delivery_settlement"),
            patch("kiro_crew.slack.gateway.read_tombstone") as read_tombstone,
            p1,
            p2,
        ):
            await subagent_done(info)
            await asyncio.wait_for(bounded_started.wait(), timeout=30)
            assert slot.task is not None
            slot.task.cancel()
            await asyncio.gather(slot.task, return_exceptions=True)
            writers = tuple(gateway.dashboard_state._background_tasks)
            await asyncio.gather(*writers, return_exceptions=True)

        settle.assert_awaited_once()
        owed = settle.await_args.args[0]
        assert [delivery.agent_id for delivery in owed] == [info.id]
        assert all(delivery.report_owed for delivery in owed)
        read_tombstone.assert_not_called()
        gateway.subagent_mgr.release_completion_delivery.assert_called_once_with(
            info.parent_session_key, info.id
        )
        run_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retained_completion_audit_failure_keeps_fail_closed_decision(self) -> None:
        """An unwritable SEL must not alter the retain: no release, no raise."""
        from kiro_crew.dashboard.state import _ChatSlot

        gateway = _make_gateway()
        slot = _ChatSlot("chat-1-pre-entry-audit-broken")
        gateway.dashboard_state.get_slot = MagicMock(return_value=slot)
        gateway.dashboard_state._background_tasks = set()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(
            id="pre-entry-audit-broken",
            task="work",
            parent_session_key=f"dashboard:{slot.key}",
        )
        info.result = "done"
        info.done = True
        bounded_started = asyncio.Event()

        async def _hold_before_runner(coro, **_bounded_kwargs: Any) -> None:
            bounded_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                coro.close()

        gateway.subagent_mgr.settle_queued_delivery = AsyncMock(
            side_effect=RuntimeError("tombstone writer failed")
        )
        broken_sel = MagicMock(side_effect=OSError("sel unwritable"))
        run_chat = AsyncMock()
        p1, p2 = self._patches()
        with (
            patch("kiro_crew.slack.gateway.bounded_chat_turn", new=_hold_before_runner),
            patch("kiro_crew.slack.gateway._run_chat", run_chat),
            patch("kiro_crew.slack.gateway._arm_queued_delivery_settlement"),
            patch("kiro_crew.slack.gateway.sel", broken_sel),
            p1,
            p2,
        ):
            await subagent_done(info)
            await asyncio.wait_for(bounded_started.wait(), timeout=30)
            assert slot.task is not None
            slot.task.cancel()
            await asyncio.gather(slot.task, return_exceptions=True)
            writer = next(iter(gateway.dashboard_state._background_tasks))
            outcome = await asyncio.gather(writer, return_exceptions=True)

        # The writer swallowed the audit failure rather than surfacing it, and
        # the fence is still retained exactly as it would be with a healthy SEL.
        assert outcome == [None]
        broken_sel.assert_called_once()
        gateway.subagent_mgr.release_completion_delivery.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_before_runner_entry_with_owed_debt_and_no_manager_retains_ownership(
        self,
    ) -> None:
        """Owed debt but no manager to settle it must fail closed, not release."""
        from kiro_crew.dashboard.state import _ChatSlot

        gateway = _make_gateway()
        slot = _ChatSlot("chat-1-pre-entry-no-manager")
        gateway.dashboard_state.get_slot = MagicMock(return_value=slot)
        gateway.dashboard_state._background_tasks = set()
        subagent_done = _capture_subagent_done(gateway)
        info = SubagentInfo(
            id="pre-entry-no-manager",
            task="work",
            parent_session_key=f"dashboard:{slot.key}",
        )
        info.result = "done"
        info.done = True
        bounded_started = asyncio.Event()

        # A spy that must never run: with no manager, settlement is unreachable.
        settle_spy = AsyncMock()
        gateway.subagent_mgr.settle_queued_delivery = settle_spy

        async def _hold_before_runner(coro, **_bounded_kwargs: Any) -> None:
            bounded_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                coro.close()

        run_chat = AsyncMock()
        mock_sel = MagicMock()
        p1, p2 = self._patches()
        with (
            patch("kiro_crew.slack.gateway.bounded_chat_turn", new=_hold_before_runner),
            patch("kiro_crew.slack.gateway._run_chat", run_chat),
            patch("kiro_crew.slack.gateway._arm_queued_delivery_settlement"),
            patch("kiro_crew.slack.gateway.sel", return_value=mock_sel),
            p1,
            p2,
        ):
            await subagent_done(info)
            owner_manager = gateway.subagent_mgr
            owner_manager.retain_completion_delivery.assert_called_once_with(
                info.parent_session_key, info.id
            )
            await asyncio.wait_for(bounded_started.wait(), timeout=30)
            assert slot.task is not None
            # Precondition: the direct-injection path recorded delivery debt for
            # this completed member, so the discard has real debt to weigh.
            assert slot._subagent_delivery_pending, "precondition: debt was deferred"
            # The manager is gone by the time the discard fires.
            gateway.subagent_mgr = None
            slot.task.cancel()
            await asyncio.gather(slot.task, return_exceptions=True)
            await asyncio.sleep(0)
            # No settlement writer may be spawned when there is no manager.
            assert not gateway.dashboard_state._background_tasks

        run_chat.assert_not_awaited()
        # Fail closed: the completion fence is retained (ownership kept) and no
        # settlement was attempted, so no "delivered" tombstone is claimed and a
        # restart can still recover the result rather than dropping it.
        owner_manager.release_completion_delivery.assert_not_called()
        settle_spy.assert_not_awaited()
        # The retain is operator-visible on the same SEL row as the settlement
        # branches, with the reason that distinguishes this one.
        mock_sel.log_tool_invocation.assert_called_once()
        audit = mock_sel.log_tool_invocation.call_args.kwargs
        assert audit["session_key"] == info.parent_session_key
        assert audit["tool_name"] == "subagent_completion_discard"
        assert audit["outcome"] == "retained"
        assert audit["metadata"] == {"subagent_id": info.id, "reason": "no_manager"}


# ── Tests: _cron_callback dashboard slot auto-inject ──


class TestCronCallbackDashboardChat:
    """_cron_callback must call inject_cron_result_to_dashboard when persistent_session=True and slot exists."""

    def test_persistent_session_with_slot_calls_inject(self) -> None:
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        gateway.dashboard_state.has_slot = MagicMock(return_value=True)
        job = _make_job(persistent_session=True)
        with patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard") as mock_inject:
            _run_callback(gateway, job, stream_result="cron output")
            mock_inject.assert_called_once_with(
                gateway.dashboard_state,
                job,
                "cron output",
                history=ANY,
                context_reading=ANY,
                turn_stats=ANY,
            )

    def test_persistent_session_without_slot_still_injects(self) -> None:
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        gateway.dashboard_state.has_slot = MagicMock(return_value=False)
        job = _make_job(persistent_session=True)
        with patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard") as mock_inject:
            _run_callback(gateway, job, stream_result="cron output")
            mock_inject.assert_called_once_with(
                gateway.dashboard_state,
                job,
                "cron output",
                history=ANY,
                context_reading=ANY,
                turn_stats=ANY,
            )

    def test_non_persistent_session_does_not_inject(self) -> None:
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        gateway.dashboard_state.has_slot = MagicMock(return_value=True)
        job = _make_job(persistent_session=False)
        with patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard") as mock_inject:
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
        with patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard") as mock_inject:
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
        with patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard") as mock_inject:
            _run_callback(gateway, job, stream_result="silent output")
            mock_inject.assert_called_once_with(
                gateway.dashboard_state,
                job,
                "silent output",
                history=ANY,
                context_reading=ANY,
                turn_stats=ANY,
            )

    def test_silent_cron_no_slot_does_not_inject(self) -> None:
        """Silent cron without existing slot does not inject."""
        gateway = _make_gateway()
        gateway.dashboard_state.has_slot = MagicMock(return_value=False)
        job = _make_job(persistent_session=True, silent=True)
        with patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard") as mock_inject:
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
        with patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard") as mock_inject:
            _run_callback(gateway, job, stream_result="hidden output")
            mock_inject.assert_not_called()

    def test_hide_in_chat_false_still_injects(self) -> None:
        """Inverse: hide_in_chat=False (default) still injects on the normal path —
        locks both directions of the gate."""
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        gateway.dashboard_state.has_slot = MagicMock(return_value=False)
        job = _make_job(persistent_session=True, hide_in_chat=False)
        with patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard") as mock_inject:
            _run_callback(gateway, job, stream_result="shown output")
            mock_inject.assert_called_once_with(
                gateway.dashboard_state,
                job,
                "shown output",
                history=ANY,
                context_reading=ANY,
                turn_stats=ANY,
            )

    def test_hide_in_chat_suppresses_inject_even_with_existing_slot(self) -> None:
        """hide_in_chat=True must suppress the re-inject paths too: even if a stale
        slot already exists (has_slot True), no injection fires."""
        gateway = _make_gateway()
        gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
        gateway.dashboard_state.has_slot = MagicMock(return_value=True)
        job = _make_job(persistent_session=True, hide_in_chat=True)
        with patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard") as mock_inject:
            _run_callback(gateway, job, stream_result="hidden output")
            mock_inject.assert_not_called()

    def test_hide_in_chat_silent_cron_does_not_inject(self) -> None:
        """A silent + hidden cron with an existing slot must not re-inject
        (the silent path is also gated on not hide_in_chat)."""
        gateway = _make_gateway()
        gateway.dashboard_state.has_slot = MagicMock(return_value=True)
        job = _make_job(persistent_session=True, silent=True, hide_in_chat=True)
        with patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard") as mock_inject:
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
        raise AssertionError(f"no cron-result notify found; calls={notify_mock.call_args_list}")

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
