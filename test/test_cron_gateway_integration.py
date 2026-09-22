"""Integration tests for script/command cron execution in the gateway.

Tests the actual _cron_callback dispatch for script and command jobs,
including delivery, concurrency guard, timeout handling, and Report().
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.executors as ex
from kiro_crew.cron import CronJob, CronSchedule, CronService
from kiro_crew.sandbox import CHILD_START_ACK_FD_ENV


def test_app_cron_env_cannot_supply_parent_start_ack_descriptor() -> None:
    from kiro_crew.slack.gateway import cron_job_env_without_reserved

    for foreign_value in ("not-a-fd", "19"):
        env = cron_job_env_without_reserved(
            {
                "ORDINARY_JOB_VALUE": "kept",
                CHILD_START_ACK_FD_ENV: foreign_value,
            }
        )
        assert env == {"ORDINARY_JOB_VALUE": "kept"}


async def _stalled_gate(*_args, **_kwargs):
    """Stand in for a fire-time gate that outlives the wake deadline.

    Models a recoverable event-loop stall rather than a slow gate: it is the
    whole ``run_in_cron_gate_pool`` call that fails to complete, so neither of
    the gate's OWN bounds is what stops it. That is the only way to reach the
    state where ``asyncio.wait_for`` in ``_execute_with_timeout`` cancels the
    coroutine AT the await -- no internal deadline sizing can prevent it.
    """
    await asyncio.sleep(30)


def _make_gw():
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.ctx_builder = MagicMock()
    gw.slack = MagicMock()
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


def _make_script_job(**overrides):
    defaults = dict(
        id="sj1",
        name="script-job",
        message="CR-123",
        schedule=CronSchedule(kind="every", every_secs=60),
        script="~/.kirocrew/crons/monitor.py:run",
    )
    defaults.update(overrides)
    return CronJob(**defaults)


def _make_command_job(**overrides):
    defaults = dict(
        id="cj1",
        name="cmd-job",
        message="",
        schedule=CronSchedule(kind="every", every_secs=60),
        command="echo hello",
    )
    defaults.update(overrides)
    return CronJob(**defaults)


def _mock_cron_service(*, manual_run: bool = False) -> MagicMock:
    svc = MagicMock()
    svc.start = AsyncMock()
    svc.run_is_manual = MagicMock(return_value=manual_run)
    svc.occurrence_for_pruned_launch = MagicMock(
        return_value=None if manual_run else "scheduled-minute"
    )
    svc._newer_owed_occurrence = CronService._newer_owed_occurrence
    svc.persist_owed_occurrence = AsyncMock(return_value=True)
    svc.remove_job_async = AsyncMock(return_value=True)
    return svc


async def _run_script_callback(
    gw,
    job,
    script_result=None,
    vet_reason=None,
    side_effect=None,
    manual_run=False,
    cron_svc=None,
):
    """Run the cron callback with a mocked run_script_sandboxed result.

    ``vet_reason`` feeds the fire-time governance gate (None = job may run);
    patching vet_job_at_fire_time also stands in for the script-path
    resolution it performs, which the removed gateway-level
    resolve_script_path call also covered.

    Pass ``side_effect`` to make the mocked call raise instead of returning.
    """
    captured_cb = None
    mock_kw = (
        {"side_effect": side_effect} if side_effect is not None else {"return_value": script_result}
    )

    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch("kiro_crew.slack.gateway.run_script_sandboxed", **mock_kw) as mock_run,
        patch("kiro_crew.slack.gateway.vet_job_at_fire_time", return_value=vet_reason),
        patch("kiro_crew.slack.gateway.sel"),
    ):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = cron_svc if cron_svc is not None else _mock_cron_service(manual_run=manual_run)
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            return await captured_cb(job)

        return await _init_and_run(), mock_run


async def _run_command_callback(
    gw,
    job,
    cmd_result=None,
    side_effect=None,
    vet_reason=None,
    manual_run=False,
    cron_svc=None,
):
    """Run the cron callback with a mocked run_command_sandboxed result.

    Pass ``side_effect`` to make the mocked call raise instead of returning.
    ``vet_reason`` feeds the fire-time governance gate (None = job may run),
    mirroring _run_script_callback.
    """
    captured_cb = None
    mock_kw = (
        {"side_effect": side_effect} if side_effect is not None else {"return_value": cmd_result}
    )
    # Only stand in for the gate when simulating a denial: other tests here drive
    # the REAL gate, and patching it unconditionally would silence them.
    gate = (
        patch("kiro_crew.slack.gateway.vet_job_at_fire_time", return_value=vet_reason)
        if vet_reason is not None
        else nullcontext()
    )

    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch("kiro_crew.slack.gateway.run_command_sandboxed", **mock_kw) as mock_run,
        gate,
        patch("kiro_crew.slack.gateway.sel"),
    ):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = cron_svc if cron_svc is not None else _mock_cron_service(manual_run=manual_run)
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            return await captured_cb(job)

        return await _init_and_run(), mock_run


class TestPrunedInstallCommandSkip:
    """Command launch ENOENT follows the same update-handoff contract as scripts."""

    @staticmethod
    def _pruned_enoent():
        return FileNotFoundError(
            2, "No such file or directory", str(Path(sys.prefix) / "bin" / "python3.12")
        )

    @pytest.mark.asyncio
    async def test_install_enoent_retains_one_shot_without_a_failure_strike(self):
        gw = _make_gw()
        job = _make_command_job(delete_after_run=True)
        job.consecutive_failures = 3

        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            result, mock_run = await _run_command_callback(
                gw, job, side_effect=self._pruned_enoent()
            )

        assert result is None
        assert job.last_status == "error"
        assert job.run_never_started is True
        assert job.keep_overdue is True
        assert job.enabled is False
        assert job.consecutive_failures == 3
        assert "replaced by an update" in job.last_error
        assert mock_run.call_args.args[-1] is True

    @pytest.mark.asyncio
    async def test_cron_expression_skip_records_one_owed_occurrence(self):
        gw = _make_gw()
        job = _make_command_job(schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"))

        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            result, _ = await _run_command_callback(gw, job, side_effect=self._pruned_enoent())

        assert result is None
        assert job.run_never_started is True
        assert job.owed_fire is True
        assert job.enabled is False

    @pytest.mark.asyncio
    async def test_durable_publish_precedes_quiesce(self):
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        job = _make_command_job(schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"))
        order: list[str] = []
        svc = _mock_cron_service()
        svc.occurrence_for_pruned_launch.return_value = "100"

        async def persist(job_id: str, occurrence_id: str) -> bool:
            assert job.enabled is True
            assert job_id == job.id
            assert occurrence_id == "100"
            order.append("persist")
            return True

        def quiesce(job_id: str) -> None:
            assert order == ["persist"]
            assert job_id == job.id
            order.append("quiesce")

        svc.persist_owed_occurrence = AsyncMock(side_effect=persist)
        svc.quiesce_pruned.side_effect = quiesce

        await _record_pruned_launch_skip(job, svc)

        assert order == ["persist", "quiesce"]
        assert job.enabled is False
        assert job.owed_occurrence() == "100"
        svc.relinquish_run_result_merges.assert_called_once_with(job.id)

    @staticmethod
    def _real_hot_cron(tmp_path, owed_occurrence: str | None = None):
        svc = CronService(base_dir=tmp_path)
        job = _make_command_job(
            id="j1",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            owed_fire=owed_occurrence is not None,
            owed_fire_id=owed_occurrence or "",
        )
        svc._jobs = [job]
        svc._save()
        assert svc.get_job(job.id) is job
        return svc, job

    @pytest.mark.asyncio
    async def test_shared_hot_job_is_saved_before_quiesce(self, tmp_path):
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        occurrence_id = str(int(now) // 60)
        svc, job = self._real_hot_cron(tmp_path)
        real_save = svc._save
        real_quiesce = svc.quiesce_pruned
        save_calls: list[str] = []

        def save() -> None:
            save_calls.append("save")
            real_save()

        def quiesce(job_id: str) -> None:
            stored = CronService(base_dir=tmp_path).get_job(job_id)
            assert stored is not None
            assert stored.owed_occurrence() == occurrence_id
            assert save_calls == ["save"]
            assert svc._pending_owed_fires == {}
            real_quiesce(job_id)

        with (
            patch("kiro_crew.slack.gateway.time.time", return_value=now),
            patch.object(svc, "_save", side_effect=save),
            patch.object(svc, "quiesce_pruned", side_effect=quiesce),
        ):
            await _record_pruned_launch_skip(job, svc)

        assert job.owed_occurrence() == occurrence_id
        assert job.enabled is False
        assert job.id in svc._pruned_quiesced

    @pytest.mark.asyncio
    async def test_successful_older_publish_preserves_newer_hot_and_disk_debt(self, tmp_path):
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        occurrence_id = str(int(now) // 60)
        newer = str(int(occurrence_id) + 1)
        svc, job = self._real_hot_cron(tmp_path, newer)

        with patch("kiro_crew.slack.gateway.time.time", return_value=now):
            await _record_pruned_launch_skip(job, svc)

        assert job.owed_occurrence() == newer
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == newer
        assert svc._pending_owed_fires == {}
        assert job.id in svc._pruned_quiesced

    @pytest.mark.asyncio
    async def test_durable_handoff_relinquishes_stale_result_merge(self, tmp_path):
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        occurrence_id = str(int(now) // 60)
        svc, job = self._real_hot_cron(tmp_path)
        job.strict_schedule = True
        job.last_result = "old gateway result"
        replacement_run_ts = now + 60

        async def handoff_then_replace(running_job: CronJob) -> None:
            await _record_pruned_launch_skip(running_job, svc)
            replacement = CronService(base_dir=tmp_path)
            replacement_job = replacement.get_job(running_job.id)
            assert replacement_job is not None
            assert replacement_job.owed_occurrence() == occurrence_id
            replacement_job.set_owed_occurrence(None)
            replacement_job.last_status = "ok"
            replacement_job.last_error = None
            replacement_job.last_run_ts = replacement_run_ts
            replacement_job.last_result = "replacement result"
            replacement._save()

        svc._on_job = handoff_then_replace
        with patch("kiro_crew.slack.gateway.time.time", return_value=now):
            await svc._run_job_isolated(job)

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert stored.last_status == "ok"
        assert stored.last_error is None
        assert stored.last_run_ts == replacement_run_ts
        assert stored.last_result == "replacement result"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("schedule", "delete_after_run"),
        [
            pytest.param(CronSchedule(kind="every", every_secs=60), False, id="every"),
            pytest.param(CronSchedule(kind="at", at_ts=1.0), True, id="at-delete-after-run"),
        ],
    )
    async def test_noncron_handoff_relinquishes_stale_result_merge(
        self,
        tmp_path: Path,
        schedule: CronSchedule,
        delete_after_run: bool,
    ) -> None:
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        replacement_run_ts = 1767333660.0
        svc = CronService(base_dir=tmp_path)
        job = _make_command_job(
            id="j1",
            schedule=schedule,
            delete_after_run=delete_after_run,
        )
        job.strict_schedule = True
        job.last_result = "drained gateway result"
        svc._jobs = [job]
        svc._save()

        async def handoff_then_replace(running_job: CronJob) -> None:
            await _record_pruned_launch_skip(running_job, svc)
            assert running_job.__dict__.get("_result_merge_relinquished") is True
            replacement = CronService(base_dir=tmp_path)
            replacement_job = replacement.get_job(running_job.id)
            assert replacement_job is not None
            assert replacement_job.enabled is True
            replacement_job.last_status = "ok"
            replacement_job.last_error = None
            replacement_job.last_run_ts = replacement_run_ts
            replacement_job.last_result = "replacement result"
            replacement._save()

        svc._on_job = handoff_then_replace
        await svc._run_job_isolated(job)

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.last_status == "ok"
        assert stored.last_error is None
        assert stored.last_run_ts == replacement_run_ts
        assert stored.last_result == "replacement result"
        assert job.id in svc._pruned_quiesced

    @pytest.mark.asyncio
    async def test_no_occurrence_relinquishes_before_local_quiesce(self) -> None:
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        job = _make_command_job(schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"))
        svc = _mock_cron_service()
        svc.occurrence_for_pruned_launch.return_value = None
        order: list[str] = []

        def relinquish(job_id: str) -> None:
            assert job_id == job.id
            assert job.enabled is True
            order.append("relinquish")

        def quiesce(job_id: str) -> None:
            assert job_id == job.id
            assert job.enabled is False
            assert order == ["relinquish"]
            order.append("quiesce")

        svc.relinquish_run_result_merges.side_effect = relinquish
        svc.quiesce_pruned.side_effect = quiesce

        await _record_pruned_launch_skip(job, svc)

        assert order == ["relinquish", "quiesce"]
        assert job.__dict__.get("_result_merge_relinquished") is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_durable_handoff_relinquishes_terminal_state_merge(
        self, tmp_path: Path, terminal: str
    ) -> None:
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        occurrence_id = str(int(now) // 60)
        replacement_run_ts = now + 60
        svc, job = self._real_hot_cron(tmp_path)
        job.strict_schedule = True
        svc._history.append = AsyncMock()  # type: ignore[method-assign]
        svc._push_refresh = MagicMock()
        audit = MagicMock()
        handoff_complete = asyncio.Event()
        hold_run = asyncio.Event()

        async def handoff_then_wait(running_job: CronJob) -> None:
            with patch("kiro_crew.slack.gateway.time.time", return_value=now):
                await _record_pruned_launch_skip(running_job, svc)
            replacement = CronService(base_dir=tmp_path)
            replacement_job = replacement.get_job(running_job.id)
            assert replacement_job is not None
            assert replacement_job.owed_occurrence() == occurrence_id
            replacement_job.set_owed_occurrence(None)
            replacement_job.last_status = "ok"
            replacement_job.last_error = None
            replacement_job.last_run_ts = replacement_run_ts
            replacement_job.last_result = "replacement result"
            replacement._save()
            await asyncio.to_thread(svc._load)
            terminal_job = svc.get_job(running_job.id)
            assert terminal_job is not None
            assert terminal_job is not running_job
            assert terminal_job.__dict__.get("_result_merge_relinquished") is None
            handoff_complete.set()
            await hold_run.wait()

        svc._on_job = handoff_then_wait
        svc._executing.add(job.id)
        run_task = asyncio.create_task(svc._run_job_isolated(job))
        svc._running_tasks[job.id] = run_task
        await asyncio.wait_for(handoff_complete.wait(), 2)

        with (
            patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False),
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            if terminal == "cancel":
                assert await asyncio.wait_for(svc.cancel(job.id), 2) is True
            else:
                await asyncio.wait_for(svc._force_reap(job.id, elapsed=1900, deadline=1800), 2)
        await asyncio.gather(run_task, return_exceptions=True)

        hot = svc.get_job(job.id)
        assert hot is not None
        assert hot.owed_occurrence() is None
        assert hot.last_status == "ok"
        assert hot.last_error is None
        assert hot.last_run_ts == replacement_run_ts
        assert hot.last_result == "replacement result"
        updated = await svc.update_job_async(job.id, name="renamed")
        assert updated is not None
        assert updated.name == "renamed"

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert stored.last_status == "ok"
        assert stored.last_error is None
        assert stored.last_run_ts == replacement_run_ts
        assert stored.last_result == "replacement result"
        assert stored.name == "renamed"
        assert run_task.done()
        svc._history.append.assert_awaited_once()  # type: ignore[attr-defined]
        history_record = svc._history.append.await_args.args[0]  # type: ignore[attr-defined]
        expected_history_status = "cancelled" if terminal == "cancel" else "timeout"
        expected_history_error = "Cancelled by user" if terminal == "cancel" else "Reaped after"
        assert history_record.status == expected_history_status
        assert expected_history_error in history_record.error
        svc._push_refresh.assert_any_call("cron_history")
        svc._push_refresh.assert_any_call("crons")
        audit.log_tool_invocation.assert_called_once()
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._run_tokens
        assert job.id not in svc._result_merge_relinquished_runs
        assert job.id not in svc._owed_fire_runs
        assert job.id not in svc._run_occurrence_ids

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_terminal_waits_for_pruned_handoff_decision(
        self, tmp_path: Path, terminal: str
    ) -> None:
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        occurrence_id = str(int(now) // 60)
        replacement_run_ts = now + 60
        svc, job = self._real_hot_cron(tmp_path)
        job.strict_schedule = True
        svc._history.append = AsyncMock()  # type: ignore[method-assign]
        audit = MagicMock()
        persistence_committed = asyncio.Event()
        decision_release = asyncio.Event()
        real_persist = svc.persist_owed_occurrence

        async def persist_then_pause(job_id: str, occurrence: str) -> bool:
            assert await real_persist(job_id, occurrence) is True
            persistence_committed.set()
            await decision_release.wait()
            return True

        async def paused_handoff(running_job: CronJob) -> None:
            with patch("kiro_crew.slack.gateway.time.time", return_value=now):
                await _record_pruned_launch_skip(running_job, svc)

        async def wait_until_cancelled(task: asyncio.Task[None]) -> None:
            while not task.cancelling():
                await asyncio.sleep(0)

        svc._on_job = paused_handoff
        with patch.object(
            svc,
            "persist_owed_occurrence",
            new=AsyncMock(side_effect=persist_then_pause),
        ):
            svc._executing.add(job.id)
            run_task = asyncio.create_task(svc._run_job_isolated(job))
            svc._running_tasks[job.id] = run_task
            await asyncio.wait_for(persistence_committed.wait(), 2)

            replacement = CronService(base_dir=tmp_path)
            replacement_job = replacement.get_job(job.id)
            assert replacement_job is not None
            assert replacement_job.owed_occurrence() == occurrence_id
            replacement_job.set_owed_occurrence(None)
            replacement_job.last_status = "ok"
            replacement_job.last_error = None
            replacement_job.last_run_ts = replacement_run_ts
            replacement_job.last_result = "replacement result"
            replacement._save()
            await asyncio.to_thread(svc._load)

            with (
                patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False),
                patch("kiro_crew.cron.sel.sel", return_value=audit),
                patch("kiro_crew.sel.sel", return_value=audit),
                patch.object(
                    svc, "_reflect_terminal_state", wraps=svc._reflect_terminal_state
                ) as reflect,
                patch.object(
                    svc,
                    "_merge_terminal_state_locked",
                    wraps=svc._merge_terminal_state_locked,
                ) as terminal_merge,
            ):
                if terminal == "cancel":
                    terminal_task = asyncio.create_task(svc.cancel(job.id))
                else:
                    terminal_task = asyncio.create_task(
                        svc._force_reap(job.id, elapsed=1900, deadline=1800)
                    )
                await asyncio.wait_for(wait_until_cancelled(run_task), 2)
                await asyncio.sleep(0)
                assert terminal_task.done() is False
                assert reflect.called is False
                assert terminal_merge.called is False

                decision_release.set()
                terminal_result = await asyncio.wait_for(terminal_task, 2)

            if terminal == "cancel":
                assert terminal_result is True
            with contextlib.suppress(asyncio.CancelledError):
                await run_task

        hot = svc.get_job(job.id)
        assert hot is not None
        assert hot.owed_occurrence() is None
        assert hot.last_status == "ok"
        assert hot.last_error is None
        assert hot.last_run_ts == replacement_run_ts
        assert hot.last_result == "replacement result"
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert stored.last_status == "ok"
        assert stored.last_error is None
        assert stored.last_run_ts == replacement_run_ts
        assert stored.last_result == "replacement result"
        assert terminal_merge.called is False
        svc._history.append.assert_awaited_once()  # type: ignore[attr-defined]
        audit.log_tool_invocation.assert_called_once()
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._run_tokens
        assert job.id not in svc._result_merge_relinquished_runs
        assert job.id not in svc._owed_fire_runs
        assert job.id not in svc._run_occurrence_ids

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    @pytest.mark.parametrize("failure", ["refused", "exception"])
    async def test_terminal_waits_for_failed_handoff_and_keeps_fallback(
        self, tmp_path: Path, terminal: str, failure: str
    ) -> None:
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        occurrence_id = str(int(now) // 60)
        svc, job = self._real_hot_cron(tmp_path)
        job.strict_schedule = True
        svc._history.append = AsyncMock()  # type: ignore[method-assign]
        audit = MagicMock()
        persistence_entered = asyncio.Event()
        decision_release = asyncio.Event()

        async def pause_then_fail(job_id: str, occurrence: str) -> bool:
            svc._queue_owed_occurrence(job_id, occurrence)
            persistence_entered.set()
            await decision_release.wait()
            if failure == "exception":
                raise OSError("handoff failed")
            return False

        async def paused_handoff(running_job: CronJob) -> None:
            with patch("kiro_crew.slack.gateway.time.time", return_value=now):
                await _record_pruned_launch_skip(running_job, svc)

        async def wait_until_cancelled(task: asyncio.Task[None]) -> None:
            while not task.cancelling():
                await asyncio.sleep(0)

        svc._on_job = paused_handoff
        with patch.object(
            svc,
            "persist_owed_occurrence",
            new=AsyncMock(side_effect=pause_then_fail),
        ):
            svc._executing.add(job.id)
            run_task = asyncio.create_task(svc._run_job_isolated(job))
            svc._running_tasks[job.id] = run_task
            await asyncio.wait_for(persistence_entered.wait(), 2)

            with (
                patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False),
                patch("kiro_crew.cron.sel.sel", return_value=audit),
                patch("kiro_crew.sel.sel", return_value=audit),
                patch.object(
                    svc,
                    "_merge_terminal_state_locked",
                    wraps=svc._merge_terminal_state_locked,
                ) as terminal_merge,
            ):
                if terminal == "cancel":
                    terminal_task = asyncio.create_task(svc.cancel(job.id))
                else:
                    terminal_task = asyncio.create_task(
                        svc._force_reap(job.id, elapsed=1900, deadline=1800)
                    )
                await asyncio.wait_for(wait_until_cancelled(run_task), 2)
                await asyncio.sleep(0)
                assert terminal_task.done() is False
                assert terminal_merge.called is False

                decision_release.set()
                terminal_result = await asyncio.wait_for(terminal_task, 2)

            if terminal == "cancel":
                assert terminal_result is True
            with contextlib.suppress(asyncio.CancelledError):
                await run_task

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id
        assert stored.last_status == "error"
        expected_error = "Cancelled by user" if terminal == "cancel" else "Reaped after"
        assert expected_error in (stored.last_error or "")
        assert svc._pending_owed_fires == {job.id: occurrence_id}
        assert job.__dict__.get("_result_merge_relinquished") is None
        assert terminal_merge.call_count == 1
        svc._history.append.assert_awaited_once()  # type: ignore[attr-defined]
        audit.log_tool_invocation.assert_called_once()
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._run_tokens
        assert job.id not in svc._result_merge_relinquished_runs
        assert job.id not in svc._owed_fire_runs
        assert job.id not in svc._run_occurrence_ids

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    @pytest.mark.parametrize("handoff", ["refused", "no-service"])
    async def test_nondurable_handoff_retains_terminal_state_merge(
        self, tmp_path: Path, terminal: str, handoff: str
    ) -> None:
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        replacement_run_ts = now + 60
        svc, job = self._real_hot_cron(tmp_path)
        job.strict_schedule = True
        svc._history.append = AsyncMock()  # type: ignore[method-assign]
        audit = MagicMock()
        fallback_complete = asyncio.Event()
        hold_run = asyncio.Event()

        async def fallback_then_wait(running_job: CronJob) -> None:
            with patch("kiro_crew.slack.gateway.time.time", return_value=now):
                if handoff == "refused":
                    with patch.object(
                        svc, "persist_owed_occurrence", new=AsyncMock(return_value=False)
                    ):
                        await _record_pruned_launch_skip(running_job, svc)
                else:
                    await _record_pruned_launch_skip(running_job, None)
            assert running_job.__dict__.get("_result_merge_relinquished") is None
            assert running_job.id not in svc._result_merge_relinquished_runs
            replacement = CronService(base_dir=tmp_path)
            replacement_job = replacement.get_job(running_job.id)
            assert replacement_job is not None
            replacement_job.last_status = "ok"
            replacement_job.last_error = None
            replacement_job.last_run_ts = replacement_run_ts
            replacement_job.last_result = "replacement result"
            replacement._save()
            fallback_complete.set()
            await hold_run.wait()

        svc._on_job = fallback_then_wait
        svc._executing.add(job.id)
        run_task = asyncio.create_task(svc._run_job_isolated(job))
        svc._running_tasks[job.id] = run_task
        await asyncio.wait_for(fallback_complete.wait(), 2)

        with (
            patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False),
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            if terminal == "cancel":
                assert await asyncio.wait_for(svc.cancel(job.id), 2) is True
            else:
                await asyncio.wait_for(svc._force_reap(job.id, elapsed=1900, deadline=1800), 2)
        await asyncio.gather(run_task, return_exceptions=True)

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.last_status == "error"
        expected_error = "Cancelled by user" if terminal == "cancel" else "Reaped after"
        assert expected_error in (stored.last_error or "")
        assert stored.last_run_ts != replacement_run_ts
        assert stored.last_result == "replacement result"
        svc._history.append.assert_awaited_once()  # type: ignore[attr-defined]
        audit.log_tool_invocation.assert_called_once()
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._run_tokens
        assert job.id not in svc._result_merge_relinquished_runs
        assert job.id not in svc._owed_fire_runs
        assert job.id not in svc._run_occurrence_ids

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure_kind", ["pre-commit", "commit-then-raise"])
    async def test_failed_publish_distinguishes_retry_from_durable_commit(
        self, tmp_path, failure_kind
    ):
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        occurrence_id = str(int(now) // 60)
        newer = str(int(occurrence_id) + 1)
        svc, job = self._real_hot_cron(tmp_path)
        real_save = svc._save

        def fail_save() -> None:
            if failure_kind == "commit-then-raise":
                real_save()
            raise OSError(failure_kind)

        with (
            patch("kiro_crew.slack.gateway.time.time", return_value=now),
            patch.object(svc, "_save", side_effect=fail_save),
        ):
            await _record_pruned_launch_skip(job, svc)

        initially_stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert initially_stored is not None
        if failure_kind == "pre-commit":
            assert job.owed_occurrence() == occurrence_id
            assert job.enabled is True
            assert job.id not in svc._pruned_quiesced
            assert svc._pending_owed_fires == {job.id: occurrence_id}
            assert svc._last_digest != b""
            assert initially_stored.owed_occurrence() is None

            sibling = CronService(base_dir=tmp_path)
            sibling_job = sibling.get_job(job.id)
            assert sibling_job is not None
            sibling_job.set_owed_occurrence(newer)
            sibling._save()
            await asyncio.to_thread(svc._merge_job_result, job)

            stored = CronService(base_dir=tmp_path).get_job(job.id)
            assert stored is not None
            assert stored.owed_occurrence() == newer
            assert svc._pending_owed_fires == {}
        else:
            assert job.owed_occurrence() == occurrence_id
            assert job.enabled is False
            assert job.id in svc._pruned_quiesced
            assert svc._pending_owed_fires == {}
            assert job.__dict__.get("_result_merge_relinquished") is True
            assert initially_stored.owed_occurrence() == occurrence_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("replacement_minute_offset", "newer_pending", "replacement_owns_state"),
        [
            pytest.param(0, False, True, id="same-occurrence-completed"),
            pytest.param(-1440, False, False, id="replacement-completion-older"),
            pytest.param(0, True, True, id="newer-pending-is-persisted"),
        ],
    )
    async def test_failed_handoff_merge_yields_to_replacement_completion(
        self,
        tmp_path: Path,
        replacement_minute_offset: int,
        newer_pending: bool,
        replacement_owns_state: bool,
    ) -> None:
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        occurrence_id = str(int(now) // 60)
        newer_occurrence = str(int(occurrence_id) + 1440)
        svc, job = await asyncio.to_thread(self._real_hot_cron, tmp_path)
        job.strict_schedule = True
        job.last_result = "drained result"
        job.last_result_ts = now - 300
        job.last_result_stamp = "drained stamp"
        job.last_posted_hash = "drained-posted"
        job.consecutive_dupes = 7
        job.last_posted_at = now - 200
        job.last_failure_hash = "drained-failure"
        job.last_failure_at = now - 100
        job.consecutive_failures = 3
        job.last_retry_count = 8
        job.last_retry_run_ts = now - 50
        await asyncio.to_thread(svc._save)
        svc._run_occurrence_ids[job.id] = occurrence_id
        svc._owed_fire_runs[job.id] = None
        replacement_states: list[tuple[object, ...]] = []

        def runtime_state(current: CronJob) -> tuple[object, ...]:
            return (
                current.last_run_ts,
                current.last_status,
                current.last_error,
                current.enabled,
                current.user_paused,
                current.auto_paused,
                current.last_result,
                current.last_result_ts,
                current.last_result_stamp,
                current.last_posted_hash,
                current.consecutive_dupes,
                current.last_posted_at,
                current.last_failure_hash,
                current.last_failure_at,
                current.consecutive_failures,
                current.last_retry_count,
                current.last_retry_run_ts,
                current.owed_occurrence(),
            )

        async def refuse_handoff(job_id: str, occurrence: str) -> bool:
            svc._queue_owed_occurrence(job_id, occurrence)
            return False

        async def handoff_then_complete(running_job: CronJob) -> None:
            with (
                patch("kiro_crew.slack.gateway.time.time", return_value=now),
                patch.object(
                    svc,
                    "persist_owed_occurrence",
                    new=AsyncMock(side_effect=refuse_handoff),
                ),
            ):
                await _record_pruned_launch_skip(running_job, svc)

            replacement = CronService(base_dir=tmp_path)
            replacement_job = replacement.get_job(running_job.id)
            assert replacement_job is not None
            replacement_run_ts = now + replacement_minute_offset * 60 + 10
            replacement_job.last_run_ts = replacement_run_ts
            replacement_job.last_status = "ok"
            replacement_job.last_error = None
            replacement_job.enabled = True
            replacement_job.user_paused = False
            replacement_job.auto_paused = False
            replacement_job.last_result = "replacement result"
            replacement_job.last_result_ts = replacement_run_ts
            replacement_job.last_result_stamp = "replacement stamp"
            replacement_job.last_posted_hash = "replacement-posted"
            replacement_job.consecutive_dupes = 2
            replacement_job.last_posted_at = replacement_run_ts
            replacement_job.last_failure_hash = "replacement-failure"
            replacement_job.last_failure_at = replacement_run_ts - 1
            replacement_job.consecutive_failures = 0
            replacement_job.last_retry_count = 2
            replacement_job.last_retry_run_ts = replacement_run_ts
            replacement_job.set_owed_occurrence(None)
            replacement._save()
            replacement_states.append(runtime_state(replacement_job))
            if newer_pending:
                svc._queue_owed_occurrence(running_job.id, newer_occurrence)

        svc._on_job = handoff_then_complete
        with patch.object(svc, "_save", wraps=svc._save) as stale_save:
            await svc._run_job_isolated(job)

        assert len(replacement_states) == 1
        stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
        assert stored is not None
        hot = svc.get_job(job.id)
        assert hot is not None
        if replacement_owns_state:
            assert runtime_state(stored)[:-1] == replacement_states[0][:-1]
            assert runtime_state(hot)[:-1] == replacement_states[0][:-1]
            expected_owed = newer_occurrence if newer_pending else None
            assert stored.owed_occurrence() == expected_owed
            assert hot.owed_occurrence() == expected_owed
            if newer_pending:
                stale_save.assert_called_once()
            else:
                stale_save.assert_not_called()
        else:
            assert runtime_state(stored) == runtime_state(job)
            assert runtime_state(hot) == runtime_state(job)
            assert stored.last_status == "error"
            assert "replaced by an update" in (stored.last_error or "")
            assert stored.owed_occurrence() == occurrence_id
            stale_save.assert_called_once()

        assert svc._pending_owed_fires == {}
        await asyncio.to_thread(svc._load)
        reloaded = svc.get_job(job.id)
        assert reloaded is not None
        if replacement_owns_state:
            assert runtime_state(reloaded)[:-1] == replacement_states[0][:-1]
            assert reloaded.owed_occurrence() == (newer_occurrence if newer_pending else None)
        else:
            assert runtime_state(reloaded) == runtime_state(job)
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    async def test_shutdown_persists_newer_debt_after_replacement_completed_old(
        self, tmp_path: Path
    ) -> None:
        now = 1767333600.0
        old_occurrence = str(int(now) // 60)
        pending_occurrence = str(int(old_occurrence) + 1)
        newer_occurrence = str(int(old_occurrence) + 2)
        newest_occurrence = str(int(old_occurrence) + 3)
        svc = CronService(base_dir=tmp_path)
        job = _make_command_job(
            id="j1",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            strict_schedule=True,
            owed_fire=True,
            owed_fire_id=old_occurrence,
        )
        svc._jobs = [job]
        svc._save()
        payload_entered = asyncio.Event()

        async def blocked_payload(_running_job: CronJob) -> None:
            payload_entered.set()
            await asyncio.Future()

        svc._on_job = blocked_payload
        svc._executing.add(job.id)
        run_task = asyncio.create_task(svc._run_job_isolated(job))
        svc._running_tasks[job.id] = run_task
        await asyncio.wait_for(payload_entered.wait(), 2)

        replacement = CronService(base_dir=tmp_path)
        replacement_job = replacement.get_job(job.id)
        assert replacement_job is not None
        replacement_job.set_owed_occurrence(None)
        replacement_job.last_run_ts = now + 10
        replacement_job.last_status = "ok"
        replacement_job.last_error = None
        replacement_job.last_result = "replacement result"
        replacement_job.consecutive_failures = 0
        replacement._save()
        svc._queue_owed_occurrence(job.id, pending_occurrence)
        real_save = svc._save
        save_calls: list[int] = []

        def save_with_newer_arrival() -> None:
            real_save()
            save_calls.append(len(save_calls) + 1)
            if len(save_calls) == 1:
                svc._queue_owed_occurrence(job.id, newer_occurrence)
            elif len(save_calls) == 2:
                svc._queue_owed_occurrence(job.id, newest_occurrence)

        with patch.object(svc, "_save", side_effect=save_with_newer_arrival):
            await asyncio.wait_for(svc.stop(), 2)

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == newest_occurrence
        assert stored.last_run_ts == now + 10
        assert stored.last_status == "ok"
        assert stored.last_error is None
        assert stored.last_result == "replacement result"
        assert stored.consecutive_failures == 0
        assert save_calls == [1, 2, 3]
        assert svc._pending_owed_fires == {}
        assert run_task.done()
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks

    @pytest.mark.asyncio
    async def test_shutdown_stabilization_is_finite_under_external_producer(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = _make_command_job(
            id="j1",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        svc._queue_owed_occurrence(job.id, "100")
        real_save = svc._save
        save_calls: list[int] = []

        def save_with_external_arrival() -> None:
            real_save()
            save_calls.append(len(save_calls) + 1)
            svc._queue_owed_occurrence(job.id, str(100 + len(save_calls)))

        with patch.object(svc, "_save", side_effect=save_with_external_arrival):
            with pytest.raises(RuntimeError, match="could not durably hand off 1 owed"):
                await asyncio.wait_for(svc.stop(), 2)

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == "102"
        assert svc._pending_owed_fires == {job.id: "103"}
        assert save_calls == [1, 2, 3]

    @pytest.mark.parametrize("failure_kind", ["before-commit", "commit-then-raise"])
    def test_newer_debt_save_failure_cannot_overwrite_later_completion(
        self, tmp_path: Path, failure_kind: str
    ) -> None:
        now = 1767333600.0
        old_occurrence = str(int(now) // 60)
        newer_occurrence = str(int(old_occurrence) + 1)
        svc = CronService(base_dir=tmp_path)
        stored_job = _make_command_job(
            id="j1",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            strict_schedule=True,
            owed_fire=True,
            owed_fire_id=old_occurrence,
        )
        svc._jobs = [stored_job]
        svc._save()
        running = _make_command_job(
            id=stored_job.id,
            schedule=stored_job.schedule,
            strict_schedule=True,
            owed_fire=True,
            owed_fire_id=old_occurrence,
            run_never_started=True,
            keep_overdue=True,
            last_status="error",
            last_error="drained result",
        )
        svc._run_occurrence_ids[running.id] = old_occurrence
        svc._queue_owed_occurrence(running.id, newer_occurrence)

        replacement = CronService(base_dir=tmp_path)
        replacement_job = replacement.get_job(running.id)
        assert replacement_job is not None
        replacement_job.set_owed_occurrence(None)
        replacement_job.last_run_ts = now + 10
        replacement_job.last_status = "ok"
        replacement_job.last_error = None
        replacement_job.last_result = "replacement A"
        replacement._save()
        real_save = svc._save

        def failing_save() -> None:
            if failure_kind == "commit-then-raise":
                real_save()
            raise OSError(failure_kind)

        with (
            patch.object(svc, "_save", side_effect=failing_save),
            pytest.raises(OSError, match=failure_kind),
        ):
            svc._merge_job_result(running, old_occurrence)

        assert svc._pending_owed_fires == {running.id: newer_occurrence}
        assert svc._last_digest == b""
        hot = svc.get_job(running.id)
        assert hot is not None
        assert hot.last_status == "ok"
        assert hot.last_result == "replacement A"
        assert hot.owed_occurrence() is None

        latest = CronService(base_dir=tmp_path)
        latest_job = latest.get_job(running.id)
        assert latest_job is not None
        latest_job.set_owed_occurrence(None)
        latest_job.last_run_ts = int(newer_occurrence) * 60 + 10
        latest_job.last_status = "ok"
        latest_job.last_error = None
        latest_job.last_result = "replacement B"
        latest._save()

        with patch.object(svc, "_save", wraps=svc._save) as stale_save:
            svc._merge_job_result(running, old_occurrence)

        final = CronService(base_dir=tmp_path).get_job(running.id)
        assert final is not None
        assert final.owed_occurrence() is None
        assert final.last_status == "ok"
        assert final.last_result == "replacement B"
        assert svc._pending_owed_fires == {}
        stale_save.assert_not_called()

    @pytest.mark.parametrize(
        ("store_state", "durable_occurrence"),
        [
            pytest.param("absent", None, id="absent-job"),
            pytest.param("obsolete", None, id="obsolete-occurrence"),
            pytest.param("durable", "100", id="durable-equal"),
            pytest.param("durable", "101", id="durable-newer"),
        ],
    )
    def test_readable_store_can_cover_ambiguous_publication(
        self,
        tmp_path: Path,
        store_state: str,
        durable_occurrence: str | None,
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job_id = "j1"
        if store_state != "absent":
            job = _make_command_job(
                id=job_id,
                schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
                strict_schedule=True,
                timezone="UTC",
                owed_fire=durable_occurrence is not None,
                owed_fire_id=durable_occurrence or "",
            )
            if store_state == "obsolete":
                job.enabled = False
                job.user_paused = True
            svc._jobs = [job]
            svc._save()
        svc._queue_owed_occurrence(job_id, "100")

        assert svc._ambiguous_owed_occurrence_is_covered_locked(job_id, "100") is True
        assert svc._load_failed is False
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    async def test_unreadable_store_keeps_pruned_handoff_owned_until_retry(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        occurrence_id = str(int(now) // 60)
        svc, job = await asyncio.to_thread(self._real_hot_cron, tmp_path)
        job.strict_schedule = True
        await asyncio.to_thread(svc._save)
        store = tmp_path / "crons.json"
        healthy = await asyncio.to_thread(store.read_bytes)
        svc._queue_owed_occurrence(job.id, occurrence_id)
        await asyncio.to_thread(store.write_text, "{", encoding="utf-8")

        covered = await asyncio.to_thread(
            svc._ambiguous_owed_occurrence_is_covered_locked,
            job.id,
            occurrence_id,
        )
        assert covered is False
        assert svc._load_failed is True
        assert svc._pending_owed_fires == {job.id: occurrence_id}

        run_token = svc._ensure_run_token(job.id)
        with patch("kiro_crew.slack.gateway.time.time", return_value=now):
            await _record_pruned_launch_skip(job, svc)

        assert svc._pending_owed_fires == {job.id: occurrence_id}
        assert svc._result_merge_relinquished_runs.get(job.id) is not run_token
        assert job.__dict__.get("_result_merge_relinquished") is None
        assert job.id not in svc._pruned_quiesced
        assert job.enabled is True
        assert job.owed_occurrence() == occurrence_id

        await asyncio.to_thread(store.write_bytes, healthy)
        assert await svc.persist_owed_occurrence(job.id, occurrence_id) is True
        stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id
        assert svc._load_failed is False
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    async def test_no_service_keeps_local_marker_without_quiescing(self):
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        occurrence_id = str(int(now) // 60)
        job = _make_command_job(schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"))

        with patch("kiro_crew.slack.gateway.time.time", return_value=now):
            await _record_pruned_launch_skip(job, None)

        assert job.owed_occurrence() == occurrence_id
        assert job.enabled is True
        assert job.keep_overdue is True
        assert job.__dict__.get("_result_merge_relinquished") is None

    @pytest.mark.asyncio
    async def test_failed_publish_does_not_quiesce_the_retry_path(self):
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        job = _make_command_job(schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"))
        job.consecutive_failures = 3
        svc = _mock_cron_service()
        svc.occurrence_for_pruned_launch.return_value = "100"
        svc.persist_owed_occurrence.return_value = False

        await _record_pruned_launch_skip(job, svc)

        assert job.run_never_started is True
        assert job.keep_overdue is True
        assert job.owed_occurrence() == "100"
        assert job.__dict__.get("_result_merge_relinquished") is None
        assert job.enabled is True
        assert job.consecutive_failures == 3
        svc.relinquish_run_result_merges.assert_not_called()
        svc.quiesce_pruned.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "missing",
        [
            FileNotFoundError(2, "No such file or directory", "/missing/user-command"),
            FileNotFoundError("Command launcher missing"),
        ],
        ids=["unrelated-path", "pathless"],
    )
    async def test_non_install_enoent_is_a_real_failure(self, missing):
        gw = _make_gw()
        job = _make_command_job()

        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            result, _ = await _run_command_callback(gw, job, side_effect=missing)

        assert result is None
        assert job.last_status == "error"
        assert job.run_never_started is False
        assert job.consecutive_failures == 1


class TestScriptExecution:
    """Test script cron dispatch through the gateway callback."""

    @pytest.mark.asyncio
    async def test_ok_status_returns_ok(self):
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_script_callback(gw, job, {"status": "ok"})
        assert result == "ok"
        assert job.last_status == "ok"
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_skip_returns_none(self):
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_script_callback(gw, job, {"status": "skip"})
        assert result is None

    @pytest.mark.asyncio
    async def test_skip_is_success_not_failure(self):
        # A completed Skip is a SUCCESS outcome (mirrors the ok/done/report
        # siblings): the branch returns None and never marks the run
        # last_status="error", so CronScheduler._execute treats the tick as
        # healthy and resets the strike counter for it one frame up. Long-lived
        # pollers end EVERY tick with Skip, so mis-classifying Skip as a failure
        # would trip the 5-strike auto-pause on a >99% healthy job.
        gw = _make_gw()
        job = _make_script_job()
        job.consecutive_failures = 3
        result, _ = await _run_script_callback(gw, job, {"status": "skip"})
        assert result is None
        assert job.last_status != "error"

    @pytest.mark.asyncio
    async def test_skip_defers_strike_reset_to_execute(self):
        # The Skip branch must NOT reset the counter or lift auto-pause itself:
        # that is record_success's job, reached only through
        # CronScheduler._execute, whose reset is guarded by the _cancelled_jobs
        # cancel-race check. An unguarded reset in this branch would clear the
        # pause and re-enable a job cancelled mid-tick, so the callback layer
        # leaves the bookkeeping untouched and defers to _execute. (The guarded
        # _execute reset — and the cancel guard — are covered by
        # TestExecuteSuccessResetsCounter in test_cron_autopause_persist.)
        gw = _make_gw()
        job = _make_script_job()
        job.consecutive_failures = 5
        job.auto_paused = True
        job.user_paused = True
        job.enabled = False
        result, _ = await _run_script_callback(gw, job, {"status": "skip"})
        assert result is None
        assert job.last_status != "error"
        assert job.consecutive_failures == 5
        assert job.auto_paused is True
        assert job.user_paused is True
        assert job.enabled is False

    @pytest.mark.asyncio
    async def test_done_removes_job(self):
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_script_callback(gw, job, {"status": "done", "message": "CR merged"})
        assert "CR merged" in (result or "")
        assert job.last_result == "CR merged"
        gw.cron_svc.remove_job_async.assert_called_once_with(
            "sj1", actor="cron", source="cron", one_shot_path="cron_gateway"
        )

    @pytest.mark.asyncio
    async def test_done_busy_store_defers_removal_not_dropped(self):
        """A busy store on the Done removal must hand off to defer_removal, not
        silently drop it (Arbiter BLOCK item 1). Otherwise the completed job
        lingers enabled and re-fires."""
        from kiro_crew.cron import CronStoreBusy

        gw = _make_gw()
        job = _make_script_job()
        captured_cb = None

        with (
            patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
            patch(
                "kiro_crew.slack.gateway.run_script_sandboxed",
                return_value={"status": "done", "message": "CR merged"},
            ),
            patch("kiro_crew.slack.gateway.vet_job_at_fire_time", return_value=None),
            patch("kiro_crew.slack.gateway.sel"),
        ):

            def capture_cron(on_job=None, **kw):
                nonlocal captured_cb
                captured_cb = on_job
                svc = MagicMock()
                svc.start = AsyncMock()
                # First removal attempt hits a contended store.
                svc.remove_job_async = AsyncMock(side_effect=CronStoreBusy("busy"))
                svc.defer_removal = MagicMock()
                return svc

            mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

            await gw._init_cron()
            assert captured_cb is not None
            await captured_cb(job)

        gw.cron_svc.remove_job_async.assert_called_once_with(
            "sj1", actor="cron", source="cron", one_shot_path="cron_gateway"
        )
        # The removal was queued for deferred drain, not dropped.
        gw.cron_svc.defer_removal.assert_called_once_with("sj1")

    @pytest.mark.asyncio
    async def test_report_does_not_remove_job(self):
        gw = _make_gw()
        job = _make_script_job(session_key="dashboard:chat-1")
        result, _ = await _run_script_callback(
            gw, job, {"status": "report", "message": "DRB passed"}
        )
        assert "DRB passed" in (result or "")
        assert job.last_result == "DRB passed"
        gw.cron_svc.remove_job_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_increments_failures(self):
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_script_callback(
            gw, job, {"status": "error", "error": "something broke"}
        )
        # Error is handled internally (logged, not re-raised)
        assert result is None
        assert job.last_status == "error"
        assert job.consecutive_failures == 1
        assert "something broke" in job.last_error

    @pytest.mark.asyncio
    async def test_concurrent_guard_skips(self):
        gw = _make_gw()
        gw._running_script_ids.add("sj1")
        job = _make_script_job()
        # Should skip without calling run_script_sandboxed
        captured_cb = None

        with (
            patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
            patch("kiro_crew.slack.gateway.run_script_sandboxed") as mock_run,
            patch("kiro_crew.slack.gateway.sel"),
        ):

            def capture_cron(on_job=None, **kw):
                nonlocal captured_cb
                captured_cb = on_job
                svc = MagicMock()
                svc.start = AsyncMock()
                svc.remove_job_async = AsyncMock(return_value=True)
                return svc

            mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

            async def _init_and_run():
                await gw._init_cron()
                return await captured_cb(job)

            result = await _init_and_run()
        assert result is None
        mock_run.assert_not_called()


class TestCommandExecution:
    """Test command cron dispatch through the gateway callback."""

    @pytest.mark.asyncio
    async def test_ok_command_stores_output(self):
        gw = _make_gw()
        job = _make_command_job()
        result, _ = await _run_command_callback(
            gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0}
        )
        assert job.last_status == "ok"
        assert "hello" in job.last_result

    @pytest.mark.asyncio
    async def test_error_command_increments_failures(self):
        gw = _make_gw()
        job = _make_command_job()
        result, _ = await _run_command_callback(
            gw, job, {"status": "error", "output": "Exit code 1\n", "exit_code": 1}
        )
        assert job.last_status == "error"
        assert job.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_empty_output_no_delivery(self):
        gw = _make_gw()
        job = _make_command_job()
        result, _ = await _run_command_callback(
            gw, job, {"status": "ok", "output": "", "exit_code": 0}
        )
        assert result is None  # no output = no delivery

    @pytest.mark.asyncio
    async def test_silent_success_overwrites_stale_result(self):
        """A silent success must not leave the previous run's failure in last_result.

        The dashboard and the cron_list renderers read last_result to show "what
        this job last produced", so a stale value is presented as this run's
        output on a job the same view reports as OK. Cleared rather than marked
        with a literal: last_status already carries the verdict, and any literal
        stored here is also legal job output, so no reader could tell the two
        apart.
        """
        gw = _make_gw()
        job = _make_command_job(last_result="⚠️ Exit code 1\n\nstderr:\nboom")
        result, _ = await _run_command_callback(
            gw, job, {"status": "ok", "output": "", "exit_code": 0}
        )
        assert result is None
        assert job.last_status == "ok"
        assert job.last_result == ""
        assert "Exit code 1" not in job.last_result

    @pytest.mark.asyncio
    async def test_report_of_exactly_ok_is_kept_as_a_result(self):
        """A job whose reported text happens to be "ok" still has a result.

        Pinned alongside the clearing tests because the two are only one string
        apart: clearing on silence is correct, and dropping this value is not.
        """
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_script_callback(gw, job, {"status": "report", "message": "ok"})
        assert result == "ok"
        assert job.last_status == "ok"
        assert job.last_result == "ok"
        assert job.result_produced is True

    @pytest.mark.asyncio
    async def test_silent_failure_clears_stale_result(self):
        """A silent failure must not present the PREVIOUS failure's text as this run's.

        Cleared rather than sentinel-marked: an empty result lets a reader fall
        back to last_error, which carries this run's actual reason.
        """
        gw = _make_gw()
        job = _make_command_job(last_result="⚠️ Exit code 1\n\nstderr:\nold failure")
        result, _ = await _run_command_callback(
            gw, job, {"status": "timeout", "output": "", "exit_code": None}
        )
        assert result is None
        assert job.last_status == "error"
        assert job.last_result == ""
        assert "old failure" not in job.last_result
        assert "no output" in job.last_error

    @pytest.mark.asyncio
    async def test_timeout_clears_stale_result(self):
        """A timed-out run must not present the previous run's output as its own."""
        gw = _make_gw()
        job = _make_command_job(last_result="42 widgets")
        result, _ = await _run_command_callback(gw, job, side_effect=asyncio.TimeoutError())
        assert result is None
        assert job.last_status == "error"
        assert job.last_result == ""
        assert "42 widgets" not in job.last_result
        assert "timeout" in job.last_error

    @pytest.mark.asyncio
    async def test_raising_command_clears_stale_result(self):
        """Same for a raising run: last_error must be the only text left to read."""
        gw = _make_gw()
        job = _make_command_job(last_result="42 widgets")
        result, _ = await _run_command_callback(gw, job, side_effect=RuntimeError("boom"))
        assert result is None
        assert job.last_status == "error"
        assert job.last_result == ""
        assert "boom" in job.last_error

    @pytest.mark.asyncio
    async def test_script_timeout_clears_stale_result(self):
        """The script branch's timeout path carries the same invariant."""
        gw = _make_gw()
        job = _make_script_job(last_result="42 widgets")
        result, _ = await _run_script_callback(gw, job, side_effect=asyncio.TimeoutError())
        assert result is None
        assert job.last_status == "error"
        assert job.last_result == ""
        assert "42 widgets" not in job.last_result
        assert "timeout" in job.last_error

    @pytest.mark.asyncio
    async def test_raising_script_clears_stale_result(self):
        """Same for a raising script run: last_error must be the only text left."""
        gw = _make_gw()
        job = _make_script_job(last_result="42 widgets")
        result, _ = await _run_script_callback(gw, job, side_effect=RuntimeError("boom"))
        assert result is None
        assert job.last_status == "error"
        assert job.last_result == ""
        assert "boom" in job.last_error

    @pytest.mark.asyncio
    async def test_command_fire_time_deny_clears_stale_result(self):
        """A governance denial is result-less, so it must not wear the last run's output."""
        gw = _make_gw()
        job = _make_command_job(last_result="42 widgets")
        result, mock_run = await _run_command_callback(
            gw,
            job,
            {"status": "ok", "output": "unused", "exit_code": 0},
            vet_reason="command not permitted at fire time",
        )
        assert result is None
        mock_run.assert_not_called()
        assert job.last_status == "error"
        assert job.last_result == "", "a denied run must not display the previous run's output"
        assert "not permitted" in job.last_error

    @pytest.mark.asyncio
    async def test_script_fire_time_deny_clears_stale_result(self):
        """Same denial path on the script side."""
        gw = _make_gw()
        job = _make_script_job(last_result="42 widgets")
        result, mock_run = await _run_script_callback(
            gw,
            job,
            {"status": "ok"},
            vet_reason="script changed on disk",
        )
        assert result is None
        mock_run.assert_not_called()
        assert job.last_status == "error"
        assert job.last_result == ""
        assert "changed on disk" in job.last_error

    @pytest.mark.asyncio
    async def test_script_skip_clears_stale_result(self):
        """A Skip is a result-less success -- carrying prior output reads as produced."""
        gw = _make_gw()
        job = _make_script_job(last_result="42 widgets")
        await _run_script_callback(gw, job, {"status": "skip"})
        assert job.last_result == "", "a Skip must not present the previous run's output"

    @pytest.mark.asyncio
    async def test_silent_script_ok_leaves_no_sentinel(self):
        """A silent ok clears rather than writing the literal ok sentinel.

        The sentinel existed only to be non-empty, and two mcp_cron readers had to
        filter it back out; clearing removes the writer and both filters.
        """
        gw = _make_gw()
        job = _make_script_job(last_result="42 widgets")
        await _run_script_callback(gw, job, {"status": "ok"})
        assert job.last_status == "ok"
        assert job.last_result == "", "no sentinel, and no stale carry either"

    @pytest.mark.asyncio
    async def test_nonempty_output_still_stored(self):
        """Negative control: the change must not suppress a real result."""
        gw = _make_gw()
        job = _make_command_job(last_result="stale")
        await _run_command_callback(
            gw, job, {"status": "ok", "output": "42 widgets\n", "exit_code": 0}
        )
        assert "42 widgets" in job.last_result
        assert job.last_result != ""

    @pytest.mark.asyncio
    async def test_timeout_passed_to_subprocess(self):
        gw = _make_gw()
        job = _make_command_job(timeout=120)
        _, mock_run = await _run_command_callback(
            gw, job, {"status": "ok", "output": "done\n", "exit_code": 0}
        )
        # Verify cmd_timeout was passed
        mock_run.assert_called_once()
        args = mock_run.call_args[0]
        assert args[0] == "echo hello"
        assert args[1] == 120  # the timeout value

    @pytest.mark.asyncio
    async def test_fire_time_command_deny_blocks_execution(self):
        # A policy tightened after this job was scheduled must still block it at
        # fire time, not just at cron_add authoring time.
        gw = _make_gw()
        job = _make_command_job()
        with (
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
            patch(
                "kiro_crew.mcp_cron._vet_command_governance",
                return_value="Error: cron command blocked by governance policy: denied",
            ),
        ):
            result, mock_run = await _run_command_callback(
                gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0}
            )
        assert result is None
        assert job.last_status == "error"
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_fire_time_capability_deny_blocks_execution(self):
        # capabilities.cron can be disabled after the job was scheduled too.
        gw = _make_gw()
        job = _make_command_job()
        with patch(
            "kiro_crew.mcp_cron._vet_cron_capability_governance",
            return_value="Error: cron scheduling blocked by governance policy: disabled",
        ):
            result, mock_run = await _run_command_callback(
                gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0}
            )
        assert result is None
        assert job.last_status == "error"
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_fire_time_governance_allow_still_executes(self):
        gw = _make_gw()
        job = _make_command_job()
        with (
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
            patch("kiro_crew.mcp_cron._vet_command_governance", return_value=None),
        ):
            result, mock_run = await _run_command_callback(
                gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0}
            )
        assert job.last_status == "ok"
        mock_run.assert_called_once()


