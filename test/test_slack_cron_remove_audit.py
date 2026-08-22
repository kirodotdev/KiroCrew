"""SEL audit for the remaining cron-removal paths (issue #5408).

PR #5405 (in review) adds the ``cron.remove`` audit to the dashboard, MCP, and
CLI single-delete paths; on base, the plural ``cron.batch_delete`` is the only
audited removal. These tests lock in the same shapes for the paths neither
covers:

* Slack keyword ``cron remove <id>`` (``slack/handler.py``)
* Slack ``cron remove all`` (the plural path, mirroring ``cron.batch_delete``)
* the automated one-shot removal after delivery — the gateway's direct
  Done removal, the deferred drain, and the ``delete_after_run`` consume in
  ``_merge_job_result``

Each path asserts both halves of the contract: the event is emitted on
success, and the removal still succeeds when the audit raises (the first
``sel()`` of a process constructs the log and can raise). The composite
gateway-then-merge test locks the exactly-one-record invariant across paths.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.cron as cron_mod
import kiro_crew.messaging.commands as mc
import kiro_crew.slack.handler as h
from kiro_crew.cron import CronJob, CronSchedule, CronService, CronStoreBusy


def _job(job_id: str = "j1", *, name: str = "nightly") -> CronJob:
    return CronJob(
        id=job_id,
        name=name,
        message="do the thing",
        schedule=CronSchedule(kind="cron", cron_expr="0 9 * * *"),
    )


# ──────────────────────────────────────────────────────────────────────
# Keyword `cron remove <id>` -- the SHARED reply, reached through Slack's handler.
# Exercised through `_handle_cron_command` so the caller attribution is tested
# end to end (it threads Slack's `user_id` into the shared `caller`), with the
# `sel` patch on the module that now emits the record.
# ──────────────────────────────────────────────────────────────────────
class TestSlackSingleRemoveAudit:
    @pytest.mark.asyncio
    async def test_remove_emits_sel_audit_with_caller(self):
        svc = MagicMock()
        svc.remove_job_async = AsyncMock(return_value=True)
        with patch("kiro_crew.messaging.commands.sel") as mock_sel:
            out = await h._handle_cron_command("cron remove j1", svc, "C", "t", user_id="U123")
        assert out is not None and "Removed cron job" in out
        mock_sel.return_value.log_api_access.assert_called_once_with(
            caller="U123",
            operation="cron.remove",
            outcome="allowed",
            source="slack",
            resources="job_id=j1",
        )

    @pytest.mark.asyncio
    async def test_remove_missing_audits_not_found(self):
        svc = MagicMock()
        svc.remove_job_async = AsyncMock(return_value=False)
        with patch("kiro_crew.messaging.commands.sel") as mock_sel:
            out = await h._handle_cron_command("cron remove ghost", svc, "C", "t", user_id="U123")
        assert out is not None and "not found" in out
        kw = mock_sel.return_value.log_api_access.call_args.kwargs
        assert kw["operation"] == "cron.remove"
        assert kw["outcome"] == "not_found"
        assert "ghost" in kw["resources"]

    @pytest.mark.asyncio
    async def test_caller_falls_back_to_surface_when_no_user(self):
        svc = MagicMock()
        svc.remove_job_async = AsyncMock(return_value=True)
        with patch("kiro_crew.messaging.commands.sel") as mock_sel:
            await h._handle_cron_command("cron remove j1", svc, "C", "t")
        assert mock_sel.return_value.log_api_access.call_args.kwargs["caller"] == "slack"

    @pytest.mark.asyncio
    async def test_busy_store_audits_nothing(self):
        # A contended store means the delete never happened — no mutation to
        # audit, matching the dashboard single-delete busy path.
        svc = MagicMock()
        svc.remove_job_async = AsyncMock(side_effect=CronStoreBusy())
        with patch("kiro_crew.messaging.commands.sel") as mock_sel:
            out = await h._handle_cron_command("cron remove j1", svc, "C", "t", user_id="U123")
        assert out is not None and "busy" in out
        mock_sel.return_value.log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_succeeds_when_audit_raises(self):
        # The first sel() of a process constructs the log and can raise; the
        # job is already removed by then, so the reply must still report the
        # completed delete instead of crashing the Slack handler.
        svc = MagicMock()
        svc.remove_job_async = AsyncMock(return_value=True)
        with patch(
            "kiro_crew.messaging.commands.sel",
            side_effect=RuntimeError("SEL trust root unavailable"),
        ):
            out = await h._handle_cron_command("cron remove j1", svc, "C", "t", user_id="U123")
        assert out is not None and "Removed cron job" in out


# ──────────────────────────────────────────────────────────────────────
# `cron remove all` (plural path) -- now the SHARED reply, so the audit covers
# every channel rather than Slack's own copy of the command. Slack threads its
# `user_id` through as `caller`; the patch target moves with the code.
# ──────────────────────────────────────────────────────────────────────
class TestSlackRemoveAllAudit:
    @pytest.mark.asyncio
    async def test_remove_all_emits_batch_audit(self):
        svc = MagicMock()
        svc.list_jobs.return_value = [_job("j1"), _job("j2")]
        svc.remove_jobs = AsyncMock(return_value=(["j1", "j2"], []))
        with patch("kiro_crew.messaging.commands.sel") as mock_sel:
            out = await mc.cron_remove_all_reply(svc, source="slack", caller="U123")
        assert "Removed 2 cron job(s)" in out
        mock_sel.return_value.log_api_access.assert_called_once()
        kw = mock_sel.return_value.log_api_access.call_args.kwargs
        assert kw["caller"] == "U123"
        assert kw["operation"] == "cron.batch_delete"
        assert kw["outcome"] == "ok"
        assert kw["source"] == "slack"
        assert "j1" in kw["resources"] and "j2" in kw["resources"]

    @pytest.mark.asyncio
    async def test_remove_all_nothing_deleted_audits_failed(self):
        # Every id vanished concurrently: no delete happened, and the audit
        # outcome must say so — mirroring the dashboard cron.batch_delete rule.
        svc = MagicMock()
        svc.list_jobs.return_value = [_job("j1")]
        svc.remove_jobs = AsyncMock(return_value=([], ["j1"]))
        with patch("kiro_crew.messaging.commands.sel") as mock_sel:
            await mc.cron_remove_all_reply(svc, source="slack", caller="U123")
        assert mock_sel.return_value.log_api_access.call_args.kwargs["outcome"] == "failed"

    @pytest.mark.asyncio
    async def test_busy_store_audits_nothing(self):
        svc = MagicMock()
        svc.list_jobs.return_value = [_job("j1")]
        svc.remove_jobs = AsyncMock(side_effect=CronStoreBusy())
        with patch("kiro_crew.messaging.commands.sel") as mock_sel:
            out = await mc.cron_remove_all_reply(svc, source="slack", caller="U123")
        assert "busy" in out
        mock_sel.return_value.log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_all_succeeds_when_audit_raises(self):
        svc = MagicMock()
        svc.list_jobs.return_value = [_job("j1"), _job("j2")]
        svc.remove_jobs = AsyncMock(return_value=(["j1", "j2"], []))
        with patch(
            "kiro_crew.messaging.commands.sel",
            side_effect=RuntimeError("SEL trust root unavailable"),
        ):
            out = await mc.cron_remove_all_reply(svc, source="slack", caller="U123")
        assert "Removed 2 cron job(s)" in out


# ──────────────────────────────────────────────────────────────────────
# One-shot auto-removal: deferred drain + delete_after_run consume (cron.py)
# ──────────────────────────────────────────────────────────────────────
class _SelRecorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_api_access(self, **kw) -> None:
        self.events.append(kw)

    def log_tool_invocation(self, **kw) -> None:
        self.events.append(kw)


@pytest.fixture()
def sel_recorder(monkeypatch):
    """Replace cron.py's module-level ``sel`` module with a recorder."""
    rec = _SelRecorder()
    monkeypatch.setattr(cron_mod, "sel", SimpleNamespace(sel=lambda: rec))
    return rec


