"""``alert_after_failures``: hold a cron's failure message until N failures in a row.

A watcher firing every 10 minutes alerts the owner on a one-off failure even
when the next run succeeds, and ``silent`` is not the answer because it also
hides the job's results. The per-job threshold gates DELIVERY only: the bell
still records every failure, the counter keeps its one owner per run, and the
dedup anchor stays put below the threshold so the failure that reaches it alerts
even with an identical reason. Policy denials and the run that auto-pauses the
job alert whatever the threshold. The default of 1 is today's behaviour.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.cron import CronJob, CronSchedule, CronService
from kiro_crew.cron_service.model import (
    _AUTO_PAUSE_MAX,
    validate_alert_after_failures,
)
from kiro_crew.mcp_cron import _call_tool_inner, _call_tool_locally, _list_tools
from kiro_crew.slack import gateway as gw
from kiro_crew.validation import MCP_CRON_SCHEMAS, ValidationError, validate_tool_args


def _job(**kwargs: object) -> CronJob:
    return CronJob(
        id="j",
        name="watcher",
        message="m",
        schedule=CronSchedule(kind="every", every_secs=600),
        **kwargs,  # type: ignore[arg-type]
    )


# ── the field ───────────────────────────────────────────────────────────────


class TestValidator:
    @pytest.mark.parametrize("value", [1, 2, 10000, 3.0])
    def test_accepts_in_range(self, value: object) -> None:
        assert validate_alert_after_failures(value) == int(value)  # type: ignore[call-overload]

    @pytest.mark.parametrize("value", [0, -1, 10001, True, False, 2.5, "x", None, float("inf")])
    def test_refuses_out_of_range_or_wrong_type(self, value: object) -> None:
        with pytest.raises(ValueError):
            validate_alert_after_failures(value)

    def test_a_damaged_record_reads_back_as_the_default(self) -> None:
        job = _job()
        job.alert_after_failures = 0
        assert job.alert_after_limit() == 1
        job.alert_after_failures = "many"  # type: ignore[assignment]
        assert job.alert_after_limit() == 1
        job.alert_after_failures = 3
        assert job.alert_after_limit() == 3


class TestStore:
    def test_create_update_and_reload(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="w", message="m", every_secs=600, alert_after_failures=3)
        assert job.alert_after_failures == 3

        reloaded = CronService(base_dir=tmp_path)
        reloaded._load()
        assert reloaded.get_job(job.id).alert_after_failures == 3

        reloaded.update_job(job.id, alert_after_failures=2)
        again = CronService(base_dir=tmp_path)
        again._load()
        assert again.get_job(job.id).alert_after_failures == 2

    def test_old_record_without_the_key_loads_as_one(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="legacy", message="m", every_secs=600, alert_after_failures=4)
        store = next(p for p in tmp_path.rglob("crons.json"))
        data = json.loads(store.read_text())
        records = data["jobs"] if isinstance(data, dict) else data
        for rec in records:
            rec.pop("alert_after_failures", None)
        store.write_text(json.dumps(data))
        fresh = CronService(base_dir=tmp_path)
        fresh._load()
        assert fresh.get_job(job.id).alert_after_failures == 1

    def test_default_is_not_written_so_old_stores_keep_their_bytes(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        plain = svc.add_job(name="plain", message="m", every_secs=600)
        custom = svc.add_job(name="custom", message="m", every_secs=600, alert_after_failures=2)
        store = next(p for p in tmp_path.rglob("crons.json"))
        data = json.loads(store.read_text())
        records = {r["id"]: r for r in (data["jobs"] if isinstance(data, dict) else data)}
        assert "alert_after_failures" not in records[plain.id]
        assert records[custom.id]["alert_after_failures"] == 2

    def test_invalid_values_refused_without_mutation(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        with pytest.raises(ValueError):
            svc.add_job(name="bad", message="m", every_secs=600, alert_after_failures=0)
        job = svc.add_job(name="ok", message="m", every_secs=600)
        with pytest.raises(ValueError):
            svc.update_job(job.id, name="renamed", alert_after_failures=_AUTO_PAUSE_MAX + 1)
        fresh = CronService(base_dir=tmp_path)
        fresh._load()
        stored = fresh.get_job(job.id)
        assert stored.name == "ok"
        assert stored.alert_after_failures == 1


class TestMcpTools:
    @pytest.fixture(autouse=True)
    def _cron_caller_is_named(self, named_cron_caller):
        """These tests exercise field handling; see ``named_cron_caller`` in conftest."""

    def _reload(self, tmp_path: Path, name: str) -> CronJob:
        matching = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.name == name]
        assert len(matching) == 1
        return matching[0]

    def test_cron_add_and_update_set_the_threshold(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = f"aaf-{uuid.uuid4().hex[:8]}"
        result = _call_tool_locally(
            "cron_add", {"name": name, "message": "go", "every": 600, "alert_after_failures": 2}
        )
        assert "Added job" in result
        job = self._reload(tmp_path, name)
        assert job.alert_after_failures == 2

        result = _call_tool_locally("cron_update", {"job_id": job.id, "alert_after_failures": 5})
        assert "Updated job" in result
        assert self._reload(tmp_path, name).alert_after_failures == 5

    def test_cron_update_out_of_range_is_an_error(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = f"aaf-{uuid.uuid4().hex[:8]}"
        _call_tool_inner("cron_add", {"name": name, "message": "go", "every": 600})
        jid = self._reload(tmp_path, name).id
        result = _call_tool_inner("cron_update", {"job_id": jid, "alert_after_failures": 0})
        assert result.startswith("Error")
        assert self._reload(tmp_path, name).alert_after_failures == 1

    @pytest.mark.parametrize("tool", ["cron_add", "cron_update"])
    def test_schema_bounds(self, tool: str) -> None:
        base = {"name": "n", "message": "m"} if tool == "cron_add" else {"job_id": "abc12345"}
        schema = MCP_CRON_SCHEMAS[tool]
        assert validate_tool_args({**base, "alert_after_failures": 1}, schema)
        assert validate_tool_args({**base, "alert_after_failures": _AUTO_PAUSE_MAX}, schema)
        for bad in (0, _AUTO_PAUSE_MAX + 1):
            with pytest.raises(ValidationError):
                validate_tool_args({**base, "alert_after_failures": bad}, schema)

    def test_published_descriptions_name_the_contract(self) -> None:
        tools = {tool["name"]: tool for tool in _list_tools()}
        for tool_name in ("cron_add", "cron_update"):
            description = tools[tool_name]["inputSchema"]["properties"]["alert_after_failures"][
                "description"
            ]
            assert f"1..{_AUTO_PAUSE_MAX}" in description
            assert "default 1" in description
            assert "bell" in description
            assert "auto-pauses" in description


# ── the delivery gate ───────────────────────────────────────────────────────


def _orchestrator() -> Any:
    """A GatewayOrchestrator whose only alert surfaces are the bell and Slack."""
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch: Any = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.slack = MagicMock()
    orch.slack.post_message = AsyncMock(return_value="111.1")
    orch._open_dm_with_retry = AsyncMock(return_value="D_OWNER")
    orch._deliver_cron_to_channel = AsyncMock(return_value=False)
    orch.dashboard_state = MagicMock()
    orch.dashboard_state.notify = MagicMock()
    return orch


def _bell_titles(orch: Any) -> list[str]:
    return [call.args[1] for call in orch.dashboard_state.notify.call_args_list]


async def _script_failure(orch: Any, job: CronJob, reason: str, *, denied: bool = False) -> None:
    """The script/command arm: the run is counted BEFORE the alert."""
    if not denied:
        job.record_failure()
    with patch.object(gw, "sel", lambda: MagicMock()):
        await orch._alert_cron_failure(job, reason, denied=denied)


async def _agent_failure(orch: Any, job: CronJob, reason: str) -> None:
    """The single-agent turn's arm: the alert charges the run itself."""
    with patch.object(gw, "sel", lambda: MagicMock()):
        await orch._cron_single_failure(job, RuntimeError(reason), "cron:j", False, True, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [_script_failure, _agent_failure])