class TestFireTimeGatesScriptAndMessage:
    """Fire-time re-vetting for script and message (LLM) cron jobs.

    Command jobs gained this gate first (TestCommandExecution above); these
    tests lock in the same policy-tightened-after-scheduling protection for
    the other two job kinds, routed through mcp_cron.vet_job_at_fire_time.
    The mcp_cron privates are patched (not the helper itself) so the
    helper's real dispatch logic is exercised.
    """

    async def _run_script_real_vet(self, gw, job, script_result, cap=None, script_vet=None):
        """Run the script callback with the REAL vet helper, privates patched."""
        captured_cb = None

        with (
            patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
            patch(
                "kiro_crew.slack.gateway.run_script_sandboxed", return_value=script_result
            ) as mock_run,
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=cap),
            patch("kiro_crew.mcp_cron.resolve_script_path", return_value=("/tmp/x.py", "run")),
            patch("kiro_crew.mcp_cron._vet_script_file", return_value=script_vet) as mock_vet_file,
            patch("kiro_crew.slack.gateway.sel") as mock_sel,
        ):

            def capture_cron(on_job=None, **kw):
                nonlocal captured_cb
                captured_cb = on_job
                svc = MagicMock()
                svc.start = AsyncMock()
                svc.remove_job_async = AsyncMock(return_value=True)
                return svc

            mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

            await gw._init_cron()
            assert captured_cb is not None
            result = await captured_cb(job)
            return result, mock_run, mock_vet_file, mock_sel

    @pytest.mark.asyncio
    async def test_script_fire_time_capability_deny_blocks_execution(self):
        # capabilities.cron disabled AFTER the script job was scheduled must
        # deny the run at fire time, not merely re-resolve the path.
        gw = _make_gw()
        job = _make_script_job()
        result, mock_run, _, mock_sel = await self._run_script_real_vet(
            gw,
            job,
            {"status": "ok"},
            cap="Error: cron scheduling blocked by governance policy: disabled",
        )
        assert result is None
        assert job.last_status == "error"
        assert "governance" in job.last_error
        mock_run.assert_not_called()
        # Job KEPT (a later policy loosening lets it resume on its own):
        # not removed, and the denial did NOT feed the auto-pause counter.
        gw.cron_svc.remove_job_async.assert_not_called()
        assert job.consecutive_failures == 0
        assert job.enabled is True
        outcomes = [
            c.kwargs.get("outcome")
            for c in mock_sel.return_value.log_tool_invocation.call_args_list
        ]
        assert "denied" in outcomes

    @pytest.mark.asyncio
    async def test_script_fire_time_body_rescan_denies_edited_script(self):
        # A script file edited on disk after authoring (e.g. to read a
        # credential path) is re-scanned at fire time and denied.
        gw = _make_gw()
        job = _make_script_job()
        result, mock_run, mock_vet_file, mock_sel = await self._run_script_real_vet(
            gw,
            job,
            {"status": "ok"},
            script_vet="Error: cron script blocked: references a credential path",
        )
        assert result is None
        assert job.last_status == "error"
        assert "credential" in job.last_error
        mock_run.assert_not_called()
        gw.cron_svc.remove_job_async.assert_not_called()
        assert job.consecutive_failures == 0
        assert job.enabled is True
        # The re-scan ran against the freshly re-resolved path.
        mock_vet_file.assert_called_once_with("/tmp/x.py")
        outcomes = [
            c.kwargs.get("outcome")
            for c in mock_sel.return_value.log_tool_invocation.call_args_list
        ]
        assert "denied" in outcomes

    @pytest.mark.asyncio
    async def test_script_fire_time_allow_still_executes(self):
        gw = _make_gw()
        job = _make_script_job()
        result, mock_run, _, _ = await self._run_script_real_vet(gw, job, {"status": "ok"})
        assert result == "ok"
        assert job.last_status == "ok"
        mock_run.assert_called_once()

    async def _run_message_callback(self, gw, job, cap=None):
        """Run the cron callback for a message (LLM) job up to the gate."""
        captured_cb = None

        with (
            patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=cap),
            patch("kiro_crew.slack.gateway.sel") as mock_sel,
        ):

            def capture_cron(on_job=None, **kw):
                nonlocal captured_cb
                captured_cb = on_job
                svc = MagicMock()
                svc.start = AsyncMock()
                svc.remove_job_async = AsyncMock(return_value=True)
                return svc

            mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

            await gw._init_cron()
            assert captured_cb is not None
            result = await captured_cb(job)
            return result, mock_sel

    @pytest.mark.asyncio
    async def test_message_fire_time_capability_deny_blocks_dispatch(self):
        # Message (LLM) jobs need a fire-time capabilities.cron
        # check: without it, disabling the capability after scheduling has no
        # effect. The gate must block the session dispatch entirely.
        gw = _make_gw()
        job = CronJob(
            id="mj1",
            name="msg-job",
            message="summarize the day",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
        result, mock_sel = await self._run_message_callback(
            gw,
            job,
            cap="Error: cron scheduling blocked by governance policy: disabled",
        )
        assert result is None
        assert job.last_status == "error"
        assert "governance" in job.last_error
        # No LLM session was acquired.
        gw.sessions.get_or_create.assert_not_called()
        gw.cron_svc.remove_job_async.assert_not_called()
        assert job.consecutive_failures == 0
        assert job.enabled is True
        outcomes = [
            c.kwargs.get("outcome")
            for c in mock_sel.return_value.log_tool_invocation.call_args_list
        ]
        assert "denied" in outcomes


class TestFireTimeDenyOneShotRetention:
    """A fire-time denial is a policy refusal, not a completed run: one-shot
    delete_after_run jobs must be RETAINED and at-jobs stay armed."""

    @pytest.mark.asyncio
    async def test_deny_sets_fire_time_denied_flag(self):
        gw = _make_gw()
        job = _make_script_job(delete_after_run=True)
        result, mock_run = await _run_script_callback(
            gw,
            job,
            {"status": "ok"},
            vet_reason="Error: cron scheduling blocked by governance policy: x",
        )
        assert result is None
        assert job.fire_time_denied is True
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowed_run_resets_flag_via_execute(self, tmp_path):
        # CronService._execute resets the marker at the start of every run.
        from kiro_crew.cron import CronService

        job = _make_script_job()
        job.fire_time_denied = True  # stale from a prior denied run

        async def _ok(j):
            return "ok"

        svc = CronService(base_dir=tmp_path)
        svc._on_job = _ok
        await svc._execute(job)
        assert job.fire_time_denied is False

    def test_merge_retains_denied_delete_after_run_job(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="one-shot", message="go", at_ts=9999999999.0)
        job.delete_after_run = True
        svc._save()
        job.fire_time_denied = True
        job.last_status = "error"
        job.enabled = False  # _execute parks a denied at-job disabled
        svc._merge_job_result(job)
        # include_disabled: the denied one-shot is parked DISABLED, so the
        # default (enabled-only) listing hides it — retention is what matters.
        stored = next((j for j in svc.list_jobs(include_disabled=True) if j.id == job.id), None)
        assert stored is not None, "denied one-shot was deleted"
        # Parked DISABLED so a past-due at-job cannot refire every tick.
        assert stored.enabled is False
        # A COMPLETED run (no denial) still deletes it.
        job.fire_time_denied = False
        job.last_status = "ok"
        svc._merge_job_result(job)
        assert not any(j.id == job.id for j in svc.list_jobs(include_disabled=True))

    @pytest.mark.asyncio
    async def test_denied_past_due_at_job_does_not_stay_due(self, tmp_path):
        """A past-due at-job denied at fire time must be parked disabled —
        leaving it enabled would make it due again on every timer tick."""
        from kiro_crew.cron import CronService

        job = _make_script_job(schedule=CronSchedule(kind="at", at_ts=1.0), delete_after_run=True)

        async def _deny(j):
            # Mirrors the gateway deny branch: mark refusal, return normally.
            j.last_status = "error"
            j.fire_time_denied = True
            return None

        svc = CronService(base_dir=tmp_path)
        svc._on_job = _deny
        await svc._execute(job)
        assert job.enabled is False
        assert job.fire_time_denied is True


class TestFireTimeAuditTrail:
    """Every fire-time decision — allowed and denied — leaves a SEL
    governance_decision event keyed cron:<job.id>."""

    def test_allowed_fire_emits_governance_decision(self):
        from kiro_crew.mcp_cron import vet_job_at_fire_time

        job = _make_command_job()
        with (
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
            patch("kiro_crew.mcp_cron._vet_command_governance", return_value=None),
            patch("kiro_crew.mcp_cron.sel") as mock_sel,
        ):
            assert vet_job_at_fire_time(job) is None
        calls = mock_sel.return_value.log_governance_decision.call_args_list
        assert any(
            c.kwargs.get("outcome") == "allowed" and c.kwargs.get("session_key") == f"cron:{job.id}"
            for c in calls
        )

    def test_denied_fire_emits_governance_decision(self):
        from kiro_crew.mcp_cron import vet_job_at_fire_time

        job = _make_command_job()
        with (
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
            patch(
                "kiro_crew.mcp_cron._vet_command_governance",
                return_value="Error: cron command blocked by governance policy: x",
            ),
            patch("kiro_crew.mcp_cron.sel") as mock_sel,
        ):
            assert vet_job_at_fire_time(job) is not None
        calls = mock_sel.return_value.log_governance_decision.call_args_list
        assert any(
            c.kwargs.get("outcome") == "denied" and c.kwargs.get("scope") == "commands"
            for c in calls
        )

    def test_script_body_deny_emits_scoped_decision(self):
        from kiro_crew.mcp_cron import vet_job_at_fire_time

        job = _make_script_job()
        with (
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
            patch("kiro_crew.mcp_cron.resolve_script_path", return_value=("/tmp/x.py", "run")),
            patch(
                "kiro_crew.mcp_cron._vet_script_file", return_value="Error: cron script blocked: x"
            ),
            patch("kiro_crew.mcp_cron.sel") as mock_sel,
        ):
            assert vet_job_at_fire_time(job) is not None
        calls = mock_sel.return_value.log_governance_decision.call_args_list
        assert any(
            c.kwargs.get("outcome") == "denied" and c.kwargs.get("scope") == "cron_script_body"
            for c in calls
        )


class TestTimeoutPersistence:
    """Test that timeout field survives save/load cycle."""

    def test_timeout_round_trips(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(
            name="timeout-test",
            message="test",
            every_secs=60,
        )
        job.timeout = 180
        svc._save()

        svc2 = CronService(base_dir=tmp_path)
        jobs = svc2.list_jobs()
        loaded = next((j for j in jobs if j.id == job.id), None)
        assert loaded is not None
        assert loaded.timeout == 180


class TestAutoPause:
    """Test that jobs auto-pause after 5 consecutive failures."""

    @pytest.mark.asyncio
    async def test_script_auto_pauses_after_5_errors(self):
        gw = _make_gw()
        job = _make_script_job()
        for i in range(4):
            await _run_script_callback(gw, job, {"status": "error", "error": f"fail {i}"})
            assert job.enabled is True, f"Should not pause after {i+1} failures"
            assert job.auto_paused is False
        await _run_script_callback(gw, job, {"status": "error", "error": "fail 4"})
        assert job.enabled is False
        # auto_paused is the durable reason the pause survives a reload; without
        # it, _load re-derives enabled=True (user_paused stays False) and the
        # failing job resurrects on the next daemon restart.
        assert job.auto_paused is True
        assert job.consecutive_failures == 5

    @pytest.mark.asyncio
    async def test_command_auto_pauses_after_5_errors(self):
        gw = _make_gw()
        job = _make_command_job()
        for i in range(4):
            await _run_command_callback(
                gw, job, {"status": "error", "output": f"err {i}", "exit_code": 1}
            )
            assert job.enabled is True
        await _run_command_callback(gw, job, {"status": "error", "output": "err 4", "exit_code": 1})
        assert job.enabled is False
        assert job.auto_paused is True
        assert job.consecutive_failures == 5

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        gw = _make_gw()
        job = _make_script_job()
        for _ in range(4):
            await _run_script_callback(gw, job, {"status": "error", "error": "fail"})
        assert job.consecutive_failures == 4
        await _run_script_callback(gw, job, {"status": "ok"})
        assert job.consecutive_failures == 0
        assert job.enabled is True
        assert job.auto_paused is False


# ── Per-job model override: _acquire_with_model_fallback / _annotate_model_downgrade ──


def _make_llm_job(**overrides):
    """LLM-based cron job (no script, no command) with optional model override."""
    defaults = dict(
        id="lj1",
        name="llm-job",
        message="Run daily check",
        schedule=CronSchedule(kind="every", every_secs=3600),
    )
    defaults.update(overrides)
    return CronJob(**defaults)


def _make_gw_for_llm():
    """Extended _make_gw with attributes the LLM single-agent path needs."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.ctx_builder = MagicMock()
    gw.slack = None  # suppress Slack delivery
    gw.conv_log = None
    gw.dashboard_state = MagicMock()
    gw.dashboard_state.get_slot = MagicMock(return_value=None)
    gw.dashboard_state.has_slot = MagicMock(return_value=False)
    gw.dashboard_state.notify = MagicMock()
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._running_script_ids = set()
    gw._no_crons = False
    gw.cron_svc = MagicMock()
    gw.cron_svc.remove_job_async = AsyncMock(return_value=True)
    gw._cfg = MagicMock()
    gw._cfg.agent.provider = "acp"
    gw._cfg.hooks = {}
    gw._approval_mode = None
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.set_thread = AsyncMock()
    gw.sessions.set_channel = AsyncMock()
    gw.sessions.get_channel = MagicMock(return_value=None)
    gw.ctx_builder.build_message = MagicMock(return_value=("full prompt", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    return gw


async def _run_llm_callback(gw, job, *, get_or_create_side_effect=None):
    """Run the cron callback for an LLM-based job through _init_cron.

    get_or_create_side_effect: if provided, set as the side_effect on
    sessions.get_or_create (for simulating model errors / fallback).
    """
    captured_cb = None

    if get_or_create_side_effect is not None:
        gw.sessions.get_or_create = AsyncMock(side_effect=get_or_create_side_effect)
    else:
        provider_mock = MagicMock()
        gw.sessions.get_or_create = AsyncMock(return_value=(provider_mock, True, False))

    _embed_mock = AsyncMock(return_value=("full prompt", None))
    _stream_mock = AsyncMock(return_value="Agent response here")

    with (
        patch.object(CronService, "create", new_callable=AsyncMock) as mock_cron_create,
        patch("kiro_crew.slack.gateway.run_in_embed_pool", _embed_mock),
        patch("kiro_crew.slack.gateway.stream_and_collect", _stream_mock),
        patch("kiro_crew.slack.gateway.sel"),
        patch("kiro_crew.slack.gateway.build_cron_session_context") as mock_ctx,
    ):

        mock_ctx.return_value = (f"cron:{job.id}", job.message)

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = _mock_cron_service()
            return svc

        mock_cron_create.side_effect = capture_cron
        await gw._init_cron()
        assert captured_cb is not None
        result = await captured_cb(job)
        return result, _stream_mock


class TestLlmCronAdmission:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_modes", [False, True])
    async def test_execution_binding_runs_off_loop(self, monkeypatch, with_modes):
        from kiro_crew import execution_context

        gw = _make_gw_for_llm()
        gw.ctx_builder._session_memory_modes = {} if with_modes else None
        job = _make_llm_job()
        loop_thread = threading.get_ident()
        bindings = []
        original = execution_context.bind_session_execution

        def bind(key, execution):
            bindings.append((threading.get_ident(), key, execution))
            return original(key, execution)

        monkeypatch.setattr(execution_context, "bind_session_execution", bind)
        result, _ = await asyncio.wait_for(_run_llm_callback(gw, job), 10)
        assert result == "Agent response here"
        # Mode publication also tightens the same canonical record.
        assert len(bindings) == (2 if with_modes else 1)
        thread, key, captured = bindings[0]
        assert all(thread != loop_thread for thread, _, _ in bindings)
        assert key == f"cron:{job.id}"
        assert captured == execution_context.read_session_execution(key, required=True)

    @pytest.mark.asyncio
    async def test_session_closing_retains_undispatched_one_shot(self):
        from kiro_crew.session import SessionClosingError

        gw = _make_gw_for_llm()
        job = _make_llm_job(
            delete_after_run=True,
            last_result="stale result",
            consecutive_failures=2,
        )

        async def _side_effect(*_args, **_kwargs):
            raise SessionClosingError("automatic update owns admission")

        result, stream = await _run_llm_callback(
            gw,
            job,
            get_or_create_side_effect=_side_effect,
        )

        assert result is None
        assert job.run_never_started is True
        assert job.last_status == "error"
        assert job.last_error == "gateway admission is closed"
        assert job.last_result == ""
        assert job.consecutive_failures == 2
        stream.assert_not_awaited()


class TestModelFallback:
    """Test _acquire_with_model_fallback and _annotate_model_downgrade paths."""

    @pytest.mark.asyncio
    async def test_model_override_passed_to_session(self):
        """When job.model is set, get_or_create receives it."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-opus-4-8")

        result, _ = await _run_llm_callback(gw, job)
        # The first get_or_create call should have model=job.model
        call_kwargs = gw.sessions.get_or_create.call_args_list[0].kwargs
        assert call_kwargs["model"] == "claude-opus-4-8"
        assert result == "Agent response here"

    @pytest.mark.asyncio
    async def test_no_model_passes_none(self):
        """When job.model is empty, get_or_create receives model=None."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")

        result, _ = await _run_llm_callback(gw, job)
        call_kwargs = gw.sessions.get_or_create.call_args_list[0].kwargs
        assert call_kwargs["model"] is None

    @pytest.mark.asyncio
    async def test_model_unavailable_falls_back(self):
        """When pinned model fails with model-related error, retries without model."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-nonexistent-9")
        provider_mock = MagicMock()

        call_count = [0]

        async def _side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("model 'claude-nonexistent-9' not found")
            return (provider_mock, True, False)

        result, _ = await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)
        assert call_count[0] == 2
        assert "unavailable" in result
        assert "Agent response here" in result

    @pytest.mark.asyncio
    async def test_model_fallback_annotates_response(self):
        """Downgraded result is prefixed with a warning annotation."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-fancy-model")
        provider_mock = MagicMock()

        call_count = [0]

        async def _side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("model 'claude-fancy-model' is not available")
            return (provider_mock, True, False)

        result, _ = await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)
        assert result.startswith("⚠️")
        assert "claude-fancy-model" in result
        assert "Agent response here" in result

    @pytest.mark.asyncio
    async def test_non_model_error_propagates(self):
        """Errors unrelated to model are not caught by the fallback."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-opus-4-8")

        async def _side_effect(*args, **kwargs):
            raise RuntimeError("connection refused to provider host")

        with pytest.raises(RuntimeError, match="connection refused"):
            await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)

    @pytest.mark.asyncio
    async def test_no_fallback_when_model_empty(self):
        """When job.model is empty, any error propagates (no fallback needed)."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")

        async def _side_effect(*args, **kwargs):
            raise RuntimeError("model spawn failed")

        with pytest.raises(RuntimeError, match="model spawn failed"):
            await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)


class TestThrottleFallbackCronWiring:
    """agent.fallback_model reaches the cron turn, and a fallback-served run
    is visibly annotated (never silent)."""

    @pytest.mark.asyncio
    async def test_chain_is_threaded_into_stream_and_collect(self):
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")
        with patch(
            "kiro_crew.slack.gateway.configured_fallback_chain",
            return_value=("fb-1", "fb-2"),
        ):
            _result, stream_mock = await _run_llm_callback(gw, job)
        assert stream_mock.await_args.kwargs["fallback_models"] == ("fb-1", "fb-2")

    @pytest.mark.asyncio
    async def test_fallback_served_run_is_annotated(self):
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")
        provider_mock = MagicMock()
        setattr(provider_mock, TURN_FALLBACK_ATTR, ("primary-m", "fb-m"))

        async def _side_effect(*args, **kwargs):
            return (provider_mock, True, False)

        result, _ = await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)
        assert result.startswith("⚠️ Model 'primary-m' throttled")
        assert "fb-m" in result
        assert "Agent response here" in result

    @pytest.mark.asyncio
    async def test_fallback_served_run_blanks_pinned_model_in_usage_row(self):
        """A pinned job.model must NOT be billed while a fallback served the
        turn — the usage row blanks it so model_source reports what ran."""
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        gw = _make_gw_for_llm()
        job = _make_llm_job(model="pinned-model")
        provider_mock = MagicMock()
        setattr(provider_mock, TURN_FALLBACK_ATTR, ("pinned-model", "fb-m"))

        async def _side_effect(*args, **kwargs):
            return (provider_mock, True, False)

        with patch(
            "kiro_crew.slack.gateway.persist_token_record_async", new_callable=AsyncMock
        ) as persist_mock:
            await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)
        assert persist_mock.await_count >= 1
        # Positional arg 1 is the model; blank = defer to model_source.
        assert persist_mock.await_args.args[1] == ""

    @pytest.mark.asyncio
    async def test_unswapped_run_still_bills_pinned_model(self):
        """No fallback marker -> the explicit pin is recorded unchanged."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="pinned-model")
        with patch(
            "kiro_crew.slack.gateway.persist_token_record_async", new_callable=AsyncMock
        ) as persist_mock:
            await _run_llm_callback(gw, job)
        assert persist_mock.await_count >= 1
        assert persist_mock.await_args.args[1] == "pinned-model"

    def test_annotate_noop_without_marker(self):
        from types import SimpleNamespace

        from kiro_crew.slack.gateway import _annotate_model_fallback

        provider = SimpleNamespace()
        assert _annotate_model_fallback("text", provider) == "text"

    def test_annotate_malformed_marker_is_noop(self):
        from types import SimpleNamespace

        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR
        from kiro_crew.slack.gateway import _annotate_model_fallback

        provider = SimpleNamespace()
        setattr(provider, TURN_FALLBACK_ATTR, ("only-one",))
        assert _annotate_model_fallback("text", provider) == "text"

    def test_gateway_annotator_is_the_shared_body(self):
        """DRIFT PIN: the gateway name must BE the shared
        helper next to TURN_FALLBACK_ATTR — not a re-spelled copy."""
        from kiro_crew.llm_helpers import annotate_model_fallback
        from kiro_crew.slack.gateway import _annotate_model_fallback

        assert _annotate_model_fallback is annotate_model_fallback

    @pytest.mark.asyncio
    async def test_chain_exhaustion_story_reaches_the_failure_alert(self):
        """A cron turn failing after the chain exhausted must
        alert with the WHOLE walk (the story the walk attached to the
        exception), not just the last candidate's error — on BOTH the
        dashboard notify and the Slack DM legs, and even when the backend
        error is verbose enough to hit the detail cap (a real ACP throttle
        payload routinely exceeds it)."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR

        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")
        # The Slack DM leg: slack client present, no channel delivery.
        gw.slack = MagicMock()
        gw.slack.post_message = AsyncMock()
        gw._open_dm_with_retry = AsyncMock(return_value="D123")
        gw._deliver_cron_to_channel = AsyncMock(return_value=False)

        # Verbose error: longer than _CRON_FAILURE_DETAIL_CAP so the story
        # would be sliced away if it were appended after the cap.
        exc = AcpError("Internal error: API Error: " + "x" * 700)
        setattr(
            exc, FALLBACK_STORY_ATTR, "primary-m throttled; fallbacks fb-1, fb-2 also unavailable"
        )

        captured_cb = None
        provider_mock = MagicMock()
        gw.sessions.get_or_create = AsyncMock(return_value=(provider_mock, True, False))
        _embed_mock = AsyncMock(return_value=("full prompt", None))
        _stream_mock = AsyncMock(side_effect=exc)

        with (
            patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
            patch("kiro_crew.slack.gateway.run_in_embed_pool", _embed_mock),
            patch("kiro_crew.slack.gateway.stream_and_collect", _stream_mock),
            patch("kiro_crew.slack.gateway.sel"),
            patch("kiro_crew.slack.gateway.build_cron_session_context") as mock_ctx,
        ):
            mock_ctx.return_value = (f"cron:{job.id}", job.message)

            def capture_cron(on_job=None, **kw):
                nonlocal captured_cb
                captured_cb = on_job
                svc = MagicMock()
                svc.start = AsyncMock()
                svc.remove_job_async = AsyncMock(return_value=True)
                return svc

            mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
            await gw._init_cron()
            assert captured_cb is not None
            with pytest.raises(AcpError):
                await captured_cb(job)

            # Same backend error, DIFFERENT walk (a candidate momentarily
            # unadvertised changes the story): the dedup hash keys on the
            # story-free error text, so the repeat is suppressed instead of
            # re-paging the user.
            exc2 = AcpError("Internal error: API Error: " + "x" * 700)
            setattr(
                exc2, FALLBACK_STORY_ATTR, "primary-m throttled; fallbacks fb-2 also unavailable"
            )
            _stream_mock.side_effect = exc2
            with pytest.raises(AcpError):
                await captured_cb(job)

        bodies = [
            str(c.args[2]) for c in gw.dashboard_state.notify.call_args_list if len(c.args) >= 3
        ]
        assert any(
            "primary-m throttled" in b and "fb-1, fb-2 also unavailable" in b for b in bodies
        ), f"expected the chain story in a failure alert, got {bodies!r}"
        # The delivered detail stays bounded by the cap even with the story
        # appended (the budget trims the error part). +60 absorbs the fixed
        # "❌ Job failed:\n" / "❌ Job failed (suppressed — same error):\n"
        # prefixes around exc_detail — the bound under test is exc_detail's.
        from kiro_crew.slack.gateway import _CRON_FAILURE_DETAIL_CAP

        for b in bodies:
            assert len(b) <= _CRON_FAILURE_DETAIL_CAP + 60, f"unbounded alert body: {len(b)} chars"
        # The Slack DM — the surface a user actually gets paged on — carries
        # the story too.
        slack_texts = [str(c.args[1]) for c in gw.slack.post_message.call_args_list]
        assert any(
            "primary-m throttled" in t and "also unavailable" in t for t in slack_texts
        ), f"expected the chain story in the Slack alert, got {slack_texts!r}"
        # The second run took the dup-suppress path (story-free hash), with a
        # bounded body that still carries its own walk.
        assert any("suppressed — same error" in b for b in bodies), bodies
        assert any("fb-2 also unavailable" in b for b in bodies), bodies


class TestExecutePreservesCallbackStatus:
    """CronService._execute must not clobber a failure the callback reported by
    mutating the job. Command/script callbacks return NORMALLY and signal
    failure via job.last_status="error"; only the LLM path raises. Overwriting
    unconditionally with "ok" mis-reported failed runs as healthy on the
    dashboard and in cron_list.
    """

    @pytest.mark.asyncio
    async def test_execute_preserves_callback_error(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)

        async def failing_cb(job):
            # command/script contract: report failure by mutation, return normally
            job.last_status = "error"
            job.last_error = "command failed (exit_code=1)"

        svc._on_job = failing_cb
        job = svc.add_job("failing", "false", every_secs=3600)
        await svc._execute(job)
        assert job.last_status == "error"
        assert job.last_error == "command failed (exit_code=1)"

    @pytest.mark.asyncio
    async def test_execute_marks_ok_when_callback_clean(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)

        async def clean_cb(job):
            return None  # no error mutation, no raise

        svc._on_job = clean_cb
        job = svc.add_job("okjob", "echo hi", every_secs=3600)
        await svc._execute(job)
        assert job.last_status == "ok"
        assert job.last_error is None

    @pytest.mark.asyncio
    async def test_execute_marks_error_when_callback_raises(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)

        async def raising_cb(job):
            raise RuntimeError("boom")

        svc._on_job = raising_cb
        job = svc.add_job("raises", "x", every_secs=3600)
        await svc._execute(job)
        assert job.last_status == "error"
        assert "boom" in job.last_error

    @pytest.mark.asyncio
    async def test_execute_clears_stale_error_on_success(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        results = ["error", "clean"]

        async def cb(job):
            if results.pop(0) == "error":
                job.last_status = "error"
                job.last_error = "prior failure"

        svc._on_job = cb
        job = svc.add_job("flappy", "cmd", every_secs=3600)
        await svc._execute(job)  # first run fails
        assert job.last_status == "error"
        await svc._execute(job)  # second run clean → must reset to ok, not stay error
        assert job.last_status == "ok"
        assert job.last_error is None


class TestCronUsageRow:
    """Every model-spending cron turn appends exactly one usage row
    tagged surface='cron'; the zero-token script/command modes append none."""

    @pytest.mark.asyncio
    async def test_llm_cron_persists_usage_row_with_surface(self):
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-opus-4-8")

        persist = AsyncMock()
        # Patch gateway's own bindings: the imports are at module scope there,
        # so patching the source module would not be seen by the call site.
        with (
            patch("kiro_crew.slack.gateway.persist_token_record_async", persist),
            patch(
                "kiro_crew.slack.gateway.read_context_tokens",
                MagicMock(return_value=(1234, 200000)),
                create=True,
            ),
        ):
            await _run_llm_callback(gw, job)

        persist.assert_awaited_once()
        kwargs = persist.await_args.kwargs
        assert kwargs["surface"] == "cron"
        assert kwargs["provider"] == "acp"
        assert kwargs["context_used"] == 1234
        assert kwargs["context_window"] == 200000

    @pytest.mark.asyncio
    async def test_cron_row_records_resolved_agent_not_requested(self):
        """The agent that served the turn wins over the configured alias."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-opus-4-8")

        persist = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.persist_token_record_async", persist),
            patch(
                "kiro_crew.slack.gateway.read_context_tokens",
                MagicMock(return_value=(10, 100)),
                create=True,
            ),
            patch(
                "kiro_crew.slack.gateway.read_effective_agent",
                MagicMock(return_value="kirocrew"),
                create=True,
            ),
        ):
            await _run_llm_callback(gw, job)

        persist.assert_awaited_once()
        assert persist.await_args.kwargs["agent"] == "kirocrew"

    @pytest.mark.asyncio
    async def test_downgraded_cron_does_not_record_rejected_model(self):
        """A model that was refused never ran, so it must not be attributed."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-nonexistent-9")
        provider_mock = MagicMock()

        call_count = [0]

        async def _side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("model 'claude-nonexistent-9' not found")
            return (provider_mock, True, False)

        persist = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.persist_token_record_async", persist),
            patch(
                "kiro_crew.slack.gateway.read_context_tokens",
                MagicMock(return_value=(10, 100)),
                create=True,
            ),
        ):
            await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)

        persist.assert_awaited_once()
        # Positional arg 1 is the model; blank defers to model_source, which
        # reports the model that actually served the turn.
        assert persist.await_args.args[1] == ""

    @pytest.mark.asyncio
    async def test_script_cron_writes_no_usage_row(self):
        gw = _make_gw()
        job = _make_script_job()

        persist = AsyncMock()
        with patch("kiro_crew.slack.gateway.persist_token_record_async", persist):
            await _run_script_callback(gw, job, {"status": "ok"})

        persist.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_command_cron_writes_no_usage_row(self):
        gw = _make_gw()
        job = _make_command_job()

        persist = AsyncMock()
        with patch("kiro_crew.slack.gateway.persist_token_record_async", persist):
            await _run_command_callback(
                gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0}
            )

        persist.assert_not_awaited()


def test_shutdown_cancel_keeps_the_last_completed_result(tmp_path) -> None:
    """A shutdown cancel must not wipe the previous run's result.

    stop() cancels the in-flight task but never adds the job to _cancelled_jobs,
    so the funnel's result-less clear would otherwise run on every gateway stop
    and persist an empty result over the last completed run's output.
    """
    import asyncio

    from kiro_crew.cron import CronJob, CronSchedule, CronService

    async def _hang(*args, **kwargs):
        await asyncio.sleep(9999)

    async def _drive() -> CronJob:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="test",
            message="go",
            command="echo hi",
            schedule=CronSchedule(kind="every", every_secs=60),
            last_result="42 widgets",
        )
        svc._jobs = [job]
        svc._save()
        with patch.object(svc, "_execute", side_effect=_hang):
            task = asyncio.create_task(svc._run_job_isolated(job))
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return job

    job = asyncio.run(_drive())
    assert job.last_result == "42 widgets", "a shutdown cancel wiped a completed result"


@pytest.mark.asyncio
async def test_shutdown_cancel_preserves_claimed_owed_occurrence(tmp_path: Path) -> None:
    now = 1767333600.0
    occurrence_id = str(int(now) // 60)
    svc = CronService(base_dir=tmp_path)
    job = CronJob(
        id="j1",
        name="owed",
        message="go",
        schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
        strict_schedule=True,
        timezone="UTC",
        owed_fire=True,
        owed_fire_id=occurrence_id,
    )
    svc._jobs = [job]
    svc._save()
    svc._running = True
    svc._mint_run_token(job.id)
    svc._owed_fire_runs[job.id] = occurrence_id
    svc._run_occurrence_ids[job.id] = occurrence_id
    svc._executing.add(job.id)
    entered = asyncio.Event()

    async def blocked(_job: CronJob) -> None:
        entered.set()
        await asyncio.Future()

    svc._on_job = blocked
    run_task = asyncio.create_task(svc._run_job_isolated(job))
    svc._running_tasks[job.id] = run_task
    await asyncio.wait_for(entered.wait(), timeout=2)

    await svc.stop()

    assert run_task.cancelled()
    stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
    assert stored is not None
    assert stored.owed_occurrence() == occurrence_id

    replacement = CronService(base_dir=tmp_path)
    retried: list[str] = []

    async def retry(retry_job: CronJob) -> None:
        retried.append(retry_job.id)

    replacement._on_job = retry
    admitted = type("Admission", (), {"admitted": True, "reason": ""})()
    with (
        patch("kiro_crew.cron.time.time", return_value=now + 3600),
        patch("kiro_crew.cron.admission_check", return_value=admitted),
    ):
        await replacement._on_timer()
        retry_tasks = list(replacement._running_tasks.values())
        assert retry_tasks
        await asyncio.gather(*retry_tasks)

    settled = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
    assert settled is not None
    assert settled.owed_occurrence() is None
    assert retried == [job.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_state", ["completed", "newer-debt"])
async def test_shutdown_cancel_arbitrates_fresh_replacement_state(
    tmp_path: Path, fresh_state: str
) -> None:
    now = 1767333600.0
    occurrence_id = str(int(now) // 60)
    newer_occurrence = str(int(occurrence_id) + 1440)
    svc = CronService(base_dir=tmp_path)
    job = CronJob(
        id="j1",
        name="owed",
        message="go",
        schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
        strict_schedule=True,
        timezone="UTC",
        owed_fire=True,
        owed_fire_id=occurrence_id,
        last_result="drained result",
        last_retry_count=9,
        consecutive_failures=4,
    )
    svc._jobs = [job]
    svc._save()
    svc._running = True
    svc._mint_run_token(job.id)
    svc._owed_fire_runs[job.id] = occurrence_id
    svc._run_occurrence_ids[job.id] = occurrence_id
    svc._executing.add(job.id)
    entered = asyncio.Event()
    expected_replacement: list[tuple[object, ...]] = []

    def runtime_state(current: CronJob) -> tuple[object, ...]:
        return (
            current.last_run_ts,
            current.last_status,
            current.last_error,
            current.last_result,
            current.last_result_ts,
            current.last_retry_count,
            current.last_retry_run_ts,
            current.consecutive_failures,
            current.owed_occurrence(),
        )

    async def blocked(_job: CronJob) -> None:
        entered.set()
        await asyncio.Future()

    real_merge = svc._merge_job_result

    def publish_then_merge(running_job: CronJob) -> None:
        replacement = CronService(base_dir=tmp_path)
        replacement_job = replacement.get_job(running_job.id)
        assert replacement_job is not None
        if fresh_state == "completed":
            replacement_job.last_run_ts = now + 10
            replacement_job.last_status = "ok"
            replacement_job.last_error = None
            replacement_job.last_result = "replacement result"
            replacement_job.last_result_ts = now + 10
            replacement_job.last_retry_count = 2
            replacement_job.last_retry_run_ts = now + 10
            replacement_job.consecutive_failures = 0
            replacement_job.set_owed_occurrence(None)
        else:
            replacement_job.set_owed_occurrence(newer_occurrence)
        replacement._save()
        expected_replacement.append(runtime_state(replacement_job))
        real_merge(running_job)

    svc._on_job = blocked
    run_task = asyncio.create_task(svc._run_job_isolated(job))
    svc._running_tasks[job.id] = run_task
    await asyncio.wait_for(entered.wait(), timeout=2)

    with patch.object(svc, "_merge_job_result", side_effect=publish_then_merge):
        await svc.stop()

    assert run_task.cancelled()
    assert len(expected_replacement) == 1
    stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
    assert stored is not None
    if fresh_state == "completed":
        assert runtime_state(stored) == expected_replacement[0]
        hot = svc.get_job(job.id)
        assert hot is not None
        assert runtime_state(hot) == expected_replacement[0]
    else:
        assert stored.owed_occurrence() == newer_occurrence


async def _run_script_callback_behind_a_busy_worker(gw, job, script_result, hold_secs):
    """Drive the script branch with the cron pool's only worker already busy.

    The blocker is submitted from INSIDE the fire-time gate. That ordering is
    what puts it ahead of the script in the cron pool's FIFO queue, so the script
    genuinely waits for a worker -- the production condition. Submitting it up
    front would let the gate run first and the script would then find a free
    worker and never queue.

    The gate itself does not touch this pool: it runs on the dedicated
    ``mc-crongate`` pool with a bound of its own, so starving the cron pool
    cannot starve the gate. That independence is the point -- it is why this
    fixture can saturate the cron pool without perturbing the gate under test.
    """
    captured_cb = None
    release = threading.Event()

    def _occupy() -> None:
        release.wait(timeout=60)

    def _vet(_job):
        ex.cron_executor().submit(_occupy)
        return None

    ex.shutdown_maintenance_executor()
    with (
        patch.object(ex, "_MAX_CRON_WORKERS", 1),
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch(
            "kiro_crew.slack.gateway.run_script_sandboxed", return_value=script_result
        ) as mock_run,
        patch("kiro_crew.slack.gateway.vet_job_at_fire_time", side_effect=_vet),
        patch("kiro_crew.slack.gateway.sel"),
    ):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = _mock_cron_service()
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
        try:
            await gw._init_cron()
            assert captured_cb is not None
            asyncio.get_running_loop().call_later(hold_secs, release.set)
            return await captured_cb(job), mock_run
        finally:
            release.set()
            ex.shutdown_maintenance_executor()


class TestCronPoolQueueWait:
    """Queue time in the bounded cron pool must not be charged to the job.

    ``run_in_executor`` only SUBMITS. The cron pool is bounded at
    ``_MAX_CRON_WORKERS``, so a job whose workers are all busy sits in the
    pool's queue having run nothing -- while the old
    ``wait_for(run_in_executor(...), timeout=job_timeout + 5)`` counted from the
    moment it was awaited. Queue time was therefore spent out of the job's own
    budget, and the kill was recorded as ``timeout (Ns)`` naming the script, so
    one wedged job reads as N broken ones.
    """

    @pytest.mark.asyncio
    async def test_fast_script_waiting_for_a_worker_is_not_killed_as_a_script_timeout(self):
        """Negative control: fails pre-fix, for the reason the defect describes.

        The script here is instant (mocked ok) but the only worker is held for
        6.5s, past the 6s the pre-fix budget allowed a 1s-timeout job. Pre-fix
        this is killed and labelled ``timeout (6s)``; the fix lets it wait for a
        worker untimed and then run.
        """
        gw = _make_gw()
        job = _make_script_job(timeout=1)  # pre-fix budget: 1 + 5 = 6s

        result, mock_run = await _run_script_callback_behind_a_busy_worker(
            gw, job, {"status": "ok"}, hold_secs=6.5
        )

        assert mock_run.called, "the script never ran -- it was killed while queued"
        assert job.last_status == "ok", f"expected ok, got {job.last_status}: {job.last_error}"
        assert job.last_error == ""
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_starved_script_says_queued_never_ran_not_timeout(self):
        """The distinct text is the point: saturation must not read as N defects."""
        gw = _make_gw()
        job = _make_script_job(last_result="42 widgets")
        result, _ = await _run_script_callback(gw, job, side_effect=ex.CronQueueTimeout(300.0))
        assert result is None
        assert job.last_status == "error"
        assert job.last_error == "queued 300s, never ran"
        # Must NOT be mistakable for the script itself overrunning.
        assert "timeout (" not in job.last_error
        # A failed run must not present the previous run's output as its own.
        assert job.last_result == ""

    @pytest.mark.asyncio
    async def test_starved_command_says_queued_never_ran_not_timeout(self):
        """The command branch carries the same defect and the same fix."""
        gw = _make_gw()
        job = _make_command_job(last_result="42 widgets")
        result, _ = await _run_command_callback(
            gw, job, side_effect=ex.CronQueueTimeout(300.0), vet_reason=None
        )
        assert result is None
        assert job.last_status == "error"
        assert job.last_error == "queued 300s, never ran"
        assert "timeout (" not in job.last_error
        assert job.last_result == ""

    @pytest.mark.asyncio
    async def test_a_real_script_overrun_is_still_reported_as_a_timeout(self):
        """The backstop label must survive: only the QUEUE case gets new text.

        CronQueueTimeout subclasses asyncio.TimeoutError, so this also pins the
        except-order -- a plain TimeoutError must not be captured by the queue
        branch and mislabelled.
        """
        gw = _make_gw()
        job = _make_script_job(timeout=30)
        result, _ = await _run_script_callback(gw, job, side_effect=asyncio.TimeoutError())
        assert result is None
        assert job.last_status == "error"
        assert job.last_error == "timeout (35s)"
        assert "queued" not in job.last_error

    @pytest.mark.asyncio
    async def test_repeated_starvation_never_auto_pauses_a_healthy_script(self):
        """Starvation must not consume the auto-pause budget.

        ``record_failure`` auto-pauses at ``_AUTO_PAUSE_THRESHOLD`` by clearing
        ``enabled``, and a paused job never fires again. Counting a queue timeout
        there means a saturated pool permanently disables jobs that never ran a
        line, so when the pool recovers the work is silently unscheduled. The
        fire-time governance deny path already declines to count for exactly this
        reason. Asserts the counter and the pause flag, not merely the absence of
        an exception.
        """
        from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD

        gw = _make_gw()
        job = _make_script_job()
        for _ in range(_AUTO_PAUSE_THRESHOLD + 2):
            result, _ = await _run_script_callback(gw, job, side_effect=ex.CronQueueTimeout(300.0))
            assert result is None
        assert job.consecutive_failures == 0, "starvation must not consume the auto-pause budget"
        assert not job.auto_paused, "a job that never ran must not be auto-paused"
        assert job.enabled, "auto-pause clears enabled -- the job would never fire again"
        # Still reported as an error, with the legible starvation text.
        assert job.last_status == "error"
        assert job.last_error == "queued 300s, never ran"

    @pytest.mark.asyncio
    async def test_repeated_starvation_never_auto_pauses_a_healthy_command(self):
        """The command branch carries the same counter and the same fix."""
        from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD

        gw = _make_gw()
        job = _make_command_job()
        for _ in range(_AUTO_PAUSE_THRESHOLD + 2):
            result, _ = await _run_command_callback(
                gw, job, side_effect=ex.CronQueueTimeout(300.0), vet_reason=None
            )
            assert result is None
        assert job.consecutive_failures == 0
        assert not job.auto_paused
        assert job.enabled

    @pytest.mark.asyncio
    async def test_the_enclosing_wake_deadline_also_excludes_queue_wait(self, tmp_path):
        """Excluding queue wait from the per-call kwarg is only half the job.

        ``CronService._execute_with_timeout`` wraps the whole run and the pool's
        queue wait happens inside it, so without a queue term there a job still
        sitting in the queue is killed by the WAKE deadline and reported as an
        execution overrun -- the same misdiagnosis, one frame out. Worse, a thread
        cannot be interrupted, so a worker that claimed the call as the deadline
        fired keeps running while the overlap guards clear.

        Fails pre-fix: the command job below is killed at its 2s wake budget with
        ``Timed out after 2s`` and its payload never runs.
        """
        from kiro_crew.cron import CronService

        ex.shutdown_maintenance_executor()
        release = threading.Event()
        try:
            svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
            job = _make_command_job(timeout_secs=2)
            for _ in range(ex._MAX_CRON_WORKERS):
                ex.cron_executor().submit(release.wait, 3.0)
            await asyncio.sleep(0.2)

            ran = threading.Event()

            async def _execute(_job):
                return await ex.run_in_cron_pool(ran.set, timeout=30, queue_timeout=60)

            svc._execute = _execute  # type: ignore[method-assign]
            await svc._execute_with_timeout(job)

            assert ran.is_set(), "the job was killed by the wake deadline while still queued"
            assert job.last_error != "Timed out after 2s"
        finally:
            release.set()
            await asyncio.sleep(0.05)
            ex.shutdown_maintenance_executor()

    @pytest.mark.asyncio
    async def test_a_message_job_wake_budget_is_not_widened(self, tmp_path):
        """The queue term is scoped: a message job never touches the pool.

        Widening every job's wake budget would loosen the backstop that exists so
        a wedged worker cannot leave an entry un-failed forever. Only command and
        script jobs dispatch through the pool, so only they get the allowance.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        job = CronJob(
            id="mj1",
            name="msg-job",
            message="hello",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
        job.timeout_secs = 2

        async def _slow(_job):
            await asyncio.sleep(30)

        svc._execute = _slow  # type: ignore[method-assign]
        await svc._execute_with_timeout(job)
        assert job.last_status == "error"
        assert job.last_error == "Timed out after 2s"

    @pytest.mark.asyncio
    async def test_a_starved_one_shot_job_is_retained_not_deleted(self, tmp_path):
        """A one-shot that never ran must not be consumed by the run it never had.

        ``_merge_job_result`` deletes a ``delete_after_run`` job unless the run was
        refused, and the queue-timeout paths return without marking a refusal -- so
        a job that never got a worker is destroyed exactly as if it had completed,
        and its scheduled work is gone with it. The fire-time deny path already
        carries the retain-on-refusal property; starvation needs the same one for
        the same reason, without borrowing the *policy* meaning of that flag.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        svc._load()
        created = svc.add_job(
            name="one-shot",
            message="",
            every_secs=300,
            command="echo hi",
            delete_after_run=True,
        )
        assert any(j.id == created.id for j in svc.list_jobs())

        gw = _make_gw()
        result, _ = await _run_command_callback(
            gw, created, side_effect=ex.CronQueueTimeout(300.0), vet_reason=None
        )
        assert result is None
        assert created.last_error == "queued 300s, never ran"

        # Off-loop: _merge_job_result enters the bounded sync store lock.
        await asyncio.to_thread(svc._merge_job_result, created)
        assert any(
            j.id == created.id for j in svc.list_jobs()
        ), "a one-shot that never ran was deleted as though it had run"

    @pytest.mark.asyncio
    async def test_a_starved_one_shot_at_job_is_not_parked_disabled(self, tmp_path):
        """Retention must not borrow the policy-denial meaning.

        ``fire_time_denied`` is read in three places: besides suppressing the
        one-shot delete it also forces an ``at`` job disabled, and it is documented
        as a governance refusal. Reusing it for a capacity event would park a
        starved one-shot needing an operator to re-enable it, and would mislabel
        pool saturation as a policy denial in history. Starvation clears on its own,
        so the job must stay schedulable.
        """
        import time

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        svc._load()
        created = svc.add_job(
            name="one-shot-at",
            message="",
            at_ts=time.time() + 3600,
            command="echo hi",
            delete_after_run=True,
        )

        gw = _make_gw()
        result, _ = await _run_command_callback(
            gw, created, side_effect=ex.CronQueueTimeout(300.0), vet_reason=None
        )
        assert result is None
        assert not created.fire_time_denied, "starvation is not a governance denial"

        await asyncio.to_thread(svc._merge_job_result, created)
        survivors = [j for j in svc.list_jobs(include_disabled=True) if j.id == created.id]
        assert survivors, "the starved one-shot at-job was deleted"
        assert survivors[0].enabled, "a starved job must stay schedulable, not be parked"
        assert not survivors[0].user_paused

    @staticmethod
    async def _one_reaper_sweep(svc, job, elapsed_secs):
        """Drive exactly one reaper sweep and report whether it force-killed.

        Registers the job as in-flight for ``elapsed_secs`` with a task that is
        NOT done, so the loop's ``task.done()`` early-continue cannot mask the
        decision, then runs the loop just long enough for one sweep.
        """
        import time as _time
        from unittest.mock import AsyncMock as _AsyncMock

        never = asyncio.get_running_loop().create_future()  # a task that is not done
        holder = asyncio.ensure_future(asyncio.wait_for(never, timeout=30))
        svc._job_start_times[job.id] = _time.time() - elapsed_secs
        svc._running_tasks[job.id] = holder
        svc._jobs = [job]
        reaped = _AsyncMock()
        svc._force_reap = reaped  # type: ignore[method-assign]
        with patch("kiro_crew.cron._REAPER_INTERVAL", 0.01):
            loop_task = asyncio.ensure_future(svc._reaper_loop())
            await asyncio.sleep(0.25)
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
        never.cancel()
        holder.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await holder
        return reaped.called

    @pytest.mark.asyncio
    async def test_the_reaper_does_not_preempt_a_job_inside_the_widened_window(self, tmp_path):
        """The reaper must account for queue wait too, or it cancels a queued job.

        Two deadlines bound one run. Widening only the execution guard leaves the
        reaper's own threshold 900s lower for every command/script job at or above
        the default wake budget, so a job whose gate and payload queued through
        that window is force-killed and the firing is skipped without ever
        executing -- the mirror image of the misdiagnosis the widening removed.

        Fails pre-fix: with the reaper unchanged its deadline is 1800s, the job
        below has been in flight 1850s, and it is reaped.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        job = _make_command_job(timeout_secs=1800)  # the default wake budget
        # 1850s: past the reaper's un-widened 1800s, inside the 2700s execution
        # deadline the job is actually entitled to.
        assert (
            await self._one_reaper_sweep(svc, job, 1850) is False
        ), "the reaper force-killed a job still inside its own execution deadline"

    @pytest.mark.asyncio
    async def test_the_reaper_still_kills_a_job_past_the_widened_deadline(self, tmp_path):
        """The defence-in-depth sweep must keep its teeth.

        The point of the reaper is to catch a run whose ``wait_for`` never fired.
        Accounting for queue wait must move that threshold, not remove it.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        job = _make_command_job(timeout_secs=1800)
        assert (
            await self._one_reaper_sweep(svc, job, 2800) is True
        ), "the reaper stopped killing a genuinely overrunning job"

    @pytest.mark.asyncio
    async def test_the_reaper_window_is_not_widened_for_a_message_job(self, tmp_path):
        """Scoped, like the execution deadline: a message job never uses the pool.

        Widening the reaper for every job would delay the force-kill backstop by a
        quarter of an hour for runs that can never have queued.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        job = CronJob(
            id="mj-reap",
            name="msg-job",
            message="hello",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
        job.timeout_secs = 1800
        assert (
            await self._one_reaper_sweep(svc, job, 1850) is True
        ), "a message job got a pool queue allowance it can never use"

    @pytest.mark.asyncio
    async def test_a_message_jobs_fire_time_gate_does_not_spend_its_execution_budget(
        self, tmp_path
    ):
        """The fire-time governance gate must not queue behind long-running cron work.

        Both deadlines deliberately withhold the pool allowance from a message job,
        on the premise that it never touches the pool. The FIRE-TIME GOVERNANCE GATE
        breaks that premise. ``vet_job_at_fire_time`` is dispatched off-loop for
        every job kind, and dispatching it to the CRON pool puts it behind however
        many long-running command/script jobs hold that pool's
        ``_MAX_CRON_WORKERS`` workers. The wait happens inside the deadline
        ``_execute_with_timeout`` arms BEFORE the callback runs, so a saturated pool
        can spend a message job's entire execution budget before its own dispatch is
        even reached -- and a one-shot is then deleted having never run.

        Widening the message job's budget is the wrong lever: it would delay the
        wedged-delivery backstop by the whole allowance for every message job,
        which is exactly what the two "not widened" tests above pin. The gate
        belongs off the pool that long-running work occupies -- it is a short,
        bounded policy check, not job execution.

        Fails pre-fix: with every cron worker held, the gate queues and the job is
        killed at its 2s budget without ever dispatching.
        """
        from kiro_crew.cron import CronService

        ex.shutdown_maintenance_executor()
        release = threading.Event()
        try:
            gw = _make_gw()
            captured_cb = None

            with (
                patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
                patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
                patch("kiro_crew.slack.gateway.sel"),
            ):

                def capture_cron(on_job=None, **kw):
                    nonlocal captured_cb
                    captured_cb = on_job
                    svc = MagicMock()
                    svc.start = AsyncMock()
                    svc.remove_job_async = AsyncMock(return_value=True)
                    return svc

                mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
                await gw._init_cron()
                assert captured_cb is not None

                # The defect's own premise: long-running pool jobs holding every
                # worker, held well past the message job's whole 2s budget.
                for _ in range(ex._MAX_CRON_WORKERS):
                    ex.cron_executor().submit(release.wait, 5.0)
                await asyncio.sleep(0.2)

                job = CronJob(
                    id="mj-gate",
                    name="msg-job",
                    message="hello",
                    schedule=CronSchedule(kind="every", every_secs=60),
                )
                job.timeout_secs = 2

                svc = CronService(base_dir=tmp_path, on_job=captured_cb)
                await svc._execute_with_timeout(job)

            assert gw.sessions.get_or_create.called, (
                "the fire-time gate wait consumed the message job's whole execution "
                "budget: it was killed before its own dispatch was reached"
            )
            assert job.last_error != "Timed out after 2s"
        finally:
            release.set()
            await asyncio.sleep(0.05)
            ex.shutdown_maintenance_executor()

    @pytest.fixture
    def gate_pool(self):
        """Fresh pools, plus a release event so a starved gate never outlives the test."""
        ex.shutdown_maintenance_executor()
        release = threading.Event()
        try:
            yield release
        finally:
            release.set()
            ex.shutdown_maintenance_executor()

    async def _message_one_shot_run(
        self,
        tmp_path,
        release,
        *,
        starve: bool,
        budget: int = 8,
        slow_gate: bool = False,
        cancel_gate: bool = False,
    ):
        """Drive the REAL message callback under the REAL wake deadline.

        Returns ``(svc, job, gw)`` after ``_execute_with_timeout`` has returned, so
        the caller can run the real ``_merge_job_result`` and inspect retention.
        ``starve`` occupies the gate pool so the call never gets a worker (the
        QUEUE bound). ``slow_gate`` leaves the pool free and makes the gate's own
        work overrun (the EXECUTION bound). Those two bounds fail differently
        inside ``run_in_cron_pool``, which is the whole reason both are driven.
        ``cancel_gate`` stalls the whole gate call so NEITHER bound fires and the
        real wake deadline cancels the await instead -- a third outcome the other
        two are structurally blind to, because they raise something the caller's
        ``except`` clause catches and a ``CancelledError`` is not caught there.
        """
        from kiro_crew.cron import CronService

        gw = _make_gw()
        captured_cb = None
        with (
            patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
            patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
            patch("kiro_crew.slack.gateway.sel"),
        ):

            def capture_cron(on_job=None, **kw):
                nonlocal captured_cb
                captured_cb = on_job
                stub = MagicMock()
                stub.start = AsyncMock()
                stub.remove_job_async = AsyncMock(return_value=True)
                return stub

            mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
            await gw._init_cron()
            assert captured_cb is not None

            if starve:
                for _ in range(ex._MAX_CRON_GATE_WORKERS):
                    ex.cron_gate_executor().submit(release.wait, 20.0)
                await asyncio.sleep(0.2)

            svc = CronService(base_dir=tmp_path, on_job=captured_cb)
            svc._load()
            job = svc.add_job(
                name="msg-one-shot",
                message="hello",
                every_secs=300,
                delete_after_run=True,
            )
            job.timeout_secs = budget
            assert any(j.id == job.id for j in svc.list_jobs())
            # slow_gate leaves the pool FREE, so the call is claimed at once and
            # then overruns its own bound -- the execution phase, not the queue
            # phase, and the two raise different exception types.
            gate_work = (
                patch(
                    "kiro_crew.slack.gateway.vet_job_at_fire_time",
                    lambda job: release.wait(20.0),
                )
                if slow_gate
                else nullcontext()
            )
            gate_stall = (
                patch("kiro_crew.slack.gateway.run_in_cron_gate_pool", _stalled_gate)
                if cancel_gate
                else nullcontext()
            )
            with gate_work, gate_stall:
                await svc._execute_with_timeout(job)
        return svc, job, gw

    @pytest.mark.asyncio
    async def test_a_starved_message_one_shot_survives_the_wake_deadline(self, tmp_path, gate_pool):
        """A message one-shot whose GATE is starved must not be consumed.

        The fire-time gate is awaited inside the wake deadline
        ``_execute_with_timeout`` has already armed, and a message job carries no
        ``_pool_queue_allowance`` to cover it. Pre-fix the gate had no bound of its
        own, so a busy pool let the WAKE deadline expire first --
        ``_execute_with_timeout`` then caught the ``TimeoutError`` and returned
        normally, ``_merge_job_result`` saw an ordinary finished run, and the
        ``delete_after_run`` job was deleted having never dispatched.

        The gate's bound is now capped below the wake budget so starvation
        surfaces as ``CronQueueTimeout`` FIRST, which is what makes the existing
        ``run_never_started`` retention marker reachable on this path.

        Fails pre-fix: with the gate unbounded the job is gone from the store.
        """
        svc, job, gw = await self._message_one_shot_run(tmp_path, gate_pool, starve=True)

        assert job.run_never_started is True, "starvation did not mark the run as never started"
        assert job.last_error.startswith(
            "fire-time gate "
        ), f"gate starvation was not reported as such: {job.last_error!r}"
        assert not gw.sessions.get_or_create.called, "the job dispatched; this is not a starved run"

        # Off-loop: _merge_job_result enters the bounded sync store lock.
        await asyncio.to_thread(svc._merge_job_result, job)
        assert any(
            j.id == job.id for j in svc.list_jobs()
        ), "the starved message one-shot was deleted by a run that never dispatched"

    @pytest.mark.asyncio
    async def test_a_dispatched_message_one_shot_is_still_deleted(self, tmp_path, gate_pool):
        """Negative control: retention must not leak onto a run that DID dispatch.

        Same path with the gate pool free. The one-shot reaches its dispatch and
        must then be consumed exactly as before -- otherwise the retention marker
        has stopped meaning "never started" and every message one-shot would
        survive forever. This fails for that reason if the marker is set
        unconditionally rather than only on ``CronQueueTimeout``.
        """
        svc, job, gw = await self._message_one_shot_run(tmp_path, gate_pool, starve=False)

        assert (
            gw.sessions.get_or_create.called
        ), "the control never dispatched, so it proves nothing"
        assert job.run_never_started is False, "a dispatched run was marked never-started"

        await asyncio.to_thread(svc._merge_job_result, job)
        assert not any(
            j.id == job.id for j in svc.list_jobs()
        ), "a message one-shot that DID dispatch was retained; the marker is too broad"

    @pytest.mark.asyncio
    async def test_the_execution_deadline_accounts_for_the_gate_bound(self, tmp_path):
        """The gate's bound is a THIRD term the run deadline has to carry.

        The gate is awaited before the pool dispatch and INSIDE the deadline armed
        for the whole run, so a gate spending its full bound leaves the subprocess
        that much less. A thread cannot be interrupted, so when the deadline then
        fires with a worker already claimed, the overlap guards clear while the
        subprocess runs on and the next wake duplicates its side effects. That is
        the hazard ``_SUBPROC_CLEANUP_ALLOWANCE_SECS`` exists for; the queue wait
        was a second term it did not account for, and this is a third.

        Observes the deadline the code actually arms, by capturing the ``timeout``
        handed to the enclosing ``wait_for`` -- asserting on the allowance helper
        instead would pass with the term missing from the deadline entirely.

        Fails pre-fix: 2700s armed where 2730s is owed.
        """
        import math

        import kiro_crew.cron as cron_mod
        from kiro_crew.cron import (
            CronService,
            _pool_queue_allowance,
            _vet_allowance,
            effective_wake_budget,
        )

        real_wait_for = asyncio.wait_for

        async def _capture(aw, timeout=None):
            captured.setdefault("timeout", timeout)
            return await real_wait_for(aw, timeout=timeout)

        for job in (_make_command_job(timeout_secs=1800), _make_command_job(timeout_secs=8)):
            captured: dict[str, object] = {}
            svc = CronService(base_dir=tmp_path, on_job=AsyncMock(return_value=None))
            with patch.object(cron_mod.asyncio, "wait_for", _capture):
                await svc._execute_with_timeout(job)
            wake = effective_wake_budget(job)
            gate = ex.cron_gate_budget(wake)
            owed = wake + _pool_queue_allowance(job) + math.ceil(gate) + _vet_allowance(job)
            assert captured["timeout"] == owed, (
                f"armed {captured['timeout']}s for a run owed {owed}s "
                f"(wake {wake} + queue {_pool_queue_allowance(job)} + gate {gate} "
                f"+ claim-time vet {_vet_allowance(job)})"
            )
            # Integer, or "Timed out after 2s" reaches the operator as "2.0s".
            assert isinstance(captured["timeout"], int)

        # A message job dispatches nothing through the pool, so it carries no
        # claimed-worker hazard and its budget stays exactly as set -- its
        # protection is that the gate's bound lands strictly below it.
        captured = {}
        msg = CronJob(id="m", name="n", message="hi", timeout_secs=8)
        svc = CronService(base_dir=tmp_path, on_job=AsyncMock(return_value=None))
        with patch.object(cron_mod.asyncio, "wait_for", _capture):
            await svc._execute_with_timeout(msg)
        assert captured["timeout"] == 8, "a message job's wake budget was widened"
        assert ex.cron_gate_budget(8) < 8

    @pytest.mark.asyncio
    async def test_the_reaper_window_also_accounts_for_the_gate_bound(self, tmp_path):
        """Both deadlines must move together or one pre-empts the other.

        This branch already fixed that once for the queue allowance: widening only
        the execution guard left the reaper lower, so it force-killed runs still
        inside their own deadline. Adding the gate term to one and not the other
        re-opens exactly that gap, 30s wide at the default budget.

        Fails pre-fix: the reaper's threshold is 2700s, the job below has been in
        flight 2715s, and it is reaped while still inside its execution deadline.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        job = _make_command_job(timeout_secs=1800)
        assert (
            await self._one_reaper_sweep(svc, job, 2715) is False
        ), "the reaper force-killed a job inside the gate-inclusive execution deadline"

    @pytest.mark.asyncio
    async def test_a_slow_gate_does_not_consume_a_message_one_shot(self, tmp_path, gate_pool):
        """A gate that RUNS but overruns its bound must retain its one-shot.

        The sibling case covers a STARVED gate, which never gets a worker and
        raises ``CronQueueTimeout``. This one gets a worker immediately and then
        overruns -- ``run_in_cron_pool``'s execution phase, which raised a plain
        ``asyncio.TimeoutError``. That bypassed the caller's
        ``except CronQueueTimeout``, so ``run_never_started`` stayed false, the
        wake deadline caught the bare timeout as an ordinary overrun, and the
        ``delete_after_run`` job was deleted having never dispatched.

        Fails pre-fix on the job being gone from the store.
        """
        svc, job, gw = await self._message_one_shot_run(
            tmp_path, gate_pool, starve=False, slow_gate=True
        )

        assert job.run_never_started is True, "a gate that overran its bound was not retained"
        assert not gw.sessions.get_or_create.called, "the job dispatched; the gate did not overrun"
        await asyncio.to_thread(svc._merge_job_result, job)
        assert any(
            j.id == job.id for j in svc.list_jobs()
        ), "a message one-shot was deleted by a gate that overran its own bound"

    @pytest.mark.asyncio
    async def test_a_gate_cancelled_by_the_wake_deadline_keeps_its_one_shot(
        self, tmp_path, gate_pool
    ):
        """A cancellation landing ON the gate await must still retain the one-shot.

        The gate's bounds are sized to fire before the wake deadline, but no
        internal sizing survives a stall that pushes wall clock past BOTH of them:
        ``asyncio.wait_for`` in ``_execute_with_timeout`` then cancels this
        coroutine AT the await, and a ``CancelledError`` is not a
        ``CronQueueTimeout``, so the ``except`` clause that sets the retention
        marker never executes at all. ``_execute_with_timeout`` catches the
        timeout and returns normally, so ``_merge_job_result`` saw an ordinary
        finished run and consumed a ``delete_after_run`` job that never
        dispatched.

        The starvation and overrun siblings both RAISE something the handler
        catches, which is exactly why they are blind to this: they can only prove
        the handler works, never that it runs. The marker is therefore set BEFORE
        the await and cleared only once a verdict comes back.

        Fails pre-fix on the job being gone from the store.
        """
        svc, job, gw = await self._message_one_shot_run(
            tmp_path, gate_pool, starve=False, budget=1, cancel_gate=True
        )

        assert (
            not gw.sessions.get_or_create.called
        ), "the job dispatched, so the gate was never cancelled and this proves nothing"
        assert (
            job.run_never_started is True
        ), "a run cancelled at the fire-time gate was not marked never-started"

        # Off-loop: _merge_job_result enters the bounded sync store lock.
        await asyncio.to_thread(svc._merge_job_result, job)
        assert any(
            j.id == job.id for j in svc.list_jobs()
        ), "the one-shot was deleted by a run cancelled at the gate before it dispatched"

    @pytest.mark.asyncio
    async def test_a_returned_verdict_clears_the_retention_marker(self, tmp_path):
        """The opposite direction: pre-setting the marker must not leak.

        Defaulting to retain is only safe if every path that reaches a verdict
        clears it again. Miss one and a HEALTHY gate leaves the marker set, so the
        delete site retains a one-shot that already ran -- silent duplicate firing
        or a job that never leaves the queue, which is the same class of bug
        pointing the other way.

        Both verdict shapes are driven, since an allow and a deny leave the gate
        by the same return: the deny still has to clear this marker, because its
        retention is owned by ``fire_time_denied``, whose readers also park an
        at-job disabled. Conflating them would park a job for a policy decision
        that was never made.
        """
        from kiro_crew.slack.gateway import _await_cron_fire_time_gate

        for verdict in (None, "denied by policy"):
            job = CronJob(
                id="j1",
                name="n",
                message="hello",
                schedule=CronSchedule(kind="every", every_secs=300),
            )
            job.run_never_started = True  # a stale True must not survive a verdict

            async def _verdict_gate(*_a, _v=verdict, **_kw):
                return _v

            with patch("kiro_crew.slack.gateway.run_in_cron_gate_pool", _verdict_gate):
                reason, starved = await _await_cron_fire_time_gate(
                    job, tool_name="t", tool_kind="k"
                )

            assert reason == verdict
            assert starved is False
            assert (
                job.run_never_started is False
            ), f"a returned verdict ({verdict!r}) left the retention marker set"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kind,extra,retained",
        [
            ("message", {}, True),
            ("command", {"command": "echo hi"}, False),
            ("script", {"script": "~/.meshclaw/crons/f.py:g"}, False),
        ],
    )
    async def test_gate_starvation_clears_the_carried_result_only_for_command_and_script(
        self, kind, extra, retained
    ):
        """Starvation at the gate must not drop a message job's dedup context.

        ``last_result`` is a CROSS-RUN field: build_cron_session_context prepends
        it as "[Previous run result -- do NOT repeat the same content]" for a
        persistent-session job, so a result-less run deliberately leaves it in
        place. ``_run_job_isolated`` scopes its own clear to
        ``job.command or job.script`` for exactly that reason, and the message
        fire-time deny path never clears at all -- but the shared gate helper
        cleared for ALL kinds, so a message cron starved at the gate lost the
        context and the next run repeated content it had already sent.

        Parametrised over all three kinds because asserting retention ALONE
        would also pass if the clear were deleted outright: the command and
        script cases are what pin the clear still firing where it belongs.
        """
        import kiro_crew.executors as ex
        from kiro_crew.slack.gateway import _await_cron_fire_time_gate

        job = CronJob(
            id="j1",
            name="n",
            message="hello",
            schedule=CronSchedule(kind="every", every_secs=300),
            **extra,
        )
        job.last_result = "previously sent content"
        assert job.result_produced is False, "this run produced nothing; the clear must be live"

        async def _starved_gate(*_a, **_kw):
            raise ex.CronQueueTimeout(300.0)

        with patch("kiro_crew.slack.gateway.run_in_cron_gate_pool", _starved_gate):
            reason, starved = await _await_cron_fire_time_gate(job, tool_name="t", tool_kind="k")

        assert starved is True and reason is None, "starvation did not report as starved"
        assert job.run_never_started is True, "a starved run was not marked never-started"
        if retained:
            assert job.last_result == "previously sent content", (
                f"a starved {kind} job lost its cross-run dedup context, so the next run "
                "repeats content it already sent"
            )
        else:
            assert job.last_result == "", (
                f"a starved {kind} job kept a previous run's output, which would display "
                "beside this run's status"
            )


class TestClaimBackstopRetainsAnUnstartedOneShot:
    """The claim backstop must not consume a one-shot whose payload never ran.

    ``_claim_backstop`` bounds the whole submitted call -- the claim-time vet AND
    the payload. When it expires the caller lands in ``except
    asyncio.TimeoutError``, and that arm fires for two materially different runs:

    * the vet burned the bound before ``handoff.claim()`` was ever granted, so
      NOTHING executed; and
    * ``claim()`` was granted, the payload started, and the subprocess overran.

    Only the first is a never-started run. ``abandon()`` is the sole thing that
    separates them -- it reports whether the payload had started -- and
    ``cron.py``'s delete site reads ``run_never_started`` to decide whether a
    ``delete_after_run`` job is retained or consumed.

    Both directions are asserted, plus the deny path, because each of the three
    is a silent failure pointing a different way: retain a run that DID dispatch
    and the one-shot fires twice or never leaves the queue; set this marker on a
    deny and an at-job is parked disabled for a policy decision never made,
    whose retention belongs to ``fire_time_denied``.
    """

    @staticmethod
    def _pool(*, grant_claim: bool):
        """Stand in for run_in_cron_pool, deciding whether claim() is reached.

        grant_claim=False models the backstop expiring DURING the vet: the
        submitted wrapper is never invoked, so ``_started`` stays False.
        grant_claim=True models a payload that started and then overran: the
        wrapper runs (reaching ``handoff.claim()``) before the bound expires.
        """

        async def _fake(func, /, *args, timeout, **kw):
            if grant_claim:
                func(*args)
            raise asyncio.TimeoutError

        return _fake

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grant_claim,expect_retained", [(False, True), (True, False)])
    async def test_backstop_retains_only_the_run_that_never_started(
        self, tmp_path, grant_claim, expect_retained
    ):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        svc._load()
        created = svc.add_job(
            name="one-shot",
            message="",
            every_secs=300,
            command="echo hi",
            delete_after_run=True,
        )
        assert any(j.id == created.id for j in svc.list_jobs())

        gw = _make_gw()
        with patch("kiro_crew.slack.gateway.run_in_cron_pool", self._pool(grant_claim=grant_claim)):
            await _run_command_callback(gw, created, {"status": "ok", "output": "x"})

        assert created.run_never_started is expect_retained, (
            "the claim backstop expired before claim() yet the run was not marked "
            "never-started, so _merge_job_result will delete a one-shot that "
            "dispatched nothing"
            if expect_retained
            else "a payload that DID start was marked never-started, so a healthy "
            "one-shot is retained and will fire again or never leave the queue"
        )

        # Off-loop: _merge_job_result enters the bounded sync store lock.
        await asyncio.to_thread(svc._merge_job_result, created)
        still_present = any(j.id == created.id for j in svc.list_jobs())
        assert still_present is expect_retained, (
            "the one-shot was deleted by a run that never dispatched"
            if expect_retained
            else "the one-shot survived a run that did dispatch"
        )

    @pytest.mark.asyncio
    async def test_a_deny_leaves_retention_to_fire_time_denied(self, tmp_path):
        """A deny must NOT borrow this marker -- its readers park an at-job disabled."""
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        svc._load()
        created = svc.add_job(
            name="one-shot-denied",
            message="",
            every_secs=300,
            command="echo hi",
            delete_after_run=True,
        )

        async def _denying_pool(func, /, *args, timeout, **kw):
            return func(*args)  # the wrapper raises CronClaimTimeDenied itself

        gw = _make_gw()
        with patch("kiro_crew.slack.gateway.run_in_cron_pool", _denying_pool):
            await _run_command_callback(
                gw, created, {"status": "ok", "output": "x"}, vet_reason="denied by policy"
            )

        assert created.fire_time_denied is True, "a policy deny did not record fire_time_denied"
        assert created.run_never_started is False, (
            "a policy deny set run_never_started, conflating a governance refusal with "
            "a never-started run -- that parks an at-job disabled for a decision never made"
        )
        # Retained either way, but by the RIGHT owner.
        await asyncio.to_thread(svc._merge_job_result, created)
        assert any(j.id == created.id for j in svc.list_jobs()), "a denied one-shot was consumed"


class TestClaimBackstopDoesNotConsumeTheFailureBudget:
    """A claim-backstop timeout must only count when the payload dispatched.

    ``_claim_backstop`` bounds the vet AND the payload, so its
    ``asyncio.TimeoutError`` arm fires for two different runs: a vet that burned
    the bound before ``handoff.claim()`` (nothing executed), and a payload that
    started and then overran. Counting the first auto-pauses at
    ``_AUTO_PAUSE_THRESHOLD`` -- and a paused job never fires again -- so
    repeated wedged wakes would permanently disable a healthy job that has not
    run a line, exactly the harm the starvation, gate-deny and vet-overrun arms
    already decline to cause.

    NOTE ON WHY THIS LIVES HERE. ``test_cron_timeout_autopause_424.py`` pins the
    same invariant one layer down, but it monkeypatches ``asyncio.wait_for`` and
    drives ``CronService._execute_with_timeout`` directly -- it never enters
    ``gateway.py``, so it is structurally blind to these two arms and would pass
    whether or not they are fixed. This test drives the gateway callback so the
    arm itself is what is measured.

    Parametrised over BOTH arms and BOTH directions: asserting only the
    exemption would also pass if the count were deleted outright, so the
    started=True cases are what pin the counter still firing for a real overrun.
    """

    @staticmethod
    def _pool(*, grant_claim: bool):
        """Stand in for run_in_cron_pool, choosing whether claim() is reached.

        grant_claim=False models the backstop expiring DURING the vet: the
        submitted wrapper is never invoked, so ``_started`` stays False and
        ``abandon()`` reports the payload as never started. grant_claim=True
        invokes the wrapper first (reaching ``handoff.claim()``) and only then
        expires, modelling a payload that ran and overran.
        """

        async def _fake(func, /, *args, timeout, **kw):
            if grant_claim:
                func(*args)
            raise asyncio.TimeoutError

        return _fake

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grant_claim,expected_count", [(False, 3), (True, 4)])
    async def test_command_backstop_counts_only_a_dispatched_run(self, grant_claim, expected_count):
        gw = _make_gw()
        job = _make_command_job(consecutive_failures=3)

        with patch("kiro_crew.slack.gateway.run_in_cron_pool", self._pool(grant_claim=grant_claim)):
            await _run_command_callback(gw, job, {"status": "ok", "output": "x"})

        assert job.consecutive_failures == expected_count, (
            "a claim-backstop timeout whose payload never started incremented the "
            "failure budget, so repeated wedged wakes will auto-pause a job that "
            "has not run a line"
            if not grant_claim
            else "a genuine execution overrun stopped counting, so a job that times "
            "out on every run would never auto-pause"
        )
        assert job.auto_paused is False, "the job was auto-paused by this single run"
        # The run is still reported as an error either way -- only the counter differs.
        assert job.last_status == "error"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grant_claim,expected_count", [(False, 3), (True, 4)])
    async def test_script_backstop_counts_only_a_dispatched_run(self, grant_claim, expected_count):
        gw = _make_gw()
        job = _make_script_job(consecutive_failures=3)

        with patch("kiro_crew.slack.gateway.run_in_cron_pool", self._pool(grant_claim=grant_claim)):
            await _run_script_callback(gw, job, {"status": "ok"})

        assert job.consecutive_failures == expected_count, (
            "the SCRIPT claim-backstop arm counted a run whose payload never "
            "started -- the command arm was fixed and this one was not"
            if not grant_claim
            else "the script arm stopped counting a genuine execution overrun"
        )
        assert job.auto_paused is False, "the job was auto-paused by this single run"
        assert job.last_status == "error"


class TestACancellationAtTheClaimAwaitKeepsItsOneShot:
    """A cancellation landing on the claim await must not consume a one-shot.

    ``asyncio.CancelledError`` is a ``BaseException``, so none of the command or
    script block's ``except`` arms caught it -- not the claim backstop's
    ``asyncio.TimeoutError``, and not the trailing ``except Exception``. The
    shared ``finally`` did call ``handoff.abandon()``, but DISCARDED its return,
    which is the one bit separating "the payload never started" from "the payload
    ran". So a shutdown or wake deadline landing mid-vet reached ``cron.py``'s
    delete site with neither ``run_never_started`` nor ``fire_time_denied`` set,
    and a ``delete_after_run`` job was consumed having never executed.

    The same class of bug was already cured one path over for the message-job
    fire-time gate (see
    ``test_a_gate_cancelled_by_the_wake_deadline_keeps_its_one_shot``, whose
    docstring names this exact mechanism); the command and script claim awaits
    were the ones still exposed.

    NOTE ON PLACEMENT, which is what the tests below actually pin. The fix is an
    ``except asyncio.CancelledError`` arm, NOT an assignment in the shared
    ``finally``: the fire-time deny path reaches that ``finally`` with the
    payload equally unstarted, and setting this marker there would park an at-job
    disabled for a policy decision that was never made -- retention on that path
    is owned by ``fire_time_denied``. ``test_deny`` below is what keeps that
    closed, and ``grant_claim=True`` is what keeps the mirror closed: a payload
    that DID run must stay deletable, or the one-shot fires twice.
    """

    @staticmethod
    def _cancelling_pool(*, grant_claim: bool):
        """Stand in for run_in_cron_pool, cancelling instead of timing out.

        grant_claim=False models the deadline cancelling the await DURING the
        claim-time vet: the submitted wrapper is never invoked, so ``_started``
        stays False and ``abandon()`` reports the payload as never started.
        grant_claim=True invokes the wrapper first, reaching ``handoff.claim()``,
        so the cancellation lands on a run that did dispatch.
        """

        async def _fake(func, /, *args, timeout, **kw):
            if grant_claim:
                func(*args)
            raise asyncio.CancelledError

        return _fake

    @staticmethod
    def _one_shot(tmp_path, **kw):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path, on_job=lambda *a, **k: None)
        svc._load()
        created = svc.add_job(
            name="one-shot", message="", every_secs=300, delete_after_run=True, **kw
        )
        assert any(j.id == created.id for j in svc.list_jobs()), "fixture did not store the job"
        return svc, created

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grant_claim,expect_retained", [(False, True), (True, False)])
    async def test_command_cancellation_retains_only_the_run_that_never_started(
        self, tmp_path, grant_claim, expect_retained
    ):
        svc, created = self._one_shot(tmp_path, command="echo hi")
        gw = _make_gw()

        # Re-raised, so cooperative cancellation is preserved: swallowing it here
        # would leave the caller believing the run completed.
        with patch(
            "kiro_crew.slack.gateway.run_in_cron_pool",
            self._cancelling_pool(grant_claim=grant_claim),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _run_command_callback(gw, created, {"status": "ok", "output": "x"})

        assert created.run_never_started is expect_retained, (
            "a cancellation landed on the claim await before claim() yet the run "
            "was not marked never-started, so _merge_job_result will delete a "
            "one-shot that dispatched nothing"
            if expect_retained
            else "a payload that DID start was marked never-started, so a healthy "
            "one-shot is retained and will fire again or never leave the queue"
        )
        assert created.fire_time_denied is False, "a cancellation recorded a denial it never made"

        # Off-loop: _merge_job_result enters the bounded sync store lock.
        await asyncio.to_thread(svc._merge_job_result, created)
        survived = any(j.id == created.id for j in svc.list_jobs())
        assert survived is expect_retained, (
            "the one-shot was deleted by a run cancelled before it dispatched"
            if expect_retained
            else "a one-shot that DID execute was retained, so it fires a second time"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grant_claim,expect_retained", [(False, True), (True, False)])
    async def test_script_cancellation_retains_only_the_run_that_never_started(
        self, tmp_path, grant_claim, expect_retained
    ):
        svc, created = self._one_shot(tmp_path, script="~/.kirocrew/crons/monitor.py:run")
        gw = _make_gw()

        with patch(
            "kiro_crew.slack.gateway.run_in_cron_pool",
            self._cancelling_pool(grant_claim=grant_claim),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _run_script_callback(gw, created, {"status": "ok"})

        assert created.run_never_started is expect_retained, (
            "the SCRIPT claim await was cancelled before claim() yet the run was "
            "not marked never-started -- the command arm was fixed and this one "
            "was not"
            if expect_retained
            else "the script arm marked a payload that DID start as never-started"
        )

        await asyncio.to_thread(svc._merge_job_result, created)
        survived = any(j.id == created.id for j in svc.list_jobs())
        assert survived is expect_retained, (
            "the script one-shot was deleted by a run cancelled before it dispatched"
            if expect_retained
            else "a script one-shot that DID execute was retained"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["command", "script"])
    async def test_pruned_persistence_cancel_after_claim_finishes_durable_handoff(
        self, tmp_path, kind
    ):
        svc = CronService(base_dir=tmp_path)
        created = (
            _make_command_job(
                id="j1",
                schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
                delete_after_run=True,
                consecutive_failures=3,
            )
            if kind == "command"
            else _make_script_job(
                id="j1",
                schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
                delete_after_run=True,
                consecutive_failures=3,
            )
        )
        svc._jobs = [created]
        svc._save()
        persistence_entered = asyncio.Event()
        persistence_release = asyncio.Event()
        worker_claimed = threading.Event()
        handoff_svc = _mock_cron_service()
        handoff_svc.occurrence_for_pruned_launch.return_value = "100"

        async def persist(_job_id: str, _occurrence_id: str) -> bool:
            persistence_entered.set()
            await persistence_release.wait()
            return True

        def pruned_launch(*_args, **_kwargs):
            worker_claimed.set()
            raise FileNotFoundError(
                2,
                "No such file or directory",
                str(Path(sys.prefix) / "bin" / "python3.12"),
            )

        handoff_svc.persist_owed_occurrence = AsyncMock(side_effect=persist)
        callback = _run_command_callback if kind == "command" else _run_script_callback
        gw = _make_gw()
        kwargs = (
            {"cmd_result": {"status": "ok", "output": "x"}}
            if kind == "command"
            else {"script_result": {"status": "ok"}}
        )
        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            task = asyncio.create_task(
                callback(
                    gw,
                    created,
                    side_effect=pruned_launch,
                    cron_svc=handoff_svc,
                    **kwargs,
                )
            )
            await asyncio.wait_for(persistence_entered.wait(), timeout=1)
            assert worker_claimed.is_set()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            persistence_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert created.run_never_started is False
        assert created.keep_overdue is True
        assert created.enabled is False
        assert created.owed_occurrence() == "100"
        assert created.consecutive_failures == 3
        handoff_svc.relinquish_run_result_merges.assert_called_once_with(created.id)
        handoff_svc.quiesce_pruned.assert_called_once_with(created.id)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["false", "raise"])
    async def test_cancelled_refused_handoff_keeps_fallback(self, outcome):
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        job = _make_command_job(schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"))
        svc = _mock_cron_service()
        svc.occurrence_for_pruned_launch.return_value = "100"
        entered = asyncio.Event()
        release = asyncio.Event()

        async def persist(_job_id: str, _occurrence_id: str) -> bool:
            entered.set()
            await release.wait()
            if outcome == "raise":
                raise OSError("worker failed")
            return False

        svc.persist_owed_occurrence = AsyncMock(side_effect=persist)
        task = asyncio.create_task(_record_pruned_launch_skip(job, svc))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert job.keep_overdue is True
        assert job.enabled is True
        assert job.owed_occurrence() == "100"
        svc.relinquish_run_result_merges.assert_not_called()
        svc.quiesce_pruned.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure_kind", ["pre-commit", "commit-then-raise"])
    async def test_cancelled_real_persistence_failure_keeps_queued_retry(
        self, tmp_path: Path, failure_kind: str
    ) -> None:
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        occurrence_id = str(int(now) // 60)
        svc, job = TestPrunedInstallCommandSkip._real_hot_cron(tmp_path)
        job.strict_schedule = True
        real_flush = svc._flush_pending_owed_fires_locked
        real_save = svc._save
        worker_entered = threading.Event()
        worker_release = threading.Event()

        def fail_save() -> None:
            if failure_kind == "commit-then-raise":
                real_save()
            raise OSError(failure_kind)

        def controlled_flush() -> set[str]:
            worker_entered.set()
            assert worker_release.wait(timeout=5)
            return real_flush()

        with (
            patch("kiro_crew.slack.gateway.time.time", return_value=now),
            patch.object(svc, "_flush_pending_owed_fires_locked", side_effect=controlled_flush),
            patch.object(svc, "_save", side_effect=fail_save),
        ):
            task = asyncio.create_task(_record_pruned_launch_skip(job, svc))
            assert await asyncio.to_thread(worker_entered.wait, 2)
            task.cancel()
            worker_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert job.keep_overdue is True
        assert job.owed_occurrence() == occurrence_id
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        if failure_kind == "pre-commit":
            assert job.enabled is True
            assert svc._pending_owed_fires == {job.id: occurrence_id}
            assert job.id not in svc._pruned_quiesced
            assert job.__dict__.get("_result_merge_relinquished") is None
            assert stored.owed_occurrence() is None
        else:
            assert job.enabled is False
            assert svc._pending_owed_fires == {}
            assert job.id in svc._pruned_quiesced
            assert job.__dict__.get("_result_merge_relinquished") is True
            assert stored.owed_occurrence() == occurrence_id

    @pytest.mark.asyncio
    async def test_cancel_after_durable_commit_cannot_republish_after_replacement_consumes(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        occurrence_id = str(int(now) // 60)
        svc, job = TestPrunedInstallCommandSkip._real_hot_cron(tmp_path)
        job.strict_schedule = True
        real_flush = svc._flush_pending_owed_fires_locked
        commit_finished = threading.Event()
        worker_release = threading.Event()

        def commit_then_pause() -> set[str]:
            result = real_flush()
            commit_finished.set()
            assert worker_release.wait(timeout=5)
            return result

        async def handoff(running_job: CronJob) -> None:
            await _record_pruned_launch_skip(running_job, svc)

        svc._on_job = handoff
        with (
            patch("kiro_crew.slack.gateway.time.time", return_value=now),
            patch.object(svc, "_flush_pending_owed_fires_locked", side_effect=commit_then_pause),
        ):
            run_task = asyncio.create_task(svc._run_job_isolated(job))
            assert await asyncio.to_thread(commit_finished.wait, 2)
            run_task.cancel()

            replacement = CronService(base_dir=tmp_path)
            replacement_job = replacement.get_job(job.id)
            assert replacement_job is not None
            assert replacement_job.owed_occurrence() == occurrence_id
            replacement_job.set_owed_occurrence(None)
            replacement_job.last_status = "ok"
            replacement_job.last_error = None
            replacement_job.last_run_ts = now + 60
            replacement._save()

            worker_release.set()
            with pytest.raises(asyncio.CancelledError):
                await run_task

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert stored.last_status == "ok"
        assert stored.last_error is None
        assert svc._pending_owed_fires == {}
        assert job.id in svc._pruned_quiesced
        assert job.id not in svc._run_tokens
        assert job.id not in svc._result_merge_relinquished_runs

    @pytest.mark.asyncio
    async def test_commit_then_raise_consumed_by_replacement_is_confirmed_handoff(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.slack.gateway import _record_pruned_launch_skip

        now = 1767333600.0
        occurrence_id = str(int(now) // 60)
        replacement_run_ts = now + 60
        svc, job = TestPrunedInstallCommandSkip._real_hot_cron(tmp_path)
        job.strict_schedule = True
        job.last_result = "old gateway result"
        real_save = svc._save
        commit_finished = threading.Event()
        allow_raise = threading.Event()

        def commit_then_pause_and_raise() -> None:
            real_save()
            commit_finished.set()
            assert allow_raise.wait(timeout=5)
            raise OSError("after commit")

        async def handoff(running_job: CronJob) -> None:
            await _record_pruned_launch_skip(running_job, svc)

        svc._on_job = handoff
        with (
            patch("kiro_crew.slack.gateway.time.time", return_value=now),
            patch.object(svc, "_save", side_effect=commit_then_pause_and_raise),
        ):
            run_task = asyncio.create_task(svc._run_job_isolated(job))
            assert await asyncio.to_thread(commit_finished.wait, 2)

            replacement = CronService(base_dir=tmp_path)
            replacement_job = replacement.get_job(job.id)
            assert replacement_job is not None
            assert replacement_job.owed_occurrence() == occurrence_id
            replacement_job.set_owed_occurrence(None)
            replacement_job.last_status = "ok"
            replacement_job.last_error = None
            replacement_job.last_run_ts = replacement_run_ts
            replacement_job.last_result = "replacement result"
            replacement._save()

            allow_raise.set()
            await run_task

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert stored.last_status == "ok"
        assert stored.last_error is None
        assert stored.last_run_ts == replacement_run_ts
        assert stored.last_result == "replacement result"
        assert svc._pending_owed_fires == {}
        assert job.id in svc._pruned_quiesced
        assert job.id not in svc._run_tokens
        assert job.id not in svc._result_merge_relinquished_runs

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["command", "script", "agent"])
    @pytest.mark.parametrize("pruned", [True, False], ids=["pruned", "live-install"])
    async def test_install_enoent_classification_runs_off_loop(
        self, kind: str, pruned: bool
    ) -> None:
        loop_thread = threading.get_ident()
        probe_threads: list[int] = []
        missing = FileNotFoundError(
            2,
            "No such file or directory",
            str(Path(sys.prefix) / "bin" / "python3.12"),
        )

        def install_probe() -> bool:
            probe_threads.append(threading.get_ident())
            return pruned

        def ownership_probe(_exc: FileNotFoundError) -> bool:
            probe_threads.append(threading.get_ident())
            return True

        with (
            patch("kiro_crew.slack.gateway._running_install_was_pruned", install_probe),
            patch("kiro_crew.slack.gateway._enoent_names_this_install", ownership_probe),
        ):
            if kind == "command":
                job = _make_command_job()
                result, _ = await _run_command_callback(_make_gw(), job, side_effect=missing)
                assert result is None
                assert job.run_never_started is pruned
            elif kind == "script":
                job = _make_script_job()
                result, _ = await _run_script_callback(_make_gw(), job, side_effect=missing)
                assert result is None
                assert job.run_never_started is pruned
            else:
                job = _make_llm_job()

                async def fail_acquire(*_args, **_kwargs):
                    raise missing

                if pruned:
                    result, stream = await _run_llm_callback(
                        _make_gw_for_llm(), job, get_or_create_side_effect=fail_acquire
                    )
                    assert result is None
                    stream.assert_not_awaited()
                    assert job.run_never_started is True
                else:
                    with pytest.raises(FileNotFoundError):
                        await _run_llm_callback(
                            _make_gw_for_llm(), job, get_or_create_side_effect=fail_acquire
                        )

        assert probe_threads
        assert all(thread_id != loop_thread for thread_id in probe_threads)
        expected_probes = 2 if pruned else 1
        assert len(probe_threads) == expected_probes

    @pytest.mark.asyncio
    async def test_a_deny_still_leaves_retention_to_fire_time_denied(self, tmp_path):
        """The placement guard: the deny path must not acquire this marker.

        A deny reaches the same shared ``finally`` with the payload unstarted, so
        an assignment placed THERE would set ``run_never_started`` here too. That
        is why the fix lives in the cancellation arm instead, and this is the
        test that would go red if it were ever moved.
        """
        svc, created = self._one_shot(tmp_path, command="echo hi")

        async def _denying_pool(func, /, *args, timeout, **kw):
            return func(*args)  # the wrapper raises CronClaimTimeDenied itself

        gw = _make_gw()
        with patch("kiro_crew.slack.gateway.run_in_cron_pool", _denying_pool):
            await _run_command_callback(
                gw, created, {"status": "ok", "output": "x"}, vet_reason="denied by policy"
            )

        assert created.fire_time_denied is True, "a policy deny did not record fire_time_denied"
        assert created.run_never_started is False, (
            "a policy deny set run_never_started, conflating a governance refusal "
            "with a never-started run -- that parks an at-job disabled for a "
            "decision never made"
        )

        await asyncio.to_thread(svc._merge_job_result, created)
        assert any(j.id == created.id for j in svc.list_jobs()), "a denied one-shot was consumed"


def test_pruned_install_requires_both_runtime_paths_to_be_absent(tmp_path, monkeypatch):
    """The detector fires ONLY when both the launch interpreter and this
    module's own file are gone. One surviving path means an unrelated
    missing file, which must stay a real failure."""
    from kiro_crew.slack import gateway as gateway_mod

    interpreter = tmp_path / "old-install" / "python"
    module = tmp_path / "old-install" / "gateway.py"
    monkeypatch.setattr(gateway_mod.sys, "executable", str(interpreter))
    monkeypatch.setattr(gateway_mod, "__file__", str(module))

    assert gateway_mod._running_install_was_pruned()

    module.parent.mkdir(parents=True)
    module.write_text("# still installed\n", encoding="utf-8")
    assert not gateway_mod._running_install_was_pruned()

    module.unlink()
    interpreter.write_text("", encoding="utf-8")
    assert not gateway_mod._running_install_was_pruned()


class TestPrunedInstallScriptSkip:
    """A script cron child that FileNotFoundErrors because the running
    install was replaced mid-update is an environmental handoff, not a job
    failure: book it never-started, spend no strike, and let the
    replacement gateway retry."""

    @staticmethod
    def _pruned_enoent():
        # ENOENT that names a file UNDER the install's own interpreter prefix.
        return FileNotFoundError(
            2, "No such file or directory", str(Path(sys.prefix) / "bin" / "python3.12")
        )

    @pytest.mark.asyncio
    async def test_pruned_install_enoent_is_recorded_never_started(self):
        gw = _make_gw()
        job = _make_script_job()
        job.consecutive_failures = 3
        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            result, _ = await _run_script_callback(gw, job, side_effect=self._pruned_enoent())

        # Never-started retention contract: last_status "error" keeps
        # _execute from recording a success, run_never_started stops
        # _merge_job_result deleting a due one-shot, and no auto-pause strike
        # is spent (consecutive_failures untouched).
        assert result is None
        assert job.last_status == "error"
        assert job.run_never_started is True
        assert job.consecutive_failures == 3
        assert "replaced by an update" in job.last_error

    @pytest.mark.asyncio
    async def test_pruned_install_retains_a_due_one_shot(self):
        gw = _make_gw()
        job = _make_script_job()
        job.delete_after_run = True
        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            result, _ = await _run_script_callback(gw, job, side_effect=self._pruned_enoent())

        # The exact data-loss shape: a due delete_after_run one-shot inside the
        # update handoff must NOT read as completed. run_never_started is what
        # _merge_job_result's delete_owed guard keys on.
        assert result is None
        assert job.delete_after_run and job.run_never_started

    @pytest.mark.asyncio
    async def test_pruned_recurring_job_is_quiesced_in_memory_too(self):
        gw = _make_gw()
        job = _make_script_job()
        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            await _run_script_callback(gw, job, side_effect=self._pruned_enoent())

        # keep_overdue leaves the job permanently due on this drained gateway,
        # so it must be disabled in memory or it refires in a zero-delay loop.
        # 'every' jobs stay due via untouched last_run_ts — no owed marker.
        assert job.enabled is False
        assert job.keep_overdue is True
        assert job.owed_fire is False

    @pytest.mark.asyncio
    async def test_pruned_cron_expression_job_persists_an_owed_fire(self):
        gw = _make_gw()
        job = _make_script_job(schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"))
        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            await _run_script_callback(gw, job, side_effect=self._pruned_enoent())

        # A cron-expression job is only due while the current minute matches;
        # the owed marker is what lets the replacement gateway dispatch the
        # missed occurrence after a slow handoff.
        assert job.owed_fire is True
        assert job.enabled is False

    @pytest.mark.asyncio
    async def test_manual_trigger_during_pruning_persists_no_debt(self):
        gw = _make_gw()
        job = _make_script_job(schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"))
        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            await _run_script_callback(gw, job, side_effect=self._pruned_enoent(), manual_run=True)

        # A manual trigger never owed a scheduled occurrence, so no debt is
        # persisted; the quiesce still applies (the process cannot launch).
        assert job.owed_fire is False
        assert job.enabled is False

    @pytest.mark.asyncio
    async def test_unrelated_missing_path_still_fails_even_when_pruned(self):
        gw = _make_gw()
        job = _make_script_job()
        missing = FileNotFoundError(2, "No such file or directory", "/missing/user-script-interp")
        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            result, _ = await _run_script_callback(gw, job, side_effect=missing)

        # The pruned install only excuses ENOENT on the install's OWN files.
        # A user script/wrapper deleted while the install happens to be pruned
        # is still a real job failure and spends a strike.
        assert result is None
        assert job.last_status == "error"
        assert job.run_never_started is False
        assert job.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_pathless_enoent_is_a_real_failure_even_when_pruned(self):
        gw = _make_gw()
        job = _make_script_job()
        # A missing user script raised with no filename attached names nothing
        # install-owned, so the pruned install must not excuse it.
        missing = FileNotFoundError("Script not found")
        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            result, _ = await _run_script_callback(gw, job, side_effect=missing)

        assert result is None
        assert job.last_status == "error"
        assert job.run_never_started is False
        assert job.consecutive_failures == 1


class TestPrunedInstallAgentSkip:
    """The agent-launch path books the same never-started skip when the
    session acquire FileNotFoundErrors on the replaced install, and refuses
    to silently replay a partially-completed sequence."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "agent_sequence",
        [[], ["first", "second"]],
        ids=["single-agent", "agent-sequence"],
    )
    async def test_pruned_install_enoent_skips_agent_launch(self, agent_sequence):
        gw = _make_gw_for_llm()
        job = _make_llm_job(agent_sequence=agent_sequence)
        job.consecutive_failures = 3
        missing = FileNotFoundError(
            2, "No such file or directory", str(Path(sys.prefix) / "bin" / "python3.12")
        )

        async def _side_effect(*args, **kwargs):
            raise missing

        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            result, stream_mock = await _run_llm_callback(
                gw, job, get_or_create_side_effect=_side_effect
            )

        assert result is None
        stream_mock.assert_not_awaited()
        assert job.last_status == "error"
        assert job.run_never_started is True
        assert job.consecutive_failures == 3
        assert "replaced by an update" in job.last_error
        expected_session_key = (
            f"cron:{job.id}:{agent_sequence[0]}" if agent_sequence else f"cron:{job.id}"
        )
        gw.cron_svc.clear_active_session_key.assert_called_once_with(job.id, expected_session_key)

    @pytest.mark.asyncio
    async def test_pruned_cron_expression_agent_publishes_one_occurrence(self):
        gw = _make_gw_for_llm()
        job = _make_llm_job(schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"))
        missing = FileNotFoundError(
            2, "No such file or directory", str(Path(sys.prefix) / "bin" / "python3.12")
        )

        async def _side_effect(*args, **kwargs):
            raise missing

        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            result, stream_mock = await _run_llm_callback(
                gw, job, get_or_create_side_effect=_side_effect
            )

        assert result is None
        stream_mock.assert_not_awaited()
        assert job.owed_occurrence() == "scheduled-minute"
        assert job.enabled is False
        gw.cron_svc.persist_owed_occurrence.assert_awaited_once_with(job.id, "scheduled-minute")
        gw.cron_svc.quiesce_pruned.assert_called_once_with(job.id)

    @pytest.mark.asyncio
    async def test_mid_sequence_pruning_records_a_failure_not_never_started(self):
        gw = _make_gw_for_llm()
        job = _make_llm_job(agent_sequence=["first", "second"])
        missing = FileNotFoundError(
            2, "No such file or directory", str(Path(sys.prefix) / "bin" / "python3.12")
        )
        provider_mock = MagicMock()
        calls = {"n": 0}

        async def _acquire(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return (provider_mock, True, False)
            raise missing

        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            result, _ = await _run_llm_callback(gw, job, get_or_create_side_effect=_acquire)

        # Agent 'first' completed a turn: its side effects exist. Marking the
        # run never-started would retain the one-shot and REPLAY that completed
        # work on the replacement gateway — record a normal failed run instead.
        assert result is None
        assert job.run_never_started is False
        assert job.last_status == "error"
        assert "duplicating finished work" in job.last_error
        assert job.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_mid_sequence_pruning_names_previous_repeated_agent_position(self):
        gw = _make_gw_for_llm()
        job = _make_llm_job(agent_sequence=["repeat", "middle", "repeat"])
        missing = FileNotFoundError(
            2, "No such file or directory", str(Path(sys.prefix) / "bin" / "python3.12")
        )
        provider_mock = MagicMock()
        calls = {"n": 0}

        async def _acquire(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                return (provider_mock, True, False)
            raise missing

        with patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=True):
            result, _ = await _run_llm_callback(gw, job, get_or_create_side_effect=_acquire)

        assert result is None
        assert "after 'middle' completed" in job.last_error
        assert "duplicating finished work" in job.last_error

    @pytest.mark.asyncio
    async def test_unrelated_agent_spawn_enoent_remains_a_failure(self):
        gw = _make_gw_for_llm()
        job = _make_llm_job()
        missing = FileNotFoundError(2, "No such file or directory", "/missing/provider")

        async def _side_effect(*args, **kwargs):
            raise missing

        with (
            patch("kiro_crew.slack.gateway._running_install_was_pruned", return_value=False),
            pytest.raises(FileNotFoundError, match="missing/provider"),
        ):
            await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)
