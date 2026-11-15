"""``cron_add`` naming a repeat AND a one-shot time stores a repeating job.

``build_job`` picks ``cron_expr``, then ``every_secs``, before ``at_ts``, so a
call that passes ``every`` or ``cron_expr`` together with ``at``, ``delay`` or
``at_time`` is stored with a repeating schedule. Such a job must not carry the
one-shot ``delete_after_run`` flag, which would erase it after its first
successful run. The call is still accepted, and its reply says the time is
not used.
"""

from __future__ import annotations

import copy
import uuid

import pytest

from kiro_crew.cron import CronService

#: A fixed instant inside the schema's range (year 2096), so no clock is read.
_FAR_FUTURE = 4_000_000_000.0


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """These tests exercise schedule field handling, not authorization."""


@pytest.fixture()
def call_tool(monkeypatch, tmp_path):
    """The real ``cron_add`` against a private store."""
    from kiro_crew.mcp_cron import _call_tool_locally

    monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)
    monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
    monkeypatch.setattr("kiro_crew.cron.published_config_timezone", lambda: "UTC")

    def _call(args: dict) -> tuple[str, list]:
        name = f"repeat-{uuid.uuid4().hex[:8]}"
        result = _call_tool_locally("cron_add", {"name": name, "message": "ping", **args})
        jobs = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.name == name]
        return result, jobs

    return _call


@pytest.mark.parametrize(
    "repeat",
    [{"every": 3600}, {"cron_expr": "0 9 * * *"}],
    ids=["every", "cron_expr"],
)
@pytest.mark.parametrize(
    "when",
    [
        lambda: {"delay": 7200},
        lambda: {"at_time": "in 2 hours"},
        lambda: {"at": _FAR_FUTURE},
    ],
    ids=["delay", "at_time", "at"],
)
def test_a_repeat_with_a_time_is_kept_as_a_repeating_job(call_tool, repeat, when):
    result, jobs = call_tool({**repeat, **when()})

    assert "Added job" in result
    assert "at/delay/at_time is not used" in result
    assert len(jobs) == 1
    assert jobs[0].schedule.kind in ("every", "cron")
    assert jobs[0].delete_after_run is False


def test_the_repeating_job_survives_its_first_successful_run(call_tool, tmp_path):
    _result, jobs = call_tool({"every": 3600, "delay": 7200})
    svc = CronService(base_dir=tmp_path)
    finished = copy.deepcopy(svc.get_job(jobs[0].id))
    finished.last_status = "ok"
    finished.last_run_ts = _FAR_FUTURE - 1

    svc._merge_job_result(finished)

    assert CronService(base_dir=tmp_path).get_job(jobs[0].id) is not None


def test_a_time_alone_is_still_a_one_shot(call_tool):
    result, jobs = call_tool({"delay": 7200})

    assert "not used" not in result
    assert jobs[0].schedule.kind == "at"
    assert jobs[0].delete_after_run is True