@pytest.fixture()
def raising_sel(monkeypatch):
    def _boom():
        raise RuntimeError("SEL trust root unavailable")

    monkeypatch.setattr(cron_mod, "sel", SimpleNamespace(sel=_boom))


def _remove_events(rec: _SelRecorder) -> list[dict]:
    return [e for e in rec.events if e.get("operation") == "cron.remove"]


class TestDeferredDrainAudit:
    def test_tick_drain_audits_each_removed_one_shot(self, tmp_path, sel_recorder):
        # Through the real tick entry point: the drain removes under the lock
        # and _tick_scan_locked emits the audit AFTER the lock releases.
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("one-shot", "ping", at_ts=time.time() + 3600, delete_after_run=True)
        svc.defer_removal(job.id)
        svc._tick_scan_locked()
        assert not [j for j in CronService(base_dir=tmp_path).list_jobs() if j.id == job.id]
        removes = _remove_events(sel_recorder)
        assert len(removes) == 1
        assert removes[0]["caller"] == "cron"
        assert removes[0]["outcome"] == "one_shot_completed"
        assert removes[0]["source"] == "cron"
        assert "path=cron_deferred_drain" in removes[0]["resources"]
        assert f"job_id={job.id}" in removes[0]["resources"]

    def test_drain_returns_removed_ids_for_the_caller_to_audit(self, tmp_path, sel_recorder):
        # The locked core itself emits nothing (the first sel() of a process
        # constructs the log and must not extend the store-lock hold); it
        # returns the ids so the post-lock caller owns the emit.
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("one-shot", "ping", at_ts=time.time() + 3600, delete_after_run=True)
        svc.defer_removal(job.id)
        with svc._file_lock():
            drained = svc._drain_pending_removals_locked()
            assert drained == [job.id]
            assert not _remove_events(sel_recorder)  # no emit inside the lock

    def test_drain_removes_even_when_audit_raises(self, tmp_path, raising_sel):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("one-shot", "ping", at_ts=time.time() + 3600, delete_after_run=True)
        svc.defer_removal(job.id)
        svc._tick_scan_locked()  # must not raise
        assert not [j for j in CronService(base_dir=tmp_path).list_jobs() if j.id == job.id]

    def test_already_removed_id_is_not_audited(self, tmp_path, sel_recorder):
        # An id already deleted elsewhere leaves nothing to drain: no event
        # may claim a delete that did not happen here.
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("one-shot", "ping", at_ts=time.time() + 3600, delete_after_run=True)
        svc.defer_removal(job.id)
        svc.remove_job(job.id)
        sel_recorder.events.clear()
        svc._tick_scan_locked()
        assert not _remove_events(sel_recorder)