async def test_the_default_alerts_on_the_first_failure(fail) -> None:
    orch = _orchestrator()
    job = _job()
    await fail(orch, job, "boom")
    orch.slack.post_message.assert_awaited_once()
    assert job.last_failure_hash != ""
    assert job.consecutive_failures == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [_script_failure, _agent_failure])
async def test_below_the_threshold_only_the_bell_rings_and_the_anchor_stays(fail) -> None:
    orch = _orchestrator()
    job = _job(alert_after_failures=2)
    await fail(orch, job, "boom")
    orch.slack.post_message.assert_not_awaited()
    orch._deliver_cron_to_channel.assert_not_awaited()
    assert job.last_failure_hash == ""
    assert job.last_failure_at == 0.0
    assert job.consecutive_failures == 1
    assert _bell_titles(orch) == ["Cron: watcher (failure 1 of 2)"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [_script_failure, _agent_failure])
async def test_the_failure_reaching_the_threshold_alerts_with_an_identical_reason(fail) -> None:
    orch = _orchestrator()
    job = _job(alert_after_failures=2)
    await fail(orch, job, "boom")
    await fail(orch, job, "boom")
    orch.slack.post_message.assert_awaited_once()
    assert "boom" in orch.slack.post_message.await_args.args[1]
    assert job.last_failure_hash != ""
    assert job.consecutive_failures == 2
    assert _bell_titles(orch)[-1] == "Cron: watcher"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [_script_failure, _agent_failure])