class TestMergeJobResultOneShotAudit:
    def test_delete_after_run_consume_is_audited(self, tmp_path, sel_recorder):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("reminder", "ping", at_ts=time.time() - 5, delete_after_run=True)
        job.last_run_ts = time.time()
        job.last_status = "ok"
        svc._merge_job_result(job)
        assert not [
            j
            for j in CronService(base_dir=tmp_path).list_jobs(include_disabled=True)
            if j.id == job.id
        ]
        removes = _remove_events(sel_recorder)
        assert len(removes) == 1
        assert removes[0]["caller"] == "cron"
        assert removes[0]["outcome"] == "one_shot_completed"
        assert removes[0]["source"] == "cron"
        assert "path=cron_run_complete" in removes[0]["resources"]
        assert f"job_id={job.id}" in removes[0]["resources"]

    def test_recurring_job_merge_is_not_audited(self, tmp_path, sel_recorder):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("recurring", "ping", every_secs=3600)
        job.last_run_ts = time.time()
        job.last_status = "ok"
        svc._merge_job_result(job)
        assert not _remove_events(sel_recorder)
        assert [
            j
            for j in CronService(base_dir=tmp_path).list_jobs(include_disabled=True)
            if j.id == job.id
        ]

    def test_already_removed_one_shot_is_not_audited(self, tmp_path, sel_recorder):
        # A Done-script one-shot the gateway already removed leaves nothing to
        # consume here; the gateway path owns that audit record.
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("reminder", "ping", at_ts=time.time() - 5, delete_after_run=True)
        svc.remove_job(job.id)
        sel_recorder.events.clear()
        job.last_run_ts = time.time()
        job.last_status = "ok"
        svc._merge_job_result(job)
        assert not _remove_events(sel_recorder)

    def test_merge_survives_audit_raise(self, tmp_path, raising_sel):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("reminder", "ping", at_ts=time.time() - 5, delete_after_run=True)
        job.last_run_ts = time.time()
        job.last_status = "ok"
        svc._merge_job_result(job)  # must not raise
        assert not [
            j
            for j in CronService(base_dir=tmp_path).list_jobs(include_disabled=True)
            if j.id == job.id
        ]


# ──────────────────────────────────────────────────────────────────────
# One-shot auto-removal: gateway direct Done removal (slack/gateway.py)
# ──────────────────────────────────────────────────────────────────────
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


async def _run_done_callback(
    gw, job, *, remove_result=True, remove_side_effect=None, real_svc=None
):
    """Drive the cron callback through the script Done path.

    Pass ``real_svc`` (a CronService) to back the removal with the real store
    instead of a MagicMock — the end-to-end tests need the job to actually
    vanish from disk and the real audit helper to run.
    """
    captured_cb = None
    recorder = MagicMock()

    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch(
            "kiro_crew.slack.gateway.run_script_sandboxed",
            return_value={"status": "done", "message": "all done"},
        ),
        patch("kiro_crew.slack.gateway.vet_job_at_fire_time", return_value=None),
        patch("kiro_crew.slack.gateway.sel", return_value=recorder),
    ):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            if real_svc is not None:
                real_svc.start = AsyncMock()  # never arm a real timer in tests
                return real_svc
            svc = MagicMock()
            svc.start = AsyncMock()
            if remove_side_effect is not None:
                svc.remove_job_async = AsyncMock(side_effect=remove_side_effect)
            else:
                svc.remove_job_async = AsyncMock(return_value=remove_result)
            svc.defer_removal = MagicMock()
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        await gw._init_cron()
        assert captured_cb is not None
        result = await captured_cb(job)

    return result, recorder


class TestGatewayDoneRemovalAudit:
    @pytest.mark.asyncio
    async def test_done_removal_calls_the_shared_audit_helper(self):
        # The gateway owns no emit of its own: the service helper is the one
        # owner of the one-shot record shape, so the exactly-one-record
        # invariant has a single point to depend on.
        gw = _make_gw()
        job = _make_script_job()
        result, _ = await _run_done_callback(gw, job)
        assert "all done" in (result or "")
        gw.cron_svc.remove_job_async.assert_called_once_with("sj1")
        gw.cron_svc.audit_one_shot_removal.assert_called_once_with("sj1", "cron_gateway")

    @pytest.mark.asyncio
    async def test_busy_store_defers_without_gateway_audit(self):
        # On a busy store the removal has NOT happened yet: the deferred drain
        # owns the audit when it lands on disk, so the gateway audits nothing.
        gw = _make_gw()
        job = _make_script_job()
        await _run_done_callback(gw, job, remove_side_effect=CronStoreBusy("busy"))
        gw.cron_svc.defer_removal.assert_called_once_with("sj1")
        gw.cron_svc.audit_one_shot_removal.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_removed_job_is_not_audited(self):
        # remove_job_async=False means another path already deleted the job —
        # that path owns the audit record; no event may claim this delete.
        gw = _make_gw()
        job = _make_script_job()
        await _run_done_callback(gw, job, remove_result=False)
        gw.cron_svc.audit_one_shot_removal.assert_not_called()

    @pytest.mark.asyncio
    async def test_delivery_succeeds_when_audit_raises(self, tmp_path, raising_sel):
        # End-to-end through the REAL service: the helper's exception
        # containment means a broken SEL never fails the delivery callback or
        # the removal itself.
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(
            "done-oneshot",
            "ping",
            every_secs=60,
            script="~/.kirocrew/crons/monitor.py:run",
        )
        gw = _make_gw()
        result, _ = await _run_done_callback(gw, job, real_svc=svc)
        assert "all done" in (result or "")
        assert not [j for j in CronService(base_dir=tmp_path).list_jobs() if j.id == job.id]

    @pytest.mark.asyncio
    async def test_gateway_then_merge_emits_exactly_one_record(self, tmp_path, sel_recorder):
        # One Done delete_after_run job through BOTH removal paths in order:
        # the gateway removes and audits (via the shared helper); the later
        # run-merge finds the job gone and must NOT add a second cron.remove
        # record. Locks the exactly-one-record invariant against a reorder
        # that lets the merge win and double-count removals in the trail.
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(
            "done-oneshot",
            "ping",
            every_secs=60,
            delete_after_run=True,
            script="~/.kirocrew/crons/monitor.py:run",
        )
        gw = _make_gw()
        result, _ = await _run_done_callback(gw, job, real_svc=svc)
        assert "all done" in (result or "")
        removes = _remove_events(sel_recorder)
        assert len(removes) == 1
        assert "path=cron_gateway" in removes[0]["resources"]
        assert not [j for j in CronService(base_dir=tmp_path).list_jobs() if j.id == job.id]
        # Now the run-state merge runs for the same (already-removed) job.
        job.last_run_ts = time.time()
        job.last_status = "ok"
        svc._merge_job_result(job)
        assert len(_remove_events(sel_recorder)) == 1  # still exactly one