async def test_a_success_between_failures_restarts_the_streak(fail) -> None:
    orch = _orchestrator()
    job = _job(alert_after_failures=2)
    await fail(orch, job, "boom")
    job.record_success()
    await fail(orch, job, "boom")
    orch.slack.post_message.assert_not_awaited()
    assert job.consecutive_failures == 1


@pytest.mark.asyncio
async def test_a_policy_denial_alerts_whatever_the_threshold() -> None:
    orch = _orchestrator()
    job = _job(alert_after_failures=2)
    await _script_failure(orch, job, "blocked path", denied=True)
    orch.slack.post_message.assert_awaited_once()
    assert "Blocked by policy" in orch.slack.post_message.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [_script_failure, _agent_failure])
async def test_the_run_that_auto_pauses_alerts_below_the_threshold(fail) -> None:
    """A job never pauses silently: the pausing run alerts even under the threshold."""
    orch = _orchestrator()
    job = _job(alert_after_failures=5, auto_pause_after_failures=2)
    await fail(orch, job, "boom")
    orch.slack.post_message.assert_not_awaited()
    await fail(orch, job, "boom")
    assert job.auto_paused is True
    orch.slack.post_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [_script_failure, _agent_failure])
async def test_a_failed_one_shot_alerts_below_the_threshold(fail) -> None:
    """A failed one-shot is paused after its run, so no later run reaches the threshold."""
    orch = _orchestrator()
    job = _job(alert_after_failures=2, delete_after_run=True)
    await fail(orch, job, "boom")
    orch.slack.post_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [_script_failure, _agent_failure])
async def test_a_retained_at_job_alerts_below_the_threshold(fail) -> None:
    """A retained `at` job is parked after its run, so no later run reaches the threshold."""
    orch = _orchestrator()
    job = CronJob(
        id="j",
        name="watcher",
        message="m",
        schedule=CronSchedule(kind="at", at_ts=1767225600.0),
        alert_after_failures=2,
    )
    await fail(orch, job, "boom")
    orch.slack.post_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [_script_failure, _agent_failure])
async def test_a_silent_job_is_unchanged(fail) -> None:
    orch = _orchestrator()
    job = _job(alert_after_failures=2, silent=True)
    await fail(orch, job, "boom")
    await fail(orch, job, "boom")
    orch.slack.post_message.assert_not_awaited()
    orch.dashboard_state.notify.assert_not_called()
    assert job.consecutive_failures == 2


@pytest.mark.asyncio
async def test_a_hand_over_run_that_pauses_the_job_alerts_under_the_threshold() -> None:
    """The hand-over raises the counter to the pause line, so this run pauses the job."""
    orch = _orchestrator()
    job = _job(alert_after_failures=2)
    with patch.object(gw, "sel", lambda: MagicMock()):
        await orch._cron_single_failure(job, RuntimeError("boom"), "cron:j", False, True, True)
    assert job.auto_paused is True
    orch.slack.post_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failure_the_job_is_not_charged_for_still_alerts_on_the_default() -> None:
    orch = _orchestrator()
    job = _job()
    with patch.object(gw, "sel", lambda: MagicMock()):
        await orch._cron_single_failure(job, RuntimeError("boom"), "cron:j", False, False, False)
    assert job.consecutive_failures == 0
    orch.slack.post_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_uncounted_script_failure_still_alerts_on_the_default() -> None:
    orch = _orchestrator()
    job = _job()
    with patch.object(gw, "sel", lambda: MagicMock()):
        await orch._alert_cron_failure(job, "queue timeout")
    orch.slack.post_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [_script_failure, _agent_failure])
async def test_a_held_failure_is_audited(fail) -> None:
    orch = _orchestrator()
    job = _job(alert_after_failures=2)
    audit = MagicMock()
    with patch.object(gw, "sel", lambda: audit):
        if fail is _script_failure:
            job.record_failure()
            await orch._alert_cron_failure(job, "boom")
        else:
            await orch._cron_single_failure(job, RuntimeError("boom"), "cron:j", False, True, False)
    audit.log_tool_invocation.assert_called_once_with(
        session_key=f"cron:{job.id}",
        tool_name="cron_failure_alert_held",
        outcome="held",
        downstream_service="none",
    )
